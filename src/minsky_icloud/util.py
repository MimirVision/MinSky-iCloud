"""Small helpers: hashing, durable file writes, disk space, locking."""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
import re
import shutil
import unicodedata
from pathlib import Path
from typing import BinaryIO

CHUNK = 1024 * 1024

# Files we treat as photos/videos and hand to the Photos app.
MEDIA_EXTENSIONS = {
    # images
    ".jpg", ".jpeg", ".jpe", ".heic", ".heif", ".png", ".gif", ".tif", ".tiff",
    ".bmp", ".webp", ".avif",
    # raw
    ".dng", ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2",
    ".raf", ".orf", ".rw2", ".pef", ".srw", ".3fr", ".erf", ".kdc", ".mrw",
    # video
    ".mov", ".mp4", ".m4v", ".3gp", ".3g2", ".avi", ".mts", ".m2ts", ".mpg",
    ".mpeg", ".mkv", ".wmv",
}

# Noise that zip tools and operating systems add. Never media, never needed.
NOISE_NAMES = {".ds_store", "thumbs.db", "desktop.ini", "ehthumbs.db"}

# Partial downloads from browsers. Never touched until they are complete.
PARTIAL_SUFFIXES = {".crdownload", ".part", ".partial", ".download", ".tmp", ".opdownload"}


class IntegrityError(Exception):
    """A file on disk does not match what we just wrote. Stop everything."""


class NotEnoughSpace(Exception):
    pass


class Stopped(Exception):
    """The user pressed Stop. Raised only between safe points."""


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def write_stream_durably(src: BinaryIO, dst: Path) -> tuple[str, int]:
    """Copy src into a new file dst, fsync it, return (sha256, size).

    dst must not exist. Any error from reading src (for example a CRC error
    in a zip member) propagates and the partial dst is removed.
    """
    h = hashlib.sha256()
    size = 0
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                b = src.read(CHUNK)
                if not b:
                    break
                out.write(b)
                h.update(b)
                size += len(b)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        try:
            os.unlink(dst)
        except FileNotFoundError:
            pass
        raise
    return h.hexdigest(), size


def fsync_dir(path: Path) -> None:
    """Make a rename inside path durable (no-op where unsupported)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def free_bytes(path: Path) -> int:
    p = Path(path)
    while not p.exists():
        p = p.parent
    return shutil.disk_usage(p).free


def human(n: int | float | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


_BAD_CHARS = re.compile(r'[\x00-\x1f\\:*?"<>|]')


def safe_component(name: str) -> str:
    name = unicodedata.normalize("NFC", name)
    name = _BAD_CHARS.sub("_", name).strip().rstrip(".")
    if name in ("", ".", ".."):
        name = "_"
    # Keep each path component well under common 255-byte limits.
    while len(name.encode("utf-8")) > 200:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) < 10 and stem:
            name = stem[:-1] + "." + ext
        else:
            name = name[:-1]
    return name


def safe_relpath(member: str) -> Path:
    """Turn a path from a zip (or a folder) into a safe relative path.

    Absolute paths and '..' are neutralised so nothing can be written
    outside the archive.
    """
    parts = [p for p in member.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    if not parts:
        parts = ["_"]
    return Path(*[safe_component(p) for p in parts])


def is_noise(member: str) -> bool:
    parts = member.replace("\\", "/").split("/")
    if "__MACOSX" in parts:
        return True
    name = parts[-1]
    return name.lower() in NOISE_NAMES or name.startswith("._")


def is_media(path: str | Path) -> bool:
    return Path(path).suffix.lower() in MEDIA_EXTENSIONS


def fix_zip_name(name: str, flag_bits: int) -> str:
    """Zip files without the UTF-8 flag get decoded as cp437 by Python.

    Many tools actually wrote UTF-8 anyway, which turns 'æøå' into garbage.
    Undo that when it decodes cleanly.
    """
    if not flag_bits & 0x800:
        try:
            name = name.encode("cp437").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return unicodedata.normalize("NFC", name)


class FileLock:
    """Make sure only one copy of the tool works on a folder at a time."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def __enter__(self):
        self._fh = open(self.path, "a+")
        try:
            import fcntl

            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self._fh.close()
                raise SystemExit(
                    "Et annet vindu kjører allerede verktøyet på denne mappen. "
                    "Vent til det er ferdig (eller lukk det) og prøv igjen."
                )
        except ImportError:  # Windows
            import msvcrt

            try:
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                self._fh.close()
                raise SystemExit("Et annet vindu kjører allerede verktøyet på denne mappen.")
        return self

    def __exit__(self, *exc):
        if self._fh:
            self._fh.close()
