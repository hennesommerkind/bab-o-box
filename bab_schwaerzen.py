"""
BAB-O-BOX – Bescheide pseudonymisieren ("schwärzen"), komplett lokal.

Statt schwarzer Balken werden persönliche Angaben durch Fantasiewerte im GLEICHEN FORMAT ersetzt
(Kundennummer 900X000001, Namen wie "Ahorn, Linde", Geburtsdatum 01.01.<Jahr>, Anschrift "Pseudoweg 1").
So bleiben Aufbau und Formulierungen erhalten und die Erkennungsregeln lassen sich daran verbessern.

Zwei Stufen:
  1. Feste Muster (zuverlässig): Kundennummern, IBAN, Geburtsdaten, E-Mail-Adressen, beschriftete Namen
  2. Lokale KI (Ollama): Personennamen, private Anschriften, Ausbildungsbetriebe – überall im Text
Danach eine Restprüfung, die verdächtige Stellen meldet.

Ausgabe:
  <Ziel>/zum_Senden/<Datei>.txt     – pseudonymisierter Text (nach Durchsicht weitergeben)
  <Ziel>/NUR_LOKAL/zuordnung.csv    – Original → Ersatz (enthält echte Daten, NIE weitergeben)
  <Ziel>/zum_Senden/bericht.txt     – Übersicht mit Restwarnungen (ohne echte Daten)
"""
from __future__ import annotations

import csv
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import requests

import bab_kern as kern

log = logging.getLogger("bab")

# Fantasiewerte: Baumnamen sind eindeutig als Pseudonym erkennbar und bestehen nur aus Buchstaben
POOL = ["Ahorn", "Birke", "Buche", "Eiche", "Erle", "Esche", "Linde", "Ulme", "Weide", "Tanne", "Kiefer", "Fichte",
        "Lärche", "Pappel", "Kastanie", "Holunder", "Hasel", "Eibe", "Zeder", "Akazie", "Robinie", "Platane",
        "Wacholder", "Mispel", "Schlehe", "Vogelbeere", "Hainbuche", "Espe", "Zirbe", "Douglasie"]
# Wörter, die nach "Frau", "Name:" usw. stehen können, aber keine Namen sind
KEIN_NAME = {w.lower() for w in (
    "E-Mail Mail Telefon Tel Datum Zeichen Ihr Ihre Ihren Ihrem Name Vorname Anschrift Geburtsdatum Guten Tag Sehr "
    "geehrte geehrter Mit freundlichen Grüßen Grüssen Auftrag im und oder Kundennummer Kundenname Herr Herrn Frau "
    "von der die das den dem bei für vom zum zur Sie Ihnen Bescheid Antrag Leistung Kenntnisnahme Rückfragen "
    "Abdruck Anlage Seite Berechnungsbogen Einkommensfeststellung Eltern Elternteil Ehegatte Ehegatten "
    "Lebenspartner Lebenspartners Kinder Kind Kunde Kunden Azubi Auszubildende Auszubildenden Team Reha BAB "
    "Antwort jeder bitte angeben Absender Absenderin Vor Zuname Straße Hausnummer Postfach Postleitzahl Wohnort "
    "Jugendberufsagentur Abt Bevollmächtigte Vertreter Vertreterin Mitte Nord Süd Ost West Main Dienststelle "
    "Servicerufnummer Online Internet Unterschrift Datum Ort Konto Zahlung Zahlungen Leistung Leistungen Für Die "
    "Der Das An Im In Mit Bei Auf Nach Von Zur Zum Sie Ihnen Ihr Ihre Wir Es Ist Wird Werden Gemäß Hinweis").split()}
# Namensbestandteile, die klein geschrieben sein dürfen
PARTIKEL = {"von", "van", "vom", "de", "del", "della", "der", "den", "di", "da", "du", "la", "le", "al", "el", "bin",
            "ibn", "ter", "ten", "zu", "zur", "dos", "das", "y"}

# übliche Wörter in beschrifteten Zeilen, die selbst keine persönlichen Daten sind
BESCHRIFTUNGEN = {w.lower() for w in (
    "Anschrift Ausbildungsbetrieb Arbeitgeber Betrieb Berufsschule Schule Elternteil Eltern Mutter Vater "
    "Erziehungsberechtigte Freibeträge Freibetrag Einkommen Summe Bedarf Unterbringung Art Neue Die Der Das "
    "Auszubildende Auszubildenden Ausbildung Wohnheim Miete Großeltern GmbH Co KG AG eG Straße Str").split()}
BA_IBAN = re.sub(r"\s", "", "DE50 7600 0000 0076 0016 17")  # Konto der Bundesagentur – öffentlich, bleibt
OFFIZIELL = re.compile(r"Agentur|Arbeit|Bundes|Kolping|Jugendwohnen|Wohnheim|Gesetz|SGB|BAf|Sozialgesetz|"
                       r"Service|Bank|Bundesbank|Internet|Online|Berufsausbildungsbeihilfe", re.IGNORECASE)


@dataclass
class Zuordnung:
    """Konsistente Ersetzungen über alle Dateien eines Laufs (gleiche Person -> gleiches Pseudonym)."""
    werte: dict[str, str] = field(default_factory=dict)
    zaehler: dict[str, int] = field(default_factory=dict)
    zeilen: list[tuple[str, str, str, str]] = field(default_factory=list)  # (datei, art, original, ersatz)

    def neu(self, art: str) -> int:
        self.zaehler[art] = self.zaehler.get(art, 0) + 1
        return self.zaehler[art]

    def ersatz(self, original: str, art: str, datei: str, macher: Callable[[int], str]) -> str:
        schluessel = f"{art}|{original}"
        if schluessel not in self.werte:
            self.werte[schluessel] = macher(self.neu(art))
            self.zeilen.append((datei, art, original, self.werte[schluessel]))
        return self.werte[schluessel]


def _pool_name(n: int) -> str:
    if n <= len(POOL):
        return POOL[n - 1]
    a, b = divmod(n - 1, len(POOL))
    return POOL[a % len(POOL)] + POOL[b].lower()


@dataclass
class Ergebnis:
    text: str
    ersetzungen: int
    warnungen: list[str]
    ki_genutzt: bool


# =========================================================================== Stufe 1: feste Muster

def _feste_muster(text: str, z: Zuordnung, datei: str) -> str:
    # Kundennummern (auch mit Leerzeichen, im Barcode *…* und in "Mein Zeichen: 021-…")
    def kdnr(m):
        roh = re.sub(r"\s", "", m.group(0)).upper()
        if roh.startswith("900X"):
            return m.group(0)
        kern_ = roh[:3] + roh[4:] if len(roh) == 10 else roh
        return z.ersatz(kern_, "Kundennummer", datei, lambda n: f"900X{n:06d}")
    text = re.sub(r"(?<![A-Z0-9])\d{3}\s?[A-Z]\s?\d{6}(?![A-Z0-9])", kdnr, text)
    # Lesefehler: Buchstabe als 0 gelesen (10 Ziffern) – und überhaupt jede alleinstehende 10-stellige Zahl
    text = re.sub(r"(?<![\w.,/-])\d{10}(?![\w.,/])", kdnr, text)
    # beschriftete Nummern mit OCR-Müll ("Kundennummer: A357D13792", "Mein Zeichen: 022-A357D13792")
    def kdnr_beschriftet(m):
        wert = m.group(2)
        if wert.startswith("900X") or not (sum(c.isdigit() for c in wert) >= 7):
            return m.group(0)
        return m.group(1) + z.ersatz(wert, "Kundennummer", datei, lambda n: f"900X{n:06d}")
    text = re.sub(r"((?:Kundennummer|Kd\.?-?Nr\.?)\s*:?\s*|Mein\s+Zeichen\s*:?\s*\d{3}\s*[-/]\s*)([A-Z0-9]{8,11})\b",
                  kdnr_beschriftet, text)

    # IBAN (außer dem öffentlichen Konto der Bundesagentur)
    def iban(m):
        roh = re.sub(r"\s", "", m.group(0))
        if roh.upper() == BA_IBAN or roh.upper().replace("O", "0") == BA_IBAN:
            return m.group(0)
        return z.ersatz(roh, "IBAN", datei, lambda n: f"DE00 0000 0000 0000 {n:04d} 00")
    text = re.sub(r"\bDE[0-9O]{2}(?:\s?[0-9O]{4}){4}\s?[0-9O]{2}\b", iban, text)

    # Geburtsdaten: Tag und Monat ersetzen, Jahr bleibt (für Altersfragen relevant, allein nicht identifizierend).
    # Erfasst wird JEDES vollständige Datum, das mind. 13 Jahre zurückliegt – Bescheid-, Antrags- und
    # Bewilligungsdaten sind immer jünger. So auch "30. April 2004", "I14.09.2001" oder Daten ohne Beschriftung.
    grenze = datetime.now().year - 13
    def geb(m):
        d = kern.datum_iso(re.sub(r"\s+", " ", m.group(0)))
        if not d or not (1940 <= int(d[:4]) <= grenze) or d[5:] == "01-01":
            return m.group(0)
        lang = not re.match(r"\d{1,2}\.\d{1,2}\.", m.group(0))
        return z.ersatz(d, "Geburtsdatum", datei, lambda n: (f"1. Januar {d[:4]}" if lang else f"01.01.{d[:4]}"))
    text = re.sub(rf"(?<!\d)(?:\d{{1,2}}\.\d{{1,2}}\.\d{{4}}|{kern.DATUM_LANG_RE})(?!\d)", geb, text)

    # E-Mail-Adressen (außer @arbeitsagentur.de – Funktionspostfächer)
    def mail(m):
        if m.group(0).lower().endswith("arbeitsagentur.de"):
            return re.sub(r"^[^@]+", "Team-Postfach", m.group(0))
        return z.ersatz(m.group(0), "E-Mail", datei, lambda n: f"person{n}@example.org")
    text = re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", mail, text)

    # Telefonnummern mit Beschriftung (Servicenummer 0800 bleibt)
    def tel(m):
        if "0800" in m.group(2):
            return m.group(0)
        return m.group(1) + z.ersatz(m.group(2), "Telefon", datei, lambda n: f"0000 {n:06d}")
    text = re.sub(r"((?:Tel(?:efon)?\.?|Mobil|Handy)\s*:?\s*)(\+?[\d /()-]{6,})", tel, text, flags=re.IGNORECASE)
    return text


# =========================================================================== Stufe 2: lokale KI

KI_ANLEITUNG = (
    "Du hilfst beim Pseudonymisieren eines Bescheids der Agentur für Arbeit. Finde im TEXT alle Angaben, "
    "die eine Privatperson oder ihren Betrieb erkennbar machen:\n"
    "- PERSON: jeder Name eines Menschen (Auszubildende, Eltern, Sachbearbeiter*innen, Ansprechpersonen), "
    "auch Teilnamen wie 'Herr Müller' oder 'Frau Kübler'\n"
    "- ANSCHRIFT: Straße mit Hausnummer und/oder PLZ mit Ort einer Privatperson\n"
    "- BETRIEB: Name eines Ausbildungsbetriebs, Arbeitgebers oder einer Berufsschule\n"
    "NICHT aufnehmen: Agentur für Arbeit, Bundesagentur, Kolping-Einrichtungen und ihre Anschriften, Gesetze, "
    "Paragrafen, Beträge, Datumsangaben, Kundennummern, Platzhalter wie 900X000001 oder Baumnamen wie 'Ahorn'.\n"
    "Gib jede Fundstelle GENAU so zurück, wie sie im Text steht (Zeichen für Zeichen). Wenn nichts da ist, "
    "gib eine leere Liste zurück.\n\nTEXT:\n"
)
KI_SCHEMA = {
    "type": "object",
    "properties": {"funde": {"type": "array", "items": {
        "type": "object",
        "properties": {"text": {"type": "string"}, "art": {"type": "string", "enum": ["PERSON", "ANSCHRIFT", "BETRIEB"]}},
        "required": ["text", "art"]}}},
    "required": ["funde"],
}


def ki_bereit(cfg: dict) -> str | None:
    """None = bereit, sonst Fehlermeldung."""
    try:
        r = requests.get(cfg["ki"]["url"].rstrip("/") + "/api/tags", timeout=10)
        r.raise_for_status()
        modelle = [m["name"] for m in r.json().get("models", [])]
    except Exception as ex:
        return f"Ollama nicht erreichbar ({ex}). Läuft Ollama? Adresse in den Einstellungen prüfen."
    if cfg["ki"]["modell"] not in modelle:
        return f"Modell {cfg['ki']['modell']} ist nicht installiert – in PowerShell: ollama pull {cfg['ki']['modell']}"
    if cfg["ki"]["modell"].endswith(":cloud") or "-cloud" in cfg["ki"]["modell"]:
        return "Cloud-Modell eingestellt – für echte Bescheide nicht zulässig. Bitte ein lokales Modell wählen."
    return None


def _abschnitte(text: str, laenge: int) -> list[str]:
    """Text in Stücke an Zeilengrenzen teilen (für das kleine Kontextfenster)."""
    stuecke, aktuell = [], ""
    for zeile in text.splitlines(keepends=True):
        if len(aktuell) + len(zeile) > laenge and aktuell:
            stuecke.append(aktuell)
            aktuell = ""
        aktuell += zeile
    if aktuell.strip():
        stuecke.append(aktuell)
    return stuecke


def ki_fundstellen(text: str, cfg: dict, melden: Callable[[str], None]) -> list[tuple[str, str]]:
    k = cfg["ki"]
    stuecke = _abschnitte(text, int(cfg.get("schwaerzen", {}).get("abschnitt_zeichen", 2500)))
    funde: list[tuple[str, str]] = []
    for i, stueck in enumerate(stuecke, 1):
        melden(f"  KI-Durchgang {i}/{len(stuecke)} …")
        try:
            r = requests.post(k["url"].rstrip("/") + "/api/chat", json={
                "model": k["modell"], "stream": False, "format": KI_SCHEMA,
                "messages": [{"role": "user", "content": KI_ANLEITUNG + stueck}],
                "options": {"temperature": 0}, "keep_alive": k.get("keep_alive", "2m")},
                timeout=k.get("timeout_s", 420))
            r.raise_for_status()
            daten = json.loads(r.json()["message"]["content"])
        except Exception as ex:
            raise RuntimeError(f"KI-Durchgang {i} fehlgeschlagen: {ex}")
        for f in daten.get("funde", []):
            t, art = str(f.get("text", "")).strip(" ,.;:"), f.get("art", "")
            if len(t) >= 3 and t in stueck and art in ("PERSON", "ANSCHRIFT", "BETRIEB"):
                funde.append((t, art))
    return funde


# =========================================================================== Ersetzen

def _hausanschriften(text: str, cfg: dict) -> set[str]:
    """Straßen der Kolping-Häuser – bleiben stehen (Haus-Erkennung braucht sie)."""
    staemme = set()
    for h in (cfg.get("_stammdaten") or {}).get("haeuser", []):
        s = kern._strassen_stamm(h.get("strasse") or h.get("anschrift") or "")
        if s:
            staemme.add(s)
    zeilen = text.splitlines()
    for i, zeile in enumerate(zeilen):  # Straße/PLZ in den Zeilen direkt unter "Kolping …"
        if not re.search(r"Kolping", zeile, re.IGNORECASE) or re.search(r"Konto|Zahlung|Abtretung", zeile):
            continue
        for folge in zeilen[i + 1: i + 6]:
            st = re.match(r"\s*([^\W\d_][\w.\- ]{2,40}?)\s+\d{1,3}\s*[a-z]?(?:\s*-\s*\d+)?\b", folge)
            if st and not re.search(r"Agentur|Service|Tel|Fax|\d{5}", st.group(1)):
                stamm = kern._strassen_stamm(st.group(1))
                if stamm:
                    staemme.add(stamm)
            plz = re.match(r"\s*(\d{5})\s+[^\W\d_]", folge)
            if plz:
                staemme.add("plz:" + plz.group(1))
                break
    for h in (cfg.get("_stammdaten") or {}).get("haeuser", []):
        p = re.search(r"\b\d{5}\b", " ".join(str(h.get(k) or "") for k in ("plz", "anschrift")))
        if p:
            staemme.add("plz:" + p.group(0))
    return staemme


def _ist_hausanschrift(fund: str, staemme: set[str]) -> bool:
    norm = kern._adresse_norm(fund)
    strassen = [s for s in staemme if not s.startswith("plz:")]
    if any(re.search(rf"\b{re.escape(s)}", norm) for s in strassen):
        return True
    plz = re.search(r"\b\d{5}\b", fund)  # nur "PLZ Ort" ohne Straße: Haus-PLZ bleibt
    return bool(plz and "plz:" + plz.group(0) in staemme and not re.search(r"[^\W\d_]{3,}\.?\s*\d", fund.split(plz.group(0))[0]))


def _name_kandidat(roh: str) -> str | None:
    """Nur echte Namensfolgen durchlassen: alle Wörter groß (außer Partikeln), keine Allerweltswörter."""
    woerter = re.findall(kern.WORT, kern.ANREDE.sub("", roh))
    if not woerter or len(woerter) > 5:
        return None
    for w in woerter:
        if w.lower() in PARTIKEL:
            continue
        if not w[0].isupper() or w.lower() in KEIN_NAME or OFFIZIELL.search(w):
            return None
    return " ".join(woerter)


def _ersetze_ueberall(text: str, original: str, ersatz: str) -> str:
    return re.sub(rf"(?<![\w]){re.escape(original)}(?![\w])", ersatz, text)


def _ist_pseudonym(w: str) -> bool:
    """Schon ersetzte Baum-Namen (auch zusammengesetzte wie 'Birkeulme') nicht erneut ersetzen."""
    rest = w.lower()
    baeume = sorted((b.lower() for b in POOL), key=len, reverse=True)
    while rest:
        b = next((b for b in baeume if rest.startswith(b)), None)
        if not b:
            return False
        rest = rest[len(b):]
    return True


def _personen_ersetzen(text: str, funde: list[str], z: Zuordnung, datei: str) -> str:
    """Namen Wort für Wort ersetzen – so wird 'Şükrü Yılmaz', 'Yılmaz, Şükrü' und 'Herr Yılmaz' einheitlich."""
    woerter = set()
    for f in funde:
        if not f:
            continue
        f = kern.ANREDE.sub("", f)
        for w in re.findall(kern.WORT, f):
            if len(w) >= 3 and not OFFIZIELL.search(w) and w.lower() not in KEIN_NAME and not (w.isupper() and len(w) <= 4) \
                    and not _ist_pseudonym(w):
                woerter.add(w)
    for w in sorted(woerter, key=len, reverse=True):
        ersatz = z.ersatz(w, "Name", datei, _pool_name)
        text = _ersetze_ueberall(text, w, ersatz)
    return text


def pseudonymisieren(text: str, cfg: dict, z: Zuordnung, datei: str, mit_ki: bool,
                     melden: Callable[[str], None] = lambda _: None) -> Ergebnis:
    vorher = len(z.zeilen)
    staemme = _hausanschriften(text, cfg)
    text = _feste_muster(text, z, datei)

    # beschriftete Namen (auch ohne KI sicher)
    personen = []
    n = kern.name_lesen(text, cfg)
    if n:
        personen.append(n)
    for m in re.finditer(r"(?:\bName\s*:|\bKundenname|\bName,\s*Vorname\s*:|\bBescheid\s+f[üu]r|\bHerrn?|\bFrau)"
                         rf"[ \t]+((?:Herrn?|Frau)[ \t]+)?({kern.WORT}(?:[ \t,]+{kern.WORT})?)", text):
        woerter = [w for w in re.findall(kern.WORT, m.group(2)) if w.lower() not in KEIN_NAME and w[0].isupper()]
        if woerter and not OFFIZIELL.search(m.group(2)):
            personen.append(" ".join(woerter))

    # "Lea Guse Kolping Jugendwohnen" / "Guten Tag Lea Guse Kolping …" -> Name vor "Kolping"
    for m in re.finditer(rf"(?m)(?:^|Guten\s+Tag\s+)[ \t]*((?:{kern.WORT}[ \t]+){{1,3}})Kolping", text):
        personen.append(_name_kandidat(m.group(1)) or '')
    # "für Zirbe, Zeka (Elternteil 1)" -> beide Teile
    for m in re.finditer(rf"(?:f[üu]e?r|und|,)\s+({kern.WORT}(?:[ ,]+{kern.WORT}){{0,3}})\s*[({{]\s*(?:Elternteil|Eltern)", text):
        personen.append(_name_kandidat(m.group(1)) or '')
    # Anschriftenfeld: "Espe En-Nhallı (Bei jeder Antwort bitte angeben)" -> Wörter davor
    for m in re.finditer(rf"(?m)^[ \t]*((?:{kern.WORT}[ \t]+){{1,4}})[({{]\s*Bei\s+jeder\s+Antwort", text):
        personen.append(_name_kandidat(m.group(1)) or '')
    # "Absender: … (Vor- und Zuname)": Zeile davor
    for m in re.finditer(rf"(?m)^[ \t]*((?:{kern.WORT}[ \t]*){{1,4}})(?:[^\n]*)\n\s*\(Vor-\s*und\s*Zuname\)", text):
        personen.append(_name_kandidat(m.group(1)) or '')

    if mit_ki:
        for t, art in ki_fundstellen(text, cfg, melden):
            if art == "PERSON":
                woerter = re.findall(kern.WORT, kern.ANREDE.sub("", t))
                klein = [w for w in woerter if w[0].islower() and w.lower() not in PARTIKEL]
                if not OFFIZIELL.search(t) and woerter and not klein:
                    personen.append(t)
            elif art == "ANSCHRIFT":
                if not _ist_hausanschrift(t, staemme) and not OFFIZIELL.search(t):
                    def adr(n_, t=t):
                        if re.search(r"\d{5}", t) and re.search(r"[^\W\d_]{3,}.*\d", t.split(",")[0]):
                            return f"Pseudoweg {n_}, 99{n_:03d} Pseudostadt"
                        if re.search(r"\b\d{5}\b", t):
                            return f"99{n_:03d} Pseudostadt"
                        return f"Pseudoweg {n_}"
                    text = _ersetze_ueberall(text, t, z.ersatz(t, "Anschrift", datei, adr))
            elif art == "BETRIEB" and not OFFIZIELL.search(t):
                text = _ersetze_ueberall(text, t, z.ersatz(t, "Betrieb", datei, lambda n_: f"Musterbetrieb {n_}"))

    text = _personen_ersetzen(text, personen, z, datei)
    return Ergebnis(text=text, ersetzungen=len(z.zeilen) - vorher, warnungen=restpruefung(text, cfg, z, staemme), ki_genutzt=mit_ki)


def restpruefung(text: str, cfg: dict, z: Zuordnung, staemme: set[str] | None = None) -> list[str]:
    """Meldet Stellen, die noch nach echten Daten aussehen (ohne die Daten selbst zu nennen, wo möglich)."""
    w = []
    ersatzwoerter = {e for (_, _, _, e) in z.zeilen}
    for m in re.finditer(r"(?<![A-Z0-9])\d{3}[A-Z]\d{6}(?![A-Z0-9])", text):
        if not m.group(0).startswith("900X"):
            w.append("Kundennummer-ähnliche Zahl übrig")
    if re.search(r"(?:geb\.?\s*am|Geburtsdatum)\s*:?\s*(?!01\.01\.)\d{1,2}\.\d{1,2}\.\d{4}", text, re.IGNORECASE):
        w.append("Geburtsdatum nicht ersetzt")
    for m in re.finditer(rf"(?<!\d)(?:\d{{1,2}}\.\d{{1,2}}\.\d{{4}}|{kern.DATUM_LANG_RE})(?!\d)", text):
        d = kern.datum_iso(re.sub(r"\s+", " ", m.group(0)))
        if d and int(d[:4]) <= datetime.now().year - 13 and d[5:] != "01-01":
            w.append(f"altes Datum (Geburtsdatum?) übrig: {m.group(0)}")
    if re.search(r"(?<![\w.,/-])\d{10}(?![\w.,/])", text):
        w.append("10-stellige Zahl übrig (Kundennummer mit Lesefehler?)")
    for m in re.finditer(rf"(?:Herrn?|Frau|Guten\s+Tag|Name\s*:)\s+({kern.WORT})", text):
        wort = m.group(1)
        if wort[0].isupper() and wort not in ersatzwoerter and not any(wort in e for e in ersatzwoerter) \
                and not OFFIZIELL.search(wort) and wort.lower() not in KEIN_NAME:
            zeile = text[max(0, m.start() - 20): m.end() + 20].replace("\n", " ")
            w.append(f"möglicher Name nicht ersetzt: …{zeile}…")
    # Beschriftete Angaben ("Anschrift: …", "Ausbildungsbetrieb: …") und "für X, Y (Elternteil …)"
    erlaubt = re.compile(r"^(?:Pseudo\w*|Musterbetrieb|\d+|[A-ZÄÖÜ]{1,3})$")
    staemme = staemme or set()
    stellen = [(m.group(1), m.group(2)) for m in re.finditer(
        r"(?im)\b(Anschrift|Ausbildungsbetrieb|Arbeitgeber|Betrieb|Berufsschule|Schule|Mutter|Vater|"
        r"Erziehungsberechtigte\w*)(?:\s+der\s+Eltern)?\s*:\s*([^\n]*)", text)]
    stellen += [("Elternteil", m.group(1)) for m in re.finditer(
        rf"f[üu]e?r\s+({kern.WORT}(?:[ ,]+{kern.WORT}){{0,3}})\s*\(\s*Elternteil", text)]
    for label, rest in stellen:
        if staemme and kern._strassen_stamm(rest) and _ist_hausanschrift(rest, staemme):
            continue
        verdacht = [x for x in re.findall(kern.WORT, rest)
                    if x[0].isupper() and x not in ersatzwoerter and not erlaubt.match(x) and x.lower() not in KEIN_NAME
                    and not OFFIZIELL.search(x) and not any(x in e for e in ersatzwoerter)
                    and x.lower() not in BESCHRIFTUNGEN]
        if verdacht:
            w.append(f"bei „{label}“ steht noch: {', '.join(sorted(set(verdacht))[:4])} – bitte prüfen")
    return sorted(set(w))


# =========================================================================== Stapel

def stapel(quelle: Path, ziel: Path, cfg: dict, mit_ki: bool, melden: Callable[[str], None],
           abbrechen: Callable[[], bool] = lambda: False) -> tuple[int, int]:
    """Alle PDFs in `quelle` pseudonymisieren. Liefert (fertig, mit_warnungen)."""
    senden, lokal, arbeit = ziel / "zum_Senden", ziel / "NUR_LOKAL", ziel / "_arbeit"
    for p in (senden, lokal, arbeit):
        p.mkdir(parents=True, exist_ok=True)
    if mit_ki:
        fehler = ki_bereit(cfg)
        if fehler:
            raise RuntimeError(fehler)
    dateien = sorted(p for p in quelle.iterdir() if p.suffix.lower() == ".pdf")
    z = Zuordnung()
    bericht = [f"BAB-O-BOX – Pseudonymisierung vom {datetime.now():%d.%m.%Y %H:%M}",
               f"KI: {'ja, ' + cfg['ki']['modell'] if mit_ki else 'NEIN – nur feste Muster, Namen im Fließtext können übrig sein'}",
               ""]
    fertig = mit_warnung = 0
    for nr, pdf in enumerate(dateien, 1):
        if abbrechen():
            melden("Abgebrochen.")
            break
        melden(f"[{nr}/{len(dateien)}] {pdf.name}")
        name = f"bescheid_{nr:03d}"  # Dateiname selbst kann Namen enthalten -> neutral
        try:
            tmp = arbeit / f"{name}_ocr.pdf"
            text = kern.ocr(pdf, tmp, cfg)
            tmp.unlink(missing_ok=True)
            erg = pseudonymisieren(text, cfg, z, name, mit_ki, melden)
            kopf = (f"# BAB-O-BOX pseudonymisiert – {name} – {erg.ersetzungen} neue Ersetzungen"
                    f"{' – KI: ja' if erg.ki_genutzt else ' – KI: nein'}\n"
                    f"# Bitte vor dem Weitergeben durchlesen. Restwarnungen: {len(erg.warnungen)}\n\n")
            (senden / f"{name}.txt").write_text(kopf + erg.text, encoding="utf-8")
            bericht.append(f"{name}: {erg.ersetzungen} Ersetzungen" + ("" if not erg.warnungen else
                           f", {len(erg.warnungen)} Warnungen:\n    - " + "\n    - ".join(erg.warnungen)))
            fertig += 1
            mit_warnung += bool(erg.warnungen)
            melden(f"   → {name}.txt ({erg.ersetzungen} Ersetzungen, {len(erg.warnungen)} Warnungen)")
        except Exception as ex:
            bericht.append(f"{name}: FEHLER {ex}")
            melden(f"   FEHLER: {ex}")
    (senden / "bericht.txt").write_text("\n".join(bericht) + "\n", encoding="utf-8")
    with open(lokal / "zuordnung.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["Datei", "Originaldatei", "Art", "Original", "Ersatz"])
        namen = {f"bescheid_{i:03d}": p.name for i, p in enumerate(dateien, 1)}
        for datei, art, orig, ers in z.zeilen:
            w.writerow([datei, namen.get(datei, ""), art, orig, ers])
    (lokal / "LIESMICH.txt").write_text(
        "Dieser Ordner enthält die ECHTEN Daten (Zuordnung Original → Ersatz).\n"
        "Nur zur eigenen Kontrolle – NICHT weitergeben, nach der Durchsicht löschen.\n", encoding="utf-8")
    try:
        arbeit.rmdir()
    except OSError:
        pass
    return fertig, mit_warnung
