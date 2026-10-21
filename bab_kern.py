"""
BAB-O-BOX – Kernlogik (ohne Oberfläche).

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
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import requests
from pypdf import PdfReader

log = logging.getLogger("bab")

APP_NAME = "BAB-O-BOX"
ALTER_APP_NAME = "BAB-Eingang"
FELDER = ["name", "kundennummer", "kategorie", "bescheiddatum", "zeitraum_von", "zeitraum_bis", "betrag", "geburtsdatum",
          "antragsdatum", "haus"]
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
    basis = Path(os.environ.get("APPDATA") or (Path.home() / ".config"))
    p = basis / APP_NAME
    alt = basis / ALTER_APP_NAME
    if not p.exists() and alt.is_dir():  # Umbenennung BAB-Eingang -> BAB-O-BOX: Daten mitnehmen
        try:
            shutil.copytree(alt, p)
        except Exception:
            pass
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
        "ki": {"aktiv": False, "url": "http://localhost:11434", "modell": "gemma4:e2b"},
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


# ---------------------------------------------------------------------------- TOML schreiben (klein, für eigene Regeln)

def _toml_schluessel(k: str) -> str:
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k, ensure_ascii=False)


def _toml_wert(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return f"'{v}'" if "'" not in v and "\n" not in v else json.dumps(v, ensure_ascii=False)
    if isinstance(v, list) and not any(isinstance(x, dict) for x in v):
        return "[" + ", ".join(_toml_wert(x) for x in v) + "]"
    raise TypeError(f"nicht als TOML-Wert darstellbar: {v!r}")


def _ist_tabellenliste(v) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)


def toml_text(d: dict) -> str:
    aus: list[str] = []

    def block(daten: dict, pfad: tuple, art: str | None) -> None:
        einfach = [(k, v) for k, v in daten.items() if not (isinstance(v, dict) or _ist_tabellenliste(v))]
        unter = [(k, v) for k, v in daten.items() if isinstance(v, dict) or _ist_tabellenliste(v)]
        name = ".".join(_toml_schluessel(x) for x in pfad)
        if art == "liste" or (art == "tabelle" and (einfach or not unter)):
            aus.append("")
            aus.append(f"[[{name}]]" if art == "liste" else f"[{name}]")
        aus.extend(f"{_toml_schluessel(k)} = {_toml_wert(v)}" for k, v in einfach)
        for k, v in unter:
            if isinstance(v, dict):
                block(v, pfad + (k,), "tabelle")
            else:
                for eintrag in v:
                    block(eintrag, pfad + (k,), "liste")

    block(d, (), None)
    return "\n".join(aus).strip("\n")


def regel_unterschiede(alt: dict, standard: dict) -> dict:
    """Nur die Werte aus `alt`, die vom `standard` abweichen (Listen zählen als Ganzes)."""
    erg = {}
    for k, v in alt.items():
        if k in ("version", "ninox_stand", "eigene_anpassungen"):
            continue
        if isinstance(v, dict) and isinstance(standard.get(k), dict):
            u = regel_unterschiede(v, standard[k])
            if u:
                erg[k] = u
        elif k not in standard or standard[k] != v:
            erg[k] = v
        # gleich -> keine Anpassung
    return erg


def nur_bekannte(anpassung: dict, standard: dict, pfad: str = "") -> tuple[dict, list[str]]:
    """Wirft Anpassungen weg, deren Schlüssel es im aktuellen Standard nicht mehr gibt."""
    erg, weg = {}, []
    for k, v in anpassung.items():
        p = f"{pfad}.{k}" if pfad else k
        if k not in standard and not (pfad.startswith("ninox.zuordnung") or pfad == "ninox.zuordnung"):
            weg.append(p)
        elif isinstance(v, dict) and isinstance(standard.get(k), dict):
            innen, w = nur_bekannte(v, standard[k], p)
            weg += w
            if innen:
                erg[k] = innen
        else:
            erg[k] = v
    return erg, weg


EIGENE_KOPF = """# BAB-O-BOX – EIGENE REGELN
# Hier steht nur, was bei euch vom Standard abweicht. Alles andere kommt aus den Standardregeln,
# die mit jedem Update automatisch aktuell sind (zum Nachschlagen: "regeln_standard_NUR_LESEN.toml"
# im selben Ordner). Abschnitt und Schlüssel genau wie dort schreiben.
# Nach dem Speichern in der App auf "Konfiguration neu laden" klicken.
#
# Beispiele:
#   [ninox.bescheid]
#   tabelle = 'BAB Bescheide'
#
#   [ninox.antrag]
#   feld_antragsdatum = 'Eingangsdatum'
#
#   [dateiname]
#   schema = '{name}_BAB-{kategorie}_{datum}.pdf'

eigene_anpassungen = true
"""


class Konfiguration:
    """Standardregeln (mitgeliefert, bei Updates neu) + eigene_regeln.toml (nur Abweichungen) + einstellungen.json (Oberfläche)."""

    def __init__(self, ordner: Path | None = None):
        self.ordner = ordner or daten_ordner()
        self.standard_pfad = ressourcen_ordner() / "config_standard.toml"
        self.eigen_pfad = self.ordner / "eigene_regeln.toml"
        self.toml_pfad = self.eigen_pfad  # für die Oberfläche ("Regeldatei bearbeiten")
        self.ansicht_pfad = self.ordner / "regeln_standard_NUR_LESEN.toml"
        self.json_pfad = self.ordner / "einstellungen.json"
        self.migriert: str | None = None
        self._umstellen()
        try:
            shutil.copy2(self.standard_pfad, self.ansicht_pfad)
        except Exception:
            pass
        self.laden()

    def _umstellen(self) -> None:
        """Alte vollständige config.toml (bis Version 4) -> eigene_regeln.toml mit nur den Abweichungen."""
        alt_pfad = self.ordner / "config.toml"
        if self.eigen_pfad.exists():
            return
        if not alt_pfad.exists():
            self.eigen_pfad.write_text(EIGENE_KOPF, encoding="utf-8")
            return
        try:
            alt = tomllib.loads(alt_pfad.read_text(encoding="utf-8"))
            with open(self.standard_pfad, "rb") as fh:
                standard = tomllib.load(fh)
            kandidaten = [standard]
            alt_ordner = ressourcen_ordner() / "regeln_alt"
            for f in sorted(alt_ordner.glob("*.toml")) if alt_ordner.is_dir() else []:
                try:
                    kandidaten.append(tomllib.loads(f.read_text(encoding="utf-8")))
                except Exception:
                    pass
            # Die alte Standarddatei, von der die Kopie stammt, ist die mit den wenigsten Abweichungen
            anpassung = min((regel_unterschiede(alt, k) for k in kandidaten), key=lambda u: len(json.dumps(u)))
            anpassung, weg = nur_bekannte(anpassung, standard)
            text = EIGENE_KOPF + ("\n# Übernommen aus der bisherigen config.toml:\n" + toml_text(anpassung) + "\n" if anpassung else "")
            tomllib.loads(text)  # muss gültig sein
            self.eigen_pfad.write_text(text, encoding="utf-8")
            alt_pfad.rename(self.ordner / f"config.alt-{datetime.now():%Y%m%d-%H%M%S}.toml")
            zahl = len(re.findall(r"^[^#\[\s].*=", toml_text(anpassung), re.M)) if anpassung else 0
            self.migriert = ("Die Regeln sind jetzt zweigeteilt: Standardregeln kommen mit jedem Update automatisch, "
                             "deine eigenen Anpassungen stehen getrennt in „eigene_regeln.toml“ und bleiben erhalten.\n\n"
                             + (f"{zahl} eigene Anpassung(en) aus der bisherigen Datei übernommen." if zahl
                                else "In der bisherigen Datei gab es keine eigenen Anpassungen.")
                             + (f"\nNicht mehr benötigt und weggelassen: {', '.join(weg)}" if weg else ""))
        except Exception as ex:
            self.eigen_pfad.write_text(EIGENE_KOPF, encoding="utf-8")
            try:
                alt_pfad.rename(self.ordner / f"config.alt-{datetime.now():%Y%m%d-%H%M%S}.toml")
            except Exception:
                pass
            self.migriert = f"Die bisherige Regeldatei konnte nicht übernommen werden ({ex}). Sie liegt gesichert im Datenordner."

    def laden(self) -> None:
        with open(self.standard_pfad, "rb") as fh:
            regeln = tomllib.load(fh)
        with open(self.eigen_pfad, "rb") as fh:
            eigene = tomllib.load(fh)
        regeln = tief_mischen(regeln, eigene)
        einst = standard_einstellungen()
        if self.json_pfad.exists():
            einst = tief_mischen(einst, json.loads(self.json_pfad.read_text(encoding="utf-8")))
        self.einstellungen = einst
        self.cfg = tief_mischen(regeln, einst)
        o = self.cfg["ordner"]
        o.setdefault("arbeit", str(self.ordner / "arbeit"))
        o.setdefault("protokoll", str(self.ordner / "protokoll.csv"))
        o.setdefault("logdatei", str(self.ordner / "bab_eingang.log"))
        self.cfg["_stammdaten"] = stammdaten_lesen(self.ordner)
        self.cfg["_datenordner"] = str(self.ordner)

    def speichern(self, einstellungen: dict) -> None:
        self.json_pfad.write_text(json.dumps(einstellungen, ensure_ascii=False, indent=2), encoding="utf-8")
        self.laden()


# =========================================================================== Datenmodell

@dataclass
class Ergebnis:
    name: str | None = None  # aus dem Bescheid gelesen, "Nachname, Vorname" – nur für Dateiname/Anzeige, NICHT für Ninox
    kundennummer: str | None = None
    kategorie: str | None = None
    bescheiddatum: str | None = None  # ISO JJJJ-MM-TT
    zeitraum_von: str | None = None
    zeitraum_bis: str | None = None
    betrag: str | None = None  # "1.234,56" – Betrag der letzten (aktuellen) Tabellenzeile
    geburtsdatum: str | None = None  # ISO
    antragsdatum: str | None = None  # ISO – "auf dem am … gestellten Antrag"
    haus: str | None = None  # erkanntes Haus (aus Ninox-Stammdaten)
    haus_id: int | None = None
    notiz: str = ""  # für das Notiz-Feld des Bescheids, z. B. Auszahlung erst nach Prüfungsnachweis
    antragsart_hinweis: str = ""  # Text deutet auf eine bestimmte Antragsart hin (nur zur Kontrolle)
    abschnitte: list = field(default_factory=list)  # [[von_iso, bis_iso, "1.234,56"], ...]
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
            for muster in kat.get(stufe, kat.get("muster", []) if stufe == "kopf" else []):
                m = re.search(muster, bereich, re.IGNORECASE | re.MULTILINE)
                if m and (beste is None or (m.start(), rang) < beste[:2]):
                    beste = (m.start(), rang, kat["name"])
        if beste:
            return beste[2]
    return None


NAME_GUELTIG = re.compile(r"^[A-ZÄÖÜ][A-Za-zÄÖÜäöüßéèáàçñ'\-]+(?:[ ,]+[A-Za-zÄÖÜäöüßéèáàçñ'\-]+){1,4}$")


def name_lesen(text: str, cfg: dict) -> str | None:
    """Name der/des Auszubildenden aus dem Bescheid als 'Nachname, Vorname'. Unsicher -> None."""
    sperrworte = [w.lower() for w in cfg["regeln"].get("name_sperrworte", [])]
    for m in alle(cfg["muster"].get("name", []), text):
        roh = re.sub(r"\s+", " ", m.group(1)).strip(" ,.;:")
        if not NAME_GUELTIG.match(roh) or any(w in roh.lower() for w in sperrworte):
            continue
        if "," in roh:
            nach, vor = [t.strip() for t in roh.split(",", 1)]
        else:
            teile = roh.split(" ")
            vor, nach = " ".join(teile[:-1]), teile[-1]
        if nach and vor:
            return f"{nach}, {vor}"
    return None


def unterart_bestimmen(text: str, cfg: dict, kategorie: str | None) -> tuple[str | None, str | None]:
    """Verfeinert z. B. 'Änderung' zu 'Änderung BAföG' oder 'Änderung TS'. Passen mehrere -> Hinweis, keine Unterart."""
    treffer = []
    for u in cfg.get("unterarten", []):
        if u.get("basis") != kategorie:
            continue
        for muster in u["muster"]:
            m = re.search(muster, text, re.IGNORECASE)
            if m:
                treffer.append((u["name"], re.sub(r"\s+", " ", m.group(0))))
                break
    if len(treffer) == 1:
        return treffer[0][0], None
    if len(treffer) > 1:
        return None, "Art nicht eindeutig: " + "; ".join(f"{n} („{w}“)" for n, w in treffer)
    return None, None


def alle(muster_liste: list[str], text: str, flags=re.IGNORECASE) -> list[re.Match]:
    treffer = []
    for muster in muster_liste:
        treffer += list(re.finditer(muster, text, flags))
    return treffer


def tabelle_lesen(text: str, cfg: dict) -> list[tuple[str, str, str]]:
    """Zeilen der Tabelle 'von bis mtl. Betrag': [(von, bis, betrag), ...] in Dokumentreihenfolge, ohne Dubletten."""
    zeilen, gesehen = [], set()
    for m in alle(cfg["muster"].get("tabellenzeile", []), text, re.IGNORECASE | re.MULTILINE):
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
    kandidaten = [(re.sub(r"\s", "", m.group(1)).upper(), 3)
                  for m in alle(m_cfg.get("kundennummer_beschriftet", m_cfg.get("kundennummer", [])), text)]
    kandidaten += [(re.sub(r"\s", "", m.group(1)).upper(), 1) for m in alle(m_cfg.get("kundennummer_frei", []), text, 0)]
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
    kandidaten = [(d, 3) for m in alle(m_cfg.get("bescheiddatum", []), text) if (d := datum_iso(m.group(1)))]
    kandidaten += [(d, 1) for m in alle(m_cfg.get("bescheiddatum_fallback", []), text) if (d := datum_iso(m.group(1)))]
    wert, eindeutig = mehrheit(kandidaten)
    if wert and eindeutig:
        e.bescheiddatum = wert
    elif wert:
        hinweise.append("Bescheiddatum nicht eindeutig: " + ", ".join(sorted({d for d, _ in kandidaten})))

    # Name (nur Dateiname/Anzeige)
    e.name = name_lesen(text, cfg)

    # Geburtsdatum
    m = erstes(m_cfg.get("geburtsdatum", []), text)
    if m:
        e.geburtsdatum = datum_iso(m.group(1))

    m = erstes(m_cfg.get("antragsdatum", []), text)
    if m:
        e.antragsdatum = datum_iso(m.group(1))
    m = erstes(m_cfg.get("auszahlung_nach_nachweis", []), text)
    if m:
        e.notiz = f"Ab {m.group(1)} Auszahlung erst nach Nachweis des letzten Prüfungstags (bewilligt bis Ausbildungsende)"
    for a in cfg.get("antragsart_hinweise", []):
        if erstes(a["muster"], text):
            e.antragsart_hinweis = a["name"]
            break

    stammdaten = cfg.get("_stammdaten") or {}
    if stammdaten.get("haeuser"):
        haus, hinweis = haus_erkennen(text, stammdaten["haeuser"])
        if haus:
            e.haus, e.haus_id = haus.get("kuerzel") or haus.get("name"), haus["id"]
        if hinweis:
            hinweise.append(hinweis)

    # Zeitraum + Betrag: Tabelle 'von bis mtl. Betrag'; Betrag = letzte Zeile (aktueller Satz)
    zeilen = tabelle_lesen(text, cfg)
    if zeilen:
        e.zeitraum_von, e.zeitraum_bis, e.betrag = zeilen[0][0], zeilen[-1][1], zeilen[-1][2]
        e.abschnitte = [list(z) for z in zeilen]
        e.zeitraeume = " | ".join(f"{iso_zu_de(v)}–{iso_zu_de(b)}: {bt}" for v, b, bt in zeilen)
    else:
        m = erstes(m_cfg.get("zeitraum", []), text)
        if m:
            e.zeitraum_von, e.zeitraum_bis = datum_iso(m.group(1)), datum_iso(m.group(2))
        m = erstes(m_cfg.get("betrag", []), text)
        if m:
            e.betrag = m.group(1)

    e.kategorie = kategorie_bestimmen(text, cfg)
    unterart, hinweis = unterart_bestimmen(text, cfg, e.kategorie)
    if unterart:
        e.kategorie = unterart
    if hinweis:
        hinweise.append(hinweis)
    elif e.kategorie in cfg["regeln"].get("verdacht_ts_bei", []):
        stichtage = {k["ab"]: k["text"] for k in kostensaetze_fuer(cfg, e.haus_id)}
        for von, _, _ in (e.abschnitte or [[e.zeitraum_von, None, None]]):
            if von in stichtage:
                hinweise.append(f"Verdacht: Abschnitt beginnt am {iso_zu_de(von)}, dem Stichtag von „{stichtage[von]}“ "
                                f"– evtl. Änderungsbescheid TS? Bitte Art prüfen")
                break

    if (cfg.get("_stammdaten") or {}).get("kostensaetze") and not kostensaetze_fuer(cfg, e.haus_id) and e.abschnitte:
        hinweise.append("Haus nicht erkannt – Kostensatz wird nicht gesetzt")

    stichwort = cfg["regeln"].get("erforderliches_stichwort")
    if stichwort and not re.search(stichwort, text, re.IGNORECASE):
        hinweise.append("Stichwort für BAB-Bescheid nicht gefunden – ist das ein BAB-Dokument?")
    e.hinweise = "; ".join(hinweise)
    return e


def _adresse_norm(t: str) -> str:
    t = t.lower().replace("ß", "ss")
    t = re.sub(r"stra(?:ss|s)e|str\.?(?=\s|\d|$)", "str ", t)
    return re.sub(r"[^\w]+", " ", t)


def _strassen_stamm(strasse: str) -> str | None:
    n = _adresse_norm(strasse)
    vor = re.split(r"\bstr\b|\d", n)[0].split()
    woerter = [w for w in (vor or n.split()) if len(w) >= 4 and not w.isdigit()]
    return max(woerter, key=len) if woerter else None


def haus_erkennen(text: str, haeuser: list[dict]) -> tuple[dict | None, str | None]:
    """Haus über Straße + PLZ im Bescheid finden (Hausanschrift steht als Empfänger/Anschrift drin)."""
    norm = _adresse_norm(text)
    treffer = []
    for h in haeuser:
        adresse = " ".join(str(h.get(k) or "") for k in ("strasse", "plz", "ort", "anschrift"))
        plz = re.search(r"\b\d{5}\b", adresse)
        stamm = _strassen_stamm(h.get("strasse") or h.get("anschrift") or "")
        if not (plz and stamm):
            continue
        for m in re.finditer(rf"\b{re.escape(stamm)}", norm):
            umgebung = norm[max(0, m.start() - 60): m.end() + 160]
            if re.search(rf"\b{plz.group(0)}\b", umgebung):
                treffer.append(h)
                break
    if len(treffer) == 1:
        return treffer[0], None
    if len(treffer) > 1:
        return None, "Haus nicht eindeutig: " + ", ".join(h.get("kuerzel") or h.get("name") for h in treffer)
    return None, None


def kostensaetze_fuer(cfg: dict, haus_id: int | None) -> list[dict]:
    """Kostensätze [{id, text, ab}] – aus den Ninox-Stammdaten (je Haus) oder aus der Regeldatei."""
    ks = (cfg.get("_stammdaten") or {}).get("kostensaetze") or []
    if ks:
        if haus_id is not None:
            return [k for k in ks if k.get("haus_id") == haus_id]
        return ks if len({k.get("haus_id") for k in ks}) <= 1 else []
    return [{"id": None, "text": k["name"], "ab": k["ab"]} for k in cfg.get("ninox", {}).get("kostensaetze", [])]


def stammdaten_lesen(ordner: Path) -> dict:
    p = ordner / "stammdaten.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


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
    # Strukturierte Ausgabe: Ollama erzwingt dieses JSON-Schema (nur lokal, nicht in der Ollama-Cloud)
    text_oder_null = {"type": ["string", "null"]}
    schema = {
        "type": "object",
        "properties": {
            "kundennummer": text_oder_null,
            "kategorie": {"type": ["string", "null"], "enum": kategorien + [None]},
            "bescheiddatum": text_oder_null, "zeitraum_von": text_oder_null,
            "zeitraum_bis": text_oder_null, "betrag": text_oder_null,
        },
        "required": ["kundennummer", "kategorie", "bescheiddatum", "zeitraum_von", "zeitraum_bis", "betrag"],
    }
    try:
        r = requests.post(
            k["url"].rstrip("/") + "/api/chat",
            json={"model": k["modell"], "messages": [{"role": "user", "content": prompt}],
                  "format": schema, "stream": False, "options": {"temperature": 0}},
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
    if wunsch.endswith(":cloud") or "-cloud" in wunsch:
        status += "\nACHTUNG: Cloud-Modell – Bescheidtexte würden an Ollama-Server gesendet. Für Sozialdaten nicht verwenden!"
    return f"Ollama erreichbar. Modell {wunsch}: {status}\nInstalliert: {', '.join(modelle) or '–'}"


# =========================================================================== Ninox

def kostensatz_waehlen(cfg: dict, beginn: str | None, haus_id: int | None = None) -> dict | None:
    """Neuester Kostensatz des Hauses, der zum Beginn des Abschnitts schon gilt -> {"_ref": id, "_text": Anzeige}."""
    if not beginn:
        return None
    beste = None
    for k in sorted(kostensaetze_fuer(cfg, haus_id), key=lambda k: k["ab"] or ""):
        if k["ab"] and k["ab"] <= beginn:
            beste = {"_ref": k["id"], "_text": k["text"]}
    return beste


def anzeige(v) -> str:
    return v["_text"] if isinstance(v, dict) and "_text" in v else str(v)


def ablage_plan(cfg: dict, e: "Ergebnis", dateiname: str, heute: str | None = None) -> dict:
    """Was in Ninox angelegt wird – ohne Verbindung. Wird für Ablage, Testlauf und Analyse genutzt."""
    n = cfg["ninox"]
    z = n.get("zuordnung", {}).get(e.kategorie or "", {})
    if not z or z.get("aktion") == "manuell":
        return {"aktion": "manuell", "grund": f"{e.kategorie or 'Unbekannte Art'}: keine automatische Ablage vorgesehen"}
    heute = heute or datetime.now().strftime("%Y-%m-%d")

    fb = n["bescheid"]["felder"]
    bescheid = {fb.get("datum"): e.bescheiddatum, fb.get("status"): z.get("status"), fb.get("notiz"): e.notiz or None}

    fa = n["abschnitte"]["felder"]
    zeilen = e.abschnitte or ([[e.zeitraum_von, e.zeitraum_bis, e.betrag]] if e.zeitraum_von and e.zeitraum_bis else [])
    abschnitte = []
    if z.get("abschnitte", True):
        for von, bis, betrag in zeilen:
            abschnitte.append({fa.get("beginn"): von, fa.get("ende"): bis,
                               fa.get("betrag"): betrag_zahl(betrag) if betrag else None,
                               fa.get("kostensatz"): kostensatz_waehlen(cfg, von, e.haus_id)})

    d = n["dokument"]
    fd = d["felder"]
    jahr, monat = (e.bescheiddatum or heute)[:4], (e.bescheiddatum or heute)[5:7]
    beschreibung = d.get("beschreibung", "{dokumentart} {monat}/{jahr2}").format(
        dokumentart=z.get("dokumentart", ""), kategorie=e.kategorie, monat=monat, jahr=jahr, jahr2=jahr[2:],
        datum=iso_zu_de(e.bescheiddatum), haus=e.haus or "")
    dokument = {fd.get("datum"): heute, fd.get("kategorie"): d.get("kategorie_wert", "Bescheid"),
                fd.get("art"): z.get("dokumentart"), fd.get("beschreibung"): beschreibung,
                fd.get("erstellt_von"): d.get("erstellt_von_wert") or None,
                fd.get("bringer"): d.get("bringer_wert") or None, fd.get("hinweise"): e.hinweise or None}

    ohne_leere = lambda f: {k: v for k, v in f.items() if k and v not in (None, "")}  # noqa: E731
    return {"aktion": "anlegen", "bescheid": ohne_leere(bescheid), "abschnitte": [ohne_leere(a) for a in abschnitte],
            "dokument": ohne_leere(dokument), "dateiname": dateiname, "antragsdatum": e.antragsdatum,
            "antragsart_hinweis": e.antragsart_hinweis}


def plan_text(plan: dict) -> str:
    if plan["aktion"] != "anlegen":
        return f"Keine automatische Ablage: {plan['grund']}"
    zeilen = [f"Antrag: vom {iso_zu_de(plan.get('antragsdatum')) or '?'} (sonst neuester Antrag)", "Bescheid:"]
    zeilen += [f"    {k}: {anzeige(v)}" for k, v in plan["bescheid"].items()]
    for i, a in enumerate(plan["abschnitte"], 1):
        zeilen += [f"  Abschnitt {i}: " + ", ".join(f"{k}={anzeige(v)}" for k, v in a.items())]
    zeilen += ["Dokument:"] + [f"    {k}: {anzeige(v)}" for k, v in plan["dokument"].items()]
    return "\n".join(zeilen)


class Ninox:
    def __init__(self, cfg: dict):
        n = cfg["ninox"]
        self.cfg = n
        self.wurzel = n["base_url"].rstrip("/")
        self.basis = f'{self.wurzel}/teams/{n["team_id"]}/databases/{n["datenbank_id"]}'
        self.s = requests.Session()
        self.s.headers["Authorization"] = f'Bearer {n["api_schluessel"]}'
        self._tabellen: list[dict] | None = None

    # ---------------------------------------------------------------- Schema
    def tabellen(self) -> list[dict]:
        if self._tabellen is None:
            r = self.s.get(f"{self.basis}/tables", timeout=20)
            r.raise_for_status()
            self._tabellen = r.json()
        return self._tabellen

    def tabelle(self, name: str) -> dict:
        for t in self.tabellen():
            if t.get("name") == name or t.get("id") == name:
                return t
        raise RuntimeError(f"Tabelle „{name}“ gibt es in Ninox nicht. Vorhanden: "
                           + ", ".join(t.get("name", "") for t in self.tabellen()))

    def schema_pruefen(self) -> list[str]:
        """Prüft, ob alle konfigurierten Tabellen und Felder existieren. Liefert Problem-Liste."""
        n, probleme = self.cfg, []
        pruefliste = [
            (n["bewohner"]["tabelle"], [n["bewohner"].get("feld_kundennummer"), n["bewohner"].get("feld_geburtsdatum")]),
            (n["bescheid"]["tabelle"], [n["bescheid"].get("feld_eltern"), *n["bescheid"]["felder"].values()]),
            (n["abschnitte"]["tabelle"], [n["abschnitte"].get("feld_eltern"), *n["abschnitte"]["felder"].values()]),
            (n["dokument"]["tabelle"], [n["dokument"].get("feld_eltern"), *n["dokument"]["felder"].values()]),
        ]
        a = n.get("antrag", {})
        if a.get("tabelle"):
            pruefliste.insert(1, (a["tabelle"], [a.get("feld_bewohner"), a.get("feld_antragsdatum"), a.get("feld_antragsart")]))
        st = n.get("stammdaten", {})
        if st.get("haeuser_tabelle"):
            pruefliste.append((st["haeuser_tabelle"], list(st.get("haeuser_felder", {}).values())))
        if st.get("kostensaetze_tabelle"):
            pruefliste.append((st["kostensaetze_tabelle"], list(st.get("kostensaetze_felder", {}).values())))
        for tab, felder in pruefliste:
            try:
                t = self.tabelle(tab)
            except RuntimeError as ex:
                probleme.append(str(ex))
                continue
            vorhanden = {f.get("name") for f in t.get("fields", [])}
            for f in felder:
                if f and f not in vorhanden:
                    probleme.append(f"Feld „{f}“ fehlt in Tabelle „{tab}“")
        return probleme

    def verbindung_testen(self) -> str:
        n = self.cfg
        if not n.get("team_id"):
            r = self.s.get(f"{self.wurzel}/teams", timeout=20); r.raise_for_status()
            return "Verbindung ok. Teams (ID eintragen):\n" + "\n".join(f'  {t["id"]}  {t["name"]}' for t in r.json())
        if not n.get("datenbank_id"):
            r = self.s.get(f'{self.wurzel}/teams/{n["team_id"]}/databases', timeout=20); r.raise_for_status()
            return "Verbindung ok. Datenbanken (ID eintragen):\n" + "\n".join(f'  {d["id"]}  {d["name"]}' for d in r.json())
        zeilen = ["Verbindung ok.", ""]
        probleme = self.schema_pruefen()
        zeilen += (["PROBLEME – bitte in „eigene_regeln.toml“ die richtigen Namen eintragen:"] + [f"  ✗ {p}" for p in probleme]
                   if probleme else ["✔ Alle Tabellen und Felder aus der Regeldatei sind vorhanden."])
        zeilen += ["", "Tabellen und Felder in Ninox:"]
        for t in self.tabellen():
            zeilen.append(f'  [{t["id"]}] {t["name"]}')
            for f in t.get("fields", []):
                zeilen.append(f'        {f.get("name")}  ({f.get("type", "")})')
        return "\n".join(zeilen)

    # ---------------------------------------------------------------- Abfragen
    def _query(self, skript: str):
        r = self.s.post(f"{self.basis}/query", json={"query": skript}, timeout=30)
        r.raise_for_status()
        return r.json()

    def bewohner_finden(self, kundennummer: str | None = None, geburtsdatum: str | None = None) -> list[dict]:
        """Sucht über die Kundennummer oder (Fallback) über das Geburtsdatum (ISO)."""
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

    def antraege(self, bewohner_id: int) -> list[dict]:
        a = self.cfg["antrag"]
        f_dat, f_art = a.get("feld_antragsdatum") or "", a.get("feld_antragsart") or ""
        teile = ["text(number(Id))", f"format('{f_dat}', \"YYYY-MM-DD\")" if f_dat else '""',
                 f"text('{f_art}')" if f_art else '""']
        skript = (f"join((select '{a['tabelle']}' where number('{a['feld_bewohner']}') = {bewohner_id}).("
                  + ' + "|" + '.join(teile) + "), \"\\n\")")
        liste = []
        for zeile in str(self._query(skript) or "").splitlines():
            if zeile.count("|") >= 2:
                rid, datum, art = zeile.split("|", 2)
                liste.append({"id": int(float(rid)), "datum": datum.strip(), "art": art.strip()})
        return liste

    def eltern_finden(self, bewohner_id: int, antragsdatum: str | None = None) -> tuple[int, dict, list[str]]:
        """Antrag, an den der Bescheid gehängt wird: der mit dem Antragsdatum aus dem Bescheid (±Toleranz),
        sonst – wenn es nur einen gibt – dieser. Liefert (id, antrag, hinweise)."""
        a = self.cfg.get("antrag", {})
        if not a.get("tabelle"):
            return bewohner_id, {}, []
        liste = self.antraege(bewohner_id)
        if not liste:
            raise PruefFall("für diese Person gibt es in Ninox keinen Antrag – bitte erst den Antrag anlegen")
        toleranz = int(a.get("toleranz_tage", 14))
        if antragsdatum and a.get("feld_antragsdatum"):
            ziel = datetime.strptime(antragsdatum, "%Y-%m-%d")
            mit_abstand = []
            for x in liste:
                try:
                    mit_abstand.append((abs((datetime.strptime(x["datum"], "%Y-%m-%d") - ziel).days), -x["id"], x))
                except ValueError:
                    pass
            passend = sorted(t for t in mit_abstand if t[0] <= toleranz)
            if passend:
                tage, _, x = passend[0]
                return x["id"], x, ([f"Antrag in Ninox vom {iso_zu_de(x['datum'])}, im Bescheid {iso_zu_de(antragsdatum)}"]
                                    if tage else [])
            if len(liste) == 1:
                x = liste[0]
                return x["id"], x, [f"Antrag vom {iso_zu_de(antragsdatum)} nicht gefunden – an den einzigen Antrag "
                                    f"(vom {iso_zu_de(x['datum']) or '?'}) gehängt"]
            raise PruefFall(f"Antrag vom {iso_zu_de(antragsdatum)} nicht in Ninox gefunden (vorhanden: "
                            + ", ".join(iso_zu_de(x["datum"]) or "ohne Datum" for x in liste) + ")")
        x = max(liste, key=lambda x: x["id"])
        hinweis = [] if len(liste) == 1 else ["Antragsdatum nicht erkannt – an den neuesten Antrag gehängt"]
        return x["id"], x, hinweis

    # ---------------------------------------------------------------- Stammdaten
    def _datensaetze(self, tabelle: str) -> list[dict]:
        tid = self.tabelle(tabelle)["id"]
        alle_, seite = [], 0
        while True:
            r = self.s.get(f"{self.basis}/tables/{tid}/records", params={"perPage": 500, "page": seite}, timeout=30)
            r.raise_for_status()
            teil = r.json()
            alle_ += teil
            if len(teil) < 500:
                return alle_
            seite += 1

    def stammdaten_laden(self, ordner: Path) -> dict:
        """Häuser (mit Anschrift) und Kostensätze aus Ninox holen und lokal speichern."""
        st = self.cfg.get("stammdaten", {})
        daten = {"geladen": datetime.now().strftime("%Y-%m-%d %H:%M"), "haeuser": [], "kostensaetze": []}
        wert = lambda f, k: f.get(k) if k else None  # noqa: E731
        if st.get("haeuser_tabelle"):
            hf = st.get("haeuser_felder", {})
            for r in self._datensaetze(st["haeuser_tabelle"]):
                f = r.get("fields", {})
                daten["haeuser"].append({"id": r["id"], **{k: wert(f, hf.get(k)) for k in
                                                           ("name", "kuerzel", "strasse", "plz", "ort", "anschrift")}})
        if st.get("kostensaetze_tabelle"):
            kf = st.get("kostensaetze_felder", {})
            for r in self._datensaetze(st["kostensaetze_tabelle"]):
                f = r.get("fields", {})
                haus = wert(f, kf.get("haus"))
                if isinstance(haus, dict):
                    haus = haus.get("id")
                daten["kostensaetze"].append({"id": r["id"], "text": str(wert(f, kf.get("bezeichnung")) or r["id"]),
                                              "ab": datum_iso(str(wert(f, kf.get("ab")) or "")[:10]),
                                              "haus_id": int(haus) if haus not in (None, "") else None})
        (ordner / "stammdaten.json").write_text(json.dumps(daten, ensure_ascii=False, indent=2), encoding="utf-8")
        return daten

    def bescheid_vorhanden(self, eltern_id: int, datum: str) -> bool:
        b = self.cfg["bescheid"]
        j, m, t = datum.split("-")
        antwort = self._query(f"cnt(select '{b['tabelle']}' where number('{b['feld_eltern']}') = {eltern_id} "
                              f"and '{b['felder']['datum']}' = date({int(j)}, {int(m)}, {int(t)}))")
        try:
            return int(float(antwort)) > 0
        except (TypeError, ValueError):
            return False

    # ---------------------------------------------------------------- Schreiben
    def _werte_aufloesen(self, tabelle: str, fields: dict) -> dict:
        """Kostensatz o. Ä.: bei Auswahl-/Textfeldern den Anzeigetext, bei Verknüpfungen die Datensatz-ID."""
        typen = {f.get("name"): (f.get("type") or "").lower() for f in self.tabelle(tabelle).get("fields", [])}
        erg = {}
        for k, v in fields.items():
            if isinstance(v, dict) and "_text" in v:
                text_typ = typen.get(k, "choice") in ("choice", "multi", "string", "text", "") or v.get("_ref") is None
                v = v["_text"] if text_typ else v["_ref"]
            erg[k] = v
        return erg

    def _anlegen(self, tabelle: str, fields: dict) -> int:
        fields = self._werte_aufloesen(tabelle, fields)
        tid = self.tabelle(tabelle)["id"]
        r = self.s.post(f"{self.basis}/tables/{tid}/records", json=[{"fields": fields}], timeout=30)
        r.raise_for_status()
        return r.json()[0]["id"]

    def _loeschen(self, tabelle: str, rid: int) -> None:
        try:
            tid = self.tabelle(tabelle)["id"]
            self.s.delete(f"{self.basis}/tables/{tid}/records/{rid}", timeout=30)
        except Exception:
            log.warning("Konnte Datensatz %s in %s nicht zurücknehmen – bitte in Ninox prüfen", rid, tabelle)

    def ablegen(self, bewohner_id: int, plan: dict, pdf: Path) -> tuple[int, list[str]]:
        """Legt Bescheid, Abschnitte und Dokument an und lädt die PDF hoch. Bei Fehlern wird zurückgenommen.
        Liefert (Bescheid-ID, Hinweise)."""
        n = self.cfg
        eltern_id, antrag, hinweise = self.eltern_finden(bewohner_id, plan.get("antragsdatum"))
        if plan.get("antragsart_hinweis") and antrag.get("art") and plan["antragsart_hinweis"] != antrag["art"]:
            hinweise.append(f"Bescheid deutet auf „{plan['antragsart_hinweis']}“, Antrag in Ninox ist „{antrag['art']}“")
        datum = plan["bescheid"].get(n["bescheid"]["felder"]["datum"])
        if datum and self.bescheid_vorhanden(eltern_id, datum):
            raise PruefFall(f"Ein Bescheid vom {iso_zu_de(datum)} ist bei dieser Person in Ninox schon vorhanden")

        angelegt: list[tuple[str, int]] = []
        try:
            bid = self._anlegen(n["bescheid"]["tabelle"], {n["bescheid"]["feld_eltern"]: eltern_id, **plan["bescheid"]})
            angelegt.append((n["bescheid"]["tabelle"], bid))
            for a in plan["abschnitte"]:
                aid = self._anlegen(n["abschnitte"]["tabelle"], {n["abschnitte"]["feld_eltern"]: bid, **a})
                angelegt.append((n["abschnitte"]["tabelle"], aid))
            d = n["dokument"]
            did = self._anlegen(d["tabelle"], {d["feld_eltern"]: bid, **plan["dokument"]})
            angelegt.append((d["tabelle"], did))
            tid = self.tabelle(d["tabelle"])["id"]
            with open(pdf, "rb") as fh:
                r = self.s.post(f"{self.basis}/tables/{tid}/records/{did}/files",
                                files={"file": (plan["dateiname"], fh, "application/pdf")}, timeout=120)
            r.raise_for_status()
            if d["felder"].get("datei"):
                r = self.s.post(f"{self.basis}/tables/{tid}/records",
                                json=[{"id": did, "fields": {d["felder"]["datei"]: plan["dateiname"]}}], timeout=30)
                r.raise_for_status()
            return bid, hinweise
        except Exception:
            for tabelle, rid in reversed(angelegt):
                self._loeschen(tabelle, rid)
            raise


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
        # Stammdaten werden NICHT hier geladen (würde den Programmstart blockieren),
        # sondern über stammdaten_auffrischen() im Hintergrund bzw. im Überwachungslauf.

    def stammdaten_auffrischen(self) -> None:
        """Einmal täglich Häuser/Kostensätze aus Ninox holen. Fehler -> gespeicherter Stand bleibt."""
        cfg = self.cfg
        if not (self.ninox and cfg["ninox"].get("stammdaten", {}).get("haeuser_tabelle")):
            return
        if (cfg.get("_stammdaten") or {}).get("geladen", "")[:10] == datetime.now().strftime("%Y-%m-%d"):
            return
        try:
            cfg["_stammdaten"] = self.ninox.stammdaten_laden(Path(cfg.get("_datenordner") or Path(cfg["ordner"]["protokoll"]).parent))
            log.info("Stammdaten aus Ninox geladen: %d Häuser, %d Kostensätze",
                     len(cfg["_stammdaten"]["haeuser"]), len(cfg["_stammdaten"]["kostensaetze"]))
        except Exception as ex:
            cfg.setdefault("_stammdaten", {})["geladen"] = datetime.now().strftime("%Y-%m-%d") + " (fehlgeschlagen)"
            log.warning("Stammdaten konnten nicht geladen werden (%s) – nutze gespeicherten Stand", ex)

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
                if any(vorgaben.get(f) and vorgaben.get(f) != getattr(e, f) for f in ("zeitraum_von", "zeitraum_bis", "betrag")):
                    e.abschnitte = []
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
            if ("nicht eindeutig" in e.hinweise or "Verdacht:" in e.hinweise) and not vorgaben:
                raise PruefFall(e.hinweise)

            bewohner_id, ninox_name = None, None
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
                bewohner_id, ninox_name = t["id"], t["name"]

            dateiname = sicherer_dateiname(self.cfg["dateiname"]["schema"].format(
                name=re.sub(r",\s*", "_", e.name) if e.name else (ninox_name or "ohne-Name"),
                ninox_name=ninox_name or "ohne-Ninox", haus=e.haus or "ohne-Haus", kategorie=e.kategorie, datum=e.bescheiddatum or "ohne-Datum",
                kundennummer=e.kundennummer or "ohne-Kdnr", geburtsdatum=e.geburtsdatum or "ohne-Geburtsdatum",
                von=e.zeitraum_von or "", bis=e.zeitraum_bis or ""))
            if not dateiname.lower().endswith(".pdf"):
                dateiname += ".pdf"

            plan = ablage_plan(self.cfg, e, dateiname)
            if plan["aktion"] != "anlegen":
                raise PruefFall(plan["grund"] + " – bitte manuell in Ninox ablegen")
            if self.ninox:
                rid, ablage_hinweise = self.ninox.ablegen(bewohner_id, plan, ocr_pdf)
                if ablage_hinweise:
                    e.hinweise = "; ".join([h for h in [e.hinweise, *ablage_hinweise] if h])
                log.info("In Ninox abgelegt (Bescheid-Datensatz %s)", rid)
            else:
                log.info("Testlauf – würde in Ninox anlegen:\n%s", plan_text(plan))

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
                 "zeitraeume": e.zeitraeume, "notiz": e.notiz, "antragsart_hinweis": e.antragsart_hinweis,
                 "hinweise": e.hinweise, "quelle": e.quelle}
        if self.protokoll.exists():
            with open(self.protokoll, encoding="utf-8-sig", newline="") as fh:
                kopf = next(csv.reader(fh, delimiter=";"), [])
            if kopf != list(zeile):  # neue Programmversion mit anderen Spalten
                bisherige = protokoll_lesen(self.protokoll, anzahl=10**9)
                self.protokoll.rename(self.protokoll.with_name(f"protokoll-bis-{datetime.now():%Y%m%d-%H%M%S}.csv"))
                protokoll_schreiben(self.protokoll, bisherige, list(zeile))
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
        self.stammdaten_auffrischen()
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


def protokoll_schreiben(pfad: Path, zeilen: list[dict], spalten: list[str] | None = None) -> None:
    spalten = spalten or (list(zeilen[0]) if zeilen else [])
    tmp = pfad.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=spalten, delimiter=";", extrasaction="ignore")
        w.writeheader()
        for z in zeilen:
            w.writerow({k: z.get(k, "") for k in spalten})
    os.replace(tmp, pfad)


def _schluessel(z: dict) -> tuple:
    return tuple(str(z.get(k) or "") for k in ("zeit", "eingang", "ergebnis", "status"))


def protokoll_entfernen(pfad: Path, weg: list[dict]) -> None:
    """Entfernt einzelne Einträge aus der Verarbeitungsliste (Dateien und Ninox bleiben unberührt)."""
    if not pfad.exists():
        return
    with open(pfad, encoding="utf-8-sig", newline="") as fh:
        r = csv.DictReader(fh, delimiter=";")
        spalten, zeilen = r.fieldnames or [], list(r)
    raus = {_schluessel(z) for z in weg}
    protokoll_schreiben(pfad, [z for z in zeilen if _schluessel(z) not in raus], spalten)


def protokoll_leeren(pfad: Path) -> None:
    """Leert die Verarbeitungsliste; die bisherige wird als Sicherung umbenannt."""
    if pfad.exists():
        pfad.rename(pfad.with_name(f"protokoll-geleert-{datetime.now():%Y%m%d-%H%M%S}.csv"))


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
