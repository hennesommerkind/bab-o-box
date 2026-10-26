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

import bab_edition  # noqa: E402

LIGHT = bab_edition.EDITION == "light"
APP_NAME = "BAB-O-BOX Light" if LIGHT else "BAB-O-BOX"          # Datenordner unter %APPDATA% und Autostart-Name
ANZEIGE_NAME = "BAB-O-BOX Light" if LIGHT else "BAB-O-BOX Pro"
ALTER_APP_NAME = "BAB-Eingang"
FELDER = ["name", "kundennummer", "kategorie", "bescheiddatum", "zeitraum_von", "zeitraum_bis", "betrag", "geburtsdatum",
          "antragsdatum", "haus"]
# Light erfasst bewusst nur diese Angaben (kein Geburtsdatum, keine Beträge/Zeiträume)
FELDER_LIGHT = ["kundennummer", "name", "haus", "kategorie", "bescheiddatum"]
# Light: weitere Angaben der Pro-Erkennung, nur zum Nachsehen (kein Geburtsdatum)
ZUSATZ_FELDER = {"antragsdatum": "Antrag gestellt am", "status": "Status", "zeitraum_von": "Bewilligt von",
                 "zeitraum_bis": "Bewilligt bis", "betrag": "BAB monatlich (aktuell)", "zeitraeume": "Bewilligungsabschnitte",
                 "kostensatz_info": "Kostensatz (aus Bedarf für den Lebensunterhalt)", "notiz": "Notiz",
                 "antragsart_hinweis": "Text deutet auf Antragsart", "hinweise": "Hinweise"}
FELD_TITEL = {"kundennummer": "Kundennummer", "name": "Name", "haus": "Haus", "kategorie": "Art",
              "bescheiddatum": "Bescheiddatum"}
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
    if not LIGHT and not p.exists() and alt.is_dir():  # Umbenennung BAB-Eingang -> BAB-O-BOX: Daten mitnehmen
        try:
            shutil.copytree(alt, p)
        except Exception:
            pass
    p.mkdir(parents=True, exist_ok=True)
    pro_stamm = basis / "BAB-O-BOX" / "stammdaten.json"
    if LIGHT and not (p / "stammdaten.json").exists() and pro_stamm.exists():  # Häuser/Kostensätze aus Pro übernehmen
        try:
            shutil.copy2(pro_stamm, p / "stammdaten.json")
        except Exception:
            pass
    return p


def standard_einstellungen() -> dict:
    basis = Path.home() / "Documents" / ("BAB-Light" if LIGHT else "BAB")
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
        "mitarbeitende": "",
        "beschreibung": {"vorsatz": "BAB", "art": True, "art_lang": True, "datum": True, "zeitraum": False,
                         "haus": False, "name": False, "kundennummer": False, "betrag": False,
                         "datumsformat": "MM/JJ"},
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
    status: str | None = None  # überschreibt den Status aus der Zuordnung (z. B. vorläufige Bewilligung)
    lebensunterhalt: list = field(default_factory=list)  # [[von, bis, "2.565,54"], ...] aus dem Berechnungsbogen
    kostensatz_info: str = ""  # Ergebnis des Kostensatz-Abgleichs (Anzeige)
    ks_treffer: dict = field(default_factory=dict)  # {abschnitt_von: {id, text, variante, haus_id, ab}}
    ks_ohne: list = field(default_factory=list)  # Abschnitte, in denen laut Bescheid kein Wohnheim-KS gilt
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


_EXPLORER_PS = r"""
param([string]$ordner, [string]$name)
$sh = New-Object -ComObject Shell.Application
foreach ($w in $sh.Windows()) {
  try { $p = $w.Document.Folder.Self.Path } catch { continue }
  if ($p -and ($p.TrimEnd('\') -ieq $ordner.TrimEnd('\'))) {
    try { $w.Document.Folder.Items() | Out-Null; $item = $w.Document.Folder.ParseName($name); if ($item) { $w.Document.SelectItem($item, 29) } } catch {}
    Write-Output $w.HWND
    exit 0
  }
}
exit 1
"""


def im_explorer_zeigen(datei: Path) -> None:
    """Zielordner zeigen und die Datei markieren. Ist der Ordner im Explorer schon offen, wird dieses Fenster
    nach vorn geholt (statt ein weiteres zu öffnen)."""
    if os.name != "nt":
        ordner_oeffnen(datei.parent)
        return
    import ctypes
    hwnd = None
    try:
        skript = Path(tempfile.gettempdir()) / "bab_o_box_explorer.ps1"
        skript.write_text(_EXPLORER_PS, encoding="utf-8-sig")
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                            str(skript), str(datei.parent), datei.name],
                           capture_output=True, text=True, timeout=20, creationflags=OHNE_FENSTER)
        zahl = (r.stdout or "").strip().splitlines()
        hwnd = int(zahl[-1]) if r.returncode == 0 and zahl and zahl[-1].strip().lstrip("-").isdigit() else None
    except Exception as ex:
        log.info("Explorer-Fenster nicht gefunden (%s)", ex)
    if hwnd:
        u = ctypes.windll.user32
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, 9)  # SW_RESTORE
        u.keybd_event(0x12, 0, 0, 0)  # Alt kurz drücken: erlaubt SetForegroundWindow
        u.SetForegroundWindow(hwnd)
        u.keybd_event(0x12, 0, 2, 0)
        return
    subprocess.Popen(["explorer", "/select,", str(datei)])


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
        nur_text = o.get("ausgabe", "original") == "original"
        befehl = [exe, str(liste), str(tmp / "aus"), "-l", o.get("sprache", "deu"), "--dpi", str(dpi)]
        if nur_text:
            befehl += ["-c", "textonly_pdf=1"]
        befehl += ["pdf", "txt"]
        r = subprocess.run(befehl, capture_output=True, text=True, timeout=o.get("timeout_s", 300),
                           env=tesseract_umgebung(exe), creationflags=OHNE_FENSTER)
        if r.returncode != 0 or not (tmp / "aus.pdf").exists():
            raise RuntimeError(f"OCR fehlgeschlagen: {r.stderr.strip()[-400:]}")
        if nur_text:
            try:
                textebene_einfuegen(quelle, tmp / "aus.pdf", ziel)
            except Exception as ex:  # z. B. beschädigtes PDF -> Bilder-PDF von Tesseract (größer, aber vollständig)
                log.warning("Textebene konnte nicht ins Original eingefügt werden (%s) – nehme OCR-PDF", ex)
                r = subprocess.run([a for a in befehl if a not in ("-c", "textonly_pdf=1")], capture_output=True,
                                   text=True, timeout=o.get("timeout_s", 300), env=tesseract_umgebung(exe),
                                   creationflags=OHNE_FENSTER)
                shutil.move(tmp / "aus.pdf", ziel)
        else:
            shutil.move(tmp / "aus.pdf", ziel)
        return text_normalisieren((tmp / "aus.txt").read_text(encoding="utf-8", errors="replace"))


def textebene_einfuegen(original: Path, nur_text_pdf: Path, ziel: Path) -> None:
    """Legt die unsichtbare Textebene von Tesseract seitenweise über das Original-PDF.
    So bleibt das Scan-Bild unverändert (gleiche Dateigröße) und das PDF ist trotzdem durchsuchbar."""
    from pypdf import PdfWriter, Transformation
    text = PdfReader(str(nur_text_pdf))
    w = PdfWriter(clone_from=str(original))
    if len(text.pages) != len(w.pages):
        raise ValueError(f"Seitenzahl passt nicht ({len(text.pages)} statt {len(w.pages)})")
    for seite, tseite in zip(w.pages, text.pages):
        if seite.rotation:
            seite.transfer_rotation_to_content()
        box = seite.cropbox
        sx = float(box.width) / float(tseite.mediabox.width)
        sy = float(box.height) / float(tseite.mediabox.height)
        seite.merge_transformed_page(tseite, Transformation().scale(sx, sy).translate(float(box.left), float(box.bottom)))
    tmp = ziel.with_name(ziel.stem + "_tmp.pdf")
    with open(tmp, "wb") as fh:
        w.write(fh)
    os.replace(tmp, ziel)


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


B = r"[^\W\d_]"  # ein Buchstabe – auch Ş, ı, ğ, ł, ć, é usw.
WORT = rf"{B}(?:{B}|['’\-.])*"  # Namensteil: Buchstaben, Apostroph, Bindestrich
NAME_GUELTIG = re.compile(rf"^{WORT}(?:[ ,]+{WORT}){{0,4}}$")
ANREDE = re.compile(r"^(?:Herrn?|Frau|Fr\.|Hr\.)\s+", re.IGNORECASE)
HAUS_HINWEIS = re.compile(r"Kolping|Jugendwohnen|Wohnheim", re.IGNORECASE)
CO_HINWEIS = re.compile(r"\bc/o\b|\bc/0\b", re.IGNORECASE)  # "c/o Kolping" = Bewohner*in; "z. Hd." = Mitarbeiter*in


def _name_form(roh: str) -> tuple[str, str] | None:
    """'Mustermann, Max' oder 'Max Mustermann' -> (Nachname, Vorname). Nur Nachname -> (Nachname, '')."""
    roh = ANREDE.sub("", re.sub(r"\s+", " ", roh)).strip(" ,.;:")
    if not NAME_GUELTIG.match(roh):
        return None
    if "," in roh:
        nach, vor = [t.strip() for t in roh.split(",", 1)]
        return (nach, vor) if nach else None
    teile = roh.split(" ")
    return (teile[-1], " ".join(teile[:-1]))


def _gesperrt(name: str, sperrworte: list[str]) -> bool:
    return any(w and w in name.lower() for w in sperrworte)


def _an_haus_gerichtet(kopf: str, nachname: str) -> bool:
    """True, wenn die Person mit diesem Nachnamen im Anschriftenfeld als Ansprechperson DES HAUSES steht
    (z. B. 'Kolping Jugendwohnen' und darunter 'Frau Lea Guse', oder 'Lea Guse Kolping Jugendwohnen') –
    nicht als Bewohner*in mit 'c/o Kolping'."""
    zeilen = [z.strip() for z in kopf.splitlines()]
    name_i = [i for i, z in enumerate(zeilen) if re.search(rf"\b{re.escape(nachname)}\b", z)
              and not re.search(r"Guten\s+Tag|Sehr\s+geehrt|Hallo", z, re.IGNORECASE)]
    haus_i = [i for i, z in enumerate(zeilen) if HAUS_HINWEIS.search(z)]
    for i in name_i:
        z = zeilen[i]
        if HAUS_HINWEIS.search(z):  # Name und Haus in derselben Zeile
            if CO_HINWEIS.search(z):
                return False  # "Max Mustermann c/o Kolping" -> Bewohner*in
            return True       # "Lea Guse Kolping Jugendwohnen" -> Ansprechperson
        if any(0 < i - h <= 5 for h in haus_i) and not CO_HINWEIS.search(z):  # Haus darüber -> Ansprechperson
            return True
        if re.search(r"z\.?\s*H(?:d|än)", z, re.IGNORECASE):  # "z. Hd. Frau Guse" -> Ansprechperson
            return True
    return False


def name_lesen(text: str, cfg: dict) -> str | None:
    """Name der/des Auszubildenden als 'Nachname, Vorname'. Unsicher -> None (nie raten).
    1. beschriftete Stellen ("Name, Vorname:", "Bescheid für … geb. am", "Kundenname" …)
    2. Anrede "Guten Tag …" und Anschriftenfeld – aber nur, wenn der Brief nicht an eine Ansprechperson
       des Hauses gerichtet ist (Abtretung)."""
    sperrworte = [w.strip().lower() for w in cfg["regeln"].get("name_sperrworte", []) if w.strip()]
    sperrworte += [w.strip().lower() for w in re.split(r"[,;\n]", str(cfg.get("mitarbeitende") or "")) if len(w.strip()) > 2]
    for m in alle(cfg["muster"].get("name", []), text):
        f = _name_form(m.group(1))
        if f and f[1] and not _gesperrt(f"{f[0]} {f[1]}", sperrworte):
            return f"{f[0]}, {f[1]}"

    kopf = text[: cfg["regeln"].get("anschrift_bereich_zeichen", 1500)]
    for m in alle(cfg["muster"].get("name_anrede", []), text):
        roh = m.group(1)
        if HAUS_HINWEIS.search(roh) or _gesperrt(roh, sperrworte):
            continue
        f = _name_form(roh)
        if not f or _an_haus_gerichtet(kopf, f[0]):
            continue
        nach, vor = f
        if not vor:  # "Guten Tag Herr Mustermann" -> Vornamen im Anschriftenfeld suchen
            v = re.search(rf"(?:^|\n)\s*(?:Herrn?\s+|Frau\s+)?({WORT}(?:\s+{WORT})?)\s+{re.escape(nach)}\b", kopf)
            if v and not ANREDE.match(v.group(1) + " ") and v.group(1).lower() not in ("herrn", "herr", "frau"):
                vor = v.group(1)
        return f"{nach}, {vor}" if vor else nach
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

    # Geburtsdatum (beschriftet; sonst erstes Datum, das mind. 13 Jahre zurückliegt)
    m = erstes(m_cfg.get("geburtsdatum", []), text)
    if m:
        e.geburtsdatum = datum_iso(m.group(1))
    if not e.geburtsdatum:
        grenze = datetime.now().year - 13
        for m in re.finditer(rf"\b(\d{{1,2}}\.\d{{1,2}}\.\d{{4}}|{DATUM_LANG_RE})", text):
            d = datum_iso(m.group(1))
            if d and 1950 <= int(d[:4]) <= grenze:
                e.geburtsdatum = d
                break

    # Mehrere Personen in einem Scan? (verschiedene Kundennummern – Lesefehler 'D'→'0' eingerechnet)
    kerne = {}
    for m in re.finditer(r"(?<![A-Z0-9])(\d{3})\s?([A-Z]|0)\s?(\d{6})(?![A-Z0-9])", text):
        kerne.setdefault(m.group(1) + m.group(3), set()).add(m.group(0))
    if len(kerne) > 1:
        hinweise.append("Kundennummer nicht eindeutig – evtl. mehrere Personen im Scan: " +
                        ", ".join(sorted(min(v) for v in kerne.values())))

    # Status-Regeln (z. B. vorläufige Entscheidung nach § 328 SGB III)
    for regel in cfg.get("status_regeln", []):
        if erstes(regel["muster"], text):
            e.status = regel["status"]
            frist = re.search(regel.get("notiz_frist", "$^"), text, re.IGNORECASE | re.DOTALL)
            zusatz = f"{regel['name'].capitalize()} bewilligt" + (f" – Unterlagen bis {frist.group(1)} nachreichen" if frist else "")
            e.notiz = "; ".join(x for x in (e.notiz, zusatz) if x)
            break

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
        aus = [x.lower() for x in cfg["regeln"].get("haeuser_ausschliessen", [])]
        kandidaten_h = [h for h in stammdaten["haeuser"]
                        if not any(a in str(h.get(k) or "").lower() for a in aus for k in ("name", "kuerzel"))]
        haus, hinweis = haus_erkennen(text, kandidaten_h)
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

    # Kostensatz aus "Bedarf für den Lebensunterhalt" (= Kostensatz + Pauschale für sonstige Bedürfnisse)
    e.lebensunterhalt = lebensunterhalt_lesen(text)
    ts_beleg = kostensatz_abgleich(e, cfg, hinweise)
    ts_name = next((u["name"] for u in cfg.get("unterarten", []) if u.get("basis") == e.kategorie
                    and re.search(r"\bTS\b", u["name"])), None)
    if ts_beleg and ts_name and not unterart and not hinweis:
        e.kategorie = ts_name
        hinweise.append(f"Art aus Kostensatz abgeleitet: {ts_beleg}")

    if hinweis:
        hinweise.append(hinweis)
    elif ts_beleg or e.ks_treffer:
        pass  # Kostensatz belegt -> kein bloßer Verdacht nötig
    elif e.kategorie in cfg["regeln"].get("verdacht_ts_bei", []):
        stichtage = {k["ab"]: k["text"] for k in kostensaetze_fuer(cfg, e.haus_id)}
        for von, _, _ in (e.abschnitte or [[e.zeitraum_von, None, None]]):
            if von in stichtage:
                hinweise.append(f"Verdacht: Abschnitt beginnt am {iso_zu_de(von)}, dem Stichtag von „{stichtage[von]}“ "
                                f"– evtl. Änderungsbescheid TS? Bitte Art prüfen")
                break

    for von, _, _ in (e.abschnitte if e.haus_id is not None else []):
        k = kostensatz_kandidaten(cfg, von, e.haus_id, e.geburtsdatum)
        if len(k) > 1 and von not in e.ks_treffer and von not in e.ks_ohne:
            hinweise.append(f"Kostensatz ab {iso_zu_de(von)} nicht eindeutig ({', '.join(x['text'] for x in k)}) – "
                            "bitte in Ninox wählen")
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


def _strassen_phrase(strasse: str) -> str | None:
    """'Frankfurter Str. 3' -> 'frankfurter str' (ohne Hausnummer), 'Dellplatz' -> 'dellplatz'."""
    n = _adresse_norm(strasse)
    n = re.split(r"\s\d", " " + n)[0] if re.search(r"\d", n) else n
    n = " ".join(n.split())
    return n if len(n) >= 6 else None


def haus_erkennen(text: str, haeuser: list[dict]) -> tuple[dict | None, str | None]:
    """Haus finden, in drei Stufen (die stärkste eindeutige gewinnt):
    1. Straße + PLZ nah beieinander, 2. Name 'Kolping Jugendwohnen <Haus>', 3. Straße allein."""
    norm = _adresse_norm(text)
    stufen: list[list[dict]] = [[], [], []]
    for h in haeuser:
        adresse = " ".join(str(h.get(k) or "") for k in ("strasse", "plz", "ort", "anschrift"))
        plz = re.search(r"\b\d{5}\b", adresse)
        phrase = _strassen_phrase(h.get("strasse") or h.get("anschrift") or "")
        stamm = _strassen_stamm(h.get("strasse") or h.get("anschrift") or "")
        if plz and stamm:
            for m in re.finditer(rf"\b{re.escape(stamm)}", norm):
                if re.search(rf"\b{plz.group(0)}\b", norm[max(0, m.start() - 60): m.end() + 160]):
                    stufen[0].append(h)
                    break
        namen = {_adresse_norm(re.sub(r"\b(gGmbH|e\.\s?V\.?|GmbH)\b", "", str(h.get("name") or ""))).strip()}
        if h.get("kuerzel"):
            namen.add("jugendwohnen " + _adresse_norm(h["kuerzel"]).strip())
        if any(len(n) > 14 and re.search(rf"\b{re.escape(n)}\b(?! (mitte|ehrenfeld|frohnhausen|st georg|eimsb))", norm)
               for n in namen if n):
            stufen[1].append(h)
        if phrase and re.search(rf"\b{re.escape(phrase)}\b", norm):
            stufen[2].append(h)
    for treffer in stufen:
        if len(treffer) == 1:
            return treffer[0], None
        if len(treffer) > 1:
            return None, "Haus nicht eindeutig: " + ", ".join(h.get("kuerzel") or h.get("name") for h in treffer)
    return None, None


BETRAG_IN_ZEILE = re.compile(r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*€")


def _zahl(betrag: str) -> float:
    return float(betrag.replace(".", "").replace(",", "."))


def _euro(wert: float) -> str:
    return f"{wert:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def lebensunterhalt_lesen(text: str) -> list[list]:
    """[[von, bis, "2.796,28"], ...] – je Zeitraum des Berechnungsbogens der 'Bedarf für den Lebensunterhalt'.
    Handschriftliche Notizen in derselben Zeile werden umgangen, indem der größte Betrag der Zeile zählt."""
    erg = []
    teile = re.split(r"(?=Zeitraum\s+von\s+\d{1,2}\.\d{1,2}\.\d{4})", text)
    for teil in teile[1:]:
        z = re.match(r"Zeitraum\s+von\s+(\d{1,2}\.\d{1,2}\.\d{4})\s+bis\W*(\d{1,2}\.\d{1,2}\.\d{4})", teil)
        lu = re.search(r"Bedarf\s+f\S{0,3}r\s+den\s+Lebensunterhalt([^\n]*)", teil)
        if not (z and lu):
            continue
        betraege = [b for b in BETRAG_IN_ZEILE.findall(lu.group(1))] or re.findall(r"(\d{1,3}(?:\.\d{3})*,\d{2})", lu.group(1))
        if betraege:
            zeile = [datum_iso(z.group(1)), datum_iso(z.group(2)), max(betraege, key=_zahl)]
            if zeile not in erg:
                erg.append(zeile)
    # Neues Formular (BAB 39 ab 07/2026): "01.04.2026 - 30.09.2027 … Lebensunterhalt … Ergebnis Lebensunterhalt 1.527,99 €"
    for m in re.finditer(r"(\d{2}\.\d{2}\.\d{4})\s*-\s*(\d{2}\.\d{2}\.\d{4})\s*\n\s*\n?\s*Lebensunterhalt.{0,1500}?"
                         r"Ergebnis\s+Lebensunterhalt\s+(\d{1,3}(?:\.\d{3})*,\d{2})", text, re.DOTALL):
        zeile = [datum_iso(m.group(1)), datum_iso(m.group(2)), m.group(3)]
        if zeile not in erg:
            erg.append(zeile)
    return erg


def rechtslage_zum(cfg: dict, datum: str | None) -> dict | None:
    """Gültige Rechtslage (aus Ninox-Tabelle „Rechtslage“) zum Datum -> {ab, pauschale, version}."""
    beste = None
    for r in sorted((cfg.get("_stammdaten") or {}).get("rechtslage", []), key=lambda r: r["ab"]):
        if datum and r["ab"] <= datum and r.get("pauschale") is not None:
            beste = r
    return beste


def pauschale_sonstige(cfg: dict, datum: str | None) -> float:
    """Pauschale für sonstige Bedürfnisse: zuerst eure Rechtslage-Tabelle, sonst Standardwert aus den Regeln."""
    r = rechtslage_zum(cfg, datum)
    if r:
        return float(r["pauschale"])
    beste = 0.0
    for p in sorted(cfg.get("kostensatz_abgleich", {}).get("pauschalen", []), key=lambda p: p["ab"]):
        if datum and p["ab"] <= datum:
            beste = float(p["betrag"])
    return beste


def kostensatz_abgleich(e: "Ergebnis", cfg: dict, hinweise: list[str]) -> str | None:
    """Monatlicher Kostensatz = Bedarf für den Lebensunterhalt − Pauschale für sonstige Bedürfnisse.
    Abgleich mit den Kostensätzen der Stammdaten (Monatsbeträge bzw. Tagessatz × Tage).
    Liefert einen Beleg-Text, falls der Bescheid eine Kostensatz-Änderung (TS) zeigt."""
    k = cfg.get("kostensatz_abgleich", {})
    toleranz = float(k.get("toleranz", 0.05))
    alle_ks = [x for x in (cfg.get("_stammdaten") or {}).get("kostensaetze", []) if x.get("betraege")]
    zeilen, beleg, haeuser = [], None, set()
    vorher = None
    for von, bis, lu in e.lebensunterhalt:
        pausch = pauschale_sonstige(cfg, von)
        wert = round(_zahl(lu) - pausch, 2)
        rl = rechtslage_zum(cfg, von)
        text = (f"{iso_zu_de(von)}–{iso_zu_de(bis)}: {lu} − {_euro(pausch)}"
                + (f" (Rechtslage {rl['version']})" if rl and rl.get("version") else "") + f" = {_euro(wert)} €")
        if wert <= 0 or not pausch:
            zeilen.append(text + " (kein Wohnheim-Kostensatz)")
            e.ks_ohne.append(von)
            vorher = None
            continue
        kandidaten = []
        for ks in alle_ks:
            if e.haus_id is not None and ks.get("haus_id") not in (None, e.haus_id):
                continue
            for variante, betrag in ks["betraege"].items():
                if betrag is not None and abs(betrag - wert) <= toleranz:
                    kandidaten.append((ks, variante))
        if not alle_ks:
            zeilen.append(text)
            continue
        kleinster = min((b for x in alle_ks for b in x["betraege"].values() if b), default=0)
        if not kandidaten and wert < 0.7 * kleinster:
            zeilen.append(text + " (deutlich unter jedem Wohnheim-Kostensatz – vermutlich Grundbedarf, nicht im Haus)")
            e.ks_ohne.append(von)
            vorher = None
            continue
        if not kandidaten:
            zeilen.append(text + " → kein passender Kostensatz in den Stammdaten")
            hinweise.append(f"Kostensatz {_euro(wert)} € ({iso_zu_de(von)}) passt zu keinem Kostensatz in den Stammdaten")
            vorher = None
            continue
        # gültig zum Abschnittsbeginn bevorzugen, sonst den jüngsten
        gueltig = [c for c in kandidaten if (c[0].get("ab") or "") <= von] or kandidaten
        ks, variante = max(gueltig, key=lambda c: c[0].get("ab") or "")
        e.ks_treffer[von] = {"id": ks["id"], "text": ks["text"], "variante": variante, "haus_id": ks.get("haus_id"),
                             "ab": ks.get("ab")}
        haeuser.add(ks.get("haus_id"))
        zeilen.append(text + f" → {ks['text']}" + (f" ({variante})" if variante else ""))
        # veralteter Kostensatz? (es gibt für dieses Haus einen neueren, der zum Abschnittsbeginn schon galt)
        neuere = [x for x in alle_ks if x.get("haus_id") == ks.get("haus_id") and (x.get("ab") or "") <= von
                  and (x.get("ab") or "") > (ks.get("ab") or "")]
        if neuere:
            n = max(neuere, key=lambda x: x.get("ab") or "")
            soll = n["betraege"].get(variante)
            hinweise.append(f"Agentur hat ab {iso_zu_de(von)} den älteren Kostensatz {ks['text']} angesetzt – gültig wäre "
                            f"{n['text']}" + (f" ({_euro(soll)} €)" if soll else "") + " – Änderung beantragen?")
        # Kostensatz-Wechsel genau zum Abschnittsbeginn = Beleg für TS-Änderung
        if vorher and vorher["id"] != ks["id"] and ks.get("ab") == von:
            beleg = f"ab {iso_zu_de(von)} gilt {ks['text']} statt {vorher['text']}"
        elif not vorher and ks.get("ab") == von and e.kategorie and "nderung" in e.kategorie:
            beleg = f"Abschnitt beginnt mit neuem Kostensatz {ks['text']}"
        vorher = e.ks_treffer[von]
    if e.haus_id is None and len(haeuser) == 1 and None not in haeuser:
        hid = next(iter(haeuser))
        h = next((h for h in (cfg.get("_stammdaten") or {}).get("haeuser", []) if h["id"] == hid), None)
        if h:
            e.haus, e.haus_id = h.get("kuerzel") or h.get("name"), hid
            zeilen.append(f"Haus aus Kostensatz abgeleitet: {e.haus}")
    e.kostensatz_info = " | ".join(zeilen)
    if e.ks_treffer:  # Kostensatz ist gesetzt -> Hinweis "Haus nicht erkannt – Kostensatz wird nicht gesetzt" entfällt
        hinweise[:] = [h for h in hinweise if not h.startswith("Haus nicht erkannt – Kostensatz")]
    return beleg


def kostensaetze_fuer(cfg: dict, haus_id: int | None) -> list[dict]:
    """Kostensätze [{id, text, ab}] – aus den Ninox-Stammdaten (je Haus) oder aus der Regeldatei."""
    ks = (cfg.get("_stammdaten") or {}).get("kostensaetze") or []
    if ks:
        if haus_id is not None:
            return [k for k in ks if k.get("haus_id") == haus_id]
        return ks if len({k.get("haus_id") for k in ks}) <= 1 else []
    return [{"id": None, "text": k["name"], "ab": k["ab"]} for k in cfg.get("ninox", {}).get("kostensaetze", [])]


def ks_ab_aus_bezeichnung(text: str) -> str | None:
    """'B-PB 01/26' -> '2026-01-01' (Gültigkeit steht bei euch im Namen)."""
    m = re.search(r"(?<!\d)(0[1-9]|1[0-2])/(\d{2})(?!\d)", text or "")
    return f"20{m.group(2)}-{m.group(1)}-01" if m else None


def _zahl_oder_none(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and re.fullmatch(r"\s*-?\d{1,3}(?:\.?\d{3})*(?:,\d+)?\s*(?:€)?\s*", v):
        return _zahl(v.replace("€", "").strip()) if "," in v else float(v.replace(".", "").replace("€", "").strip())
    return None


def stammdaten_aufbereiten(haeuser_roh: list[dict], ks_roh: list[dict], cfg: dict,
                           rechtslage_roh: list[dict] | None = None) -> dict:
    """Aus Datensätzen (API: {id, fields}) bzw. CSV-Zeilen die interne Form bauen."""
    st = cfg["ninox"].get("stammdaten", {})
    hf, kf = st.get("haeuser_felder", {}), st.get("kostensaetze_felder", {})
    tage = float(cfg.get("kostensatz_abgleich", {}).get("tage_pro_monat", 30.4))
    alt_tage = [float(t) for t in cfg.get("kostensatz_abgleich", {}).get("tage_alternativ", []) if float(t) != tage]
    w = lambda f, k: f.get(k) if k else None  # noqa: E731
    daten = {"geladen": datetime.now().strftime("%Y-%m-%d %H:%M"), "haeuser": [], "kostensaetze": []}
    for i, r in enumerate(haeuser_roh, 1):
        f = r.get("fields", r)
        h = {"id": r.get("id", i), **{k: (str(w(f, hf.get(k))).strip() if w(f, hf.get(k)) not in (None, "") else None)
                                       for k in ("name", "kuerzel", "strasse", "plz", "ort", "anschrift")}}
        if h["name"] or h["kuerzel"]:
            daten["haeuser"].append(h)
    nach_name = {}
    for h in daten["haeuser"]:
        for k in ("kuerzel", "name"):
            if h.get(k):
                nach_name[h[k].lower()] = h["id"]
    for i, r in enumerate(ks_roh, 1):
        f = r.get("fields", r)
        bez = str(w(f, kf.get("bezeichnung")) or "").strip()
        if not bez:
            continue
        haus = w(f, kf.get("haus"))
        if isinstance(haus, dict):
            haus = haus.get("id")
        if isinstance(haus, str) and not haus.strip().isdigit():
            haus = nach_name.get(haus.strip().lower())
        betraege = {}
        for variante, feld in (kf.get("betraege") or {}).items():
            v = _zahl_oder_none(w(f, feld))
            if v is not None:
                betraege[variante] = round(v, 2)
        for variante, feld in (kf.get("tagessaetze") or {}).items():
            v = _zahl_oder_none(w(f, feld))
            if v is not None and variante not in betraege:
                betraege[variante] = round(v * tage, 2)
                for alt in alt_tage:  # manche Häuser lassen "Tagessatz × 30 Tage" bescheinigen
                    betraege[f"{variante} ×{alt:g}".strip()] = round(v * alt, 2)
        ab = datum_iso(str(w(f, kf.get("ab")) or "")[:10]) if kf.get("ab") else None
        daten["kostensaetze"].append({
            "id": r.get("id", i), "text": bez, "ab": ab or ks_ab_aus_bezeichnung(bez),
            "haus_id": int(haus) if haus not in (None, "") and str(haus).isdigit() else None,
            "betraege": betraege, "verpflegung_tag": _zahl_oder_none(w(f, kf.get("verpflegung")))})
    rf = st.get("rechtslage_felder", {})
    for r in rechtslage_roh or []:
        f = r.get("fields", r)
        ab = datum_iso(str(w(f, rf.get("ab")) or "")[:10])
        if ab:
            daten.setdefault("rechtslage", []).append({
                "ab": ab, "pauschale": _zahl_oder_none(w(f, rf.get("pauschale"))),
                "version": str(w(f, rf.get("version")) or "").strip() or None})
    return daten


def stammdaten_aus_csv(haus_csv: Path, ks_csv: Path | None, cfg: dict, ordner: Path,
                       rechtslage_csv: Path | None = None) -> dict:
    """Ninox-Exporte (CSV, Semikolon) der Häuser- und Kostensatz-Tabelle einlesen – ohne API-Zugang."""
    def lesen(p: Path) -> list[dict]:
        roh = p.read_bytes()
        for enc in ("utf-8-sig", "cp1252"):
            try:
                text = roh.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        trenner = ";" if text.split("\n", 1)[0].count(";") >= text.split("\n", 1)[0].count(",") else ","
        return list(csv.DictReader(text.splitlines(keepends=True), delimiter=trenner))
    haeuser = lesen(haus_csv)
    for zeile in haeuser:  # Nr-Spalte als ID nutzen, wenn vorhanden
        nr = zeile.get("Nr") or zeile.get("Id") or zeile.get("ID")
        if nr and str(nr).strip().isdigit():
            zeile["id"] = int(nr)
    alt = (cfg.get("_stammdaten") or {}).get("rechtslage")
    daten = stammdaten_aufbereiten(haeuser, lesen(ks_csv) if ks_csv else [], cfg,
                                   lesen(rechtslage_csv) if rechtslage_csv else None)
    if not rechtslage_csv and alt:
        daten["rechtslage"] = alt  # früher eingelesene Rechtslage behalten
    daten["quelle"] = "CSV"
    (ordner / "stammdaten.json").write_text(json.dumps(daten, ensure_ascii=False, indent=2), encoding="utf-8")
    return daten


def _csv_lesen(p: Path) -> list[dict]:
    roh = p.read_bytes()
    text = roh.decode("cp1252", errors="replace")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = roh.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    kopf = text.split("\n", 1)[0]
    return list(csv.DictReader(text.splitlines(keepends=True), delimiter=";" if kopf.count(";") >= kopf.count(",") else ","))


STAMMDATEN_TEILE = {"haeuser": "Häuser", "kostensaetze": "Kostensätze", "rechtslage": "Rechtslage"}


def stammdaten_teil_aus_csv(teil: str, pfad: Path, cfg: dict, ordner: Path) -> dict:
    """Nur einen Teil (Häuser, Kostensätze ODER Rechtslage) aus einem Ninox-CSV-Export ersetzen; der Rest bleibt."""
    daten = stammdaten_lesen(ordner) or {"haeuser": [], "kostensaetze": []}
    zeilen = _csv_lesen(pfad)
    if not zeilen:
        raise ValueError("Die Datei enthält keine Zeilen.")
    st = cfg["ninox"].get("stammdaten", {})
    if teil == "haeuser":
        for z in zeilen:
            nr = z.get("Nr") or z.get("Id") or z.get("ID")
            if nr and str(nr).strip().isdigit():
                z["id"] = int(nr)
        neu = stammdaten_aufbereiten(zeilen, [], cfg)["haeuser"]
        if not neu:
            raise ValueError(f"Keine Häuser erkannt – erwartet wird u. a. die Spalte „{st.get('haeuser_felder', {}).get('kuerzel')}“.")
    elif teil == "kostensaetze":
        hf = st.get("haeuser_felder", {})
        bekannte = [{"id": h["id"], "fields": {hf.get("name"): h.get("name"), hf.get("kuerzel"): h.get("kuerzel")}}
                    for h in daten.get("haeuser", [])]
        neu = stammdaten_aufbereiten(bekannte, zeilen, cfg)["kostensaetze"]
        if not neu:
            raise ValueError(f"Keine Kostensätze erkannt – erwartet wird u. a. die Spalte „{st.get('kostensaetze_felder', {}).get('bezeichnung')}“.")
    elif teil == "rechtslage":
        neu = stammdaten_aufbereiten([], [], cfg, zeilen).get("rechtslage", [])
        if not neu:
            raise ValueError(f"Keine Rechtslage erkannt – erwartet wird u. a. die Spalte „{st.get('rechtslage_felder', {}).get('ab')}“.")
    else:
        raise ValueError(teil)
    daten[teil] = neu
    jetzt = datetime.now().strftime("%Y-%m-%d %H:%M")
    daten["geladen"] = jetzt
    daten.setdefault("stand", {})[teil] = jetzt
    daten["quelle"] = "CSV"
    (ordner / "stammdaten.json").write_text(json.dumps(daten, ensure_ascii=False, indent=2), encoding="utf-8")
    return daten


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
                  "format": schema, "stream": False, "options": {"temperature": 0},
                  # Modell nach kurzer Zeit wieder aus dem Arbeitsspeicher nehmen (wichtig bei 16 GB ohne Grafikkarte)
                  "keep_alive": k.get("keep_alive", "2m")},
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

def kostensatz_kandidaten(cfg: dict, beginn: str | None, haus_id: int | None = None,
                          geburtsdatum: str | None = None) -> list[dict]:
    """Kostensätze des Hauses mit dem jüngsten 'ab' ≤ Abschnittsbeginn. Varianten VJ/MJ werden über das Alter
    zum Abschnittsbeginn entschieden; bleiben mehrere (z. B. EZ/DZ), entscheidet der Mensch."""
    if not beginn:
        return []
    gueltig = [k for k in kostensaetze_fuer(cfg, haus_id) if k.get("ab") and k["ab"] <= beginn]
    if not gueltig:
        return []
    neuestes = max(k["ab"] for k in gueltig)
    kandidaten = [k for k in gueltig if k["ab"] == neuestes]
    if geburtsdatum and len(kandidaten) > 1:
        volljaehrig = f"{int(geburtsdatum[:4]) + 18}{geburtsdatum[4:]}" <= beginn
        passend = [k for k in kandidaten if re.search(r"\bVJ\b" if volljaehrig else r"\bMJ\b", k["text"])]
        kandidaten = passend or kandidaten
    return kandidaten


def kostensatz_waehlen(cfg: dict, beginn: str | None, haus_id: int | None = None,
                       geburtsdatum: str | None = None) -> dict | None:
    """Eindeutiger Kostensatz zum Abschnittsbeginn -> {"_ref": id, "_text": Anzeige}; mehrdeutig -> None."""
    k = kostensatz_kandidaten(cfg, beginn, haus_id, geburtsdatum)
    return {"_ref": k[0]["id"], "_text": k[0]["text"]} if len(k) == 1 else None


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
    bescheid = {fb.get("datum"): e.bescheiddatum, fb.get("status"): e.status or z.get("status"), fb.get("notiz"): e.notiz or None}

    fa = n["abschnitte"]["felder"]
    zeilen = e.abschnitte or ([[e.zeitraum_von, e.zeitraum_bis, e.betrag]] if e.zeitraum_von and e.zeitraum_bis else [])
    abschnitte = []
    if z.get("abschnitte", True):
        for von, bis, betrag in zeilen:
            abschnitte.append({fa.get("beginn"): von, fa.get("ende"): bis,
                               fa.get("betrag"): betrag_zahl(betrag) if betrag else None,
                               fa.get("kostensatz"): ({"_ref": e.ks_treffer[von]["id"], "_text": e.ks_treffer[von]["text"]}
                                                      if von in e.ks_treffer else None if von in e.ks_ohne
                                                      else kostensatz_waehlen(cfg, von, e.haus_id, e.geburtsdatum))})

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
        n["_tage_pro_monat"] = cfg.get("kostensatz_abgleich", {}).get("tage_pro_monat", 30.4)

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
            kf = st.get("kostensaetze_felder", {})
            felder = [v for v in kf.values() if isinstance(v, str)]
            felder += list((kf.get("betraege") or {}).values()) + list((kf.get("tagessaetze") or {}).values())
            pruefliste.append((st["kostensaetze_tabelle"], felder))
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
        haeuser = self._datensaetze(st["haeuser_tabelle"]) if st.get("haeuser_tabelle") else []
        ks = self._datensaetze(st["kostensaetze_tabelle"]) if st.get("kostensaetze_tabelle") else []
        rechtslage = None
        if st.get("rechtslage_tabelle"):
            try:
                rechtslage = self._datensaetze(st["rechtslage_tabelle"])
            except Exception as ex:  # optional – fehlt die Tabelle, gilt der Standardwert
                log.warning("Rechtslage nicht geladen (%s) – nutze Pauschale aus den Regeln", ex)
        daten = stammdaten_aufbereiten(haeuser, ks, {"ninox": self.cfg, "kostensatz_abgleich":
                                                     {"tage_pro_monat": self.cfg.get("_tage_pro_monat", 30.4)}},
                                       rechtslage)
        daten["quelle"] = "Ninox"
        (ordner / "stammdaten.json").write_text(json.dumps(daten, ensure_ascii=False, indent=2), encoding="utf-8")
        return daten

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

def _datum_format(iso: str | None, fmt: str) -> str:
    if not iso or len(iso) != 10:
        return iso or ""
    j, m, t = iso.split("-")
    return {"MM/JJ": f"{m}/{j[2:]}", "MM/JJJJ": f"{m}/{j}", "TT.MM.JJ": f"{t}.{m}.{j[2:]}", "JJJJ-MM-TT": iso,
            "JJJJMMTT": f"{j}{m}{t}", "MM-JJ": f"{m}-{j[2:]}", "JJJJ-MM": f"{j}-{m}"}.get(fmt, f"{t}.{m}.{j}")


def beschreibung_bauen(werte: dict, zusatz: dict, b: dict, art_lang: dict) -> str:
    """Text fürs Ninox-Feld „Beschreibung“, z. B. „BAB Änderungsbescheid 12/25“. Welche Teile: Einstellungen."""
    fmt = b.get("datumsformat", "MM/JJ")
    teile = [b.get("vorsatz", "")]
    art = werte.get("kategorie") or ""
    if b.get("art") and art:
        teile.append(art_lang.get(art, art) if b.get("art_lang") else art)
    if b.get("datum"):
        teile.append(_datum_format(werte.get("bescheiddatum"), fmt))
    if b.get("zeitraum") and zusatz.get("zeitraum_von"):
        teile.append(f"{_datum_format(zusatz['zeitraum_von'], fmt)}–{_datum_format(zusatz.get('zeitraum_bis'), fmt)}")
    if b.get("haus"):
        teile.append(werte.get("haus") or "")
    if b.get("name"):
        teile.append(werte.get("name") or "")
    if b.get("kundennummer"):
        teile.append(werte.get("kundennummer") or "")
    if b.get("betrag") and zusatz.get("betrag"):
        teile.append(f"{zusatz['betrag']} €")
    return " ".join(t.strip() for t in teile if t and t.strip())


DATEINAME_PLATZHALTER = ["kundennummer", "name", "haus", "art", "art_lang", "datum", "von", "bis"]


def dateiname_bauen(cfg: dict, e: "Ergebnis", ninox_name: str | None = None) -> str:
    """Dateiname nach dem Muster aus den Einstellungen (cfg["dateiname"]["schema"]); fehlende Angaben -> leer."""
    d = cfg.get("dateiname", {})
    fmt = d.get("datumsformat", "JJJJ-MM-TT")
    art = e.kategorie or ""
    werte = {
        "kundennummer": e.kundennummer or "ohne-Kdnr", "name": re.sub(r",\s*", "_", e.name) if e.name else (ninox_name or "ohne-Name"),
        "ninox_name": ninox_name or "", "haus": e.haus or "ohne-Haus", "kategorie": art, "art": art,
        "art_lang": cfg.get("beschreibung_art", {}).get(art, art), "datum": _datum_format(e.bescheiddatum, fmt) or "ohne-Datum",
        "geburtsdatum": e.geburtsdatum or "", "von": _datum_format(e.zeitraum_von, fmt), "bis": _datum_format(e.zeitraum_bis, fmt)}

    class Leer(dict):
        def __missing__(self, k):
            return ""
    try:
        name = (d.get("schema") or "{kundennummer}_BAB-{art}_{name}").format_map(Leer(werte))
    except (ValueError, IndexError):
        name = "{kundennummer}_BAB-{art}_{name}".format_map(Leer(werte))
    name = re.sub(r"([_\-\s])\1+", r"\1", sicherer_dateiname(name)).strip("_- ")
    return name if name.lower().endswith(".pdf") else name + ".pdf"


def light_grund(e: "Ergebnis") -> str:
    """Light: jedes Dokument geht in „Prüfen“. Der Grund sagt, was fehlt bzw. unsicher ist."""
    fehlend = [FELD_TITEL[f] for f in FELDER_LIGHT if not getattr(e, f)]
    unsicher = [h for h in (e.hinweise or "").split("; ")
                if h and re.search(r"Kundennummer|Haus nicht eindeutig|mehrere Personen|Name", h)
                and "Kostensatz" not in h]
    teile = (["fehlt: " + ", ".join(fehlend)] if fehlend else ["alles erkannt – bitte abgleichen"]) + unsicher
    return "; ".join(teile)


class Sperre:
    """Lock-Datei im Scan-Ordner: Es soll immer nur EIN Rechner den Ordner verarbeiten.
    Inhalt: Computer, Benutzer*in, Programm, seit, zuletzt aktiv. Gelöscht beim Stoppen bzw. Beenden."""
    NAME = "BAB-O-BOX.lock"

    def __init__(self, ordner: Path):
        import socket
        import uuid
        self.datei = Path(ordner) / self.NAME
        self.ich = {"computer": os.environ.get("COMPUTERNAME") or socket.gethostname(),
                    "benutzer": os.environ.get("USERNAME") or os.environ.get("USER") or "?",
                    "programm": ANZEIGE_NAME, "pid": os.getpid(), "kennung": uuid.uuid4().hex}

    def lesen(self) -> dict | None:
        try:
            return json.loads(self.datei.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception:
            return {"computer": "?", "benutzer": "?", "seit": "?", "unlesbar": True}

    def ist_meine(self) -> bool:
        info = self.lesen()
        return bool(info and info.get("kennung") == self.ich["kennung"])

    def verwaist(self, info: dict) -> bool:
        """Sperre vom selben Rechner, aber das Programm läuft dort nicht mehr (z. B. nach Absturz)."""
        if info.get("computer") != self.ich["computer"] or not info.get("pid"):
            return False
        return not _prozess_laeuft(int(info["pid"]))

    def holen(self, erzwingen: bool = False) -> dict | None:
        """Sperre setzen. Gibt None zurück, wenn es geklappt hat – sonst die Angaben der fremden Sperre."""
        info = self.lesen()
        if info and info.get("kennung") != self.ich["kennung"] and not erzwingen and not self.verwaist(info):
            return info
        jetzt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        daten = {**self.ich, "seit": jetzt, "zuletzt": jetzt}
        if info and info.get("kennung") != self.ich["kennung"]:
            daten["uebernommen_von"] = f"{info.get('benutzer', '?')} auf {info.get('computer', '?')}"
            log.warning("Sperre übernommen (vorher %s)", daten["uebernommen_von"])
        self.datei.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.datei.with_name(self.NAME + f".{self.ich['kennung'][:8]}.tmp")
        tmp.write_text(json.dumps(daten, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.datei)
        if not self.ist_meine():  # gleichzeitig hat jemand anderes geschrieben
            return self.lesen()
        return None

    def auffrischen(self) -> bool:
        """'zuletzt aktiv' aktualisieren. False, wenn die Sperre inzwischen jemand anderem gehört."""
        info = self.lesen()
        if not info or info.get("kennung") != self.ich["kennung"]:
            return False
        info["zuletzt"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            tmp = self.datei.with_name(self.NAME + f".{self.ich['kennung'][:8]}.tmp")
            tmp.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.datei)
        except Exception:
            pass
        return True

    def freigeben(self) -> None:
        if self.ist_meine():
            try:
                self.datei.unlink()
            except Exception as ex:
                log.warning("Sperre konnte nicht gelöscht werden: %s", ex)

    @staticmethod
    def beschreiben(info: dict) -> str:
        if info.get("unlesbar"):
            return "einer unbekannten Person (Sperrdatei nicht lesbar)"
        t = f"{info.get('benutzer', '?')} auf dem Computer {info.get('computer', '?')}"
        if info.get("seit"):
            t += f"\nseit {iso_zeit_de(info['seit'])}"
        if info.get("zuletzt") and info.get("zuletzt") != info.get("seit"):
            t += f", zuletzt aktiv {iso_zeit_de(info['zuletzt'])}"
        return t


def iso_zeit_de(t: str) -> str:
    try:
        return datetime.strptime(t, "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y %H:%M")
    except (ValueError, TypeError):
        return str(t)


def _prozess_laeuft(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


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
        self.ninox = None if (testlauf or LIGHT) else Ninox(cfg)
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
        aus_pruefen = original.parent.resolve() == self.pruefen.resolve()
        if aus_pruefen:
            grund_pfad(original).unlink(missing_ok=True)
            arbeitskopie = eindeutiger_pfad(self.arbeit / original.name)
            shutil.move(original, arbeitskopie)
            original = arbeitskopie
        self._erneut = aus_pruefen  # Original liegt dann schon im Archiv

        log.info("Verarbeite %s", original.name)
        ocr_pdf = eindeutiger_pfad(self.arbeit / f"{original.stem}_ocr.pdf")
        e = Ergebnis()
        try:
            text = ocr(original, ocr_pdf, self.cfg)
            e = regeln_anwenden(text, self.cfg)
            pflicht = self.cfg["regeln"]["pflichtfelder"]
            if e.fehlend(pflicht) and self.cfg["ki"].get("aktiv", False) and not vorgaben:
                e = ki_ergaenzen(text, e, self.cfg)
            if LIGHT:
                raise PruefFall(light_grund(e))
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

            dateiname = dateiname_bauen(self.cfg, e, ninox_name)

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
            erkannt = {f: getattr(e, f) for f in FELDER_LIGHT} if LIGHT else asdict(e)
            zusatz = {k: v for k, v in asdict(e).items() if k in ZUSATZ_FELDER and v} if LIGHT else {}
            grund_pfad(ziel).write_text(json.dumps({"grund": str(grund), "erkannt": erkannt, "zusatz": zusatz},
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

    def freigeben(self, pfad: Path, werte: dict) -> str:
        """Light: Dokument aus „Prüfen“ mit den von Hand bestätigten Angaben benennen und nach „Verarbeitet“ legen."""
        with self.sperre:
            fehlend = [FELD_TITEL[f] for f in FELDER_LIGHT if not (werte.get(f) or "").strip()]
            if fehlend:
                raise ValueError("Bitte noch ausfüllen: " + ", ".join(fehlend))
            e = Ergebnis(**{f: werte[f].strip() for f in FELDER_LIGHT}, quelle="manuell")
            zusatz = werte.get("_zusatz") or {}
            e.zeitraum_von, e.zeitraum_bis = zusatz.get("zeitraum_von"), zusatz.get("zeitraum_bis")
            dateiname = dateiname_bauen(self.cfg, e)
            ziel = eindeutiger_pfad(self.verarbeitet / dateiname)
            shutil.move(pfad, ziel)
            grund_pfad(pfad).unlink(missing_ok=True)
            self._protokoll(pfad.name, ziel.name, "ok", "", e)
            log.info("Freigegeben: %s", ziel.name)
            return ziel.name

    def loeschen(self, pfade: list[Path]) -> list[str]:
        """Dokumente aus „Prüfen“ endgültig löschen (nach Warnung in der Oberfläche). Gibt Fehlermeldungen zurück."""
        fehler = []
        with self.sperre:
            for p in pfade:
                info = {}
                try:
                    if grund_pfad(p).exists():
                        info = json.loads(grund_pfad(p).read_text(encoding="utf-8")).get("erkannt", {})
                except Exception:
                    pass
                try:
                    p.unlink()
                    grund_pfad(p).unlink(missing_ok=True)
                except Exception as ex:
                    fehler.append(f"{p.name}: {ex}")
                    continue
                e = Ergebnis(**{k: v for k, v in info.items() if k in FELDER_LIGHT and v})
                self._protokoll(p.name, p.name, "geloescht", "von Hand gelöscht", e)
                log.warning("Gelöscht: %s", p.name)
        return fehler

    def _original_wegraeumen(self, original: Path) -> None:
        if not original.exists():
            return
        if self.archiv and not getattr(self, "_erneut", False):
            shutil.move(original, eindeutiger_pfad(self.archiv / original.name))
        else:
            original.unlink()

    def _protokoll(self, ein: str, aus: str, status: str, grund: str, e: Ergebnis) -> None:
        zeile = {"zeit": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "eingang": ein, "ergebnis": aus,
                 "status": status, "grund": grund, **{f: getattr(e, f) or "" for f in (FELDER_LIGHT if LIGHT else FELDER)}}
        if not LIGHT:
            zeile.update({"zeitraeume": e.zeitraeume, "kostensatz": e.kostensatz_info, "notiz": e.notiz,
                          "antragsart_hinweis": e.antragsart_hinweis})
        zeile.update({"hinweise": e.hinweise, "quelle": e.quelle})
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
