"""Logging to the console and to <mappe>/logg/minsky-icloud.log."""

from __future__ import annotations

import logging
import sys

from .workspace import Workspace

log = logging.getLogger("minsky")
_FILE_HANDLER: logging.Handler | None = None


def setup(ws: Workspace, verbose: bool = False) -> None:
    log.setLevel(logging.DEBUG)
    for h in list(log.handlers):
        if getattr(h, "_minsky_console", False):
            log.removeHandler(h)
    ch = logging.StreamHandler(sys.stdout)
    ch._minsky_console = True  # type: ignore[attr-defined]
    ch.setFormatter(logging.Formatter("%(message)s"))
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.addHandler(ch)
    attach_file(ws)


def attach_file(ws: Workspace) -> None:
    """(Re)point the log file at this workspace."""
    global _FILE_HANDLER
    ws.ensure()
    if _FILE_HANDLER is not None:
        log.removeHandler(_FILE_HANDLER)
        _FILE_HANDLER.close()
    fh = logging.FileHandler(ws.logs / "minsky-icloud.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    fh.setLevel(logging.DEBUG)
    log.addHandler(fh)
    _FILE_HANDLER = fh
