"""Status, reports, the 'is it safe to cancel Min Sky?' checklist, and the archive check."""

from __future__ import annotations

import csv
import html
import logging
import os
from pathlib import Path

from . import ledger as L
from .util import human, now_iso, sha256_file
from .workspace import Workspace

log = logging.getLogger("minsky")

LABELS = {
    L.ARKIVERT: "Arkivert lokalt, venter på import",
    L.IMPORTERER: "Import påbegynt (fullføres neste kjøring)",
    L.IMPORTERT: "I Bilder, ikke kontrollert ennå",
    L.BEKREFTET: "I Bilder og kontrollert, venter på iCloud-opplasting",
    L.I_ICLOUD: "FERDIG: kontrollert og lastet opp til iCloud",
    L.IKKE_MEDIA: "Ikke bilde/video (bare i arkivet)",
    L.FEIL: "FEIL: Bilder-appen avviste filen",
    L.AVVIK: "AVVIK: kopien i Bilder stemmer ikke",
    L.I_PAPIRKURV: "Ligger i «Nylig slettet» i Bilder",
}


def gather(ws: Workspace) -> dict:
    db = ws.ledger.db
    c = ws.ledger.counts()
    media_total = db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM items WHERE is_media=1").fetchone()
    occ_media = db.execute(
        "SELECT COUNT(*) FROM occurrences o JOIN items i ON i.sha256=o.sha256 WHERE i.is_media=1"
    ).fetchone()[0]
    sources = db.execute("SELECT * FROM sources ORDER BY id").fetchall()
    expected = ws.ledger.get_meta("forventet_antall")
    deselected = db.execute(
        "SELECT COUNT(*) FROM items WHERE is_media=1 AND selected=0 AND state=?", (L.ARKIVERT,)
    ).fetchone()[0]
    return {
        "counts": c,
        "media_unique": media_total[0],
        "media_bytes": media_total[1],
        "media_seen": occ_media,
        "sources": sources,
        "expected": int(expected) if expected else None,
        "deselected": deselected,
        "already_in_photos": db.execute(
            "SELECT COUNT(*) FROM items WHERE role='fantes_allerede'"
        ).fetchone()[0],
        "archive_checked": ws.ledger.get_meta("arkiv_kontrollert"),
        "archive_check_ok": ws.ledger.get_meta("arkiv_kontroll_ok"),
    }


def checklist(g: dict) -> list[tuple[bool, str]]:
    c = g["counts"]
    broken = [s for s in g["sources"] if s["status"] != "ferdig"]
    pending = c[L.ARKIVERT] - g["deselected"] + c[L.IMPORTERER] + c[L.IMPORTERT]  # BEKREFTET = checked, uploading
    problems = c[L.FEIL] + c[L.AVVIK] + c[L.I_PAPIRKURV]
    items = [
        (g["media_unique"] > 0, f"Bilder/videoer funnet i nedlastingene: {g['media_unique']}"),
        (not broken, "Alle nedlastede zip-filer er lest inn uten feil"
         + (f" ({len(broken)} gjenstår/ødelagt)" if broken else "")),
    ]
    if g["expected"] is None:
        items.append((False, "Antall bilder i Min Sky er registrert (kjør: minsky-icloud forventet <antall>)"))
    else:
        ok = g["media_seen"] >= g["expected"] or g["media_unique"] >= g["expected"]
        items.append((ok, f"Antall stemmer med Min Sky: forventet {g['expected']}, "
                          f"funnet {g['media_unique']} unike ({g['media_seen']} inkl. duplikater)"))
    items += [
        (pending == 0, f"Alle valgte er importert og kontrollert ({pending} gjenstår)"),
        (g["deselected"] == 0, "Ingen bilder er valgt bort" if not g["deselected"] else
         f"{g['deselected']} {'bilde' if g['deselected'] == 1 else 'bilder'} er valgt bort og ligger bare i arkivet"),
        (problems == 0, f"Ingen feil, avvik eller slettede ({problems} trenger tilsyn)"),
        (c[L.I_ICLOUD] == g["media_unique"] and g["media_unique"] > 0,
         f"Alle er bekreftet lastet opp til iCloud ({c[L.I_ICLOUD]} av {g['media_unique']})"),
        (g["archive_check_ok"] == "1",
         f"Arkivet er kontrollert (sist {g['archive_checked']})" if g["archive_check_ok"] == "1"
         else "Arkivet er kontrollert etter siste innlesing (kjør: minsky-icloud kontroller-arkiv)"),
    ]
    return items


def print_status(ws: Workspace) -> bool:
    g = gather(ws)
    c = g["counts"]
    p = print
    p("")
    p(f"Arbeidsmappe: {ws.root}")
    p("─" * 64)
    zips_ok = sum(1 for s in g["sources"] if s["status"] == "ferdig")
    zips_bad = [s for s in g["sources"] if s["status"] == "ødelagt"]
    zips_run = [s for s in g["sources"] if s["status"] == "pågår"]
    p(f"Nedlastinger lest inn: {zips_ok} ferdig, {len(zips_run)} påbegynt, {len(zips_bad)} ødelagt")
    for s in zips_bad:
        p(f"   ✗ {s['path']}: {s['error'] or 'ødelagt'}  → last ned på nytt")
    p(f"Unike bilder/videoer: {g['media_unique']} ({human(g['media_bytes'])})"
      f"  – sett totalt {g['media_seen']} ganger i nedlastingene")
    p("")
    for state in (L.I_ICLOUD, L.BEKREFTET, L.IMPORTERT, L.IMPORTERER, L.ARKIVERT,
                  L.FEIL, L.AVVIK, L.I_PAPIRKURV, L.IKKE_MEDIA):
        if c[state]:
            p(f"  {c[state]:>7}  {LABELS[state]}")
    if g["already_in_photos"]:
        p(f"  (hvorav {g['already_in_photos']} fantes allerede i Bilder/iCloud og ble ikke lagt inn to ganger)")
    p("")
    p("Er det trygt å si opp / slette Min Sky?")
    all_ok = True
    for ok, text in checklist(g):
        all_ok &= ok
        p(f"  [{'✓' if ok else ' '}] {text}")
    p("")
    if all_ok:
        p("JA – alt er kontrollert. Behold likevel arkiv-mappen (helst også en kopi på ekstern disk).")
    else:
        p("NEI – ikke slett noe i Min Sky ennå. " + next_step(g))
    p("")
    return all_ok


def next_step(g: dict) -> str:
    c = g["counts"]
    if g["media_unique"] == 0:
        return "Neste steg: last ned fra Min Sky til innboks-mappen og kjør «minsky-icloud kjør»."
    if any(s["status"] == "ødelagt" for s in g["sources"]):
        return "Neste steg: last ned de ødelagte zip-filene på nytt og kjør «minsky-icloud kjør»."
    if (c[L.ARKIVERT] - g["deselected"]) or c[L.IMPORTERER] or c[L.IMPORTERT]:
        return "Neste steg: kjør «minsky-icloud kjør»."
    if c[L.I_PAPIRKURV]:
        return ("Neste steg: åpne Bilder > Nylig slettet, gjenopprett bildene, og kjør "
                "«minsky-icloud verifiser».")
    if c[L.FEIL] or c[L.AVVIK]:
        return "Neste steg: se rapporten (minsky-icloud rapport) for filene med feil/avvik."
    if c[L.BEKREFTET]:
        return ("Neste steg: la Mac-en stå på med Bilder åpen til opplastingen er ferdig, "
                "og kjør «minsky-icloud verifiser» igjen senere.")
    if g["expected"] is None:
        return "Neste steg: registrer antallet bilder Min Sky viser: «minsky-icloud forventet <antall>»."
    return "Neste steg: kjør «minsky-icloud kontroller-arkiv»."


def write_reports(ws: Workspace) -> tuple[Path, Path]:
    db = ws.ledger.db
    stamp = now_iso()[:19].replace(":", "")
    csv_path = ws.reports / f"rapport-{stamp}.csv"
    rows = db.execute(
        """SELECT i.*, GROUP_CONCAT(s.path || ' › ' || o.member, ' | ') AS seen_in
           FROM items i
           LEFT JOIN occurrences o ON o.sha256 = i.sha256
           LEFT JOIN sources s ON s.id = o.source_id
           GROUP BY i.sha256 ORDER BY i.archive_path"""
    ).fetchall()
    # Semicolon + BOM opens cleanly in Norwegian Excel/Numbers.
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["status", "forklaring", "fil i arkivet", "størrelse", "dato (fra zip)",
                    "id i Bilder", "rolle", "kontrollert med", "lastet opp til iCloud",
                    "siste feil", "funnet i", "sha256"])
        for r in rows:
            w.writerow([r["state"], LABELS.get(r["state"], ""), r["archive_path"], r["size"],
                        r["taken_hint"] or "", r["uuid"] or "", r["role"] or "",
                        r["verified_how"] or "", r["icloud_at"] or "", r["last_error"] or "",
                        r["seen_in"] or "", r["sha256"]])

    g = gather(ws)
    html_path = ws.reports / f"rapport-{stamp}.html"
    problems = [r for r in rows if r["state"] in (L.FEIL, L.AVVIK, L.I_PAPIRKURV)]
    other = [r for r in rows if r["state"] == L.IKKE_MEDIA]
    e = html.escape
    parts = [
        "<!doctype html><meta charset='utf-8'><title>Min Sky → iCloud</title>",
        "<style>body{font:15px/1.5 -apple-system,system-ui,sans-serif;max-width:960px;margin:2em auto;"
        "padding:0 16px;color:#222;background:#fff}td,th{padding:4px 8px;border-bottom:1px solid #ddd;"
        "text-align:left;vertical-align:top}table{border-collapse:collapse;width:100%}.ok{color:#17803d}"
        ".no{color:#b42318}code{font-size:13px}@media(prefers-color-scheme:dark){body{background:#111;"
        "color:#eee}td,th{border-color:#333}}</style>",
        f"<h1>Min Sky → iCloud</h1><p>Laget {e(now_iso())} · {e(str(ws.root))}</p>",
        "<h2>Sjekkliste</h2><ul>",
    ]
    for ok, text in checklist(g):
        parts.append(f"<li class='{'ok' if ok else 'no'}'>{'✓' if ok else '✗'} {e(text)}</li>")
    parts.append("</ul><h2>Status</h2><table>")
    for state, n in g["counts"].items():
        if n:
            parts.append(f"<tr><td>{n}</td><td>{e(LABELS[state])}</td></tr>")
    parts.append("</table><h2>Nedlastinger</h2><table><tr><th>Fil</th><th>Status</th><th>Filer</th><th>Merknad</th></tr>")
    for s in g["sources"]:
        parts.append(f"<tr><td>{e(s['path'])}</td><td>{e(s['status'])}</td><td>{s['files']}</td>"
                     f"<td>{e(s['error'] or '')}</td></tr>")
    parts.append("</table>")
    if problems:
        parts.append(f"<h2 class='no'>Trenger tilsyn ({len(problems)})</h2><p>Filene ligger trygt i arkivet. "
                     "Du kan dra dem inn i Bilder manuelt eller åpne dem for å se hva som er galt.</p><table>")
        for r in problems:
            parts.append(f"<tr><td><code>{e(r['archive_path'])}</code></td><td>{e(LABELS[r['state']])}</td>"
                         f"<td>{e(r['last_error'] or '')}</td></tr>")
        parts.append("</table>")
    if other:
        parts.append(f"<h2>Andre filer ({len(other)})</h2><p>Ikke bilder/video. Ligger i arkivet.</p><ul>")
        parts += [f"<li><code>{e(r['archive_path'])}</code></li>" for r in other[:500]]
        parts.append("</ul>")
    parts.append(f"<p>Full liste over alle filer: <code>{e(csv_path.name)}</code></p>")
    html_path.write_text("\n".join(parts), encoding="utf-8")
    return csv_path, html_path


def check_archive(ws: Workspace) -> bool:
    """Re-hash every archived file. Catches disk errors and accidental deletions."""
    db = ws.ledger.db
    rows = db.execute("SELECT sha256, size, archive_path FROM items ORDER BY archive_path").fetchall()
    bad = []
    total = sum(r["size"] for r in rows)
    done = 0
    log.info("Kontrollerer %d filer i arkivet (%s) …", len(rows), human(total))
    for n, r in enumerate(rows, 1):
        ws.check_stop()
        p = ws.archive_abs(r["archive_path"])
        if not p.exists():
            bad.append((r["archive_path"], "mangler"))
        elif os.path.getsize(p) != r["size"]:
            bad.append((r["archive_path"], "feil størrelse"))
        elif sha256_file(p) != r["sha256"]:
            bad.append((r["archive_path"], "innholdet er endret/skadet"))
        done += r["size"]
        if n % 500 == 0:
            log.info("  … %d/%d (%s av %s)", n, len(rows), human(done), human(total))
    with ws.ledger.tx():
        ws.ledger.set_meta("arkiv_kontrollert", now_iso())
        ws.ledger.set_meta("arkiv_kontroll_ok", "0" if bad else "1")
        ws.ledger.event("arkivkontroll", f"{len(rows)} filer, {len(bad)} feil")
    if bad:
        log.error("%d filer i arkivet er ikke i orden:", len(bad))
        for path, why in bad[:50]:
            log.error("  %s: %s", path, why)
        log.error("Legg zip-filen(e) de kom fra i innboksen igjen og kjør «hent»: "
                  "arkivet repareres automatisk.")
        # Allow the repair path: forget these sources' 'done' markers.
        with ws.ledger.tx():
            for path, _ in bad:
                ws.ledger.db.execute(
                    """UPDATE sources SET status='pågår', mtime=NULL WHERE id IN (
                       SELECT o.source_id FROM occurrences o JOIN items i ON i.sha256=o.sha256
                       WHERE i.archive_path=?)""", (path,))
                ws.ledger.db.execute(
                    """DELETE FROM occurrences WHERE sha256=(SELECT sha256 FROM items WHERE archive_path=?)""",
                    (path,))
    else:
        log.info("Arkivet er i orden: alle %d filer er intakte.", len(rows))
    return not bad
