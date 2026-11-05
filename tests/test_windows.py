"""The Windows route: iCloud for Windows uploads from a folder, we verify via the iCloud library."""

import datetime as dt
import io
import os
from pathlib import Path

import pytest

from conftest import photo
from minsky_icloud import autopilot as A
from minsky_icloud import winsend as W
from minsky_icloud.icloud import Asset
from minsky_icloud.util import sha256_file
from minsky_icloud.workspace import Workspace

pillow_heif = pytest.importorskip("pillow_heif")
from PIL import Image  # noqa: E402


class FakeICloud:
    """The user's iCloud library, plus 'iCloud for Windows' uploading from a folder."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.assets: list[Asset] = []
        self.ready = True
        self.scans = 0

    def state(self):
        return {"logged_in": self.ready, "needs_code": False, "apple_id": "test@example.com"}

    def scan(self):
        self.scans += 1
        yield from list(self.assets)

    def add(self, name, size, created=None):
        self.assets.append(Asset(f"A{len(self.assets)}", name, size, created))

    def upload_folder(self):
        """What iCloud for Windows does in the background (iCloud reads the capture time from EXIF)."""
        self.uploaded = getattr(self, "uploaded", set())
        for p in self.folder.iterdir():
            if p.is_file() and p.name not in self.uploaded:
                self.uploaded.add(p.name)
                d = W.media_date(p)
                self.add(p.name, p.stat().st_size, d.replace(tzinfo=dt.timezone.utc) if d else None)


def jpeg(seed: str, when="2019:07:14 12:34:56") -> bytes:
    im = Image.new("RGB", (40, 30), (int(seed.encode()[0]) % 255, 90, 160))
    exif = Image.Exif()
    exif.get_ifd(0x8769)[0x9003] = when  # DateTimeOriginal
    b = io.BytesIO()
    im.save(b, "JPEG", exif=exif.tobytes(), quality=90)
    return b.getvalue()


def heic(seed: str, orientation=6, size=(64, 48)) -> bytes:
    im = Image.new("RGB", size, (200, int(seed.encode()[0]) % 255, 80))
    for x in range(size[0] // 2):
        im.putpixel((x, 0), (0, 0, 0))  # a dark line along the top edge marks orientation
    exif = Image.Exif()
    exif[0x0112] = orientation
    exif[0x0132] = "2019:07:14 12:34:56"
    pillow_heif.register_heif_opener()
    b = io.BytesIO()
    im.save(b, "HEIF", exif=exif.tobytes(), quality=90)
    return b.getvalue()


@pytest.fixture
def win(env, monkeypatch, tmp_path):
    monkeypatch.setenv("MINSKY_PLATTFORM", "windows")
    folder = tmp_path / "iCloud Photos" / "Uploads"
    folder.mkdir(parents=True)
    ws = Workspace().open()
    ic = FakeICloud(folder)
    settings = {"icloud_mappe": str(folder), "heic": "jpeg", "maks_ventende": 1500,
                "hent_fra_nedlastinger": False}
    yield env, ws, ic, settings, folder
    ws.close()


def run_once(ws, ic, settings, index=None):
    index = index or W.Index()
    A.run(ws, settings=settings, icloud=ic, index=index, once=True)
    return index


def states(ws):
    return {r["archive_path"].rsplit("/", 1)[-1]: dict(r) for r in ws.ledger.db.execute("SELECT * FROM items")}


def test_heic_conversion_keeps_date_orientation_and_quality(tmp_path):
    src = tmp_path / "IMG_1.HEIC"
    src.write_bytes(heic("x", orientation=6, size=(64, 48)))
    dst = tmp_path / "out.jpg"
    sha, size = W.convert_heic(src, dst)
    assert sha == sha256_file(dst) and size == dst.stat().st_size
    with Image.open(dst) as im:
        assert im.format == "JPEG"
        assert im.size == (48, 64)  # rotation applied to the pixels
        ex = im.getexif()
        assert ex.get(0x0112, 1) == 1  # and orientation reset, so it is not rotated twice
        assert ex.get(0x0132) == "2019:07:14 12:34:56"
    assert src.read_bytes()[:4] != b"\xff\xd8\xff\xe0"  # the original is untouched


def test_full_windows_flow(win):
    env, ws, ic, settings, folder = win
    env.make_zip("Min Sky.zip", {
        "Ferie/IMG_0001.HEIC": heic("a"),
        "Ferie/IMG_0002.JPG": photo("b"),
        "Ferie/VID_0003.MP4": photo("c", 30000),
        "Ferie/IMG_0004.JPG": jpeg("d"),      # already in iCloud from the iPhone
        "Annet/IMG_0002.JPG": photo("e"),     # same name, different photo
    })
    # iCloud stores capture time in UTC; the camera wrote local summer time (UTC+2).
    ic.add("IMG_0004.JPG", len(jpeg("d")), dt.datetime(2019, 7, 14, 10, 34, 56, tzinfo=dt.timezone.utc))
    index = run_once(ws, ic, settings)
    st = states(ws)
    assert st["IMG_0004.JPG"]["state"] == "i_icloud" and st["IMG_0004.JPG"]["role"] == "fantes_allerede"
    sent = sorted(p.name for p in folder.iterdir())
    assert sent == ["IMG_0001.jpg", "IMG_0002 (2).JPG", "IMG_0002.JPG", "VID_0003.MP4"]
    heic_row = st["IMG_0001.HEIC"]
    assert heic_row["state"] == "sendt" and heic_row["converted"] == 1
    assert sha256_file(folder / "IMG_0001.jpg") == heic_row["upload_sha"]
    # original HEIC still intact in the archive
    assert sha256_file(ws.archive_abs(heic_row["archive_path"])) == heic_row["sha256"]

    ic.upload_folder()            # iCloud for Windows uploads
    index.when = 0                # time passes: the library is read again
    run_once(ws, ic, settings, index)
    assert {r["state"] for r in states(ws).values()} == {"i_icloud"}
    assert len(ic.assets) == 5    # nothing uploaded twice


def test_crash_after_copy_does_not_duplicate(win, monkeypatch):
    env, ws, ic, settings, folder = win
    env.make_zip("a.zip", {f"IMG_{i}.JPG": photo(str(i)) for i in range(6)})
    real = W._mark_sent
    calls = {"n": 0}

    def flaky(ws_, sha):
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt  # power cut right after the copy, before the ledger knew
        real(ws_, sha)

    monkeypatch.setattr(W, "_mark_sent", flaky)
    with pytest.raises(KeyboardInterrupt):
        run_once(ws, ic, settings)
    monkeypatch.setattr(W, "_mark_sent", real)
    ws.cleanup_tmp()
    run_once(ws, ic, settings)
    assert len(list(folder.iterdir())) == 6, "the half-finished file must be recognised, not copied again"
    assert {r["state"] for r in states(ws).values()} == {"sendt"}


def test_heic_can_be_left_in_archive(win):
    env, ws, ic, settings, folder = win
    settings["heic"] = "hopp"
    env.make_zip("a.zip", {"IMG_1.HEIC": heic("a"), "IMG_2.JPG": photo("b")})
    run_once(ws, ic, settings)
    assert [p.name for p in folder.iterdir()] == ["IMG_2.JPG"]
    assert states(ws)["IMG_1.HEIC"]["state"] == "arkivert"


def test_backpressure(win):
    env, ws, ic, settings, folder = win
    settings["maks_ventende"] = 3
    env.make_zip("a.zip", {f"IMG_{i}.JPG": photo(str(i)) for i in range(10)})
    run_once(ws, ic, settings)
    assert len(list(folder.iterdir())) == 3
    run_once(ws, ic, settings)
    assert len(list(folder.iterdir())) == 3  # still waiting for iCloud
    ic.upload_folder()
    idx = W.Index()
    run_once(ws, ic, settings, idx)
    assert len(list(folder.iterdir())) == 6


def test_never_arriving_is_flagged(win):
    env, ws, ic, settings, folder = win
    env.make_zip("a.zip", {"IMG_1.JPG": photo("a")})
    run_once(ws, ic, settings)
    old = (dt.datetime.now().astimezone() - dt.timedelta(hours=30)).isoformat(timespec="seconds")
    with ws.ledger.tx():
        ws.ledger.db.execute("UPDATE items SET sent_at=?", (old,))
    run_once(ws, ic, settings, W.Index())
    assert states(ws)["IMG_1.JPG"]["state"] == "avvik"


def test_live_photo_videos_detected(win):
    env, ws, ic, settings, folder = win
    env.make_zip("a.zip", {"IMG_1.HEIC": heic("a"), "IMG_1.MOV": photo("v", 9000), "Film.MOV": photo("f", 9000)})
    from minsky_icloud.ingest import ingest
    ingest(ws)
    lv = W.live_photo_videos(ws)
    assert len(lv) == 1 and ws.ledger.item(lv[0])["archive_path"].endswith("IMG_1.MOV")


def test_upload_folder_detection(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert W.find_upload_folder({}) is None
    f = tmp_path / "Pictures" / "iCloud Photos" / "Uploads"
    f.mkdir(parents=True)
    assert W.find_upload_folder({}) == f


def test_app_in_windows_mode(win, monkeypatch):
    import http.client, json, threading, time
    from http.server import ThreadingHTTPServer
    from minsky_icloud.server import App, Handler

    env, ws, ic, settings, folder = win
    monkeypatch.setattr(A, "IDLE_SLEEP", 0.2)
    env.make_zip("a.zip", {"IMG_1.HEIC": heic("a"), "IMG_1.MOV": photo("v", 9000), "IMG_2.JPG": photo("b")})
    app = App(ws)
    app.icloud = ic
    app.settings.update(settings)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), type("H", (Handler,), {"app": app}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def req(method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        c.request(method, path, json.dumps(body) if body is not None else None,
                  {"X-Token": app.token, "Content-Type": "application/json"})
        r = c.getresponse()
        return r.status, json.loads(r.read())

    try:
        st, s = req("GET", "/api/status")
        assert s["platform"] == "windows" and s["win"]["folder"] == str(folder)
        assert s["win"]["icloud"]["logged_in"]
        req("POST", "/api/job", {"name": "hent"})
        time.sleep(0.5)
        assert req("GET", "/api/status")[1]["win"]["live_videos"] == 1
        req("POST", "/api/select", {"livevideo": True, "selected": False})
        req("POST", "/api/job", {"name": "autopilot"})
        end = time.time() + 20
        while time.time() < end and len(list(folder.iterdir())) < 2:
            time.sleep(0.1)
        ic.upload_folder()
        app.index.when = 0
        end = time.time() + 20
        while time.time() < end and req("GET", "/api/status")[1]["done"] < 2:
            time.sleep(0.2)
        s = req("GET", "/api/status")[1]
        assert s["done"] == 2 and s["deselected"] == 1
        assert sorted(p.name for p in folder.iterdir()) == ["IMG_1.jpg", "IMG_2.JPG"]
        assert req("POST", "/api/settings", {"icloud_mappe": "/finnes/ikke"})[0] == 400
    finally:
        app.stop_job()
        end = time.time() + 10
        while app.job and time.time() < end:
            time.sleep(0.05)
        httpd.shutdown()
        httpd.server_close()


def test_same_name_and_size_but_other_photo_is_still_sent(win):
    env, ws, ic, settings, folder = win
    # iPhone file names repeat. A different photo with the same name and size must not be skipped.
    env.make_zip("a.zip", {"IMG_0007.JPG": jpeg("x", "2023:05:17 09:00:00"), "IMG_0008.JPG": photo("y")})
    ic.add("IMG_0007.JPG", len(jpeg("x", "2023:05:17 09:00:00")),
           dt.datetime(2016, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc))      # taken years earlier
    ic.add("IMG_0008.JPG", len(photo("y")), None)                         # no date to compare
    run_once(ws, ic, settings)
    # sent under names the old photos in iCloud do not have
    assert sorted(p.name for p in folder.iterdir()) == ["IMG_0007 (2).JPG", "IMG_0008 (2).JPG"]
    assert {r["state"] for r in states(ws).values()} == {"sendt"}
    ic.upload_folder()
    run_once(ws, ic, settings, W.Index())
    st = states(ws)
    assert {r["state"] for r in st.values()} == {"i_icloud"}
    assert {r["uuid"] for r in st.values()}.isdisjoint({"A0", "A1"}), "must match the new uploads, not the old photos"


def test_same_moment():
    local = dt.datetime(2019, 7, 14, 12, 34, 56)
    assert W.same_moment(local, dt.datetime(2019, 7, 14, 10, 34, 56, tzinfo=dt.timezone.utc))
    assert W.same_moment(local, dt.datetime(2019, 7, 14, 10, 35, 30, tzinfo=dt.timezone.utc))
    assert W.same_moment(local, dt.datetime(2019, 7, 14, 10, 52, 0, tzinfo=dt.timezone.utc)) is False
    assert W.same_moment(local, dt.datetime(2019, 7, 20, 10, 34, 56, tzinfo=dt.timezone.utc)) is False
    assert W.same_moment(None, dt.datetime(2019, 7, 14, tzinfo=dt.timezone.utc)) is None
