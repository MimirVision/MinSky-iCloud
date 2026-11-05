"""Autopilot: one button that keeps everything moving.

While it runs, it repeatedly:
  1. picks up new Min Sky zips from ~/Downloads (optional),
  2. reads in and checks new downloads,
  3. imports up to ~1000 selected photos into Photos,
  4. checks them in Photos and asks whether they are in iCloud,
then waits for more downloads or for iCloud to finish uploading.

You can keep downloading from Min Sky while it imports, so the two
overlap instead of running one after the other. The Mac is kept awake
while it runs, and a notification is shown when everything is done or
something needs attention.

All work goes through the same functions as the command line, so every
safety guarantee still holds. Stop is honoured at the next safe point.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Callable

from . import config
from . import ledger as L
from . import photos as P
from . import winsend as W
from .ingest import ingest
from .report import gather
from .util import PARTIAL_SUFFIXES, fix_zip_name, is_media, is_noise, now_iso
from .workspace import Workspace

log = logging.getLogger("minsky")

ROUND_LIMIT = 1000          # files imported before looking for new downloads again
VERIFY_EVERY = 10 * 60      # seconds between iCloud checks while waiting
IDLE_SLEEP = 15             # seconds between looks at the Downloads folder
MIN_MEDIA_SHARE = 0.8       # a zip must be mostly photos/videos to be picked up


def notify(title: str, text: str) -> None:
    if sys.platform.startswith("win"):
        _notify_windows(title, text)
        return
    if sys.platform != "darwin":
        return
    script = f'display notification {_q(text)} with title {_q(title)} sound name "Glass"'
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


def _notify_windows(title: str, text: str) -> None:
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing;"
        "$n = New-Object System.Windows.Forms.NotifyIcon; $n.Icon = [System.Drawing.SystemIcons]::Information;"
        f"$n.BalloonTipTitle = {_ps(title)}; $n.BalloonTipText = {_ps(text)}; $n.Visible = $true;"
        "$n.ShowBalloonTip(10000); Start-Sleep -Seconds 10; $n.Dispose()"
    )
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        pass


def _ps(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


class KeepAwake:
    """Stop the Mac from sleeping while work is going on (macOS 'caffeinate')."""

    def __init__(self):
        self.proc = None

    def __enter__(self):
        if sys.platform == "darwin" and shutil.which("caffeinate"):
            self.proc = subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())])
        elif sys.platform.startswith("win"):
            import ctypes  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED: no sleep while this thread runs

            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
        return self

    def __exit__(self, *exc):
        if self.proc:
            self.proc.terminate()
        elif sys.platform.startswith("win"):
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)


def downloads_dir() -> Path:
    return Path(os.environ.get("MINSKY_NEDLASTINGER") or Path.home() / "Downloads")


def looks_like_photo_zip(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as zf:
            names = [fix_zip_name(i.filename, i.flag_bits) for i in zf.infolist() if not i.is_dir()]
    except (zipfile.BadZipFile, OSError, EOFError):
        return False
    names = [n for n in names if not is_noise(n)]
    if not names:
        return False
    media = sum(1 for n in names if is_media(n))
    return media >= 1 and media / len(names) >= MIN_MEDIA_SHARE


def pull_downloads(ws: Workspace, since: float, min_age: float | None = None) -> list[str]:
    """Move finished photo zips that appeared in Downloads after `since` into the inbox.

    Only zips that are complete (not .crdownload, not changed in the last
    two minutes) and mostly photos/videos are touched, so unrelated
    downloads are left alone.
    """
    src = downloads_dir()
    if min_age is None:
        min_age = float(os.environ.get("MINSKY_MIN_ALDER", 120))
    moved: list[str] = []
    if not src.is_dir():
        return moved
    now = time.time()
    for p in sorted(src.iterdir()):
        if not p.is_file() or p.suffix.lower() != ".zip" or p.name.startswith("."):
            continue
        if any(p.name.lower().endswith(s) for s in PARTIAL_SUFFIXES):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_mtime < since or now - st.st_mtime < min_age:
            continue
        if not looks_like_photo_zip(p):
            continue
        dest = ws.inbox / p.name
        n = 2
        while dest.exists():
            dest = ws.inbox / f"{p.stem} ({n}){p.suffix}"
            n += 1
        shutil.move(str(p), str(dest))  # rename on the same disk, copy+delete across disks
        log.info("Hentet %s fra Nedlastinger", p.name)
        moved.append(p.name)
    return moved


def pending_selected(ws: Workspace) -> int:
    return ws.ledger.db.execute(
        "SELECT COUNT(*) FROM items WHERE state IN (?, ?) AND selected=1", (L.ARKIVERT, L.IMPORTERER)
    ).fetchone()[0]


def run(ws: Workspace, *, osxphotos: list[str] | None = None, settings: dict,
        phase: Callable[[str], None] = lambda s: None, once: bool = False,
        idle_sleep: float | None = None, verify_every: float = VERIFY_EVERY,
        icloud=None, index: "W.Index | None" = None) -> None:
    windows = config.target() == "windows"
    if windows and index is None:
        index = W.Index()
    if idle_sleep is None:
        idle_sleep = IDLE_SLEEP
    started = time.time()
    since = settings.get("nedlastinger_fra") or started
    last_verify = 0.0
    told_done = told_problem = False
    with KeepAwake():
        while True:
            ws.check_stop()
            if settings.get("hent_fra_nedlastinger"):
                phase("Ser etter nye nedlastinger")
                pull_downloads(ws, since)

            phase("Leser inn og kontrollerer nedlastinger")
            ingest(ws)

            imported = False
            if windows:
                imported = W.round_(ws, icloud, settings, index, phase, limit=ROUND_LIMIT,
                                    force_scan=index.when == 0)["sent"] > 0
            else:
                if pending_selected(ws):
                    phase("Legger bilder inn i Bilder-appen")
                    P.import_to_photos(ws, osxphotos=osxphotos, batch_size=int(settings.get("gruppe") or 500),
                                       limit=ROUND_LIMIT, album=settings.get("album") or None)
                    imported = True
                if imported or time.time() - last_verify >= verify_every:
                    phase("Sjekker bildene i Bilder og iCloud")
                    P.verify_in_photos(ws, osxphotos=osxphotos)
                    last_verify = time.time()

            g = gather(ws)
            c = g["counts"]
            problems = c[L.FEIL] + c[L.AVVIK] + c[L.I_PAPIRKURV]
            selected = ws.ledger.db.execute(
                "SELECT COUNT(*) FROM items WHERE is_media=1 AND selected=1").fetchone()[0]
            if problems and not told_problem:
                notify("Min Sky → iCloud", f"{problems} bilder trenger tilsyn. Åpne programmet for detaljer.")
                told_problem = True
            if selected and c[L.I_ICLOUD] >= selected and not pending_selected(ws):
                if not told_done:
                    log.info("✓ Alle %d valgte bilder er i iCloud.", selected)
                    notify("Min Sky → iCloud", f"Ferdig! Alle {selected} valgte bilder er i iCloud.")
                    told_done = True
            else:
                told_done = False

            if once:
                return
            if windows and imported:
                continue  # room for more in the iCloud folder
            if not windows and pending_selected(ws):
                continue  # more to import right away
            uploading = c[L.BEKREFTET] + c[L.SENDT]
            phase("Venter på at iCloud blir ferdig med opplastingen (sjekker igjen om noen minutter)"
                  if uploading else "Venter på nye nedlastinger fra Min Sky")
            end = time.time() + idle_sleep
            while time.time() < end:
                ws.check_stop()
                time.sleep(0.5)


def stamp_downloads_since(settings: dict) -> dict:
    if not settings.get("nedlastinger_fra"):
        settings["nedlastinger_fra"] = time.time()
        settings["nedlastinger_fra_tekst"] = now_iso()
    return settings
