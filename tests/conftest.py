import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from minsky_icloud.cli import main  # noqa: E402
from minsky_icloud.workspace import Workspace  # noqa: E402

FAKE = Path(__file__).with_name("fake_osxphotos.py")


@pytest.fixture
def env(tmp_path, monkeypatch):
    lib = tmp_path / "photoslib.json"
    monkeypatch.setenv("MINSKY_MAPPE", str(tmp_path / "flytting"))
    monkeypatch.setenv("MINSKY_OSXPHOTOS", f"{sys.executable} {FAKE}")
    monkeypatch.setenv("MINSKY_TILLAT_IKKE_MAC", "1")
    monkeypatch.setenv("MINSKY_PLATTFORM", "mac")  # these tests cover the Photos-app route; test_windows.py the other
    monkeypatch.setenv("MINSKY_MIN_ALDER", "0")
    monkeypatch.setenv("MINSKY_KONFIG", str(tmp_path / "innstillinger.json"))
    monkeypatch.setenv("HOME", str(tmp_path / "hjem"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "hjem"))
    monkeypatch.setenv("FAKE_LIB", str(lib))
    monkeypatch.setenv("FAKE_INCLOUD", "1")
    ws = Workspace()
    ws.ensure()
    return Env(tmp_path, ws, lib)


class Env:
    def __init__(self, tmp, ws, lib):
        self.tmp, self.ws, self.lib = tmp, ws, lib

    def run(self, *args):
        return main(list(args))

    def make_zip(self, name, files: dict, *, utf8_flag=True):
        p = self.ws.inbox / name
        with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for member, data in files.items():
                z.writestr(member, data)
        if not utf8_flag:
            # Clear the UTF-8 flag but keep the UTF-8 bytes (what many tools do).
            raw = bytearray(p.read_bytes())
            for sig, off in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
                i = raw.find(sig)
                while i >= 0:
                    raw[i + off + 1] &= ~0x08 & 0xFF
                    i = raw.find(sig, i + 4)
            p.write_bytes(bytes(raw))
        return p

    def library(self):
        return json.loads(self.lib.read_text())["assets"] if self.lib.exists() else {}

    def db(self):
        import sqlite3

        con = sqlite3.connect(self.ws.db_path)
        con.row_factory = sqlite3.Row
        return con

    def states(self):
        with self.db() as con:
            return {r["archive_path"]: r["state"] for r in con.execute("SELECT * FROM items")}


def photo(seed: str, size: int = 5000) -> bytes:
    """Deterministic unique fake jpeg bytes."""
    import hashlib

    out = b"\xff\xd8\xff\xe0" + seed.encode()
    while len(out) < size:
        out += hashlib.sha256(out[-64:]).digest()
    return out[:size]
