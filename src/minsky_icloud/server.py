"""The app: a small web interface served only to this computer (127.0.0.1).

Run `minsky-icloud app` and it opens in the browser. All work is done by the
same code as the command line, in a background thread, so the safety
guarantees are identical. The Stop button takes effect at the next safe
point (between files when reading in, between batches when importing).
"""

from __future__ import annotations

import collections
import json
import logging
import mimetypes
import os
import re
import secrets
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

from . import __version__
from . import ledger as L
from . import photos as P
from .ingest import ingest
from .report import LABELS, check_archive, checklist, gather, next_step, write_reports
from .util import IntegrityError, NotEnoughSpace, PARTIAL_SUFFIXES, Stopped, free_bytes, human
from .workspace import Workspace

log = logging.getLogger("minsky")

# Images a browser can show as a thumbnail (Safari also shows HEIC).
PREVIEW_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".avif"}


class LogBuffer(logging.Handler):
    def __init__(self, size=400):
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


class App:
    def __init__(self, ws: Workspace, osxphotos_path: str | None = None):
        self.ws = ws
        self.osxphotos_path = osxphotos_path
        self.token = secrets.token_urlsafe(24)
        self.logbuf = LogBuffer()
        log.addHandler(self.logbuf)
        self.job: dict | None = None
        self.last_job: dict | None = None
        self.job_guard = threading.Lock()
        self._mac_info: dict | None = None

    # ------------------------------------------------------------- jobs
    JOBS = {
        "hent": "Leser inn nedlastinger",
        "prøverunde": "Prøverunde (20 bilder)",
        "flytt": "Flytter til iCloud",
        "verifiser": "Sjekker iCloud-opplasting",
        "kontroller-arkiv": "Kontrollerer arkivet",
    }

    def start_job(self, name: str, opts: dict) -> str | None:
        if name not in self.JOBS:
            return "Ukjent jobb"
        with self.job_guard:
            if self.job:
                return "En jobb kjører allerede"
            self.ws.stop_event.clear()
            self.job = {"name": name, "title": self.JOBS[name], "started": time.time(), "stopping": False}
        threading.Thread(target=self._run, args=(name, opts), daemon=True).start()
        return None

    def stop_job(self):
        with self.job_guard:
            if self.job:
                self.job["stopping"] = True
                self.ws.stop_event.set()

    def _osx(self):
        P.require_macos()
        return P.find_osxphotos(self.osxphotos_path)

    def _run(self, name: str, opts: dict):
        ws = self.ws
        result = "ok"
        log.info("▶ %s", self.JOBS[name])
        try:
            if name == "hent":
                res = ingest(ws)
                if res.zips_broken:
                    result = "advarsel"
            elif name in ("prøverunde", "flytt"):
                osx = self._osx()
                log.info("Steg 1/3: leser inn nye nedlastinger")
                ingest(ws)
                log.info("Steg 2/3: legger bildene inn i Bilder-appen")
                P.import_to_photos(
                    ws, osxphotos=osx, batch_size=int(opts.get("gruppe") or 100),
                    limit=20 if name == "prøverunde" else None,
                    album=opts.get("album", P.DEFAULT_ALBUM) or None,
                    retry_failed=bool(opts.get("retry")),
                )
                log.info("Steg 3/3: kontrollerer bildene i Bilder og iCloud")
                P.verify_in_photos(ws, osxphotos=osx)
            elif name == "verifiser":
                P.verify_in_photos(ws, osxphotos=self._osx())
            elif name == "kontroller-arkiv":
                if not check_archive(ws):
                    result = "advarsel"
            log.info("✓ Ferdig: %s", self.JOBS[name])
        except Stopped:
            result = "stoppet"
            log.warning("Stoppet. Ingenting er tapt – trykk på knappen igjen for å fortsette.")
        except (NotEnoughSpace, P.OsxphotosMissing, IntegrityError) as e:
            result = "feil"
            log.error(str(e))
        except SystemExit as e:
            result = "feil"
            log.error(str(e))
        except Exception as e:  # pragma: no cover - shown to the user, full trace in the log file
            result = "feil"
            log.error("Uventet feil: %s", e)
            log.debug(traceback.format_exc())
        finally:
            with self.job_guard:
                self.last_job = {**(self.job or {}), "ended": time.time(), "result": result}
                self.job = None

    # ------------------------------------------------------------ views
    def mac_info(self) -> dict:
        if self._mac_info is None:
            info = {"is_mac": sys.platform == "darwin", "apple_id": None, "osxphotos": None}
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
                v = subprocess.run([*osx, "--version"], capture_output=True, text=True, timeout=30)
                info["osxphotos"] = (v.stdout or v.stderr).strip() or None
            except Exception:
                info["osxphotos"] = None
            self._mac_info = info
        return self._mac_info

    def status(self, since: int = 0) -> dict:
        ws = self.ws
        g = gather(ws)
        db = ws.ledger.db
        selected_media = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(size),0) FROM items WHERE is_media=1 AND selected=1"
        ).fetchone()
        done = g["counts"][L.I_ICLOUD]
        with self.job_guard:
            job = dict(self.job) if self.job else None
            last = dict(self.last_job) if self.last_job else None
        if job:
            job["elapsed"] = int(time.time() - job["started"])
        with self.logbuf.guard:
            lines = [ln for ln in self.logbuf.lines if ln["n"] > since]
        checks = checklist(g)
        return {
            "version": __version__,
            "root": str(ws.root),
            "inbox_path": str(ws.inbox),
            "counts": g["counts"],
            "labels": LABELS,
            "media_unique": g["media_unique"],
            "media_bytes": human(g["media_bytes"]),
            "selected": selected_media[0],
            "selected_bytes": human(selected_media[1]),
            "deselected": g["deselected"],
            "done": done,
            "expected": g["expected"],
            "checklist": [{"ok": ok, "text": ui_text(t)} for ok, t in checks],
            "safe": all(ok for ok, _ in checks),
            "next": ui_text(next_step(g)),
            "sources": [
                {"path": s["path"], "status": s["status"], "files": s["files"], "error": s["error"]}
                for s in g["sources"]
            ],
            "inbox": self.inbox_listing(g["sources"]),
            "free": human(free_bytes(ws.archive)),
            "job": job,
            "last_job": last,
            "log": lines,
            "mac": self.mac_info(),
        }

    def inbox_listing(self, sources) -> list[dict]:
        known = {s["path"]: s["status"] for s in sources}
        out = []
        try:
            entries = sorted(self.ws.inbox.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return out
        for p in entries:
            if p.name.startswith("."):
                continue
            rel = p.name
            if p.is_dir():
                size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                status = known.get(rel, "ny")
            else:
                size = p.stat().st_size
                if p.suffix.lower() in PARTIAL_SUFFIXES:
                    status = "lastes ned"
                else:
                    status = known.get(rel, "ny" if p.suffix.lower() == ".zip" else "fil")
            out.append({"name": rel, "size": human(size), "dir": p.is_dir(), "status": status})
        return out

    def tree(self) -> list[dict]:
        """Folder tree of the archive with counts, for choosing what to move."""
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
            "path": path, "total": len(rows),
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
            elif body.get("all"):
                n = db.execute("UPDATE items SET selected=?", (val,)).rowcount
            else:
                path = body.get("path") or ""
                n = db.execute(
                    "UPDATE items SET selected=? WHERE archive_path LIKE ? ESCAPE '\\'",
                    (val, _like_prefix(path)),
                ).rowcount
            self.ws.ledger.event("utvalg", json.dumps({**body, "endret": n}, ensure_ascii=False))
        return n

    def open_path(self, what: str) -> None:
        target = {
            "innboks": self.ws.inbox, "arkiv": self.ws.archive, "mappe": self.ws.root,
            "rapporter": self.ws.reports,
        }.get(what)
        if target and sys.platform == "darwin":
            subprocess.run(["open", str(target)], check=False)
        elif target and sys.platform.startswith("win"):
            os.startfile(str(target))  # type: ignore[attr-defined]


# The shared status texts mention terminal commands; in the app, name the buttons instead.
_UI_WORDS = [
    (re.compile(r"«minsky-icloud forventet <antall>»|\(kjør: minsky-icloud forventet <antall>\)"),
     "(skriv inn antallet under)"),
    (re.compile(r"kjør «minsky-icloud kjør»"), "trykk «Flytt valgte bilder» i steg 3"),
    (re.compile(r"kjør «minsky-icloud verifiser»( igjen senere)?"), "trykk «Sjekk iCloud-opplasting» senere"),
    (re.compile(r"kjør «minsky-icloud kontroller-arkiv»"), "trykk «Kontroller arkivet»"),
    (re.compile(r"\(kjør: minsky-icloud kontroller-arkiv\)"), "(trykk «Kontroller arkivet»)"),
    (re.compile(r"\(minsky-icloud rapport\)"), "(trykk «Lag rapport»)"),
]


def ui_text(s: str) -> str:
    for rx, repl in _UI_WORDS:
        s = rx.sub(repl, s)
    return s


def _pretty_source(name: str) -> str:
    # "Min Sky 2019__a1b2c3d4" -> "Min Sky 2019"
    return re.sub(r"__(?:[0-9a-f]{8}|mappe\d*)$", "", name)


def _like_prefix(path: str) -> str:
    esc = path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return esc + "/%"


class Handler(BaseHTTPRequestHandler):
    app: App  # set on the subclass
    server_version = "MinSkyICloud"

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    # --------------------------------------------------------- helpers
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
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    # ------------------------------------------------------------- GET
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
                    "Content-Security-Policy": "default-src 'self'; img-src 'self' blob:; "
                    "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"})
            if u.path == "/api/status":
                return self._json(app.status(int(q.get("since", 0))))
            if u.path == "/api/tree":
                return self._json(app.tree())
            if u.path == "/api/folder":
                return self._json(app.folder(q.get("path", ""), int(q.get("offset", 0))))
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

    # ------------------------------------------------------------ POST
    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-Token") != self.app.token:
            return self._send(403, b"forbidden", "text/plain")
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except ValueError:
            return self._json({"error": "ugyldig forespørsel"}, 400)
        app = self.app
        u = urlparse(self.path)
        try:
            if u.path == "/api/job":
                err = app.start_job(body.get("name", ""), body)
                return self._json({"ok": err is None, "error": err}, 200 if err is None else 409)
            if u.path == "/api/stop":
                app.stop_job()
                return self._json({"ok": True})
            if u.path == "/api/select":
                if app.job and app.job["name"] in ("prøverunde", "flytt"):
                    return self._json({"error": "Stopp flyttingen før du endrer utvalget"}, 409)
                return self._json({"ok": True, "changed": app.select(body)})
            if u.path == "/api/expected":
                n = int(body.get("antall"))
                if n < 0:
                    raise ValueError
                with app.ws.ledger.tx():
                    app.ws.ledger.set_meta("forventet_antall", str(n))
                    app.ws.ledger.event("forventet", str(n))
                return self._json({"ok": True})
            if u.path == "/api/open":
                app.open_path(body.get("what", ""))
                return self._json({"ok": True})
            if u.path == "/api/report":
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
    print("La dette vinduet stå åpent. Trykk Ctrl+C her for å avslutte.")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nAvslutter …")
        app.stop_job()
        for _ in range(100):  # let a running job reach its next safe point
            if not app.job:
                break
            time.sleep(0.1)
    finally:
        httpd.server_close()
    return 0
