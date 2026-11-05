"""The ledger: a SQLite database that remembers every file and where it is.

Every step of the migration writes its result here inside a transaction,
so the tool can be stopped at any moment (Ctrl+C, power cut, laptop lid)
and pick up exactly where it left off.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .util import now_iso

log = logging.getLogger("minsky")

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row per downloaded zip file, or per folder of loose files in the inbox.
CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,          -- 'zip' | 'mappe'
    path        TEXT NOT NULL,          -- where it was in the inbox
    size        INTEGER,
    mtime       REAL,
    sha256      TEXT UNIQUE,            -- zips only
    archive_dir TEXT NOT NULL,          -- folder in arkiv/ the files went to
    status      TEXT NOT NULL,          -- 'pågår' | 'ferdig' | 'ødelagt'
    files       INTEGER DEFAULT 0,
    ignored     INTEGER DEFAULT 0,
    error       TEXT,
    first_seen  TEXT,
    finished    TEXT
);

-- One row per unique file content (same photo downloaded twice = one row).
CREATE TABLE IF NOT EXISTS items (
    sha256         TEXT PRIMARY KEY,
    size           INTEGER NOT NULL,
    archive_path   TEXT NOT NULL UNIQUE, -- relative to arkiv/
    ext            TEXT,
    is_media       INTEGER NOT NULL,
    taken_hint     TEXT,                 -- timestamp stored in the zip
    state          TEXT NOT NULL,
    uuid           TEXT,                 -- the photo's id in the Photos app
    role           TEXT,                 -- 'importert' | 'fantes_allerede' | 'følgefil'
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    force_import   INTEGER NOT NULL DEFAULT 0,
    selected       INTEGER NOT NULL DEFAULT 1, -- 0 = user chose not to move it
    import_started TEXT,
    imported_at    TEXT,
    verified_at    TEXT,
    verified_how   TEXT,
    icloud_at      TEXT,
    created        TEXT
);
CREATE INDEX IF NOT EXISTS items_state ON items(state);

-- Every place a file was seen (which zip, which path inside it).
CREATE TABLE IF NOT EXISTS occurrences (
    id        INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    member    TEXT NOT NULL,
    sha256    TEXT NOT NULL REFERENCES items(sha256),
    size      INTEGER,
    mtime     REAL,
    UNIQUE(source_id, member, sha256)
);
CREATE INDEX IF NOT EXISTS occ_sha ON occurrences(sha256);

-- Append-only audit trail.
CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY,
    ts     TEXT NOT NULL,
    kind   TEXT NOT NULL,
    detail TEXT
);
"""

# Item states, in the order a file normally moves through them.
ARKIVERT = "arkivert"            # safely copied into the local archive, verified
IMPORTERER = "importerer"        # handed to the Photos app, result not yet known
IMPORTERT = "importert"          # Photos app has it (we know its id)
BEKREFTET = "bekreftet"          # we checked the copy inside Photos matches
I_ICLOUD = "i_icloud"            # Photos reports it is uploaded to iCloud
# Side states
IKKE_MEDIA = "ikke_media"        # not a photo/video; kept in the archive only
FEIL = "feil"                    # Photos refused it several times
AVVIK = "avvik"                  # the copy in Photos differs; needs a look
I_PAPIRKURV = "i_papirkurv"      # someone deleted it in Photos ('Nylig slettet')

ALL_STATES = [ARKIVERT, IMPORTERER, IMPORTERT, BEKREFTET, I_ICLOUD,
              IKKE_MEDIA, FEIL, AVVIK, I_PAPIRKURV]


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        # One connection per thread; check_same_thread=False only so close() can run from any thread.
        self.db = sqlite3.connect(path, isolation_level=None, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        # Durability first: WAL + full sync means a committed row survives a power cut.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(items)")}
        if "selected" not in cols:  # databases from version 1.0
            self.db.execute("ALTER TABLE items ADD COLUMN selected INTEGER NOT NULL DEFAULT 1")
        cur = self.get_meta("schema_version")
        if cur is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
            self.set_meta("created", now_iso())
        elif int(cur) > SCHEMA_VERSION:
            raise SystemExit(
                "Databasen er laget av en nyere versjon av verktøyet. Oppdater verktøyet først."
            )

    # -- transactions -------------------------------------------------
    def tx(self):
        return _Tx(self.db)

    def close(self):
        self.db.close()

    # -- meta ----------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def set_meta(self, key: str, value: str | None) -> None:
        self.db.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def event(self, kind: str, detail: str = "") -> None:
        self.db.execute(
            "INSERT INTO events(ts, kind, detail) VALUES(?,?,?)", (now_iso(), kind, detail)
        )

    # -- queries -------------------------------------------------------
    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in ALL_STATES}
        for r in self.db.execute("SELECT state, COUNT(*) FROM items GROUP BY state"):
            out[r[0]] = r[1]
        return out

    def item(self, sha: str):
        return self.db.execute("SELECT * FROM items WHERE sha256=?", (sha,)).fetchone()


class _Tx:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def __enter__(self):
        self.db.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.db.execute("COMMIT")
        else:
            self.db.execute("ROLLBACK")
        return False
