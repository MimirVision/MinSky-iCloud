"""Settings remembered between runs (working folder, album, …)."""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULTS = {
    "mappe": None,              # working folder; None = ~/MinSky-flytting
    "album": "Fra Min Sky",
    "gruppe": 500,              # files per import round
    "hent_fra_nedlastinger": True,
    "nedlastinger_fra": None,   # only zips downloaded after this time are picked up
    # Windows
    "icloud_mappe": None,       # the iCloud Photos folder iCloud for Windows uploads from
    "heic": "jpeg",             # "jpeg" = convert HEIC to high-quality JPEG, "hopp" = leave HEIC in the archive
    "maks_ventende": 1500,      # files waiting in the iCloud folder before we pause sending
}


def icloud_session_dir() -> Path:
    return path().parent / "icloud-økt"


def target() -> str:
    """'mac' (Photos app) or 'windows' (iCloud for Windows)."""
    import sys

    t = os.environ.get("MINSKY_PLATTFORM")
    if t in ("mac", "windows"):
        return t
    return "windows" if sys.platform.startswith("win") else "mac"


def path() -> Path:
    base = os.environ.get("MINSKY_KONFIG")
    if base:
        return Path(base)
    return Path.home() / ".config" / "minsky-icloud" / "innstillinger.json"


def load() -> dict:
    data = dict(DEFAULTS)
    try:
        data.update(json.loads(path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return data


def save(data: dict) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: data.get(k) for k in DEFAULTS}, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)
