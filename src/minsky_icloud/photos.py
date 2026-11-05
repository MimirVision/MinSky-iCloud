"""Steps 2 and 3: put archived photos into the Photos app and check them.

Uploading to iCloud is done by Apple's own Photos app (with iCloud Photos
switched on). We only talk to Photos through osxphotos
(https://github.com/RhetTbull/osxphotos), which uses Apple's PhotoKit.

Safety:
* Before a batch is handed to Photos, its items are marked 'importerer'.
  If we are interrupted, the next run imports the batch again with
  --skip-dups, so anything that already made it in is recognised as a
  duplicate and simply mapped to the existing photo instead of being added
  twice.
* After import we look each photo up in the Photos library and compare
  the stored original with the archived file (SHA-256, or size if the
  original is not stored locally).
* We ask Photos whether each photo has been uploaded to iCloud.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from . import ledger as L
from .util import NotEnoughSpace, free_bytes, human, now_iso, sha256_file
from .workspace import Workspace

log = logging.getLogger("minsky")

MAX_ATTEMPTS = 3
IMPORT_SPACE_MARGIN = 5 * 1024**3
DEFAULT_ALBUM = "Fra Min Sky"


class OsxphotosMissing(Exception):
    pass


def find_osxphotos(explicit: str | None = None) -> list[str]:
    if explicit:
        return [explicit]
    env = os.environ.get("MINSKY_OSXPHOTOS")
    if env:
        return env.split()
    exe = shutil.which("osxphotos")
    if exe:
        return [exe]
    raise OsxphotosMissing(
        "Fant ikke osxphotos. Installer det med:\n"
        "    brew install pipx && pipx install osxphotos\n"
        "(se README, steg 1)."
    )


# --------------------------------------------------------------------- import


def import_to_photos(ws: Workspace, *, osxphotos: list[str], batch_size: int = 100,
                     limit: int | None = None, album: str | None = DEFAULT_ALBUM,
                     retry_failed: bool = False) -> dict[str, int]:
    db = ws.ledger.db
    if retry_failed:
        with ws.ledger.tx():
            n = db.execute(
                "UPDATE items SET state=?, attempts=0 WHERE state=?", (L.ARKIVERT, L.FEIL)
            ).rowcount
        if n:
            log.info("Prøver %d filer som feilet tidligere på nytt.", n)

    rows = db.execute(
        "SELECT * FROM items WHERE state IN (?, ?) AND selected=1 ORDER BY force_import, archive_path",
        (L.IMPORTERER, L.ARKIVERT),
    ).fetchall()
    if limit is not None:
        rows = _cut_at_group(rows, limit)
    stats = {"importert": 0, "fantes_allerede": 0, "følgefil": 0, "feil": 0, "venter": 0}
    if not rows:
        log.info("Ingen nye bilder å importere.")
        return stats

    total = len(rows)
    log.info("Importerer %d filer til Bilder-appen i grupper på ca. %d …", total, batch_size)
    done = 0
    for force in (0, 1):
        subset = [r for r in rows if r["force_import"] == force]
        for batch in _batches(subset, batch_size):
            ws.check_stop()
            need = sum(r["size"] for r in batch)
            free = free_bytes(Path.home())
            if need + IMPORT_SPACE_MARGIN > free:
                raise NotEnoughSpace(
                    f"For lite plass til Bilder-biblioteket: {human(free)} ledig, trenger "
                    f"{human(need)} + {human(IMPORT_SPACE_MARGIN)}. Slå på «Optimaliser Mac-lagring» "
                    "i Bilder > Innstillinger > iCloud og vent til opplastingen har tatt igjen, "
                    "eller frigjør plass."
                )
            res = _import_batch(ws, osxphotos, batch, album=album, skip_dups=not force)
            for k, v in res.items():
                stats[k] = stats.get(k, 0) + v
            done += len(batch)
            log.info("  %d/%d behandlet (importert %d, fantes fra før %d, feil %d)",
                     done, total, stats["importert"] + stats["følgefil"],
                     stats["fantes_allerede"], stats["feil"])
    return stats


def _group_key(row) -> tuple[str, str]:
    p = Path(row["archive_path"])
    return (str(p.parent), p.stem.lower())


def _cut_at_group(rows, limit):
    out = []
    for r in rows:
        if len(out) >= limit and _group_key(r) != _group_key(out[-1]):
            break
        out.append(r)
    return out


def _batches(rows, size):
    """Split into batches without separating files that belong together
    (Live Photo .HEIC + .MOV, RAW + JPEG share folder and name)."""
    batch = []
    for r in rows:
        if len(batch) >= size and _group_key(r) != _group_key(batch[-1]):
            yield batch
            batch = []
        batch.append(r)
    if batch:
        yield batch


def _import_batch(ws: Workspace, osxphotos: list[str], batch, *, album, skip_dups) -> dict[str, int]:
    db = ws.ledger.db
    stamp = now_iso().replace(":", "")
    report = ws.logs / f"import-{stamp}.json"
    outlog = ws.logs / f"import-{stamp}.log"

    with ws.ledger.tx():
        for r in batch:
            db.execute(
                "UPDATE items SET state=?, attempts=attempts+1, import_started=? WHERE sha256=?",
                (L.IMPORTERER, now_iso(), r["sha256"]),
            )
        ws.ledger.event("import_start", f"{len(batch)} filer, rapport {report.name}")

    paths = [str(ws.archive_abs(r["archive_path"]).resolve()) for r in batch]
    cmd = [*osxphotos, "import", *paths, "--report", str(report), "--no-progress", "--verbose"]
    if skip_dups:
        cmd.append("--skip-dups")
    if album:
        cmd += ["--album", album]
        if skip_dups:
            cmd.append("--dup-albums")

    interrupted = None
    with open(outlog, "w", encoding="utf-8") as out:
        try:
            proc = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            rc = proc.returncode
        except KeyboardInterrupt as e:
            interrupted = e
            rc = None

    records = _read_report(report)
    if records is None:
        log.warning("  osxphotos skrev ingen rapport (returkode %s). Se %s", rc, outlog)
        if interrupted:
            raise interrupted
        # Items stay 'importerer' and are retried (with duplicate check) next time.
        _bump_or_fail(ws, batch, f"osxphotos feilet (kode {rc}), se {outlog.name}")
        return {"venter": len(batch)}

    stats = _apply_report(ws, batch, records, outlog)
    if interrupted:
        raise interrupted
    return stats


def _read_report(path: Path):
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return data if isinstance(data, list) else None


def _norm(p: str) -> str:
    try:
        return str(Path(p).resolve())
    except OSError:
        return p


def _apply_report(ws: Workspace, batch, records, outlog: Path) -> dict[str, int]:
    db = ws.ledger.db
    by_path = {_norm(r.get("filepath", "")): r for r in records}
    companions: dict[str, str] = {}
    by_group: dict[tuple[str, str], str] = {}
    for r in records:
        u = r.get("uuid") or r.get("duplicate_of")
        if not u:
            continue
        for k in ("live_video", "raw_image"):
            if r.get(k):
                companions[_norm(r[k])] = u
        fp = Path(r.get("filepath", ""))
        by_group[(str(fp.parent), fp.stem.lower())] = u

    stats = defaultdict(int)
    with ws.ledger.tx():
        for row in batch:
            p = str(ws.archive_abs(row["archive_path"]).resolve())
            rec = by_path.get(p)
            sha = row["sha256"]
            prev_attempts = row["attempts"]  # value before this batch incremented it
            if rec and rec.get("imported") and not rec.get("error") and rec.get("uuid"):
                _set_imported(db, sha, rec["uuid"], "importert")
                stats["importert"] += 1
            elif rec and rec.get("skipped") and rec.get("duplicate_of"):
                # Already in Photos. If we tried before, it was us who put it there.
                role = "importert" if prev_attempts > 0 else "fantes_allerede"
                _set_imported(db, sha, rec["duplicate_of"], role)
                stats[role] += 1
            elif rec and rec.get("error"):
                stats[_fail(db, sha, "Bilder-appen kunne ikke importere filen", outlog)] += 1
            elif p in companions:
                _set_imported(db, sha, companions[p], "følgefil")
                stats["følgefil"] += 1
            elif (k := (str(Path(p).parent), Path(p).stem.lower())) in by_group:
                _set_imported(db, sha, by_group[k], "følgefil")
                stats["følgefil"] += 1
            else:
                stats[_fail(db, sha, "osxphotos hoppet over filen (ukjent filtype?)", outlog)] += 1
        ws.ledger.event("import_slutt", json.dumps(stats))
    return dict(stats)


def _set_imported(db, sha, uuid, role):
    db.execute(
        "UPDATE items SET state=?, uuid=?, role=?, imported_at=?, last_error=NULL, force_import=0"
        " WHERE sha256=?",
        (L.IMPORTERT, uuid, role, now_iso(), sha),
    )


def _fail(db, sha, msg, outlog) -> str:
    row = db.execute("SELECT attempts FROM items WHERE sha256=?", (sha,)).fetchone()
    state = L.FEIL if row["attempts"] >= MAX_ATTEMPTS else L.ARKIVERT
    db.execute(
        "UPDATE items SET state=?, last_error=? WHERE sha256=?",
        (state, f"{msg} (se {outlog.name})", sha),
    )
    return "feil" if state == L.FEIL else "venter"


def _bump_or_fail(ws, batch, msg):
    with ws.ledger.tx():
        for r in batch:
            row = ws.ledger.db.execute("SELECT attempts FROM items WHERE sha256=?", (r["sha256"],)).fetchone()
            if row["attempts"] >= MAX_ATTEMPTS:
                ws.ledger.db.execute(
                    "UPDATE items SET state=?, last_error=? WHERE sha256=?", (L.FEIL, msg, r["sha256"])
                )
            else:
                ws.ledger.db.execute(
                    "UPDATE items SET last_error=? WHERE sha256=?", (msg, r["sha256"])
                )


# --------------------------------------------------------------------- verify


def verify_in_photos(ws: Workspace, *, osxphotos: list[str], recheck_all: bool = False) -> dict:
    db = ws.ledger.db
    # Finished photos are always looked up again (cheap) so a photo deleted
    # in Photos afterwards is noticed. Only re-hashing is optional (--alle).
    states = [L.IMPORTERT, L.BEKREFTET, L.I_ICLOUD, L.I_PAPIRKURV, L.AVVIK]
    rows = db.execute(
        f"SELECT * FROM items WHERE uuid IS NOT NULL AND state IN ({','.join('?' * len(states))})",
        states,
    ).fetchall()
    summary = {"sjekket": 0, "bekreftet": 0, "i_icloud": 0, "mangler": 0, "papirkurv": 0,
               "avvik": 0, "ikke_icloud_bibliotek": 0}
    if not rows:
        log.info("Ingenting å kontrollere i Bilder ennå.")
        return summary

    uuids = sorted({r["uuid"] for r in rows})
    log.info("Slår opp %d bilder i Bilder-biblioteket (kan ta litt tid) …", len(uuids))
    info = query_photos(osxphotos, uuids, ws.logs)

    with ws.ledger.tx():
        for row in rows:
            summary["sjekket"] += 1
            p = info.get(row["uuid"])
            sha = row["sha256"]
            if p is None:
                # The photo is gone from Photos. Put it back in the queue.
                log.warning("  Finnes ikke lenger i Bilder, importeres på nytt: %s", row["archive_path"])
                db.execute(
                    "UPDATE items SET state=?, uuid=NULL, role=NULL, verified_at=NULL, verified_how=NULL,"
                    " icloud_at=NULL, attempts=0, last_error=? WHERE sha256=?",
                    (L.ARKIVERT, "forsvant fra Bilder", sha),
                )
                ws.ledger.event("forsvant", row["archive_path"])
                summary["mangler"] += 1
                continue
            if p.get("intrash"):
                db.execute(
                    "UPDATE items SET state=?, last_error=? WHERE sha256=?",
                    (L.I_PAPIRKURV, "ligger i «Nylig slettet» i Bilder", sha),
                )
                summary["papirkurv"] += 1
                continue

            how = row["verified_how"]
            if row["role"] == "følgefil":
                how = "del av bilde"  # e.g. the video half of a Live Photo
            elif not how or recheck_all:
                how = _compare(row, p)
                if how is None:
                    _handle_mismatch(ws, row, p)
                    summary["avvik"] += 1
                    continue

            if p.get("iscloudasset") is False:
                summary["ikke_icloud_bibliotek"] += 1
            state = L.I_ICLOUD if p.get("incloud") else L.BEKREFTET
            db.execute(
                "UPDATE items SET state=?, verified_how=?, verified_at=COALESCE(verified_at, ?),"
                " icloud_at=CASE WHEN ?=1 THEN COALESCE(icloud_at, ?) ELSE NULL END, last_error=NULL"
                " WHERE sha256=?",
                (state, how, now_iso(), int(state == L.I_ICLOUD), now_iso(), sha),
            )
            summary["bekreftet"] += 1
            summary["i_icloud"] += state == L.I_ICLOUD
        ws.ledger.event("kontroll", json.dumps(summary))

    if summary["ikke_icloud_bibliotek"]:
        log.error(
            "Bilder-biblioteket ser IKKE ut til å være koblet til iCloud. Åpne Bilder > "
            "Innstillinger > iCloud og slå på «iCloud-bilder». Bildene er trygge i arkivet og i Bilder, "
            "men de lastes ikke opp før dette er på."
        )
    return summary


def _compare(row, p: dict) -> str | None:
    path = p.get("path")
    if path and os.path.exists(path):
        try:
            disk_size = os.path.getsize(path)
            if disk_size == row["size"]:
                # Same size: the bytes must match exactly.
                return "sha256" if sha256_file(path) == row["sha256"] else None
        except OSError:
            return None
        if row["role"] == "importert" and p.get("original_filesize") == row["size"]:
            # Photos stored the file in another container for a few formats but
            # records the original size we gave it.
            return "størrelse"
        return None
    # Original not on this Mac (only in iCloud): compare the size Photos reports.
    if p.get("original_filesize") == row["size"]:
        return "størrelse"
    return None


def _handle_mismatch(ws, row, p):
    db = ws.ledger.db
    sha = row["sha256"]
    if row["role"] == "fantes_allerede":
        # Photos thought it already had this picture, but the copy there is not
        # identical. Import our copy too: a duplicate is better than a loss.
        log.warning("  Bildet i Bilder er ikke identisk; importerer Min Sky-kopien i tillegg: %s",
                    row["archive_path"])
        db.execute(
            "UPDATE items SET state=?, uuid=NULL, role=NULL, force_import=1, attempts=0,"
            " last_error=? WHERE sha256=?",
            (L.ARKIVERT, "lignende bilde fantes, men var ikke identisk", sha),
        )
    else:
        log.error("  AVVIK: kopien i Bilder stemmer ikke med arkivet: %s", row["archive_path"])
        db.execute(
            "UPDATE items SET state=?, last_error=? WHERE sha256=?",
            (L.AVVIK, f"kopien i Bilder ({p.get('original_filesize')} B) ≠ arkivet ({row['size']} B)", sha),
        )
    ws.ledger.event("avvik", row["archive_path"])


def query_photos(osxphotos: list[str], uuids: list[str], logdir: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    # Chunk to keep each call reasonable on huge libraries.
    for i in range(0, len(uuids), 5000):
        chunk = uuids[i:i + 5000]
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, dir=logdir) as f:
            f.write("\n".join(chunk) + "\n")
            uuid_file = f.name
        result_file = Path(uuid_file).with_suffix(".json")
        try:
            with open(result_file, "w", encoding="utf-8") as res:
                proc = subprocess.run(
                    [*osxphotos, "query", "--uuid-from-file", uuid_file, "--json", "--deleted"],
                    stdout=res, stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL,
                )
            if proc.returncode != 0:
                raise RuntimeError(f"osxphotos query feilet: {proc.stderr.strip()[-2000:]}")
            text = result_file.read_text(encoding="utf-8")
            start = text.find("[")
            data = json.loads(text[start:]) if start >= 0 else []
            for p in data:
                if isinstance(p, str):
                    p = json.loads(p)
                out[p["uuid"]] = p
        finally:
            for fpath in (uuid_file, result_file):
                try:
                    os.unlink(fpath)
                except OSError:
                    pass
    return out


def require_macos():
    if sys.platform != "darwin" and not os.environ.get("MINSKY_TILLAT_IKKE_MAC"):
        raise SystemExit(
            "Dette steget må kjøres på en Mac med Bilder-appen og iCloud-bilder slått på.\n"
            "Steget «hent» (innlesing og arkivering) fungerer også på Windows/Linux."
        )
