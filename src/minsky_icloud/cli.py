"""Command line: minsky-icloud <kommando>"""

from __future__ import annotations

import argparse
import logging
import platform
import shutil
import subprocess
import sys

from . import __version__
from . import config, logsetup
from . import photos as P
from .ingest import ingest
from .report import check_archive, print_status, write_reports
from .util import IntegrityError, NotEnoughSpace, Stopped, free_bytes, human
from .workspace import Workspace

log = logging.getLogger("minsky")

ALIASES = {"kjor": "kjør", "kontroller": "kontroller-arkiv"}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="minsky-icloud",
        description="Flytt bilder og videoer fra Telenor Min Sky til iCloud-bilder – uten å miste noe.",
    )
    ap.add_argument("--mappe", help="arbeidsmappe (standard: ~/MinSky-flytting)")
    ap.add_argument("--osxphotos", help="sti til osxphotos hvis den ikke finnes automatisk")
    ap.add_argument("-v", "--detaljert", action="store_true", help="vis mer informasjon")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", metavar="kommando")

    appp = sub.add_parser("app", help="åpne programmet i nettleseren (anbefalt)")
    appp.add_argument("--port", type=int, default=8765)
    appp.add_argument("--ikke-åpne", dest="no_browser", action="store_true", help="ikke åpne nettleseren")
    sub.add_parser("snarvei", help="lag en snarvei på skrivebordet som starter programmet")
    sub.add_parser("sjekk", help="sjekk at alt er klart (Mac, osxphotos, diskplass)")
    sub.add_parser("hent", help="les inn det som er lastet ned til innboksen og arkiver det trygt")

    imp = sub.add_parser("importer", help="legg arkiverte bilder inn i Bilder-appen (lastes opp til iCloud)")
    _import_args(imp)

    ver = sub.add_parser("verifiser", help="kontroller bildene i Bilder og om de er lastet opp til iCloud")
    ver.add_argument("--alle", action="store_true", help="kontroller alt på nytt, også det som er ferdig")

    run = sub.add_parser("kjør", help="gjør alt: hent → importer → verifiser → status")
    _import_args(run)

    sub.add_parser("status", help="vis hvor langt flyttingen har kommet")
    sub.add_parser("rapport", help="lag rapport (HTML + CSV) over alle filer")
    fv = sub.add_parser("forventet", help="registrer hvor mange bilder+videoer Min Sky viser")
    fv.add_argument("antall", type=int)
    sub.add_parser("kontroller-arkiv", help="les hele arkivet på nytt og sjekk at ingen filer er skadet")
    return ap


def _import_args(p):
    p.add_argument("--antall", type=int, help="importer bare så mange (fint for en prøverunde)")
    p.add_argument("--gruppe", type=int, default=500, help="filer per runde mot Bilder (standard 500)")
    p.add_argument("--album", default=P.DEFAULT_ALBUM,
                   help=f'album i Bilder som får alle bildene (standard "{P.DEFAULT_ALBUM}", "" for intet)')
    p.add_argument("--prøv-feil-igjen", dest="retry", action="store_true",
                   help="prøv filer som har feilet 3 ganger på nytt")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    argv = [ALIASES.get(a, a) for a in argv]
    ap = build_parser()
    args = ap.parse_args(argv)
    if not args.cmd:
        argv.append("app")  # double-click / no arguments: open the app
        args = ap.parse_args(argv)

    ws = Workspace(args.mappe)
    logsetup.setup(ws, args.detaljert)
    log.debug("Start: %s (versjon %s)", " ".join(argv), __version__)
    try:
        with ws:
            return _dispatch(ws, args)
    except (KeyboardInterrupt, Stopped):
        log.warning("\nAvbrutt. Ingenting er tapt – kjør samme kommando igjen for å fortsette der du slapp.")
        return 130
    except (NotEnoughSpace, P.OsxphotosMissing) as e:
        log.error(str(e))
        return 2
    except IntegrityError as e:
        log.critical(str(e))
        return 3


def _dispatch(ws: Workspace, args) -> int:
    cmd = args.cmd
    if cmd == "app":
        from .server import serve

        return serve(ws, port=args.port, open_browser=not args.no_browser, osxphotos_path=args.osxphotos)
    if cmd == "snarvei":
        from .server import make_shortcut

        log.info("Snarvei laget: %s", make_shortcut())
        return 0
    if cmd == "sjekk":
        return doctor(ws, args)
    if cmd == "hent":
        res = ingest(ws)
        return 1 if res.zips_broken else 0
    if cmd == "status":
        return 0 if print_status(ws) else 1
    if cmd == "rapport":
        c, h = write_reports(ws)
        log.info("Rapport skrevet:\n  %s\n  %s", h, c)
        if sys.platform == "darwin":
            subprocess.run(["open", str(h)], check=False)
        return 0
    if cmd == "forventet":
        with ws.ledger.tx():
            ws.ledger.set_meta("forventet_antall", str(args.antall))
            ws.ledger.event("forventet", str(args.antall))
        log.info("Registrert: Min Sky viser %d bilder og videoer.", args.antall)
        return 0
    if cmd == "kontroller-arkiv":
        return 0 if check_archive(ws) else 1

    if config.target() == "windows":
        return _windows_cli(ws, cmd)
    P.require_macos()
    osx = P.find_osxphotos(args.osxphotos)
    if cmd == "importer":
        _do_import(ws, osx, args)
        return 0
    if cmd == "verifiser":
        P.verify_in_photos(ws, osxphotos=osx, recheck_all=args.alle)
        print_status(ws)
        return 0
    if cmd == "kjør":
        log.info("== 1/3 Leser inn nedlastinger ==")
        res = ingest(ws)
        log.info("== 2/3 Importerer til Bilder ==")
        _do_import(ws, osx, args)
        log.info("== 3/3 Kontrollerer i Bilder ==")
        P.verify_in_photos(ws, osxphotos=osx)
        ok = print_status(ws)
        return 0 if ok and not res.zips_broken else 1
    raise SystemExit(f"Ukjent kommando: {cmd}")


def _do_import(ws, osx, args):
    P.import_to_photos(
        ws, osxphotos=osx, batch_size=args.gruppe, limit=args.antall,
        album=args.album or None, retry_failed=args.retry,
    )


def _windows_cli(ws: Workspace, cmd: str) -> int:
    """On Windows the iCloud login happens in the app; the CLI reuses that session."""
    from . import autopilot as A
    from . import winsend as W
    from .icloud import ICloud

    ic = ICloud(config.icloud_session_dir())
    if not ic.resume():
        log.error("Logg inn i iCloud i programmet først (minsky-icloud app → Innstillinger).")
        return 2
    settings = config.load()
    if cmd == "verifiser":
        W.round_(ws, ic, settings, W.Index(), lambda s: None, limit=0, force_scan=True)
    else:
        A.run(ws, settings=settings, icloud=ic, once=True)
    return 0 if print_status(ws) else 1


def doctor(ws: Workspace, args) -> int:
    ok = True

    def line(good: bool, text: str):
        nonlocal ok
        ok &= good
        print(f"  [{'✓' if good else '✗'}] {text}")

    print(f"\nArbeidsmappe: {ws.root}")
    line(sys.version_info >= (3, 9), f"Python {platform.python_version()}")
    is_mac = sys.platform == "darwin"
    line(is_mac, "macOS (trengs for import til Bilder/iCloud)" if is_mac
         else f"{platform.system()} – bare «hent»/arkivering fungerer her, import må gjøres på Mac")
    try:
        osx = P.find_osxphotos(args.osxphotos)
        v = subprocess.run([*osx, "--version"], capture_output=True, text=True)
        line(v.returncode == 0, f"osxphotos: {(v.stdout or v.stderr).strip()}")
    except P.OsxphotosMissing as e:
        line(False, str(e).splitlines()[0] + " → pipx install osxphotos")
    free = free_bytes(ws.archive)
    inbox_size = sum(p.stat().st_size for p in ws.inbox.rglob("*") if p.is_file())
    line(free > 10 * 1024**3, f"Ledig plass der arkivet ligger: {human(free)}")
    print(f"      (innboksen inneholder {human(inbox_size)}; arkivet trenger omtrent like mye, "
          "og Bilder-biblioteket like mye til – med mindre «Optimaliser Mac-lagring» er på)")
    if is_mac:
        home_free = free_bytes(ws.root.home())
        line(home_free > 10 * 1024**3, f"Ledig plass på Mac-ens hoveddisk (Bilder-biblioteket): {human(home_free)}")
        print("  [ ] Sjekk selv: Bilder > Innstillinger > iCloud > «iCloud-bilder» er PÅ")
        print("  [ ] Sjekk selv: Systeminnstillinger > Personvern og sikkerhet > Full disktilgang for Terminal")
    print("")
    print("Klar!" if ok else "Noe må fikses før du starter (se ✗ over).")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
