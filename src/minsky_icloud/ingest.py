"""Step 1: take what was downloaded from Min Sky and put it safely in the archive.

* Zip files are read member by member. Python checks each member's CRC
  while reading, so a damaged or half-downloaded zip is detected.
* Every file is written to a temp file, fsync'ed, moved into place, and
  then read back and hashed again before it is registered.
* Identical files (same SHA-256) are stored once but every place they
  were seen is remembered.
* Nothing in the inbox is ever changed or deleted.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import time
import uuid
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from . import ledger as L
from .util import (
    PARTIAL_SUFFIXES,
    IntegrityError,
    NotEnoughSpace,
    fix_zip_name,
    free_bytes,
    fsync_dir,
    human,
    is_media,
    is_noise,
    now_iso,
    safe_component,
    safe_relpath,
    sha256_file,
    write_stream_durably,
)
from .workspace import Workspace

log = logging.getLogger("minsky")

SPACE_MARGIN = 2 * 1024**3       # always leave 2 GB free
STILL_DOWNLOADING_SECONDS = 120  # files touched this recently may still be downloading


@dataclass
class IngestResult:
    new_files: int = 0
    duplicates: int = 0
    zips_done: int = 0
    zips_broken: list[str] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)
    skipped_known: int = 0


def ingest(ws: Workspace, *, min_age: float | None = None) -> IngestResult:
    if min_age is None:
        min_age = float(os.environ.get("MINSKY_MIN_ALDER", STILL_DOWNLOADING_SECONDS))
    res = IngestResult()
    if not any(ws.inbox.iterdir()):
        log.info("Innboksen er tom: %s", ws.inbox)
        log.info("Last ned bildene fra Min Sky og legg zip-filene (eller mappene) der.")
        return res

    zips: list[Path] = []
    loose: dict[Path, list[Path]] = {}  # top-level group -> files

    for path in sorted(_walk(ws.inbox)):
        rel = path.relative_to(ws.inbox)
        if path.suffix.lower() in PARTIAL_SUFFIXES or any(
            part.lower().endswith(".download") for part in rel.parts[:-1]
        ):
            res.waiting.append(str(rel))
            continue
        try:
            age = time.time() - path.stat().st_mtime
        except FileNotFoundError:
            continue
        if age < min_age:
            res.waiting.append(str(rel))
            continue
        if path.suffix.lower() == ".zip" or _looks_like_zip(path):
            zips.append(path)
            continue
        if is_noise(str(rel)):
            continue
        group = ws.inbox / rel.parts[0] if len(rel.parts) > 1 else ws.inbox
        loose.setdefault(group, []).append(path)

    if res.waiting:
        log.warning(
            "%d fil(er) ser ut til å lastes ned fortsatt og blir tatt neste gang: %s",
            len(res.waiting), ", ".join(res.waiting[:5]) + (" …" if len(res.waiting) > 5 else ""),
        )

    for z in zips:
        _ingest_zip(ws, z, res)
    for group, files in loose.items():
        _ingest_folder(ws, group, files, res)

    log.info(
        "Innlesing ferdig: %d nye filer arkivert, %d var allerede arkivert (duplikater).",
        res.new_files, res.duplicates,
    )
    if res.zips_broken:
        log.error(
            "%d zip-fil(er) er ødelagt eller ufullstendig. Last dem ned på nytt fra Min Sky: %s",
            len(res.zips_broken), ", ".join(res.zips_broken),
        )
    return res


def _walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for f in filenames:
            if f.startswith(".") and not f.startswith("._"):
                continue
            yield Path(dirpath) / f


def _looks_like_zip(path: Path) -> bool:
    if path.suffix:  # only sniff extension-less files
        return False
    try:
        return zipfile.is_zipfile(path)
    except OSError:
        return False


# --------------------------------------------------------------------------- zip


def _ingest_zip(ws: Workspace, path: Path, res: IngestResult) -> None:
    db = ws.ledger.db
    st = path.stat()
    rel = str(path.relative_to(ws.inbox))

    known = db.execute(
        "SELECT * FROM sources WHERE kind='zip' AND path=? AND size=? AND mtime=?",
        (rel, st.st_size, st.st_mtime),
    ).fetchone()
    if known and known["status"] in ("ferdig", "ødelagt"):
        res.skipped_known += 1
        if known["status"] == "ødelagt":
            res.zips_broken.append(rel)
        return

    log.info("Kontrollerer %s (%s) …", rel, human(st.st_size))
    zsha = sha256_file(path)
    src = db.execute("SELECT * FROM sources WHERE sha256=?", (zsha,)).fetchone()
    if src is None:
        archive_dir = f"{safe_component(path.stem)}__{zsha[:8]}"
        with ws.ledger.tx():
            cur = db.execute(
                "INSERT INTO sources(kind, path, size, mtime, sha256, archive_dir, status, first_seen)"
                " VALUES('zip', ?, ?, ?, ?, ?, 'pågår', ?)",
                (rel, st.st_size, st.st_mtime, zsha, archive_dir, now_iso()),
            )
            ws.ledger.event("zip_ny", rel)
        src = db.execute("SELECT * FROM sources WHERE id=?", (cur.lastrowid,)).fetchone()
    else:
        # Same content as a zip we have seen (maybe renamed or downloaded twice).
        db.execute(
            "UPDATE sources SET path=?, size=?, mtime=? WHERE id=?",
            (rel, st.st_size, st.st_mtime, src["id"]),
        )
        if src["status"] == "ferdig":
            log.info("  %s er allerede lest inn (samme innhold). Hopper over.", rel)
            res.skipped_known += 1
            return
        if src["status"] == "ødelagt":
            res.zips_broken.append(rel)
            return

    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError, EOFError) as e:
        _mark_broken(ws, src["id"], f"kan ikke åpnes: {e}")
        res.zips_broken.append(rel)
        return

    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        ignored = sum(1 for i in infos if is_noise(fix_zip_name(i.filename, i.flag_bits)))
        done = {
            r[0] for r in db.execute("SELECT member FROM occurrences WHERE source_id=?", (src["id"],))
        }
        todo = []
        for i in infos:
            name = fix_zip_name(i.filename, i.flag_bits)
            if is_noise(name) or name in done:
                continue
            todo.append((i, name))

        need = sum(i.file_size for i, _ in todo)
        _check_space(ws, need)
        if todo:
            log.info("  %d filer å pakke ut (%s)", len(todo), human(need))

        bad: list[str] = []
        for n, (info, name) in enumerate(todo, 1):
            ws.check_stop()
            mtime = _zip_time(info)
            tmp = ws.tmp / f"{uuid.uuid4().hex}.part"
            try:
                with zf.open(info) as fh:
                    sha, size = write_stream_durably(fh, tmp)
            except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError) as e:
                log.error("  Ødelagt fil i zip: %s (%s)", name, e)
                bad.append(name)
                continue
            if size != info.file_size:
                tmp.unlink(missing_ok=True)
                log.error("  Feil størrelse på %s i zip (forventet %d, fikk %d)", name, info.file_size, size)
                bad.append(name)
                continue
            new = _register(ws, src, name, tmp, sha, size, mtime)
            if new:
                res.new_files += 1
            else:
                res.duplicates += 1
            if n % 200 == 0:
                log.info("  … %d/%d", n, len(todo))

    files = db.execute("SELECT COUNT(*) FROM occurrences WHERE source_id=?", (src["id"],)).fetchone()[0]
    with ws.ledger.tx():
        if bad:
            db.execute(
                "UPDATE sources SET status='ødelagt', files=?, ignored=?, error=? WHERE id=?",
                (files, ignored, f"{len(bad)} ødelagte filer: " + ", ".join(bad[:20]), src["id"]),
            )
            ws.ledger.event("zip_ødelagt", f"{rel}: {len(bad)} filer")
            res.zips_broken.append(rel)
        else:
            db.execute(
                "UPDATE sources SET status='ferdig', files=?, ignored=?, finished=? WHERE id=?",
                (files, ignored, now_iso(), src["id"]),
            )
            ws.ledger.event("zip_ferdig", f"{rel}: {files} filer")
            res.zips_done += 1
    log.info("  %s: %s", rel, "FERDIG" if not bad else f"{len(bad)} ødelagte filer – last ned på nytt")


def _zip_time(info: zipfile.ZipInfo) -> float | None:
    try:
        return _dt.datetime(*info.date_time).timestamp()
    except (ValueError, OverflowError):
        return None


def _mark_broken(ws: Workspace, source_id: int, msg: str) -> None:
    with ws.ledger.tx():
        ws.ledger.db.execute(
            "UPDATE sources SET status='ødelagt', error=? WHERE id=?", (msg, source_id)
        )
        ws.ledger.event("zip_ødelagt", msg)
    log.error("  Zip-filen %s", msg)


# ------------------------------------------------------------------------ folder


def _ingest_folder(ws: Workspace, group: Path, files: list[Path], res: IngestResult) -> None:
    db = ws.ledger.db
    rel = "." if group == ws.inbox else str(group.relative_to(ws.inbox))
    src = db.execute("SELECT * FROM sources WHERE kind='mappe' AND path=?", (rel,)).fetchone()
    if src is None:
        archive_dir = "løse-filer" if rel == "." else f"{safe_component(group.name)}__mappe"
        # Two different inbox folders with the same name must not share a directory.
        n = 2
        while db.execute("SELECT 1 FROM sources WHERE archive_dir=?", (archive_dir,)).fetchone():
            archive_dir = f"{safe_component(group.name)}__mappe{n}"
            n += 1
        with ws.ledger.tx():
            cur = db.execute(
                "INSERT INTO sources(kind, path, archive_dir, status, first_seen)"
                " VALUES('mappe', ?, ?, 'pågår', ?)",
                (rel, archive_dir, now_iso()),
            )
        src = db.execute("SELECT * FROM sources WHERE id=?", (cur.lastrowid,)).fetchone()

    seen = {
        (r["member"], r["size"], r["mtime"])
        for r in db.execute("SELECT member, size, mtime FROM occurrences WHERE source_id=?", (src["id"],))
    }
    todo = []
    for f in files:
        st = f.stat()
        member = str(f.relative_to(group)).replace(os.sep, "/")
        if (member, st.st_size, st.st_mtime) in seen:
            continue
        todo.append((f, member, st))
    if not todo:
        return
    _check_space(ws, sum(st.st_size for _, _, st in todo))
    log.info("Leser inn %d filer fra %s", len(todo), "innboksen" if rel == "." else rel)
    for f, member, st in todo:
        ws.check_stop()
        tmp = ws.tmp / f"{uuid.uuid4().hex}.part"
        with open(f, "rb") as fh:
            sha, size = write_stream_durably(fh, tmp)
        if size != st.st_size or os.stat(f).st_mtime != st.st_mtime:
            tmp.unlink(missing_ok=True)
            log.warning("  %s endret seg mens den ble kopiert; tas neste gang.", member)
            continue
        new = _register(ws, src, member, tmp, sha, size, st.st_mtime, occ_mtime=st.st_mtime)
        res.new_files += new
        res.duplicates += not new
    files_n = db.execute("SELECT COUNT(*) FROM occurrences WHERE source_id=?", (src["id"],)).fetchone()[0]
    with ws.ledger.tx():
        db.execute(
            "UPDATE sources SET status='ferdig', files=?, finished=? WHERE id=?",
            (files_n, now_iso(), src["id"]),
        )


# ---------------------------------------------------------------------- register


def _register(ws, src, member: str, tmp: Path, sha: str, size: int,
              mtime: float | None, occ_mtime: float | None = None) -> bool:
    """Move a verified temp file into the archive and record it.

    Returns True if this was new content, False if we already had it.
    Safe to crash at any point: a leftover file in place is recognised by
    its hash on the next run.
    """
    db = ws.ledger.db
    existing = ws.ledger.item(sha)
    if existing is not None:
        target = ws.archive_abs(existing["archive_path"])
        if not target.exists() or target.stat().st_size != existing["size"]:
            # The archived copy went missing or was damaged: repair it from this copy.
            log.warning("  Reparerer arkivkopi: %s", existing["archive_path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp, target)
            fsync_dir(target.parent)
            _verify_readback(target, sha)
        else:
            tmp.unlink(missing_ok=True)
        with ws.ledger.tx():
            db.execute(
                "INSERT OR IGNORE INTO occurrences(source_id, member, sha256, size, mtime) VALUES(?,?,?,?,?)",
                (src["id"], member, sha, size, occ_mtime),
            )
        return False

    rel = Path(src["archive_dir"]) / safe_relpath(member)
    target = ws.archive / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    n = 2
    while target.exists():
        if target.stat().st_size == size and sha256_file(target) == sha:
            # Left over from an interrupted run: same bytes, reuse it.
            tmp.unlink(missing_ok=True)
            break
        target = target.with_name(f"{rel.stem} ({n}){rel.suffix}")
        n += 1
    else:
        os.replace(tmp, target)
        fsync_dir(target.parent)
    if mtime:
        try:
            os.utime(target, (mtime, mtime))
        except OSError:
            pass
    _verify_readback(target, sha)

    archive_rel = str(target.relative_to(ws.archive)).replace(os.sep, "/")
    taken = _dt.datetime.fromtimestamp(mtime).isoformat(timespec="seconds") if mtime else None
    media = is_media(target)
    with ws.ledger.tx():
        db.execute(
            "INSERT INTO items(sha256, size, archive_path, ext, is_media, taken_hint, state, created)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (sha, size, archive_rel, target.suffix.lower(), int(media), taken,
             L.ARKIVERT if media else L.IKKE_MEDIA, now_iso()),
        )
        ws.ledger.set_meta("arkiv_kontroll_ok", "0")  # new content: archive must be re-checked
        db.execute(
            "INSERT OR IGNORE INTO occurrences(source_id, member, sha256, size, mtime) VALUES(?,?,?,?,?)",
            (src["id"], member, sha, size, occ_mtime),
        )
    return True


def _verify_readback(path: Path, sha: str) -> None:
    got = sha256_file(path)
    if got != sha:
        raise IntegrityError(
            f"Filen {path} ble ikke skrevet riktig til disken (kontrollsum stemmer ikke). "
            "Stopper for sikkerhets skyld. Sjekk disken før du fortsetter."
        )


def _check_space(ws: Workspace, need: int) -> None:
    free = free_bytes(ws.archive)
    if need + SPACE_MARGIN > free:
        raise NotEnoughSpace(
            f"For lite ledig plass: trenger {human(need)} + {human(SPACE_MARGIN)} margin, "
            f"men bare {human(free)} er ledig på {ws.archive}. Frigjør plass eller bruk en "
            "ekstern disk (--mappe /Volumes/<disk>/MinSky-flytting)."
        )
