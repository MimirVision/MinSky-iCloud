import http.client
import json
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from conftest import photo
from minsky_icloud.server import App, Handler
from minsky_icloud.workspace import Workspace


@pytest.fixture
def app(env):
    ws = Workspace().open()
    a = App(ws)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), type("H", (Handler,), {"app": a}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    a.port = httpd.server_address[1]
    a.env = env
    yield a
    a.stop_job()
    wait(a)
    httpd.shutdown()
    httpd.server_close()
    ws.close()


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


def job(a, name, **opts):
    st, d = req(a, "POST", "/api/job", {"name": name, **opts})
    assert st == 200, d
    wait(a)
    return a.last_job["result"]


def test_page_and_security(app):
    st, page = req(app, "GET", "/")
    assert st == 200 and app.token.encode() in page
    assert req(app, "POST", "/api/job", {"name": "hent"}, token=False)[0] == 403
    assert req(app, "GET", "/api/status", host="evil.example")[0] == 403
    assert req(app, "GET", "/api/file?id=../../etc/passwd")[0] == 404


def test_select_folder_and_move_only_selected(app):
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
    one = folder["items"][0]
    assert req(app, "GET", f"/api/file?id={one['id']}")[0] == 200

    assert job(app, "flytt") == "ok"
    assert len(env.library()) == 2
    s = req(app, "GET", "/api/status")[1]
    assert s["done"] == 2 and s["selected"] == 2 and s["deselected"] == 2
    assert not s["safe"]  # deselected photos keep the answer at NEI
    assert any("valgt bort" in c["text"] and not c["ok"] for c in s["checklist"])
    assert "minsky-icloud" not in s["next"]

    req(app, "POST", "/api/select", {"all": True, "selected": True})
    assert job(app, "flytt") == "ok"
    assert len(env.library()) == 4


def test_stop_is_safe_and_resumable(app):
    env = app.env
    env.make_zip("a.zip", {f"IMG_{i:03}.jpg": photo(str(i)) for i in range(30)})
    assert job(app, "hent") == "ok"
    st, _ = req(app, "POST", "/api/job", {"name": "flytt", "gruppe": 5})
    app.stop_job()  # press Stop straight away
    wait(app)
    assert app.last_job["result"] in ("stoppet", "ok")
    assert job(app, "flytt", gruppe=5) == "ok"
    assert len(env.library()) == 30
    assert set(env.states().values()) == {"i_icloud"}


def test_one_job_at_a_time(app):
    app.env.make_zip("a.zip", {f"x{i}.jpg": photo(str(i)) for i in range(5)})
    with app.job_guard:
        app.job = {"name": "hent", "title": "x", "started": time.time(), "stopping": False}
    assert req(app, "POST", "/api/job", {"name": "hent"})[0] == 409
    with app.job_guard:
        app.job = None
