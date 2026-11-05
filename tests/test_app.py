import http.client
import json
import os
import threading
import time
import zipfile
from http.server import ThreadingHTTPServer

import pytest

from conftest import photo
from minsky_icloud import autopilot as A
from minsky_icloud.server import App, Handler
from minsky_icloud.workspace import Workspace


@pytest.fixture
def app(env, monkeypatch):
    dl = env.tmp / "Nedlastinger"
    dl.mkdir()
    monkeypatch.setenv("MINSKY_NEDLASTINGER", str(dl))
    monkeypatch.setattr(A, "IDLE_SLEEP", 0.2)
    ws = Workspace().open()
    a = App(ws)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), type("H", (Handler,), {"app": a}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    a.port = httpd.server_address[1]
    a.env = env
    a.dl = dl
    yield a
    a.stop_job()
    wait(a)
    httpd.shutdown()
    httpd.server_close()
    a.ws.close()


def req(a, method, path, body=None, token=True, host=None):
    c = http.client.HTTPConnection("127.0.0.1", a.port, timeout=30)
    headers = {"Content-Type": "application/json", "Host": host or f"127.0.0.1:{a.port}"}
    if token:
        headers["X-Token"] = a.token
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    r = c.getresponse()
    data = r.read()
    try:
        return r.status, json.loads(data)
    except ValueError:
        return r.status, data


def wait(a, timeout=60):
    end = time.time() + timeout
    while a.job and time.time() < end:
        time.sleep(0.05)
    assert not a.job, "job did not finish"


def job(a, name):
    st, d = req(a, "POST", "/api/job", {"name": name})
    assert st == 200, d
    wait(a)
    return a.last_job["result"]


def autopilot_until(a, cond, timeout=60):
    st, d = req(a, "POST", "/api/job", {"name": "autopilot"})
    assert st == 200, d
    end = time.time() + timeout
    while time.time() < end and not cond():
        assert a.job, f"autopilot ended early: {a.last_job}"
        time.sleep(0.1)
    a.stop_job()
    wait(a)
    assert cond()


def all_in_icloud(env, n):
    return lambda: list(env.states().values()).count("i_icloud") == n


def make_dl_zip(a, name, files, age=0):
    p = a.dl / name
    with zipfile.ZipFile(p, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    if age:
        t = time.time() - age
        os.utime(p, (t, t))
    return p


def test_page_and_security(app):
    st, page = req(app, "GET", "/")
    assert st == 200 and app.token.encode() in page
    assert req(app, "POST", "/api/job", {"name": "hent"}, token=False)[0] == 403
    assert req(app, "GET", "/api/status", host="evil.example")[0] == 403
    assert req(app, "GET", "/api/file?id=../../etc/passwd")[0] == 404


def test_select_folder_and_autopilot_moves_only_selected(app):
    env = app.env
    env.make_zip("alle.zip", {
        "Barnehage/a.jpg": photo("a"), "Barnehage/b.jpg": photo("b"),
        "Hytta/c.jpg": photo("c"), "Hytta/d.jpg": photo("d"),
    })
    assert job(app, "hent") == "ok"
    tree = req(app, "GET", "/api/tree")[1]
    hytta = next(n for n in tree if n["name"] == "Hytta")
    assert hytta["total"] == 2 and hytta["selected"] == 2
    req(app, "POST", "/api/select", {"path": hytta["path"], "selected": False})
    folder = req(app, "GET", "/api/folder?path=" + tree[1]["path"].replace(" ", "%20"))[1]
    assert req(app, "GET", f"/api/file?id={folder['items'][0]['id']}")[0] == 200

    autopilot_until(app, all_in_icloud(env, 2))
    assert len(env.library()) == 2
    s = req(app, "GET", "/api/status")[1]
    assert s["done"] == 2 and s["selected"] == 2 and s["deselected"] == 2
    assert not s["safe"]  # deselected photos keep the answer at NO
    assert "minsky-icloud" not in s["next"]

    req(app, "POST", "/api/select", {"all": True, "selected": True})
    autopilot_until(app, all_in_icloud(env, 4))
    assert len(env.library()) == 4


def test_autopilot_picks_up_new_downloads_while_running(app):
    env = app.env
    req(app, "POST", "/api/settings", {"hent_fra_nedlastinger": True})
    time.sleep(0.05)
    make_dl_zip(app, "Min Sky 1.zip", {f"a/IMG_{i}.jpg": photo(f"a{i}") for i in range(5)})
    make_dl_zip(app, "faktura.zip", {"faktura.pdf": b"%PDF", "notat.txt": b"x"})   # not photos
    (app.dl / "Min Sky 3.zip.crdownload").write_bytes(b"PK")                      # still downloading
    st, _ = req(app, "POST", "/api/job", {"name": "autopilot"})
    end = time.time() + 30
    while time.time() < end and list(env.states().values()).count("i_icloud") < 5:
        time.sleep(0.1)
    # A second download arrives while the autopilot is running.
    make_dl_zip(app, "Min Sky 2.zip", {f"b/IMG_{i}.jpg": photo(f"b{i}") for i in range(4)})
    end = time.time() + 30
    while time.time() < end and list(env.states().values()).count("i_icloud") < 9:
        time.sleep(0.1)
    app.stop_job()
    wait(app)
    assert list(env.states().values()).count("i_icloud") == 9
    assert sorted(p.name for p in app.dl.iterdir()) == ["Min Sky 3.zip.crdownload", "faktura.zip"]
    assert app.last_job["result"] == "stoppet"


def test_old_downloads_are_offered_not_taken(app):
    req(app, "POST", "/api/settings", {"hent_fra_nedlastinger": True})
    make_dl_zip(app, "Gammel Min Sky.zip", {"x.jpg": photo("x")}, age=3600)
    s = req(app, "GET", "/api/status")[1]
    assert [d["name"] for d in s["downloads"]] == ["Gammel Min Sky.zip"]
    assert A.pull_downloads(app.ws, app.settings["nedlastinger_fra"]) == []
    req(app, "POST", "/api/take-downloads", {"names": ["Gammel Min Sky.zip", "../../etc/passwd"]})
    assert (app.ws.inbox / "Gammel Min Sky.zip").exists()


def test_stop_is_safe_and_resumable(app):
    env = app.env
    env.make_zip("a.zip", {f"IMG_{i:03}.jpg": photo(str(i)) for i in range(30)})
    req(app, "POST", "/api/settings", {"gruppe": 10})
    req(app, "POST", "/api/job", {"name": "autopilot"})
    app.stop_job()  # press Pause straight away
    wait(app)
    assert app.last_job["result"] in ("stoppet", "ok")
    autopilot_until(app, all_in_icloud(env, 30))
    assert len(env.library()) == 30


def test_clean_inbox_only_removes_fully_archived_zips(app):
    env = app.env
    env.make_zip("a.zip", {"a.jpg": photo("a")})
    env.make_zip("b.zip", {"b.jpg": photo("b")})
    assert job(app, "hent") == "ok"
    next(app.ws.archive.rglob("b.jpg")).unlink()  # archive lost a file from b.zip
    s = req(app, "GET", "/api/status")[1]
    assert s["disk"]["cleanable"]["files"] == 2
    st, r = req(app, "POST", "/api/clean-inbox", {})
    assert r["files"] == 1
    assert not (app.ws.inbox / "a.zip").exists() and (app.ws.inbox / "b.zip").exists()
    names = {x["name"]: x["status"] for x in req(app, "GET", "/api/status")[1]["inbox"]}
    assert names["a.zip"] == "ryddet"


def test_switch_workspace(app, tmp_path):
    new = tmp_path / "Ekstern" / "MinSky-flytting"
    st, r = req(app, "POST", "/api/workspace", {"path": str(new)})
    assert r["ok"], r
    assert app.ws.root == new.resolve() and (new / "flytting.db").exists()
    from minsky_icloud import config
    assert config.load()["mappe"] == str(new.resolve())


def test_shortcut(app):
    st, r = req(app, "POST", "/api/shortcut", {})
    p = r["path"]
    if p.endswith(".bat"):  # Windows
        assert "app" in open(p).read()
    else:
        assert p.endswith("Min Sky til iCloud.command") and os.access(p, os.X_OK)
        assert open(p).read().rstrip().endswith(" app")


def test_one_job_at_a_time(app):
    with app.job_guard:
        app.job = {"name": "hent", "title": "x", "started": time.time(), "stopping": False}
    assert req(app, "POST", "/api/job", {"name": "hent"})[0] == 409
    with app.job_guard:
        app.job = None
