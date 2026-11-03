"""A stand-in for osxphotos used by the tests (runs on Linux).

State lives in a JSON file named by FAKE_LIB. Behaviour switches:
  FAKE_INCLOUD=1        new assets report incloud=True
  FAKE_CRASH_AFTER=n    die (no report written) after importing n assets
  FAKE_REJECT_EXT=.avi  refuse files with this extension
Assets are "fingerprinted" by the sha256 of their bytes; a test can put
an asset in the library whose fingerprint matches but whose stored bytes
differ, to simulate Photos' fuzzy duplicate detection.
"""

import hashlib
import json
import os
import shutil
import sys
import uuid as uuidlib
from pathlib import Path

LIB = Path(os.environ["FAKE_LIB"])
STORE = LIB.with_suffix(".store")


def load():
    if LIB.exists():
        return json.loads(LIB.read_text())
    return {"assets": {}}


def save(lib):
    LIB.write_text(json.dumps(lib, indent=1))


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def cmd_import(args):
    files, report, skip_dups = [], None, False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--report":
            report = args[i + 1]; i += 2; continue
        if a == "--album":
            i += 2; continue
        if a == "--skip-dups":
            skip_dups = True
        elif not a.startswith("--"):
            files.append(a)
        i += 1
    lib = load()
    STORE.mkdir(exist_ok=True)
    crash_after = int(os.environ.get("FAKE_CRASH_AFTER", "-1"))
    reject = os.environ.get("FAKE_REJECT_EXT")
    # group live photos: image + .mov with same stem in the same folder
    groups, used = [], set()
    for f in files:
        if f in used:
            continue
        p = Path(f)
        if p.suffix.lower() in (".heic", ".jpg"):
            mov = [g for g in files if Path(g).parent == p.parent and Path(g).stem == p.stem
                   and Path(g).suffix.lower() == ".mov"]
            if mov:
                groups.append((f, mov[0])); used.update([f, mov[0]]); continue
        groups.append((f,)); used.add(f)
    records, imported = [], 0
    for g in groups:
        f = g[0]
        rec = {"filepath": f, "filename": Path(f).name, "uuid": "", "imported": False,
               "error": False, "skipped": False, "duplicate_of": "",
               "live_video": g[1] if len(g) > 1 else "", "raw_image": ""}
        fp = sha(f)
        dup = next((u for u, a in lib["assets"].items() if a["fingerprint"] == fp), None)
        if skip_dups and dup:
            rec["skipped"] = True
            rec["duplicate_of"] = dup
            records.append(rec)
            continue
        if reject and f.lower().endswith(reject):
            rec["error"] = True
            records.append(rec)
            continue
        u = str(uuidlib.uuid4()).upper()
        dst = STORE / f"{u}{Path(f).suffix}"
        shutil.copyfile(f, dst)
        lib["assets"][u] = {
            "uuid": u, "fingerprint": fp, "path": str(dst),
            "original_filesize": os.path.getsize(f), "intrash": False,
            "incloud": os.environ.get("FAKE_INCLOUD") == "1", "iscloudasset": True,
            "path_live_photo": None,
        }
        save(lib)
        imported += 1
        rec["uuid"] = u
        rec["imported"] = True
        records.append(rec)
        if imported == crash_after:
            os._exit(9)
    if report:
        Path(report).write_text(json.dumps(records))


def cmd_query(args):
    uf = args[args.index("--uuid-from-file") + 1]
    wanted = {l.strip() for l in open(uf) if l.strip() and not l.startswith("#")}
    lib = load()
    include_deleted = "--deleted" in args
    out = [json.dumps(a) for u, a in lib["assets"].items()
           if u in wanted and (include_deleted or not a["intrash"])]
    print("[" + ", ".join(out) + "]")


if __name__ == "__main__":
    if sys.argv[1] == "--version":
        print("osxphotos, version 0.0-fake")
    elif sys.argv[1] == "import":
        cmd_import(sys.argv[2:])
    elif sys.argv[1] == "query":
        cmd_query(sys.argv[2:])
