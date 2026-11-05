"""The app: a web interface served only to this computer (127.0.0.1).

Run `minsky-icloud app` (or double-click the desktop shortcut) and it opens
in the browser. All work runs through the same code as the command line,
in a background thread, so the safety guarantees are identical. Stop takes
effect at the next safe point.
"""

from __future__ import annotations

import collections
import datetime as _dt
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, config, logsetup
from . import autopilot as A
from . import ledger as L
from . import photos as P
from . import winsend as W
from .icloud import ICloud, NeedLogin
from .ingest import ingest
from .report import LABELS, check_archive, checklist, gather, next_step, write_reports
from .util import IntegrityError, NotEnoughSpace, PARTIAL_SUFFIXES, Stopped, free_bytes, human
from .workspace import Workspace

log = logging.getLogger("minsky")

# Images a browser can show as a thumbnail (Safari also shows HEIC).
PREVIEW_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".avif"}


class LogBuffer(logging.Handler):
    def __init__(self, size=500):
        super().__init__(logging.INFO)
        self.lines = collections.deque(maxlen=size)
        self.seq = 0
        self.guard = threading.Lock()

    def emit(self, record):
        msg = record.getMessage().strip("\n")
        if not msg:
            return
        with self.guard:
            self.seq += 1
            self.lines.append({"n": self.seq, "t": time.strftime("%H:%M:%S"),
                               "lvl": record.levelname.lower(), "msg": msg})


# The shared status texts mention terminal commands; in the app, name the buttons instead.
_UI_WORDS = [
    (re.compile(r"«minsky-icloud forventet <antall>»|\(kjør: minsky-icloud forventet <antall>\)"),
     "(skriv inn antallet under)"),
    (re.compile(r"kjør «minsky-icloud kjør»"), "trykk «Start»"),
    (re.compile(r"og kjør «minsky-icloud verifiser»( igjen senere)?"), "og trykk «Sjekk iCloud nå»"),
    (re.compile(r"kjør «minsky-icloud verifiser»( igjen senere)?"), "trykk «Sjekk iCloud nå» senere"),
    (re.compile(r"kjør «minsky-icloud kontroller-arkiv»"), "trykk «Kontroller arkivet»"),
    (re.compile(r"\(kjør: minsky-icloud kontroller-arkiv\)"), "(trykk «Kontroller arkivet»)"),
    (re.compile(r"\(minsky-icloud rapport\)"), "(trykk «Lag rapport»)"),
]


def ui_text(s: str) -> str:
    for rx, repl in _UI_WORDS:
        s = rx.sub(repl, s)
    return s


class App:
    JOBS = {
        "autopilot": "Autopilot",
        "prøverunde": "Prøverunde (20 bilder)",
        "hent": "Leser inn nedlastinger",
        "verifiser": "Sjekker iCloud-opplasting",
        "kontroller-arkiv": "Kontrollerer arkivet",
    }

    def __init__(self, ws: Workspace, osxphotos_path: str | None = None):
        self.ws = ws
        self.osxphotos_path = osxphotos_path
        self.settings = config.load()
        self.token = secrets.token_urlsafe(24)
        self.logbuf = LogBuffer()
        log.addHandler(self.logbuf)
        self.job: dict | None = None
        self.last_job: dict | None = None
        self.job_guard = threading.Lock()
        self.phase = ""
        self._mac_info: dict | None = None
        self._dl_cache: dict[tuple, bool] = {}
        self.target = config.target()
        self.icloud = ICloud(config.icloud_session_dir())
        self.index = W.Index()
        if self.target == "windows":
            threading.Thread(target=self.icloud.resume, daemon=True).start()

    # ---------------------------------------------------------------- jobs
    def start_job(self, name: str) -> str | None:
        if name not in self.JOBS:
            return "Ukjent jobb"
        with self.job_guard:
            if self.job:
                return "En jobb kjører allerede"
            self.ws.stop_event.clear()
            self.job = {"name": name, "title": self.JOBS[name], "started": time.time(), "stopping": False}
            self.phase = ""
        threading.Thread(target=self._run, args=(name,), daemon=True).start()
        return None

    def stop_job(self):
        with self.job_guard:
            if self.job:
                self.job["stopping"] = True
                self.ws.stop_event.set()

    def _osx(self):
        P.require_macos()
        return P.find_osxphotos(self.osxphotos_path)

    def _set_phase(self, text: str):
        self.phase = text

    def _run(self, name: str):
        ws = self.ws
        result = "ok"
        log.info("▶ %s", self.JOBS[name])
        try:
            if self.target == "windows" and name in ("autopilot", "prøverunde", "verifiser"):
                self._run_windows(name)
            elif name == "autopilot":
                A.run(ws, osxphotos=self._osx(), settings=self.settings, phase=self._set_phase)
            elif name == "prøverunde":
                osx = self._osx()
                self._set_phase("Leser inn nedlastinger")
                ingest(ws)
                self._set_phase("Legger 20 bilder inn i Bilder-appen")
                P.import_to_photos(ws, osxphotos=osx, limit=20, album=self.settings.get("album") or None)
                self._set_phase("Sjekker bildene i Bilder og iCloud")
                P.verify_in_photos(ws, osxphotos=osx)
                log.info("Se etter albumet «%s» i Bilder og sjekk at bildene ser riktige ut.",
                         self.settings.get("album") or "Alle bilder")
            elif name == "hent":
                if ingest(ws).zips_broken:
                    result = "advarsel"
            elif name == "verifiser":
                self._set_phase("Sjekker bildene i Bilder og iCloud")
                P.verify_in_photos(ws, osxphotos=self._osx())
            elif name == "kontroller-arkiv":
                self._set_phase("Leser hele arkivet og sjekker sjekksummene")
                if not check_archive(ws):
                    result = "advarsel"
            log.info("✓ Ferdig: %s", self.JOBS[name])
        except Stopped:
            result = "stoppet"
            log.warning("Satt på pause. Ingenting er tapt. Trykk «Start» for å fortsette.")
        except (NotEnoughSpace, P.OsxphotosMissing, IntegrityError, NeedLogin, W.UploadFolderMissing) as e:
            result = "feil"
            log.error(str(e))
            A.notify("Min Sky → iCloud", "Flyttingen stoppet. Åpne programmet for detaljer.")
        except SystemExit as e:
            result = "feil"
            log.error(str(e))
        except Exception as e:  # pragma: no cover - full trace goes to the log file
            result = "feil"
            log.error("Uventet feil: %s", e)
            log.debug(traceback.format_exc())
        finally:
            with self.job_guard:
                self.last_job = {**(self.job or {}), "ended": time.time(), "result": result}
                self.job = None
                self.phase = ""

    def _run_windows(self, name: str):
        ws = self.ws
        if name == "autopilot":
            A.run(ws, settings=self.settings, phase=self._set_phase, icloud=self.icloud, index=self.index)
        elif name == "prøverunde":
            self._set_phase("Leser inn nedlastinger")
            ingest(ws)
            W.round_(ws, self.icloud, self.settings, self.index, self._set_phase, limit=20,
                     force_scan=self.index.when == 0)
            log.info("20 bilder ligger nå i iCloud-mappen. Når iCloud for Windows har lastet dem opp (vanligvis "
                     "noen minutter), trykk «Sjekk iCloud nå». Se også etter dem på iCloud.com eller iPhone.")
        elif name == "verifiser":
            W.round_(ws, self.icloud, self.settings, self.index, self._set_phase, limit=0, force_scan=True)

    def win_info(self) -> dict:
        folder = W.find_upload_folder(self.settings)
        lv = W.live_photo_videos(self.ws)
        lv_selected = 0
        if lv:
            q = ",".join("?" * len(lv))
            lv_selected = self.ws.ledger.db.execute(
                f"SELECT COUNT(*) FROM items WHERE selected=1 AND sha256 IN ({q})", lv).fetchone()[0]
        heic_waiting = self.ws.ledger.db.execute(
            "SELECT COUNT(*) FROM items WHERE ext IN ('.heic','.heif') AND state=? AND selected=1",
            (L.ARKIVERT,)).fetchone()[0]
        return {
            "icloud": self.icloud.state(),
            "folder": str(folder) if folder else None,
            "folder_chosen": self.settings.get("icloud_mappe"),
            "candidates": [str(c) for c in W.upload_folder_candidates() if c.is_dir()],
            "heic": self.settings.get("heic", "jpeg"),
            "heic_waiting": heic_waiting,
            "live_videos": len(lv), "live_videos_selected": lv_selected,
            "library_count": self.index.count if self.index.when else None,
            "library_read": time.strftime("%H:%M", time.localtime(self.index.when)) if self.index.when else None,
        }

    # --------------------------------------------------------------- status
    def mac_info(self, refresh: bool = False) -> dict:
        if self._mac_info is None or refresh:
            info = {"is_mac": sys.platform == "darwin" or bool(os.environ.get("MINSKY_TILLAT_IKKE_MAC")),
                    "apple_id": None, "osxphotos": None}
            if info["is_mac"]:
                try:
                    out = subprocess.run(["defaults", "read", "MobileMeAccounts", "Accounts"],
                                         capture_output=True, text=True, timeout=5).stdout
                    m = re.search(r'AccountID\s*=\s*"?([^";\n]+)"?;', out)
                    info["apple_id"] = m.group(1).strip() if m else None
                except Exception:
                    pass
            try:
                osx = P.find_osxphotos(self.osxphotos_path)
                v = subprocess.run([*osx, "--version"], capture_output=True, text=True, timeout=60)
                info["osxphotos"] = (v.stdout or v.stderr).strip() or None
            except Exception:
                info["osxphotos"] = None
            self._mac_info = info
        return self._mac_info

    def metrics(self, pending: int) -> dict:
        """Import speed over the last 15 minutes and a time estimate."""
        db = self.ws.ledger.db
        cutoff = (_dt.datetime.now().astimezone() - _dt.timedelta(minutes=15)).isoformat(timespec="seconds")
        rows = [r[0] for r in db.execute(
            "SELECT imported_at FROM items WHERE imported_at >= ? ORDER BY imported_at", (cutoff,))]
        rate = None
        if len(rows) >= 5:
            try:
                first = _dt.datetime.fromisoformat(rows[0])
                span = max(60.0, (_dt.datetime.now().astimezone() - first).total_seconds())
                rate = len(rows) / span * 60  # per minute
            except ValueError:
                rate = None
        eta = int(pending / rate * 60) if rate and pending else None
        return {"rate": round(rate) if rate else None, "eta": eta}

    def disk(self, g) -> dict:
        ws = self.ws
        db = ws.ledger.db
        not_read = 0
        known = {s["path"]: s["status"] for s in g["sources"]}
        for p in ws.inbox.iterdir() if ws.inbox.exists() else []:
            if p.is_file() and p.suffix.lower() == ".zip" and known.get(p.name) != "ferdig":
                not_read += p.stat().st_size
        to_import = db.execute(
            "SELECT COALESCE(SUM(size),0) FROM items WHERE state IN (?,?) AND selected=1",
            (L.ARKIVERT, L.IMPORTERER)).fetchone()[0]
        free_ws = free_bytes(ws.archive)
        free_home = free_bytes(Path.home())
        same = _same_disk(ws.archive, Path.home())
        need_ws = not_read + (to_import if same else 0)
        warn = []
        if need_ws + 2 * 1024**3 > free_ws:
            warn.append(f"Disken med arbeidsmappen har {human(free_ws)} ledig, men trenger ca. {human(need_ws)}. "
                        "Slå på «Optimaliser Mac-lagring» i Bilder → Innstillinger → iCloud, frigjør plass "
                        "eller flytt arbeidsmappen til en ekstern disk (Innstillinger nederst).")
        if not same and to_import + 5 * 1024**3 > free_home:
            warn.append(f"Mac-ens hoveddisk (Bilder-biblioteket) har {human(free_home)} ledig, men trenger "
                        f"ca. {human(to_import)}. Slå på «Optimaliser Mac-lagring» i Bilder → Innstillinger → iCloud.")
        return {"free_ws": human(free_ws), "free_home": human(free_home), "same_disk": same,
                "need_ws": human(need_ws), "need_home": human(to_import), "warnings": warn,
                "cleanable": self.cleanable(g)}

    def cleanable(self, g) -> dict:
        n = size = 0
        for s in g["sources"]:
            if s["kind"] != "zip" or s["status"] != "ferdig":
                continue
            try:
                st = (self.ws.inbox / s["path"]).stat()
            except OSError:
                continue
            if st.st_size == s["size"]:
                n += 1
                size += st.st_size
        return {"files": n, "size": human(size), "bytes": size}

    def downloads_candidates(self) -> list[dict]:
        """Photo zips in Downloads that autopilot will not pick up by itself
        (downloaded before automatic pickup was switched on)."""
        d = A.downloads_dir()
        out = []
        if not d.is_dir():
            return out
        since = self.settings.get("nedlastinger_fra") or time.time()
        for p in sorted(d.iterdir()):
            if p.suffix.lower() != ".zip" or not p.is_file():
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if self.settings.get("hent_fra_nedlastinger") and st.st_mtime >= since:
                continue  # autopilot takes these
            key = (p.name, st.st_size, st.st_mtime)
            if key not in self._dl_cache:
                self._dl_cache[key] = A.looks_like_photo_zip(p)
            if self._dl_cache[key]:
                out.append({"name": p.name, "size": human(st.st_size)})
        return out

    def status(self, since: int = 0) -> dict:
        ws = self.ws
        g = gather(ws)
        db = ws.ledger.db
        c = g["counts"]
        sel = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(size),0) FROM items WHERE is_media=1 AND selected=1").fetchone()
        pending = A.pending_selected(ws)
        with self.job_guard:
            job = dict(self.job) if self.job else None
            last = dict(self.last_job) if self.last_job else None
        if job:
            job["elapsed"] = int(time.time() - job["started"])
            job["phase"] = self.phase
        with self.logbuf.guard:
            lines = [ln for ln in self.logbuf.lines if ln["n"] > since]
        checks = checklist(g)
        problems = db.execute(
            "SELECT archive_path, state, last_error FROM items WHERE state IN (?,?,?) ORDER BY archive_path LIMIT 50",
            (L.FEIL, L.AVVIK, L.I_PAPIRKURV)).fetchall()
        return {
            "version": __version__,
            "root": str(ws.root),
            "inbox_path": str(ws.inbox),
            "counts": c,
            "media_unique": g["media_unique"],
            "media_bytes": human(g["media_bytes"]),
            "selected": sel[0],
            "selected_bytes": human(sel[1]),
            "deselected": g["deselected"],
            "pending": pending,
            "done": c[L.I_ICLOUD],
            "expected": g["expected"],
            "checklist": [{"ok": ok, "text": ui_text(t)} for ok, t in checks],
            "safe": all(ok for ok, _ in checks),
            "next": ui_text(next_step(g)),
            "inbox": self.inbox_listing(g["sources"]),
            "problems": [{"path": r["archive_path"], "state": LABELS.get(r["state"], r["state"]),
                          "error": r["last_error"]} for r in problems],
            "metrics": self.metrics(pending),
            "disk": self.disk(g),
            "downloads": self.downloads_candidates(),
            "downloads_dir": str(A.downloads_dir()),
            "settings": {k: self.settings.get(k) for k in ("album", "gruppe", "hent_fra_nedlastinger")},
            "job": job,
            "last_job": last,
            "log": lines,
            "mac": self.mac_info() if self.target == "mac" else {"is_mac": False},
            "platform": self.target,
            "win": self.win_info() if self.target == "windows" else None,
        }

    def inbox_listing(self, sources) -> list[dict]:
        known = {s["path"]: s for s in sources}
        out = []
        try:
            entries = sorted(self.ws.inbox.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return out
        for p in entries:
            if p.name.startswith("."):
                continue
            src = known.get(p.name)
            if p.is_dir():
                size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                status = src["status"] if src else "ny"
            else:
                size = p.stat().st_size
                if p.suffix.lower() in PARTIAL_SUFFIXES:
                    status = "lastes ned"
                else:
                    status = src["status"] if src else ("ny" if p.suffix.lower() == ".zip" else "fil")
            out.append({"name": p.name, "size": human(size), "dir": p.is_dir(), "status": status,
                        "files": src["files"] if src else None, "error": src["error"] if src else None})
        # Zips that were cleaned up after being read in still count.
        listed = {o["name"] for o in out}
        for s in sources:
            if s["kind"] == "zip" and s["path"] not in listed and s["status"] == "ferdig":
                out.append({"name": s["path"], "size": human(s["size"]), "dir": False, "status": "ryddet",
                            "files": s["files"], "error": None})
        return out

    # ------------------------------------------------------------ selection
    def tree(self) -> list[dict]:
        db = self.ws.ledger.db
        nodes: dict[str, dict] = {}
        for r in db.execute("SELECT archive_path, size, state, selected FROM items WHERE is_media=1"):
            parts = r["archive_path"].split("/")[:-1]
            for i in range(1, len(parts) + 1):
                key = "/".join(parts[:i])
                n = nodes.get(key)
                if n is None:
                    n = nodes[key] = {"path": key, "name": parts[i - 1], "depth": i - 1,
                                      "total": 0, "selected": 0, "done": 0, "bytes": 0, "direct": 0}
                n["total"] += 1
                n["bytes"] += r["size"]
                n["selected"] += r["selected"]
                n["done"] += r["state"] == L.I_ICLOUD
                if i == len(parts):
                    n["direct"] += 1
        out = sorted(nodes.values(), key=lambda n: n["path"].lower())
        for n in out:
            n["size"] = human(n.pop("bytes"))
            n["label"] = _pretty_source(n["name"]) if n["depth"] == 0 else n["name"]
        return out

    def folder(self, path: str, offset: int = 0, limit: int = 120) -> dict:
        db = self.ws.ledger.db
        rows = db.execute(
            "SELECT sha256, archive_path, size, state, selected, ext, taken_hint FROM items"
            " WHERE is_media=1 AND archive_path LIKE ? ESCAPE '\\' ORDER BY archive_path",
            (_like_prefix(path),),
        ).fetchall()
        rows = [r for r in rows if r["archive_path"].rsplit("/", 1)[0] == path]
        page = rows[offset:offset + limit]
        return {
            "path": path, "total": len(rows), "offset": offset,
            "items": [{
                "id": r["sha256"], "name": r["archive_path"].rsplit("/", 1)[-1],
                "size": human(r["size"]), "state": r["state"], "selected": bool(r["selected"]),
                "preview": r["ext"] in PREVIEW_EXT, "video": r["ext"] not in PREVIEW_EXT,
                "date": (r["taken_hint"] or "")[:10],
            } for r in page],
        }

    def select(self, body: dict) -> int:
        val = 1 if body.get("selected") else 0
        db = self.ws.ledger.db
        with self.ws.ledger.tx():
            if body.get("id"):
                n = db.execute("UPDATE items SET selected=? WHERE sha256=?", (val, body["id"])).rowcount
            elif body.get("livevideo"):
                n = 0
                for sha in W.live_photo_videos(self.ws):
                    n += db.execute("UPDATE items SET selected=? WHERE sha256=? AND state=?",
                                    (val, sha, L.ARKIVERT)).rowcount
            elif body.get("all"):
                n = db.execute("UPDATE items SET selected=?", (val,)).rowcount
            else:
                n = db.execute("UPDATE items SET selected=? WHERE archive_path LIKE ? ESCAPE '\\'",
                               (val, _like_prefix(body.get("path") or ""))).rowcount
            self.ws.ledger.event("utvalg", json.dumps({**body, "endret": n}, ensure_ascii=False))
        return n

    # --------------------------------------------------------------- actions
    def save_settings(self, body: dict) -> None:
        if "album" in body:
            self.settings["album"] = str(body["album"]).strip()[:100]
        if "gruppe" in body:
            self.settings["gruppe"] = max(10, min(2000, int(body["gruppe"])))
        if "heic" in body and body["heic"] in ("jpeg", "hopp"):
            self.settings["heic"] = body["heic"]
        if "icloud_mappe" in body:
            folder = str(body["icloud_mappe"] or "").strip().strip('"')
            if folder and not Path(folder).is_dir():
                raise ValueError("Fant ikke den mappen.")
            self.settings["icloud_mappe"] = folder or None
        if "hent_fra_nedlastinger" in body:
            on = bool(body["hent_fra_nedlastinger"])
            self.settings["hent_fra_nedlastinger"] = on
            if on and not self.settings.get("nedlastinger_fra"):
                self.settings["nedlastinger_fra"] = time.time()
        config.save(self.settings)

    def take_downloads(self, names: list[str]) -> int:
        """Move chosen photo zips from Downloads into the inbox (the user's explicit choice)."""
        d = A.downloads_dir()
        n = 0
        for name in names:
            p = d / Path(name).name
            if not p.is_file() or p.suffix.lower() != ".zip" or not A.looks_like_photo_zip(p):
                continue
            dest = self.ws.inbox / p.name
            k = 2
            while dest.exists():
                dest = self.ws.inbox / f"{p.stem} ({k}){p.suffix}"
                k += 1
            shutil.move(str(p), str(dest))
            log.info("Hentet %s fra Nedlastinger", p.name)
            n += 1
        return n

    def clean_inbox(self) -> dict:
        """Delete zips that are completely read in. Every file from the zip is
        first checked to be in the archive with the right size."""
        ws = self.ws
        db = ws.ledger.db
        removed, freed = [], 0
        for s in db.execute("SELECT * FROM sources WHERE kind='zip' AND status='ferdig'").fetchall():
            p = ws.inbox / s["path"]
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size != s["size"]:
                continue
            ok = True
            for r in db.execute(
                "SELECT i.archive_path, i.size FROM occurrences o JOIN items i ON i.sha256=o.sha256 "
                "WHERE o.source_id=?", (s["id"],)):
                a = ws.archive_abs(r["archive_path"])
                if not a.exists() or a.stat().st_size != r["size"]:
                    ok = False
                    break
            if not ok:
                log.warning("Beholder %s: arkivet mangler filer fra den.", s["path"])
                continue
            p.unlink()
            removed.append(s["path"])
            freed += st.st_size
        with ws.ledger.tx():
            ws.ledger.event("rydd_innboks", json.dumps(removed, ensure_ascii=False))
        log.info("Ryddet innboksen: slettet %d ferdige zip-filer og frigjorde %s.", len(removed), human(freed))
        return {"files": len(removed), "freed": human(freed)}

    def volumes(self) -> list[dict]:
        out = []
        cands = [Path.home()]
        vol = Path("/Volumes")
        if sys.platform.startswith("win"):
            # Other drives: USB disks and memory cards (D:, E:, …)
            cands += [Path(f"{d}:\\") for d in "DEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{d}:\\")]
        elif vol.is_dir():
            cands += [p for p in sorted(vol.iterdir()) if p.is_dir() and not p.name.startswith(".")]
        seen = set()
        for c in cands:
            try:
                dev = os.stat(c).st_dev
            except OSError:
                continue
            if dev in seen:
                continue  # the system disk also shows up under /Volumes
            seen.add(dev)
            target = c / "MinSky-flytting"
            here = "Denne PC-en" if sys.platform.startswith("win") or config.target() == "windows" else "Denne Mac-en"
            out.append({"name": here if c == Path.home() else (c.name or f"Disk {str(c)[:2]}"),
                        "path": str(target), "free": human(free_bytes(c)),
                        "current": self.ws.root == target.resolve()})
        return out

    def switch_workspace(self, path: str) -> str | None:
        if self.job:
            return "Sett flyttingen på pause først."
        new_root = Path(path).expanduser()
        try:
            new_root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return f"Kan ikke bruke {new_root}: {e}"
        new = Workspace(new_root)
        try:
            new.open()
        except SystemExit as e:
            return str(e)
        old = self.ws
        self.ws = new
        old.close()
        logsetup.attach_file(new)
        self.settings["mappe"] = str(new.root)
        config.save(self.settings)
        log.info("Arbeidsmappe: %s", new.root)
        return None

    def open_path(self, what: str) -> None:
        target = {"innboks": self.ws.inbox, "arkiv": self.ws.archive, "mappe": self.ws.root,
                  "rapporter": self.ws.reports, "nedlastinger": A.downloads_dir()}.get(what)
        if target and sys.platform == "darwin":
            subprocess.run(["open", str(target)], check=False)
        elif target and sys.platform.startswith("win"):
            os.startfile(str(target))  # type: ignore[attr-defined]


def _same_disk(a: Path, b: Path) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return True


def _pretty_source(name: str) -> str:
    # "Min Sky 2019__a1b2c3d4" -> "Min Sky 2019"
    return re.sub(r"__(?:[0-9a-f]{8}|mappe\d*)$", "", name)


def _like_prefix(path: str) -> str:
    esc = path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return esc + "/%"


def _desktop() -> Path:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    for d in (home / "OneDrive" / "Desktop", home / "OneDrive" / "Skrivebord", home / "Desktop"):
        if d.is_dir():
            return d
    return home / "Desktop"


def make_shortcut() -> Path:
    """A double-clickable launcher on the Desktop."""
    import shlex

    if sys.platform.startswith("win"):
        exe = shutil.which("minsky-icloud")
        cmd = f'"{exe}" app' if exe else f'"{sys.executable}" -m minsky_icloud app'
        p = _desktop() / "Min Sky til iCloud.bat"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("@echo off\r\ntitle Min Sky til iCloud\r\nrem La dette vinduet sta apent mens programmet brukes.\r\n"
                     f"{cmd}\r\n", encoding="utf-8")
        return p

    exe = shutil.which("minsky-icloud")
    cmd = shlex.quote(exe) if exe else f"{shlex.quote(sys.executable)} -m minsky_icloud"
    desk = Path.home() / "Desktop"
    desk.mkdir(parents=True, exist_ok=True)
    p = desk / "Min Sky til iCloud.command"
    p.write_text("#!/bin/bash\n# Starter Min Sky → iCloud. La vinduet stå åpent mens programmet brukes.\n"
                 f"exec {cmd} app\n", encoding="utf-8")
    p.chmod(0o755)
    return p


class Handler(BaseHTTPRequestHandler):
    app: App
    server_version = "MinSkyICloud"

    def log_message(self, fmt, *args):
        pass

    def _host_ok(self) -> bool:
        # Refuse requests that arrive under a foreign host name (DNS rebinding).
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code=200):
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain")
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        app = self.app
        try:
            if u.path == "/":
                page = resources.files("minsky_icloud").joinpath("ui/index.html").read_text("utf-8")
                page = page.replace("__TOKEN__", app.token)
                return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8", {
                    "Content-Security-Policy": "default-src 'self'; img-src 'self' blob: data:; "
                    "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"})
            if u.path == "/api/status":
                return self._json(app.status(int(q.get("since", 0))))
            if u.path == "/api/tree":
                return self._json(app.tree())
            if u.path == "/api/folder":
                return self._json(app.folder(q.get("path", ""), int(q.get("offset", 0))))
            if u.path == "/api/volumes":
                return self._json(app.volumes())
            if u.path == "/api/file":
                return self._file(q.get("id", ""))
        except Exception as e:
            log.debug(traceback.format_exc())
            return self._json({"error": str(e)}, 500)
        self._send(404, b"not found", "text/plain")

    def _file(self, sha: str):
        row = self.app.ws.ledger.item(sha) if re.fullmatch(r"[0-9a-f]{64}", sha) else None
        if not row or row["ext"] not in PREVIEW_EXT:
            return self._send(404, b"not found", "text/plain")
        p = self.app.ws.archive_abs(row["archive_path"])
        if not p.exists():
            return self._send(404, b"not found", "text/plain")
        ctype = {".heic": "image/heic", ".heif": "image/heif", ".avif": "image/avif"}.get(
            row["ext"]) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(p.stat().st_size))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        with open(p, "rb") as f:
            while chunk := f.read(1024 * 256):
                self.wfile.write(chunk)

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-Token") != self.app.token:
            return self._send(403, b"forbidden", "text/plain")
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except ValueError:
            return self._json({"error": "ugyldig forespørsel"}, 400)
        app = self.app
        path = urlparse(self.path).path
        busy_import = app.job and app.job["name"] in ("autopilot", "prøverunde")
        try:
            if path == "/api/job":
                err = app.start_job(body.get("name", ""))
                return self._json({"ok": err is None, "error": err}, 200 if err is None else 409)
            if path == "/api/stop":
                app.stop_job()
                return self._json({"ok": True})
            if path == "/api/select":
                if busy_import:
                    return self._json({"error": "Sett flyttingen på pause før du endrer utvalget."}, 409)
                return self._json({"ok": True, "changed": app.select(body)})
            if path == "/api/settings":
                try:
                    app.save_settings(body)
                except ValueError as e:
                    return self._json({"error": str(e) or "ugyldig verdi"}, 400)
                return self._json({"ok": True})
            if path == "/api/expected":
                n = int(body.get("antall"))
                if n < 0:
                    raise ValueError
                with app.ws.ledger.tx():
                    app.ws.ledger.set_meta("forventet_antall", str(n))
                    app.ws.ledger.event("forventet", str(n))
                return self._json({"ok": True})
            if path == "/api/take-downloads":
                return self._json({"ok": True, "moved": app.take_downloads(list(body.get("names") or []))})
            if path == "/api/clean-inbox":
                if app.job and app.job["name"] in ("autopilot", "hent"):
                    return self._json({"error": "Sett flyttingen på pause først."}, 409)
                return self._json({"ok": True, **app.clean_inbox()})
            if path == "/api/workspace":
                err = app.switch_workspace(str(body.get("path") or ""))
                return self._json({"ok": err is None, "error": err}, 200 if err is None else 409)
            if path == "/api/shortcut":
                return self._json({"ok": True, "path": str(make_shortcut())})
            if path == "/api/open":
                app.open_path(body.get("what", ""))
                return self._json({"ok": True})
            if path == "/api/icloud/login":
                try:
                    return self._json(app.icloud.login(str(body.get("apple_id", "")), str(body.get("password", ""))))
                except NeedLogin as e:
                    return self._json({"error": str(e)}, 400)
            if path == "/api/icloud/code":
                try:
                    st = app.icloud.submit_code(str(body.get("code", "")))
                    log.info("Logget inn i iCloud som %s.", st["apple_id"])
                    return self._json(st)
                except NeedLogin as e:
                    return self._json({"error": str(e)}, 400)
            if path == "/api/icloud/logout":
                if app.job:
                    return self._json({"error": "Sett flyttingen på pause først."}, 409)
                app.icloud.logout()
                return self._json({"ok": True})
            if path == "/api/recheck-mac":
                return self._json(app.mac_info(refresh=True))
            if path == "/api/report":
                c, h = write_reports(app.ws)
                if sys.platform == "darwin":
                    subprocess.run(["open", str(h)], check=False)
                return self._json({"ok": True, "html": str(h), "csv": str(c)})
        except (ValueError, TypeError):
            return self._json({"error": "ugyldig verdi"}, 400)
        except Exception as e:
            log.debug(traceback.format_exc())
            return self._json({"error": str(e)}, 500)
        self._send(404, b"not found", "text/plain")


def serve(ws: Workspace, *, port: int = 8765, open_browser: bool = True,
          osxphotos_path: str | None = None) -> int:
    app = App(ws, osxphotos_path)
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = None
    for p in [port, *range(port + 1, port + 20), 0]:
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), handler)
            break
        except OSError:
            continue
    assert httpd is not None
    httpd.daemon_threads = True
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"Min Sky → iCloud kjører på {url}")
    print("La dette vinduet stå åpent. Lukk det (eller trykk Ctrl+C) for å avslutte.")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nAvslutter …")
        app.stop_job()
        for _ in range(100):
            if not app.job:
                break
            time.sleep(0.1)
    finally:
        httpd.server_close()
        app.ws.close()
    return 0
