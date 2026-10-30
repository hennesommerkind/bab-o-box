#!/usr/bin/env python3
"""Lädt die Dateien der Ninox-Tabelle „Dokument“ (Kategorie „Bescheid“) in einen lokalen Ordner.

Läuft nur auf deinem Rechner. Der API-Schlüssel kommt aus der Umgebungsvariable NINOX_API_KEY und wird nie
gespeichert oder ausgegeben. Dateinamen: <Nr>_<n>.<Endung> – keine Namen, keine Kundennummern.

Aufruf (Windows, PowerShell):
    $env:NINOX_API_KEY = "…"            # Schlüssel aus Ninox: Konto → Integrationen → API
    python ninox_bescheide_laden.py                 # Probelauf: zählt nur
    python ninox_bescheide_laden.py --los --max 5   # lädt die ersten 5 (Test)
    python ninox_bescheide_laden.py --los           # lädt alle, bereits geladene werden übersprungen

Sicher wiederholbar: vorhandene Dateien werden nie überschrieben.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SERVER = "https://kjw.ninoxdb.de"
TEAM = "madtv50mpits9na0f"
DATENBANK = "hwo3y1rf8msk"
TABELLE_NAME = "Dokument"
FELD_KATEGORIE = "Dokumentkategorie"
FELD_NR = "Nr"
KATEGORIE_WERT = "Bescheid"
SEITE = 200
PAUSE = 0.3  # Sekunden zwischen Downloads – schont den Server


def anfrage(pfad: str, schluessel: str, roh: bool = False):
    url = f"{SERVER}/v1/teams/{TEAM}/databases/{DATENBANK}{pfad}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {schluessel}"})
    for versuch in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                daten = r.read()
                return daten if roh else json.loads(daten.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(2 ** versuch)
                continue
            raise SystemExit(f"Ninox antwortet mit {e.code} auf {pfad.split('?')[0]} – Schlüssel/Rechte prüfen.")
        except urllib.error.URLError as e:
            time.sleep(2 ** versuch)
            if versuch == 3:
                raise SystemExit(f"Keine Verbindung zu Ninox: {e.reason}")
    raise SystemExit("Ninox nicht erreichbar.")


def tabelle_finden(schluessel: str) -> str:
    for t in anfrage("/tables", schluessel):
        if t.get("name") == TABELLE_NAME:
            return t["id"]
    raise SystemExit(f"Tabelle „{TABELLE_NAME}“ nicht gefunden.")


def alle_datensaetze(tabelle: str, schluessel: str):
    seite = 0
    while True:
        pfad = f"/tables/{tabelle}/records?" + urllib.parse.urlencode({"page": seite, "perPage": SEITE})
        block = anfrage(pfad, schluessel)
        if not block:
            return
        yield from block
        if len(block) < SEITE:
            return
        seite += 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--los", action="store_true", help="wirklich herunterladen (ohne: nur zählen)")
    p.add_argument("--max", type=int, default=0, help="höchstens so viele Datensätze verarbeiten (0 = alle)")
    p.add_argument("--ziel", default=str(Path.home() / "Documents" / "BAB" / "Ninox-Bescheide"),
                   help="Zielordner (nur lokal!)")
    a = p.parse_args()

    schluessel = os.environ.get("NINOX_API_KEY", "").strip()
    if not schluessel:
        print("Bitte zuerst die Umgebungsvariable NINOX_API_KEY setzen (siehe Kopf der Datei).")
        return 2

    tabelle = tabelle_finden(schluessel)
    ziel = Path(a.ziel)
    treffer = dateien = geladen = uebersprungen = 0
    for satz in alle_datensaetze(tabelle, schluessel):
        felder = satz.get("fields") or {}
        if felder.get(FELD_KATEGORIE) != KATEGORIE_WERT:
            continue
        treffer += 1
        if a.max and treffer > a.max:
            break
        rid, nr = satz["id"], felder.get(FELD_NR) or satz["id"]
        for n, datei in enumerate(anfrage(f"/tables/{tabelle}/records/{rid}/files", schluessel) or [], 1):
            dateien += 1
            name = datei["name"] if isinstance(datei, dict) else str(datei)
            ende = Path(name).suffix.lower() or ".pdf"
            out = ziel / f"{nr}_{n}{ende}"
            if out.exists():
                uebersprungen += 1
                continue
            if not a.los:
                continue
            ziel.mkdir(parents=True, exist_ok=True)
            inhalt = anfrage(f"/tables/{tabelle}/records/{rid}/files/{urllib.parse.quote(name)}", schluessel, roh=True)
            out.write_bytes(inhalt)
            geladen += 1
            time.sleep(PAUSE)
        if a.los and treffer % 25 == 0:
            print(f"… {treffer} Datensätze, {geladen} Dateien geladen")

    print(f"Bescheid-Datensätze: {treffer} | Dateien: {dateien} | neu geladen: {geladen} | schon vorhanden: {uebersprungen}")
    if not a.los:
        print("Probelauf – nichts geladen. Mit --los --max 5 testen, dann ohne --max alle.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
