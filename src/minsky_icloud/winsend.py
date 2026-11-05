"""Windows: send photos to iCloud through Apple's "iCloud for Windows" app.

How it works:
  1. The iCloud library is read (read-only) to build an index of
     (file name, exact size) for everything already there.
  2. Photos that are already in iCloud (often the case: Min Sky and iCloud
     both backed up the same iPhone) are recognised and NOT sent again.
  3. The rest are copied into the "iCloud Photos" folder that iCloud for
     Windows uploads from. HEIC photos are converted to high-quality JPEG
     first, because iCloud for Windows does not upload HEIC. The original
     HEIC stays untouched in the archive.
  4. A photo only counts as done when it shows up in the iCloud library
     with the exact name and size we sent.

Crash safety: before a file is copied, its target name is recorded. On the
next run a file found in the folder with the recorded name and checksum
is recognised, so nothing is ever placed in the folder twice.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from . import ledger as L
from .icloud import ICloud, NeedLogin
from .util import NotEnoughSpace, free_bytes, fsync_dir, human, now_iso, sha256_file, write_stream_durably
from .workspace import Workspace

log = logging.getLogger("minsky")

HEIC_EXT = {".heic", ".heif"}
MAX_WAITING = 1500           # files sent but not yet confirmed before we stop sending more
SEND_ROUND = 500             # files placed in the folder per round
RESCAN_SECONDS = 10 * 60     # how often the iCloud library is re-read while waiting
STALE_HOURS = 24             # sent but still not in iCloud after this long -> flag it
SPACE_MARGIN = 5 * 1024**3
JPEG_QUALITY = 95


class UploadFolderMissing(Exception):
    pass


# ------------------------------------------------------------------ folder
def upload_folder_candidates() -> list[Path]:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    pics = home / "Pictures"
    out = []
    for base in (pics, home / "OneDrive" / "Pictures", home / "Bilder"):
        out += [base / "iCloud Photos" / "Uploads", base / "iCloud Photos" / "Photos", base / "iCloud Photos"]
    return out


def find_upload_folder(settings: dict) -> Path | None:
    chosen = settings.get("icloud_mappe")
    if chosen and Path(chosen).is_dir():
        return Path(chosen)
    for c in upload_folder_candidates():
        if c.is_dir():
            return c
    return None


# ------------------------------------------------------------------- index
_COPY_SUFFIX = re.compile(r"\s*\(\d+\)$")
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".3gp", ".avi", ".mts", ".m2ts", ".mkv", ".wmv", ".mpg", ".mpeg"}


def _keys(name: str, size: int | None):
    p = Path(name.lower())
    stem = _COPY_SUFFIX.sub("", p.stem)
    return (p.name, size), (stem + p.suffix.replace(".jpeg", ".jpg"), size)


def media_date(path: Path) -> _dt.datetime | None:
    """When the photo was taken, from EXIF (local camera time), if readable."""
    try:
        from PIL import Image
        import pillow_heif

        pillow_heif.register_heif_opener()
        with Image.open(path) as im:
            ex = im.getexif()
            v = ex.get_ifd(0x8769).get(0x9003) or ex.get(0x0132)
        return _dt.datetime.strptime(str(v).strip("\x00 "), "%Y:%m:%d %H:%M:%S") if v else None
    except Exception:
        return None


def same_moment(local: _dt.datetime | None, created) -> bool | None:
    """Do an EXIF time (camera's local time) and iCloud's capture time (UTC) describe
    the same moment? They may differ by a whole time-zone offset. None = cannot tell."""
    if local is None or created is None:
        return None
    try:
        utc = created.astimezone(_dt.timezone.utc).replace(tzinfo=None) if created.tzinfo else created
    except Exception:
        return None
    d = abs((utc - local).total_seconds())
    if d > 14 * 3600 + 120:
        return False
    r = d % 3600
    return r <= 120 or r >= 3600 - 120


class Index:
    """What is in the iCloud library, keyed by (file name, size)."""

    def __init__(self):
        self.exact: dict[tuple, list] = {}
        self.loose: dict[tuple, list] = {}
        self.names: set[str] = set()   # lower-case file names in iCloud
        self.when = 0.0
        self.count = 0

    def stale(self, max_age: float = RESCAN_SECONDS) -> bool:
        return time.time() - self.when > max_age

    def refresh(self, icloud: ICloud, ws: Workspace | None = None) -> None:
        exact, loose, names, n = {}, {}, set(), 0
        for a in icloud.scan():
            if ws is not None and n % 500 == 0:
                ws.check_stop()
            n += 1
            k1, k2 = _keys(a.filename, a.size)
            exact.setdefault(k1, []).append((a.id, a.created))
            loose.setdefault(k2, []).append((a.id, a.created))
            names.add(a.filename.lower())
            if n % 5000 == 0:
                log.info("  … har lest %d bilder fra iCloud", n)
        self.exact, self.loose, self.names, self.count, self.when = exact, loose, names, n, time.time()
        log.info("iCloud-biblioteket inneholder %d bilder og videoer.", n)

    def find(self, name: str, size: int | None, *, taken: _dt.datetime | None = None,
             strict: bool = True, exclude: set | None = None) -> str | None:
        """An iCloud photo with this name and exact size (and, for photos, the same capture time).

        strict: when the capture time cannot be compared, only accept videos
        (photos then count as 'not found' and are sent: a duplicate is better than a loss).
        """
        exclude = exclude or set()
        is_video = Path(name).suffix.lower() in VIDEO_EXT
        k1, k2 = _keys(name, size)
        for exact, cands in ((True, self.exact.get(k1, [])), (False, self.loose.get(k2, []))):
            for asset_id, created in cands:
                if asset_id in exclude:
                    continue
                same = same_moment(taken, created)
                if same is False:
                    continue
                if same is None and (strict or not exact) and not is_video:
                    continue  # a near-match on name alone is not proof for a photo
                return asset_id
        return None


# ------------------------------------------------------------------- steps
def mark_already_in_icloud(ws: Workspace, index: Index) -> int:
    """Photos iCloud already has need not be sent: same original file name, exact size and,
    for photos, the same capture time. Anything less certain is sent (a duplicate beats a loss)."""
    db = ws.ledger.db
    rows = db.execute(
        "SELECT sha256, archive_path, size FROM items WHERE state=? AND selected=1", (L.ARKIVERT,)
    ).fetchall()
    used = {u for (u,) in db.execute("SELECT uuid FROM items WHERE uuid IS NOT NULL")}
    found = []
    for r in rows:
        name = r["archive_path"].rsplit("/", 1)[-1]
        k1, k2 = _keys(name, r["size"])
        if k1 not in index.exact and k2 not in index.loose:
            continue  # cheap check first; only read the date when a name+size match exists
        asset = index.find(name, r["size"], taken=media_date(ws.archive_abs(r["archive_path"])),
                           strict=True, exclude=used)
        if asset:
            used.add(asset)
            found.append((r, asset))
    n = 0
    with ws.ledger.tx():
        for r, asset in found:
            db.execute(
                "UPDATE items SET state=?, uuid=?, role='fantes_allerede', verified_how=?, verified_at=?,"
                " icloud_at=?, last_error=NULL WHERE sha256=?",
                (L.I_ICLOUD, asset, "fantes i iCloud (navn, nøyaktig størrelse og tidspunkt)", now_iso(), now_iso(),
                 r["sha256"]))
            n += 1
    if n:
        log.info("%d bilder fantes allerede i iCloud og sendes ikke på nytt.", n)
    return n


def confirm_sent(ws: Workspace, index: Index) -> int:
    db = ws.ledger.db
    rows = db.execute("SELECT * FROM items WHERE state=?", (L.SENDT,)).fetchall()
    used = {u for (u,) in db.execute("SELECT uuid FROM items WHERE uuid IS NOT NULL")}
    n = stale = 0
    limit = (_dt.datetime.now().astimezone() - _dt.timedelta(hours=STALE_HOURS)).isoformat(timespec="seconds")
    with ws.ledger.tx():
        for r in rows:
            sent_file = (ws.converted / f"{r['sha256']}.jpg") if r["converted"] else ws.archive_abs(r["archive_path"])
            asset = index.find(r["upload_name"], r["upload_size"], taken=media_date(sent_file),
                               strict=False, exclude=used)
            if asset:
                used.add(asset)
                db.execute(
                    "UPDATE items SET state=?, uuid=?, role='importert', verified_how=?, verified_at=?, icloud_at=?,"
                    " last_error=NULL WHERE sha256=?",
                    (L.I_ICLOUD, asset, "funnet i iCloud (navn og nøyaktig størrelse)", now_iso(), now_iso(),
                     r["sha256"]))
                n += 1
            elif (r["sent_at"] or "") < limit:
                db.execute("UPDATE items SET state=?, last_error=? WHERE sha256=?",
                           (L.AVVIK, f"lagt i iCloud-mappen for over {STALE_HOURS} timer siden, men finnes ikke "
                                     "i iCloud. Sjekk at iCloud for Windows kjører.", r["sha256"]))
                stale += 1
    if n:
        log.info("%d bilder er bekreftet i iCloud.", n)
    if stale:
        log.error("%d bilder er ikke kommet fram til iCloud etter %d timer.", stale, STALE_HOURS)
    return n


def convert_heic(src: Path, dst: Path) -> tuple[str, int]:
    """HEIC -> JPEG at quality 95, full colour resolution, keeping EXIF (date,
    place, camera), XMP and the colour profile. Orientation is applied to
    the pixels and reset in EXIF, so the photo is never shown rotated."""
    from PIL import Image
    import pillow_heif

    pillow_heif.register_heif_opener()
    tmp = dst.with_name(dst.name + ".part")
    with Image.open(src) as im:
        info = dict(im.info)
        img = im.convert("RGB") if im.mode != "RGB" else im.copy()
    kw = {"quality": JPEG_QUALITY, "subsampling": 0, "optimize": True}
    if info.get("exif"):
        kw["exif"] = info["exif"]
    if info.get("icc_profile"):
        kw["icc_profile"] = info["icc_profile"]
    if info.get("xmp"):
        kw["xmp"] = info["xmp"]
    with open(tmp, "wb") as f:
        try:
            img.save(f, "JPEG", **kw)
        except TypeError:  # older Pillow without xmp=
            kw.pop("xmp", None)
            f.seek(0)
            f.truncate()
            img.save(f, "JPEG", **kw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, dst)
    st = src.stat()
    os.utime(dst, (st.st_atime, st.st_mtime))
    return sha256_file(dst), dst.stat().st_size


def _unique_name(folder: Path, name: str, taken: set[str], in_icloud: set[str] = frozenset()) -> str:
    """A name not used in the folder, by us before, or by a different photo already in iCloud
    (so confirmation can never mistake an old photo for the new upload)."""
    stem, suffix = Path(name).stem, Path(name).suffix
    cand, n = name, 2
    while cand.lower() in taken or cand.lower() in in_icloud or (folder / cand).exists():
        cand = f"{stem} ({n}){suffix}"
        n += 1
    taken.add(cand.lower())
    return cand


def send(ws: Workspace, folder: Path, settings: dict, *, limit: int | None = None,
         index: "Index | None" = None) -> int:
    """Copy the next photos into the iCloud Photos folder."""
    db = ws.ledger.db
    convert = settings.get("heic", "jpeg") == "jpeg"
    waiting = db.execute("SELECT COUNT(*) FROM items WHERE state=?", (L.SENDT,)).fetchone()[0]
    room = max(0, int(settings.get("maks_ventende") or MAX_WAITING) - waiting)
    if room == 0:
        log.info("Venter på at iCloud tar unna %d bilder før flere sendes.", waiting)
        return 0
    n_max = min(room, limit or SEND_ROUND)
    heic_filter = "" if convert else f" AND ext NOT IN ({','.join(repr(e) for e in HEIC_EXT)})"
    rows = db.execute(
        f"SELECT * FROM items WHERE state IN (?, ?) AND selected=1 {heic_filter}"
        " ORDER BY state DESC, archive_path LIMIT ?", (L.IMPORTERER, L.ARKIVERT, n_max)
    ).fetchall()
    if not rows:
        return 0
    need = sum(r["size"] for r in rows)
    if need + SPACE_MARGIN > free_bytes(folder):
        raise NotEnoughSpace(
            f"For lite plass på disken med iCloud-mappen ({human(free_bytes(folder))} ledig). "
            "Åpne iCloud for Windows og vent til opplastingen har tatt igjen, eller frigjør plass.")
    taken = {r[0].lower() for r in db.execute("SELECT upload_name FROM items WHERE upload_name IS NOT NULL")}
    ws.converted.mkdir(parents=True, exist_ok=True)
    sent = 0
    for r in rows:
        ws.check_stop()
        _send_one(ws, folder, r, convert, taken, index.names if index else frozenset())
        sent += 1
        if sent % 100 == 0:
            log.info("  … %d/%d lagt i iCloud-mappen", sent, len(rows))
    log.info("%d bilder lagt i iCloud-mappen. iCloud for Windows laster dem opp.", sent)
    return sent


def _send_one(ws: Workspace, folder: Path, r, convert: bool, taken: set[str], in_icloud=frozenset()) -> None:
    db = ws.ledger.db
    src = ws.archive_abs(r["archive_path"])
    is_heic = r["ext"] in HEIC_EXT and convert

    # Resume: we recorded a name before and the file may already be there.
    if r["state"] == L.IMPORTERER and r["upload_name"]:
        dest = folder / r["upload_name"]
        if dest.exists() and dest.stat().st_size == r["upload_size"] and sha256_file(dest) == r["upload_sha"]:
            _mark_sent(ws, r["sha256"])
            return

    if is_heic:
        conv = ws.converted / f"{r['sha256']}.jpg"
        if not (conv.exists() and r["upload_sha"] and conv.stat().st_size == r["upload_size"]
                and sha256_file(conv) == r["upload_sha"]):
            try:
                up_sha, up_size = convert_heic(src, conv)
            except Exception as e:
                log.error("  Kunne ikke gjøre om %s til JPEG: %s", r["archive_path"], e)
                with ws.ledger.tx():
                    db.execute("UPDATE items SET state=?, last_error=? WHERE sha256=?",
                               (L.FEIL, f"HEIC-konvertering feilet: {e}", r["sha256"]))
                return
        else:
            up_sha, up_size = r["upload_sha"], r["upload_size"]
        payload, name = conv, Path(r["archive_path"]).stem + ".jpg"
    else:
        payload, name, up_sha, up_size = src, Path(r["archive_path"]).name, r["sha256"], r["size"]

    name = r["upload_name"] if (r["upload_name"] and not (folder / r["upload_name"]).exists()) \
        else _unique_name(folder, name, taken, in_icloud)
    with ws.ledger.tx():  # record the intent before touching the folder
        db.execute(
            "UPDATE items SET state=?, upload_name=?, upload_size=?, upload_sha=?, converted=?, attempts=attempts+1"
            " WHERE sha256=?", (L.IMPORTERER, name, up_size, up_sha, int(is_heic), r["sha256"]))

    tmp = ws.tmp / f"{uuid.uuid4().hex}.part"
    with open(payload, "rb") as fh:
        sha, size = write_stream_durably(fh, tmp)
    if sha != up_sha:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"{payload} endret seg under kopiering")
    st = payload.stat()
    os.utime(tmp, (st.st_atime, st.st_mtime))
    dest = folder / name
    shutil.move(str(tmp), str(dest))  # a rename when on the same disk
    fsync_dir(folder)
    if sha256_file(dest) != up_sha:
        raise RuntimeError(f"Kopien i iCloud-mappen ble ikke riktig: {dest}")
    _mark_sent(ws, r["sha256"])


def _mark_sent(ws: Workspace, sha: str) -> None:
    with ws.ledger.tx():
        ws.ledger.db.execute("UPDATE items SET state=?, sent_at=?, last_error=NULL WHERE sha256=?",
                             (L.SENDT, now_iso(), sha))


def live_photo_videos(ws: Workspace) -> list[str]:
    """The .MOV halves of Live Photos (same folder and name as a photo)."""
    db = ws.ledger.db
    photos = set()
    movs = []
    for r in db.execute("SELECT sha256, archive_path, ext FROM items WHERE is_media=1"):
        folder, _, fname = r["archive_path"].rpartition("/")
        key = (folder, Path(fname).stem.lower())
        if r["ext"] == ".mov":
            movs.append((key, r["sha256"]))
        elif r["ext"] in (".heic", ".heif", ".jpg", ".jpeg"):
            photos.add(key)
    return [sha for key, sha in movs if key in photos]


def round_(ws: Workspace, icloud: ICloud, settings: dict, index: Index, phase, *, limit=None,
           force_scan: bool = False) -> dict:
    """One autopilot round on Windows."""
    folder = find_upload_folder(settings)
    if folder is None:
        raise UploadFolderMissing(
            "Fant ikke iCloud-bilder-mappen. Installer «iCloud for Windows» fra Microsoft Store, logg inn, "
            "slå på «Bilder», og velg mappen under Innstillinger.")
    if not icloud.ready:
        raise NeedLogin("Logg inn i iCloud for å fortsette.")
    db = ws.ledger.db
    pending = db.execute("SELECT COUNT(*) FROM items WHERE state IN (?,?) AND selected=1",
                         (L.ARKIVERT, L.SENDT)).fetchone()[0]
    if force_scan or (pending and index.stale()):
        phase("Leser iCloud-biblioteket (for å se hva som allerede er der)")
        index.refresh(icloud, ws)
    phase("Ser etter bilder som allerede er i iCloud")
    mark_already_in_icloud(ws, index)
    confirm_sent(ws, index)
    phase("Legger bilder i iCloud-mappen")
    sent = send(ws, folder, settings, limit=limit, index=index)
    return {"sent": sent}
