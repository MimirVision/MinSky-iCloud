import os
import shutil
import zipfile

import pytest

from conftest import photo
from minsky_icloud import ingest as ingest_mod
from minsky_icloud.util import sha256_file


def all_archived_ok(env):
    with env.db() as con:
        for r in con.execute("SELECT * FROM items"):
            p = env.ws.archive / r["archive_path"]
            assert p.exists(), r["archive_path"]
            assert sha256_file(p) == r["sha256"]


# --------------------------------------------------------------------------- hent


def test_ingest_zip_dedup_and_names(env):
    env.make_zip("Min Sky 1.zip", {
        "Sommer på hytta/IMG_0001.jpg": photo("a"),
        "Sommer på hytta/IMG_0002.HEIC": photo("b"),
        "Barnehage/Video 1.mp4": photo("c", 20000),
        "__MACOSX/._IMG_0001.jpg": b"junk",
        ".DS_Store": b"junk",
        "notat.txt": b"hei",
    })
    # Second download overlaps the first (same photo in an album) and has a
    # Norwegian name stored without the UTF-8 flag.
    env.make_zip("Min Sky 2.zip", {
        "Album/IMG_0001.jpg": photo("a"),
        "Ærlig øyeblikk å huske.jpg": photo("d"),
    }, utf8_flag=False)
    assert env.run("hent") == 0
    states = env.states()
    assert len(states) == 5  # a, b, c, d + notat.txt
    assert sum(1 for s in states.values() if s == "arkivert") == 4
    assert sum(1 for s in states.values() if s == "ikke_media") == 1
    assert any("Ærlig øyeblikk å huske.jpg" in k for k in states)
    with env.db() as con:
        assert con.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0] == 6
        assert {r[0] for r in con.execute("SELECT status FROM sources")} == {"ferdig"}
    all_archived_ok(env)
    # Nothing in the inbox was touched.
    assert sorted(p.name for p in env.ws.inbox.iterdir()) == ["Min Sky 1.zip", "Min Sky 2.zip"]

    # Running again changes nothing.
    assert env.run("hent") == 0
    assert env.states() == states


def test_same_zip_downloaded_twice_is_recognised(env):
    z = env.make_zip("a.zip", {"x.jpg": photo("x")})
    shutil.copy(z, env.ws.inbox / "a (1).zip")
    assert env.run("hent") == 0
    assert len(env.states()) == 1
    with env.db() as con:
        assert con.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1


def test_truncated_zip_is_flagged_then_redownload_works(env):
    z = env.make_zip("stor.zip", {f"IMG_{i}.jpg": photo(str(i)) for i in range(10)})
    data = z.read_bytes()
    z.write_bytes(data[: len(data) // 2])  # browser download cut off
    assert env.run("hent") == 1
    with env.db() as con:
        assert con.execute("SELECT status FROM sources").fetchone()[0] == "ødelagt"
    assert env.run("status") == 1

    z.write_bytes(data)  # downloaded again, complete
    os.utime(z, (1, 1))  # different mtime -> treated as a new download
    assert env.run("hent") == 0
    assert len(env.states()) == 10
    all_archived_ok(env)


def test_corrupt_member_detected_by_crc(env):
    z = env.make_zip("crc.zip", {"good.jpg": photo("g"), "bad.jpg": photo("b", 50000)})
    with zipfile.ZipFile(z) as zf:
        info = zf.getinfo("bad.jpg")
        offset = info.header_offset + 30 + len(info.filename) + 200
    raw = bytearray(z.read_bytes())
    raw[offset] ^= 0xFF
    z.write_bytes(bytes(raw))
    assert env.run("hent") == 1
    states = env.states()
    assert list(states) == ["crc__" + list(states)[0].split("__")[1].split("/")[0] + "/good.jpg"]
    with env.db() as con:
        src = con.execute("SELECT * FROM sources").fetchone()
        assert src["status"] == "ødelagt" and "bad.jpg" in src["error"]


def test_crash_during_ingest_resumes_without_loss(env, monkeypatch):
    env.make_zip("z.zip", {f"d/IMG_{i}.jpg": photo(str(i)) for i in range(20)})
    real = ingest_mod._register
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 8:
            # Simulate dying after the file was moved into place but before
            # the ledger was updated: the worst moment.
            ws, src, member, tmp, sha, size, mtime = a[:7]
            target = ws.archive / src["archive_dir"] / member
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp, target)
            raise KeyboardInterrupt
        return real(*a, **kw)

    monkeypatch.setattr(ingest_mod, "_register", flaky)
    assert env.run("hent") == 130
    assert len(env.states()) == 7
    monkeypatch.setattr(ingest_mod, "_register", real)
    assert env.run("hent") == 0
    states = env.states()
    assert len(states) == 20
    assert not any("(2)" in k for k in states), "leftover file must be reused, not duplicated"
    assert list(env.ws.tmp.iterdir()) == []
    all_archived_ok(env)


def test_partial_download_is_left_alone(env):
    (env.ws.inbox / "Min Sky.zip.crdownload").write_bytes(b"PK\x03\x04 halfway")
    env.make_zip("ok.zip", {"a.jpg": photo("a")})
    assert env.run("hent") == 0
    assert len(env.states()) == 1


def test_loose_folder_files(env):
    d = env.ws.inbox / "Min Sky (pakket ut)" / "2019"
    d.mkdir(parents=True)
    (d / "a.jpg").write_bytes(photo("a"))
    (env.ws.inbox / "b.jpg").write_bytes(photo("b"))
    assert env.run("hent") == 0
    assert len(env.states()) == 2
    (d / "c.jpg").write_bytes(photo("c"))
    assert env.run("hent") == 0
    assert len(env.states()) == 3


# ------------------------------------------------------------------- full flow


def test_full_run_to_icloud(env):
    env.make_zip("alle.zip", {
        "IMG_0001.HEIC": photo("1"),
        "IMG_0001.MOV": photo("1v", 30000),  # Live Photo video half
        "IMG_0002.jpg": photo("2"),
        "film.mp4": photo("3", 40000),
    })
    assert env.run("forventet", "3") == 0
    assert env.run("kjør") == 1  # archive not yet checked -> checklist not complete
    states = env.states()
    assert set(states.values()) == {"i_icloud"}, states
    lib = env.library()
    assert len(lib) == 3  # live photo pair is one asset
    with env.db() as con:
        roles = {r["archive_path"].split("/")[-1]: r["role"] for r in con.execute("SELECT * FROM items")}
    assert roles["IMG_0001.MOV"] == "følgefil"
    assert env.run("kontroller-arkiv") == 0
    assert env.run("status") == 0  # everything green now
    assert env.run("rapport") == 0
    assert list(env.ws.reports.glob("*.html")) and list(env.ws.reports.glob("*.csv"))


def test_waits_for_icloud_upload(env, monkeypatch):
    monkeypatch.setenv("FAKE_INCLOUD", "0")
    env.make_zip("a.zip", {"a.jpg": photo("a")})
    env.run("kjør")
    assert set(env.states().values()) == {"bekreftet"}
    # Photos finishes uploading.
    import json
    lib = json.loads(env.lib.read_text())
    for a in lib["assets"].values():
        a["incloud"] = True
    env.lib.write_text(json.dumps(lib))
    env.run("verifiser")
    assert set(env.states().values()) == {"i_icloud"}


def test_crash_during_import_never_duplicates(env, monkeypatch):
    env.make_zip("a.zip", {f"IMG_{i:03}.jpg": photo(str(i)) for i in range(12)})
    env.run("hent")
    monkeypatch.setenv("FAKE_CRASH_AFTER", "5")
    env.run("importer", "--gruppe", "100")
    states = env.states()
    assert set(states.values()) == {"importerer"}
    assert len(env.library()) == 5
    monkeypatch.delenv("FAKE_CRASH_AFTER")
    env.run("kjør")
    assert set(env.states().values()) == {"i_icloud"}
    assert len(env.library()) == 12, "the 5 already imported must not be imported twice"
    with env.db() as con:
        assert {r[0] for r in con.execute("SELECT role FROM items")} == {"importert"}


def test_photo_already_in_icloud_is_not_duplicated(env):
    env.make_zip("a.zip", {"a.jpg": photo("a"), "b.jpg": photo("b")})
    env.run("hent")
    # 'a' is already in the user's library (e.g. synced from their iPhone).
    src = next(env.ws.archive.rglob("a.jpg"))
    env.lib.write_text('{"assets": {}}')
    import json, subprocess, sys
    from conftest import FAKE
    subprocess.run([sys.executable, str(FAKE), "import", str(src)], check=True, env=os.environ)
    env.run("kjør")
    assert set(env.states().values()) == {"i_icloud"}
    assert len(env.library()) == 2
    with env.db() as con:
        roles = sorted(r[0] for r in con.execute("SELECT role FROM items"))
    assert roles == ["fantes_allerede", "importert"]


def test_lookalike_in_library_gets_our_copy_imported_too(env):
    import json
    env.make_zip("a.zip", {"a.jpg": photo("a")})
    env.run("hent")
    src = next(env.ws.archive.rglob("a.jpg"))
    # Photos thinks it has this picture (same fingerprint) but its stored file differs.
    other = env.tmp / "other.jpg"
    other.write_bytes(photo("annen versjon"))
    env.lib.write_text(json.dumps({"assets": {"OLD": {
        "uuid": "OLD", "fingerprint": sha256_file(src), "path": str(other),
        "original_filesize": other.stat().st_size, "intrash": False, "incloud": True,
        "iscloudasset": True, "path_live_photo": None}}}))
    env.run("kjør")
    assert set(env.states().values()) == {"arkivert"}  # queued for forced import
    env.run("kjør")
    assert set(env.states().values()) == {"i_icloud"}
    assert len(env.library()) == 2  # a duplicate is better than a loss


def test_deleted_from_photos_is_reimported_and_trash_is_flagged(env):
    import json
    env.make_zip("a.zip", {"a.jpg": photo("a"), "b.jpg": photo("b")})
    env.run("kjør")
    lib = json.loads(env.lib.read_text())
    uuids = list(lib["assets"])
    del lib["assets"][uuids[0]]                 # permanently gone
    lib["assets"][uuids[1]]["intrash"] = True   # in 'Recently Deleted'
    env.lib.write_text(json.dumps(lib))
    env.run("verifiser")
    assert sorted(env.states().values()) == ["arkivert", "i_papirkurv"]
    assert env.run("status") == 1
    env.run("kjør")
    assert sorted(env.states().values()) == ["i_icloud", "i_papirkurv"]


def test_rejected_file_ends_as_error_after_retries(env, monkeypatch):
    monkeypatch.setenv("FAKE_REJECT_EXT", ".avi")
    env.make_zip("a.zip", {"gammel.avi": photo("v", 9000), "ok.jpg": photo("o")})
    for _ in range(3):
        env.run("kjør")
    states = env.states()
    assert sorted(states.values()) == ["feil", "i_icloud"]
    assert env.run("status") == 1
    monkeypatch.delenv("FAKE_REJECT_EXT")
    env.run("kjør", "--prøv-feil-igjen")
    assert set(env.states().values()) == {"i_icloud"}


def test_trial_run_with_limit(env):
    env.make_zip("a.zip", {f"IMG_{i:03}.jpg": photo(str(i)) for i in range(10)})
    env.run("hent")
    env.run("importer", "--antall", "3")
    assert len(env.library()) == 3
    assert list(env.states().values()).count("importert") == 3


def test_archive_check_detects_damage_and_hent_repairs(env):
    z = env.make_zip("a.zip", {"a.jpg": photo("a"), "b.jpg": photo("b")})
    env.run("hent")
    f = next(env.ws.archive.rglob("a.jpg"))
    f.write_bytes(b"skadet")
    assert env.run("kontroller-arkiv") == 1
    assert env.run("hent") == 0
    assert env.run("kontroller-arkiv") == 0
    all_archived_ok(env)


def test_lock_prevents_two_runs(env):
    from minsky_icloud.workspace import Workspace
    with Workspace():
        with pytest.raises(SystemExit):
            Workspace().open()


def test_new_files_reset_archive_check(env):
    env.make_zip("a.zip", {"a.jpg": photo("a")})
    env.run("kjør")
    env.run("forventet", "1")
    env.run("kontroller-arkiv")
    assert env.run("status") == 0
    env.make_zip("b.zip", {"b.jpg": photo("b")})
    env.run("kjør")
    assert env.run("status") == 1  # archive must be checked again
    env.run("kontroller-arkiv")
    assert env.run("status") == 0
