"""
BAB-Eingang – Kernlogik (ohne Oberfläche).

Auslesen, Benennen und Ablegen gescannter BAB-Bescheide in Ninox.
Wird von der Oberfläche (bab_gui.py) genutzt, läuft aber auch allein:

  python bab_kern.py --einmal            Eingangsordner einmal verarbeiten
  python bab_kern.py --testlauf --einmal ohne Ninox
  python bab_kern.py --analyse datei.pdf Text und erkannte Felder anzeigen
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import requests
from pypdf import PdfReader

log = logging.getLogger("bab")

APP_NAME = "BAB-Eingang"
FELDER = ["kundennummer", "kategorie", "bescheiddatum", "zeitraum_von", "zeitraum_bis", "betrag", "geburtsdatum"]
MONATE = {"januar": 1, "februar": 2, "märz": 3, "marz": 3, "maerz": 3, "april": 4, "mai": 5, "juni": 6, "juli": 7,
          "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12}
DATUM_RE = r"\d{1,2}\.\d{1,2}\.\d{4}"
DATUM_LANG_RE = r"\d{1,2}\.?\s*(?:Januar|Februar|M[äa]e?rz|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)\s*\d{4}"
BETRAG_RE = r"\d{1,3}(?:\.\d{3})*,\d{2}"
KDNR_FORMAT = re.compile(r"^\d{3}[A-Z]\d{6}$")
BETRAG_FORMAT = re.compile(r"^\d{1,3}(?:\.\d{3})*,\d{2}$|^\d+,\d{2}$")
OHNE_FENSTER = 0x08000000 if os.name == "nt" else 0  # keine Konsolenfenster für Tesseract


# =========================================================================== Pfade & Konfiguration

def ressourcen_ordner() -> Path:
    """Mitgelieferte Dateien (in der .exe: entpackter Bundle-Ordner)."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def programm_ordner() -> Path:
    """Ordner der .exe bzw. des Skripts – hier liegt der mitgelieferte Tesseract-Ordner."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def daten_ordner() -> Path:
    basis = os.environ.get("APPDATA") or (Path.home() / ".config")
    p = Path(basis) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def standard_einstellungen() -> dict:
    basis = Path.home() / "Documents" / "BAB"
    return {
        "ordner": {
            "eingang": str(basis / "Scan-Eingang"),
            "verarbeitet": str(basis / "Verarbeitet"),
            "pruefen": str(basis / "Pruefen"),
            "fehler": str(basis / "Fehler"),
            "archiv_original": str(basis / "_original"),
        },
        "ninox": {"base_url": "https://api.ninox.com/v1", "api_schluessel": "", "team_id": "", "datenbank_id": ""},
        "ki": {"aktiv": False, "url": "http://localhost:11434", "modell": "qwen2.5:7b"},
        "ocr": {"aktiv": True, "sprache": "deu", "tesseract": ""},
        "ueberwachung": {"intervall_s": 30, "beim_start_starten": False},
        "testlauf": True,
    }


def tief_mischen(basis: dict, oben: dict) -> dict:
    erg = copy.deepcopy(basis)
    for k, v in oben.items():
        if isinstance(v, dict) and isinstance(erg.get(k), dict):
            erg[k] = tief_mischen(erg[k], v)
        else:
            erg[k] = v
    return erg


class Konfiguration:
    """config.toml = Regeln/Felder (für Fortgeschrittene), einstellungen.json = was die Oberfläche pflegt."""

    def __init__(self, ordner: Path | None = None):
        self.ordner = ordner or daten_ordner()
        self.toml_pfad = self.ordner / "config.toml"
        self.json_pfad = self.ordner / "einstellungen.json"
        if not self.toml_pfad.exists():
            shutil.copy2(ressourcen_ordner() / "config_standard.toml", self.toml_pfad)
        self.laden()

    def laden(self) -> None:
        with open(self.toml_pfad, "rb") as fh:
            regeln = tomllib.load(fh)
        einst = standard_einstellungen()
        if self.json_pfad.exists():
            einst = tief_mischen(einst, json.loads(self.json_pfad.read_text(encoding="utf-8")))
        self.einstellungen = einst
        self.cfg = tief_mischen(regeln, einst)
        o = self.cfg["ordner"]
        o.setdefault("arbeit", str(self.ordner / "arbeit"))
        o.setdefault("protokoll", str(self.ordner / "protokoll.csv"))
        o.setdefault("logdatei", str(self.ordner / "bab_eingang.log"))

    def speichern(self, einstellungen: dict) -> None:
        self.json_pfad.write_text(json.dumps(einstellungen, ensure_ascii=False, indent=2), encoding="utf-8")
        self.laden()


# =========================================================================== Datenmodell

@dataclass
class Ergebnis:
    kundennummer: str | None = None
    kategorie: str | None = None
    bescheiddatum: str | None = None  # ISO JJJJ-MM-TT
    zeitraum_von: str | None = None
    zeitraum_bis: str | None = None
    betrag: str | None = None  # "1.234,56" – Betrag der letzten (aktuellen) Tabellenzeile
    geburtsdatum: str | None = None  # ISO
    zeitraeume: str = ""  # alle Tabellenzeilen, z. B. "01.10.2025–05.10.2025: 0,00 | 06.10.2025–31.03.2027: 2.015,00"
    hinweise: str = ""  # Auffälligkeiten bei der Erkennung
    quelle: str = "regeln"

    def fehlend(self, pflicht: list[str]) -> list[str]:
        return [f for f in pflicht if not getattr(self, f)]


class PruefFall(Exception):
    """Dokument kann nicht sicher automatisch verarbeitet werden."""


# =========================================================================== Hilfsfunktionen

def datum_iso(text: str | None) -> str | None:
    """'03.12.2025', '2025-12-03' oder '03. Dezember 2025' -> '2025-12-03'; sonst None."""
    if not text:
        return None
    text = str(text).strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    m = re.match(r"(\d{1,2})\.?\s*([A-Za-zäöüÄÖÜ]+)\s*(\d{4})$", text)
    if m and m.group(2).lower() in MONATE:
        try:
            return datetime(int(m.group(3)), MONATE[m.group(2).lower()], int(m.group(1))).strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def mehrheit(kandidaten: list[tuple[str, int]]) -> tuple[str | None, bool]:
    """Gewichtete Mehrheit. Liefert (Wert, eindeutig). Nicht eindeutig = die zwei besten liegen gleichauf."""
    punkte: dict[str, int] = {}
    for wert, gewicht in kandidaten:
        punkte[wert] = punkte.get(wert, 0) + gewicht
    if not punkte:
        return None, True
    rang = sorted(punkte.items(), key=lambda kv: -kv[1])
    if len(rang) > 1 and rang[0][1] == rang[1][1]:
        return rang[0][0], False
    return rang[0][0], True


def betrag_zahl(betrag: str) -> float:
    return float(betrag.replace(".", "").replace(",", "."))


def sicherer_dateiname(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = re.sub(r"\s+", "_", name.strip())
    return name[:180]


def eindeutiger_pfad(ziel: Path) -> Path:
    if not ziel.exists():
        return ziel
    i = 2
    while True:
        kandidat = ziel.with_name(f"{ziel.stem}_{i}{ziel.suffix}")
        if not kandidat.exists():
            return kandidat
        i += 1


def text_normalisieren(text: str) -> str:
    text = text.replace("\u00ad", "").replace("\f", "\n")
    text = re.sub(r"-\n(?=[a-zäöü])", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text


def grund_pfad(pdf: Path) -> Path:
    return pdf.with_name(pdf.stem + ".grund.json")


def ordner_oeffnen(pfad: str | Path) -> None:
    pfad = str(pfad)
    if os.name == "nt":
        os.startfile(pfad)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", pfad])
    else:
        subprocess.Popen(["xdg-open", pfad])


# =========================================================================== OCR (Tesseract direkt)

def tesseract_finden(cfg: dict) -> str:
    eigen = cfg["ocr"].get("tesseract")
    kandidaten = [Path(eigen)] if eigen else []
    kandidaten += [
        programm_ordner() / "tesseract" / "tesseract.exe",
        programm_ordner() / "tesseract" / "tesseract",
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
    ]
    for k in kandidaten:
        if k.is_file():
            return str(k)
    gefunden = shutil.which("tesseract")
    if gefunden:
        return gefunden
    raise RuntimeError("Tesseract wurde nicht gefunden. In den Einstellungen den Pfad zu tesseract.exe angeben.")


def tesseract_umgebung(exe: str) -> dict:
    env = os.environ.copy()
    tessdata = Path(exe).parent / "tessdata"
    if tessdata.is_dir():
        env["TESSDATA_PREFIX"] = str(tessdata)
    return env


def hat_textebene(pfad: Path, min_zeichen: int = 30) -> bool:
    """Textebene vorhanden, wenn die erste Seite und mindestens die Hälfte aller Seiten Text tragen
    (leere Rückseiten sind normal)."""
    try:
        reader = PdfReader(str(pfad))
        laengen = [len((s.extract_text() or "").strip()) for s in reader.pages]
        return bool(laengen) and laengen[0] >= min_zeichen and sum(l >= min_zeichen for l in laengen) * 2 >= len(laengen)
    except Exception:
        return False


def pdf_text(pfad: Path) -> str:
    reader = PdfReader(str(pfad))
    return text_normalisieren("\n".join((s.extract_text() or "") for s in reader.pages))


def ocr(quelle: Path, ziel: Path, cfg: dict) -> str:
    """Erzeugt ein durchsuchbares PDF unter `ziel` und gibt den erkannten Text zurück."""
    o = cfg["ocr"]
    if not o.get("aktiv", True) or hat_textebene(quelle):
        shutil.copy2(quelle, ziel)
        return pdf_text(ziel)

    import pypdfium2 as pdfium  # erst hier laden, damit der Rest auch ohne läuft

    exe = tesseract_finden(cfg)
    dpi = int(o.get("dpi", 300))
    with tempfile.TemporaryDirectory(prefix="bab_ocr_") as tmp:
        tmp = Path(tmp)
        bilder = []
        doc = pdfium.PdfDocument(str(quelle))
        try:
            for i in range(len(doc)):
                bild = doc[i].render(scale=dpi / 72).to_pil().convert("L")
                p = tmp / f"seite_{i:03d}.jpg"
                bild.save(p, quality=o.get("jpeg_qualitaet", 80), dpi=(dpi, dpi))
                bilder.append(str(p))
        finally:
            doc.close()
        liste = tmp / "seiten.txt"
        liste.write_text("\n".join(bilder), encoding="utf-8")
        befehl = [exe, str(liste), str(tmp / "aus"), "-l", o.get("sprache", "deu"), "--dpi", str(dpi), "pdf", "txt"]
        r = subprocess.run(befehl, capture_output=True, text=True, timeout=o.get("timeout_s", 300),
                           env=tesseract_umgebung(exe), creationflags=OHNE_FENSTER)
        if r.returncode != 0 or not (tmp / "aus.pdf").exists():
            raise RuntimeError(f"OCR fehlgeschlagen: {r.stderr.strip()[-400:]}")
        shutil.move(tmp / "aus.pdf", ziel)
        return text_normalisieren((tmp / "aus.txt").read_text(encoding="utf-8", errors="replace"))


# =========================================================================== Regeln

def erstes(muster_liste: list[str], text: str, flags=re.IGNORECASE) -> re.Match | None:
    for muster in muster_liste:
        m = re.search(muster, text, flags)
        if m:
            return m
    return None


def kategorie_bestimmen(text: str, cfg: dict) -> str | None:
    """Zwei Stufen: 'kopf'-Muster (Überschrift/Betreff) gewinnen immer, egal wo sie stehen –
    Bescheide enthalten viel Standardtext ('Bewilligung', 'aufgehoben' …), der in jeder Art vorkommt.
    Erst wenn kein Kopf-Muster passt, zählen die 'hinweis'-Muster, und dort das am weitesten oben stehende."""
    for stufe, bereich in (("kopf", text), ("hinweis", text[: cfg["regeln"].get("kategorie_suchbereich_zeichen", 6000)])):
        beste: tuple[int, int, str] | None = None
        for rang, kat in enumerate(cfg["kategorien"]):
            for muster in kat.get(stufe, []):
                m = re.search(muster, bereich, re.IGNORECASE | re.MULTILINE)
                if m and (beste is None or (m.start(), rang) < beste[:2]):
                    beste = (m.start(), rang, kat["name"])
        if beste:
            return beste[2]
    return None


def alle(muster_liste: list[str], text: str, flags=re.IGNORECASE) -> list[re.Match]:
    treffer = []
    for muster in muster_liste:
        treffer += list(re.finditer(muster, text, flags))
    return treffer


def tabelle_lesen(text: str, cfg: dict) -> list[tuple[str, str, str]]:
    """Zeilen der Tabelle 'von bis mtl. Betrag': [(von, bis, betrag), ...] in Dokumentreihenfolge, ohne Dubletten."""
    zeilen, gesehen = [], set()
    for m in alle(cfg["muster"]["tabellenzeile"], text, re.IGNORECASE | re.MULTILINE):
        von, bis = datum_iso(m.group(1)), datum_iso(m.group(2))
        if not (von and bis and von <= bis):
            continue
        z = (von, bis, m.group(3))
        if z not in gesehen:
            gesehen.add(z)
            zeilen.append(z)
    return zeilen


def regeln_anwenden(text: str, cfg: dict) -> Ergebnis:
    m_cfg = cfg["muster"]
    e = Ergebnis()
    hinweise = []

    # Kundennummer: beschriftete Fundstellen zählen 3-fach, Barcode/Freitext 1-fach; bei Gleichstand -> unsicher
    kandidaten = [(re.sub(r"\s", "", m.group(1)).upper(), 3) for m in alle(m_cfg["kundennummer_beschriftet"], text)]
    kandidaten += [(re.sub(r"\s", "", m.group(1)).upper(), 1) for m in alle(m_cfg["kundennummer_frei"], text, 0)]
    kandidaten = [(k, g) for k, g in kandidaten if KDNR_FORMAT.match(k)]
    wert, eindeutig = mehrheit(kandidaten)
    verschiedene = sorted({k for k, _ in kandidaten})
    if wert and eindeutig:
        e.kundennummer = wert
        if len(verschiedene) > 1:
            hinweise.append("Kundennummer unterschiedlich gelesen: " + ", ".join(verschiedene) + f" – verwendet: {wert}")
    elif wert:
        hinweise.append("Kundennummer nicht eindeutig: " + ", ".join(verschiedene))

    # Bescheiddatum: 'Datum: 03. Dezember 2025' (3-fach) vor 'Bescheid vom 03.12.2025' (1-fach, mehrfach im Dokument)
    kandidaten = [(d, 3) for m in alle(m_cfg["bescheiddatum"], text) if (d := datum_iso(m.group(1)))]
    kandidaten += [(d, 1) for m in alle(m_cfg["bescheiddatum_fallback"], text) if (d := datum_iso(m.group(1)))]
    wert, eindeutig = mehrheit(kandidaten)
    if wert and eindeutig:
        e.bescheiddatum = wert
    elif wert:
        hinweise.append("Bescheiddatum nicht eindeutig: " + ", ".join(sorted({d for d, _ in kandidaten})))

    # Geburtsdatum
    m = erstes(m_cfg["geburtsdatum"], text)
    if m:
        e.geburtsdatum = datum_iso(m.group(1))

    # Zeitraum + Betrag: Tabelle 'von bis mtl. Betrag'; Betrag = letzte Zeile (aktueller Satz)
    zeilen = tabelle_lesen(text, cfg)
    if zeilen:
        e.zeitraum_von, e.zeitraum_bis, e.betrag = zeilen[0][0], zeilen[-1][1], zeilen[-1][2]
        e.zeitraeume = " | ".join(f"{iso_zu_de(v)}–{iso_zu_de(b)}: {bt}" for v, b, bt in zeilen)
    else:
        m = erstes(m_cfg["zeitraum"], text)
        if m:
            e.zeitraum_von, e.zeitraum_bis = datum_iso(m.group(1)), datum_iso(m.group(2))
        m = erstes(m_cfg["betrag"], text)
        if m:
            e.betrag = m.group(1)

    e.kategorie = kategorie_bestimmen(text, cfg)

    stichwort = cfg["regeln"].get("erforderliches_stichwort")
    if stichwort and not re.search(stichwort, text, re.IGNORECASE):
        hinweise.append("Stichwort für BAB-Bescheid nicht gefunden – ist das ein BAB-Dokument?")
    e.hinweise = "; ".join(hinweise)
    return e


def iso_zu_de(iso: str | None) -> str:
    if not iso or len(iso) != 10:
        return iso or ""
    j, m, t = iso.split("-")
    return f"{t}.{m}.{j}"


# =========================================================================== Lokale KI (Ollama)

def ki_ergaenzen(text: str, e: Ergebnis, cfg: dict) -> Ergebnis:
    k = cfg["ki"]
    kategorien = [kat["name"] for kat in cfg["kategorien"]]
    prompt = (
        "Du liest einen Bescheid der Agentur für Arbeit zur Berufsausbildungsbeihilfe (BAB). "
        "Gib NUR ein JSON-Objekt mit diesen Schlüsseln zurück:\n"
        '  "kundennummer": Kundennummer im Format 123A456789 oder null,\n'
        f'  "kategorie": genau einer von {kategorien} oder null,\n'
        '  "bescheiddatum": Datum des Schreibens als TT.MM.JJJJ oder null,\n'
        '  "zeitraum_von", "zeitraum_bis": Bewilligungszeitraum als TT.MM.JJJJ oder null,\n'
        '  "betrag": monatlicher BAB-Betrag im Format 1.234,56 oder null.\n'
        "Erfinde nichts. Wenn ein Wert nicht eindeutig im Text steht, setze null.\n\n"
        "TEXT:\n" + text[: k.get("max_zeichen", 6000)]
    )
    try:
        r = requests.post(
            k["url"].rstrip("/") + "/api/chat",
            json={"model": k["modell"], "messages": [{"role": "user", "content": prompt}],
                  "format": "json", "stream": False, "options": {"temperature": 0}},
            timeout=k.get("timeout_s", 180),
        )
        r.raise_for_status()
        daten = json.loads(r.json()["message"]["content"])
    except Exception as ex:
        log.warning("KI nicht erreichbar oder Antwort unbrauchbar: %s", ex)
        return e

    benutzt = False
    kdnr = str(daten.get("kundennummer") or "").replace(" ", "").upper()
    if not e.kundennummer and KDNR_FORMAT.match(kdnr):
        e.kundennummer, benutzt = kdnr, True
    if not e.kategorie and daten.get("kategorie") in kategorien:
        e.kategorie, benutzt = daten["kategorie"], True
    for feld in ("bescheiddatum", "zeitraum_von", "zeitraum_bis"):
        wert = datum_iso(daten.get(feld))
        if not getattr(e, feld) and wert:
            setattr(e, feld, wert)
            benutzt = True
    betrag = str(daten.get("betrag") or "").replace(" ", "").replace("EUR", "").replace("€", "")
    if not e.betrag and BETRAG_FORMAT.match(betrag):
        e.betrag, benutzt = betrag, True
    if benutzt:
        e.quelle = "regeln+ki"
    return e


def ki_testen(cfg: dict) -> str:
    r = requests.get(cfg["ki"]["url"].rstrip("/") + "/api/tags", timeout=10)
    r.raise_for_status()
    modelle = [m["name"] for m in r.json().get("models", [])]
    wunsch = cfg["ki"]["modell"]
    status = "vorhanden" if wunsch in modelle else "NICHT installiert (ollama pull " + wunsch + ")"
    return f"Ollama erreichbar. Modell {wunsch}: {status}\nInstalliert: {', '.join(modelle) or '–'}"


# =========================================================================== Ninox

class Ninox:
    def __init__(self, cfg: dict):
        n = cfg["ninox"]
        self.cfg = n
        self.wurzel = n["base_url"].rstrip("/")
        self.basis = f'{self.wurzel}/teams/{n["team_id"]}/databases/{n["datenbank_id"]}'
        self.s = requests.Session()
        self.s.headers["Authorization"] = f'Bearer {n["api_schluessel"]}'

    def verbindung_testen(self) -> str:
        """Hilft beim Einrichten: listet Teams, Datenbanken oder Tabellen samt IDs."""
        n = self.cfg
        if not n.get("team_id"):
            r = self.s.get(f"{self.wurzel}/teams", timeout=20); r.raise_for_status()
            return "Verbindung ok. Teams:\n" + "\n".join(f'  {t["id"]}  {t["name"]}' for t in r.json())
        if not n.get("datenbank_id"):
            r = self.s.get(f'{self.wurzel}/teams/{n["team_id"]}/databases', timeout=20); r.raise_for_status()
            return "Verbindung ok. Datenbanken:\n" + "\n".join(f'  {d["id"]}  {d["name"]}' for d in r.json())
        r = self.s.get(f"{self.basis}/tables", timeout=20); r.raise_for_status()
        zeilen = []
        for t in r.json():
            felder = ", ".join(f.get("name", "") for f in t.get("fields", []))
            zeilen.append(f'  {t["id"]}  {t["name"]}  [{felder}]')
        return "Verbindung ok. Tabellen (ID, Name, Felder):\n" + "\n".join(zeilen)

    def _query(self, skript: str):
        r = self.s.post(f"{self.basis}/query", json={"query": skript}, timeout=30)
        r.raise_for_status()
        return r.json()

    def bewohner_finden(self, kundennummer: str | None = None, geburtsdatum: str | None = None) -> list[dict]:
        """Sucht über die Kundennummer oder (Fallback) über das Geburtsdatum (ISO).
        Liefert [{id, name, kundennummer, geburtsdatum}] – die letzten beiden nur, wenn das Feld konfiguriert ist."""
        b = self.cfg["bewohner"]
        f_kdnr, f_geb = b["feld_kundennummer"], b.get("feld_geburtsdatum") or ""
        if kundennummer:
            bedingung = f"'{f_kdnr}' = \"{kundennummer}\""
        elif geburtsdatum and f_geb:
            j, m, t = geburtsdatum.split("-")
            bedingung = f"'{f_geb}' = date({int(j)}, {int(m)}, {int(t)})"
        else:
            return []
        teile = ["text(number(Id))", b["name_ausdruck"], f"text('{f_kdnr}')",
                 f"format('{f_geb}', \"YYYY-MM-DD\")" if f_geb else '""']
        skript = f"join((select '{b['tabelle']}' where {bedingung}).(" + ' + "|" + '.join(teile) + "), \"\\n\")"
        treffer = []
        for zeile in str(self._query(skript) or "").splitlines():
            if zeile.count("|") >= 3:
                rid, name, kdnr, geb = zeile.split("|", 3)
                treffer.append({"id": int(float(rid)), "name": name, "kundennummer": kdnr.strip().upper(),
                                "geburtsdatum": geb.strip()})
        return treffer

    def dokument_anlegen(self, bewohner_id: int, e: Ergebnis, pdf: Path, dateiname: str) -> int:
        d = self.cfg["dokumente"]
        f = d["felder"]
        werte = {
            "bewohner": bewohner_id, "kategorie": e.kategorie, "bescheiddatum": e.bescheiddatum,
            "zeitraum_von": e.zeitraum_von, "zeitraum_bis": e.zeitraum_bis,
            "betrag": betrag_zahl(e.betrag) if e.betrag else None,
            "kundennummer": e.kundennummer, "dateiname": dateiname,
            "geburtsdatum": e.geburtsdatum, "zeitraeume": e.zeitraeume or None, "hinweise": e.hinweise or None,
        }
        fields = {f[k]: v for k, v in werte.items() if f.get(k) and v is not None}
        tab = f'{self.basis}/tables/{d["tabelle_id"]}/records'
        r = self.s.post(tab, json=[{"fields": fields}], timeout=30)
        r.raise_for_status()
        rid = r.json()[0]["id"]
        with open(pdf, "rb") as fh:
            r = self.s.post(f"{tab}/{rid}/files", files={"file": (dateiname, fh, "application/pdf")}, timeout=120)
        r.raise_for_status()
        if f.get("datei"):
            r = self.s.post(tab, json=[{"id": rid, "fields": {f["datei"]: dateiname}}], timeout=30)
            r.raise_for_status()
        return rid


# =========================================================================== Verarbeitung

class Verarbeitung:
    """Eine Instanz pro Konfiguration. Thread-sicher: es läuft immer nur ein Dokument gleichzeitig."""

    def __init__(self, cfg: dict, testlauf: bool, melden: Callable[[dict], None] | None = None):
        self.cfg = cfg
        self.testlauf = testlauf
        self.melden = melden or (lambda _: None)
        self.sperre = threading.Lock()
        o = cfg["ordner"]
        self.eingang = Path(o["eingang"])
        self.verarbeitet = Path(o["verarbeitet"])
        self.pruefen = Path(o["pruefen"])
        self.fehler = Path(o["fehler"])
        self.arbeit = Path(o["arbeit"])
        self.archiv = Path(o["archiv_original"]) if o.get("archiv_original") else None
        for p in [self.eingang, self.verarbeitet, self.pruefen, self.fehler, self.arbeit, self.archiv]:
            if p:
                p.mkdir(parents=True, exist_ok=True)
        self.protokoll = Path(o["protokoll"])
        self.ninox = None if testlauf else Ninox(cfg)

    # ---------------------------------------------------------------- einzelnes Dokument
    def datei(self, original: Path, vorgaben: dict | None = None) -> str:
        """Verarbeitet eine PDF. `vorgaben` überschreibt erkannte Felder (manuelle Korrektur).
        Gibt "ok", "pruefen" oder "fehler" zurück."""
        with self.sperre:
            return self._datei(original, vorgaben)

    def _datei(self, original: Path, vorgaben: dict | None) -> str:
        # Aus "Prüfen" erneut verarbeiten: erst aus dem Ordner holen, damit nichts doppelt liegt
        if original.parent.resolve() == self.pruefen.resolve():
            grund_pfad(original).unlink(missing_ok=True)
            arbeitskopie = eindeutiger_pfad(self.arbeit / original.name)
            shutil.move(original, arbeitskopie)
            original = arbeitskopie

        log.info("Verarbeite %s", original.name)
        ocr_pdf = eindeutiger_pfad(self.arbeit / f"{original.stem}_ocr.pdf")
        e = Ergebnis()
        try:
            text = ocr(original, ocr_pdf, self.cfg)
            e = regeln_anwenden(text, self.cfg)
            pflicht = self.cfg["regeln"]["pflichtfelder"]
            if e.fehlend(pflicht) and self.cfg["ki"].get("aktiv", False) and not vorgaben:
                e = ki_ergaenzen(text, e, self.cfg)
            erkanntes_geburtsdatum = e.geburtsdatum
            if vorgaben:
                for k, v in vorgaben.items():
                    if k in FELDER and v:
                        setattr(e, k, v)
                e.quelle = "manuell"

            fehlend = e.fehlend(pflicht)
            if fehlend:
                raise PruefFall("nicht erkannt: " + ", ".join(fehlend) + (f" ({e.hinweise})" if e.hinweise else ""))
            geb_feld = self.cfg["ninox"]["bewohner"].get("feld_geburtsdatum")
            if not e.kundennummer and not (e.geburtsdatum and geb_feld):
                raise PruefFall("Kundennummer nicht erkannt" + (f" ({e.hinweise})" if e.hinweise else ""))
            if "nicht eindeutig" in e.hinweise and not vorgaben:
                raise PruefFall(e.hinweise)

            bewohner_id, name = None, e.kundennummer or f"geb-{e.geburtsdatum}"
            if self.ninox:
                treffer = self.ninox.bewohner_finden(e.kundennummer, e.geburtsdatum)
                schluessel = f"Kundennummer {e.kundennummer}" if e.kundennummer else f"Geburtsdatum {iso_zu_de(e.geburtsdatum)}"
                if not treffer:
                    raise PruefFall(f"{schluessel} in Ninox nicht gefunden")
                if len(treffer) > 1:
                    raise PruefFall(f"{schluessel} mehrfach in Ninox vorhanden")
                t = treffer[0]
                geburtsdatum_bestaetigt = bool(vorgaben) and vorgaben.get("geburtsdatum") not in (None, "", erkanntes_geburtsdatum)
                if (e.kundennummer and e.geburtsdatum and t["geburtsdatum"] and t["geburtsdatum"] != e.geburtsdatum
                        and not geburtsdatum_bestaetigt):
                    raise PruefFall(f"Geburtsdatum im Bescheid ({iso_zu_de(e.geburtsdatum)}) passt nicht zu "
                                    f"{t['name']} in Ninox ({iso_zu_de(t['geburtsdatum'])}) – Kundennummer falsch gelesen?")
                if not e.kundennummer and t["kundennummer"]:
                    e.kundennummer = t["kundennummer"]
                    log.info("Kundennummer %s über Geburtsdatum aus Ninox übernommen", e.kundennummer)
                bewohner_id, name = t["id"], t["name"]

            dateiname = sicherer_dateiname(self.cfg["dateiname"]["schema"].format(
                name=name, kategorie=e.kategorie, datum=e.bescheiddatum or "ohne-Datum",
                kundennummer=e.kundennummer or "ohne-Kdnr", geburtsdatum=e.geburtsdatum or "ohne-Geburtsdatum",
                von=e.zeitraum_von or "", bis=e.zeitraum_bis or ""))
            if not dateiname.lower().endswith(".pdf"):
                dateiname += ".pdf"

            if self.ninox:
                rid = self.ninox.dokument_anlegen(bewohner_id, e, ocr_pdf, dateiname)
                log.info("In Ninox abgelegt (Datensatz %s)", rid)

            ziel = eindeutiger_pfad(self.verarbeitet / dateiname)
            shutil.move(ocr_pdf, ziel)
            self._original_wegraeumen(original)
            self._protokoll(original.name, ziel.name, "ok", "", e)
            log.info("-> %s (%s)", ziel.name, e.quelle)
            return "ok"

        except PruefFall as grund:
            quelle = ocr_pdf if ocr_pdf.exists() else original
            ziel = eindeutiger_pfad(self.pruefen / original.name)
            shutil.move(quelle, ziel)
            grund_pfad(ziel).write_text(json.dumps({"grund": str(grund), "erkannt": asdict(e)},
                                                   ensure_ascii=False, indent=2), encoding="utf-8")
            if quelle != original:
                self._original_wegraeumen(original)
            self._protokoll(original.name, ziel.name, "pruefen", str(grund), e)
            log.warning("-> prüfen: %s (%s)", ziel.name, grund)
            return "pruefen"

        except Exception as ex:
            ziel = eindeutiger_pfad(self.fehler / original.name)
            if original.exists():
                shutil.move(original, ziel)
            ocr_pdf.unlink(missing_ok=True)
            self._protokoll(original.name, ziel.name, "fehler", str(ex), e)
            log.exception("-> Fehler bei %s", original.name)
            return "fehler"

    def _original_wegraeumen(self, original: Path) -> None:
        if not original.exists():
            return
        if self.archiv:
            shutil.move(original, eindeutiger_pfad(self.archiv / original.name))
        else:
            original.unlink()

    def _protokoll(self, ein: str, aus: str, status: str, grund: str, e: Ergebnis) -> None:
        zeile = {"zeit": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "eingang": ein, "ergebnis": aus,
                 "status": status, "grund": grund, **{f: getattr(e, f) or "" for f in FELDER},
                 "zeitraeume": e.zeitraeume, "hinweise": e.hinweise, "quelle": e.quelle}
        neu = not self.protokoll.exists()
        with open(self.protokoll, "a", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=list(zeile), delimiter=";")
            if neu:
                w.writeheader()
            w.writerow(zeile)
        try:
            self.melden(zeile)
        except Exception:
            pass

    # ---------------------------------------------------------------- Ordner
    def bereite_dateien(self) -> list[Path]:
        kandidaten = sorted(p for p in self.eingang.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
        if not kandidaten:
            return []
        groessen = {p: p.stat().st_size for p in kandidaten}
        time.sleep(self.cfg["ueberwachung"].get("stabil_warten_s", 3))
        return [p for p in kandidaten if p.exists() and p.stat().st_size == groessen[p] > 0]

    def durchlauf(self) -> int:
        dateien = self.bereite_dateien()
        for p in dateien:
            self.datei(p)
        return len(dateien)

    def pruef_faelle(self) -> list[tuple[Path, dict]]:
        faelle = []
        for p in sorted(self.pruefen.glob("*.pdf")):
            info = {}
            if grund_pfad(p).exists():
                try:
                    info = json.loads(grund_pfad(p).read_text(encoding="utf-8"))
                except Exception:
                    pass
            faelle.append((p, info))
        return faelle


def protokoll_lesen(pfad: Path, anzahl: int = 300) -> list[dict]:
    if not pfad.exists():
        return []
    with open(pfad, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh, delimiter=";"))[-anzahl:]


def analysieren(pdf: Path, cfg: dict) -> tuple[str, Ergebnis, Ergebnis | None]:
    arbeit = Path(cfg["ordner"]["arbeit"])
    arbeit.mkdir(parents=True, exist_ok=True)
    ziel = eindeutiger_pfad(arbeit / f"{pdf.stem}_analyse.pdf")
    try:
        text = ocr(pdf, ziel, cfg)
    finally:
        ziel.unlink(missing_ok=True)
    e = regeln_anwenden(text, cfg)
    mit_ki = None
    if cfg["ki"].get("aktiv", False) and e.fehlend(cfg["regeln"]["pflichtfelder"]):
        mit_ki = ki_ergaenzen(text, copy.copy(e), cfg)
    return text, e, mit_ki


# =========================================================================== Kommandozeile

def logging_einrichten(cfg: dict, weitere: list[logging.Handler] | None = None) -> None:
    handler = [logging.FileHandler(cfg["ordner"]["logdatei"], encoding="utf-8")]
    if sys.stdout:
        handler.append(logging.StreamHandler(sys.stdout))
    handler += weitere or []
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handler, force=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="BAB-Bescheide auslesen, benennen und in Ninox ablegen")
    ap.add_argument("--einmal", action="store_true")
    ap.add_argument("--testlauf", action="store_true")
    ap.add_argument("--analyse", metavar="PDF")
    ap.add_argument("--datenordner", help="anderer Ordner für config.toml/einstellungen.json")
    args = ap.parse_args()

    k = Konfiguration(Path(args.datenordner) if args.datenordner else None)
    cfg = k.cfg
    logging_einrichten(cfg)

    if args.analyse:
        text, e, mit_ki = analysieren(Path(args.analyse), cfg)
        print(text[:4000], "\n", "=" * 60)
        print("REGELN:", json.dumps(asdict(e), ensure_ascii=False, indent=2))
        if mit_ki:
            print("MIT KI:", json.dumps(asdict(mit_ki), ensure_ascii=False, indent=2))
        return

    v = Verarbeitung(cfg, testlauf=args.testlauf or cfg.get("testlauf", False))
    if args.einmal:
        log.info("%d Datei(en) verarbeitet", v.durchlauf())
        return
    while True:
        try:
            v.durchlauf()
        except Exception:
            log.exception("Fehler im Durchlauf")
        time.sleep(cfg["ueberwachung"].get("intervall_s", 30))


if __name__ == "__main__":
    main()
