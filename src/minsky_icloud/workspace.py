"""The working folder and its layout."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from .ledger import Ledger
from .util import FileLock, Stopped

log = logging.getLogger("minsky")

DEFAULT_DIR = Path.home() / "MinSky-flytting"


class Workspace:
    """
    <mappe>/
      innboks/         <- put what you download from Min Sky here (zip or folders)
      arkiv/           <- verified copy of every file; your permanent backup
      rapporter/       <- CSV/HTML reports
      logg/            <- log files and raw output from osxphotos
      flytting.db      <- the ledger
    """

    def __init__(self, root: Path | str | None = None):
        root = root or os.environ.get("MINSKY_MAPPE") or DEFAULT_DIR
        self.root = Path(root).expanduser().resolve()
        self.inbox = self.root / "innboks"
        self.archive = self.root / "arkiv"
        self.tmp = self.archive / ".tmp"
        self.reports = self.root / "rapporter"
        self.logs = self.root / "logg"
        self.db_path = self.root / "flytting.db"
        self.lock_path = self.root / ".lås"
        self._local = threading.local()     # one database connection per thread
        self._ledgers: list[Ledger] = []
        self._ledgers_guard = threading.Lock()
        self._lock: FileLock | None = None
        self.stop_event = threading.Event()  # set by the app's Stop button

    def ensure(self) -> None:
        for d in (self.root, self.inbox, self.archive, self.tmp, self.reports, self.logs):
            d.mkdir(parents=True, exist_ok=True)

    def open(self) -> "Workspace":
        self.ensure()
        self._lock = FileLock(self.lock_path).__enter__()
        _ = self.ledger  # create/migrate the database now
        self.cleanup_tmp()
        return self

    def close(self) -> None:
        with self._ledgers_guard:
            for led in self._ledgers:
                led.close()
            self._ledgers.clear()
        self._local = threading.local()
        if self._lock:
            self._lock.__exit__(None, None, None)
            self._lock = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    @property
    def ledger(self) -> Ledger:
        assert self._lock is not None, "workspace not opened"
        led = getattr(self._local, "ledger", None)
        if led is None:
            led = Ledger(self.db_path)
            self._local.ledger = led
            with self._ledgers_guard:
                self._ledgers.append(led)
        return led

    def check_stop(self) -> None:
        """Called between safe points; raises Stopped if the user pressed Stop."""
        if self.stop_event.is_set():
            raise Stopped()

    def cleanup_tmp(self) -> None:
        """Half-written files from an interrupted run. They are never
        registered anywhere, so removing them loses nothing."""
        for p in self.tmp.iterdir():
            try:
                p.unlink()
            except OSError:
                pass

    def archive_abs(self, rel: str) -> Path:
        return self.archive / rel
