# BAB-O-BOX – BAB-Bescheide scannen, auslesen, ablegen

Windows-Programm mit Oberfläche in zwei Ausgaben:

| | **BAB-O-BOX Pro** | **BAB-O-BOX Light** |
|---|---|---|
| Erkennung | Kundennummer, Name, Geburtsdatum, Art, Datum, Antragsdatum, Zeiträume, Beträge, Haus, Kostensatz | nur Kundennummer, Name, Haus, Art, Bescheiddatum |
| Ablage | automatisch in Ninox (Bewohner*in → Antrag → Bescheid → Abschnitte → Dokument); Unsicheres unter „Prüfen“ | **jedes** Dokument unter „Prüfen“, Freigabe von Hand; danach benannt im Ordner „Verarbeitet“ |
| Prüfen-Reiter | Liste + Korrekturformular | Liste oben, PDF-Vorschau unten, Formular rechts, Freigabe erst nach Häkchen „verglichen“ |
| Ninox-Anbindung | ja (API) | nein |
| Weitere Reiter | Analyse, Schwärzen | – |
| Texterkennung | Tesseract, nur deutsch | Tesseract, nur deutsch |
| Datenordner | `%APPDATA%\BAB-O-BOX` | `%APPDATA%\BAB-O-BOX Light` |

Beide laufen auch nebeneinander auf einem PC.

## Dateien

| Datei | Zweck |
|---|---|
| `bab_gui.py` | Oberfläche (tkinter), Startpunkt Pro |
| `bab_light.py` | Startpunkt Light (setzt die Ausgabe, startet dieselbe Oberfläche) |
| `bab_edition.py` | merkt sich, welche Ausgabe läuft |
| `bab_kern.py` | Erkennung, Regeln, Ninox, Verarbeitung – läuft auch allein (`python bab_kern.py --einmal`) |
| `tools/verschlanken.py` | entfernt beim Bauen unnötige Sprach-, Zeitzonen- und Trainingsdateien |
| `bab_schwaerzen.py` | Pseudonymisieren (nur Pro, Reiter „Schwärzen“) |
| `config_standard.toml` | Standardregeln (Suchmuster, Arten, Ninox-Felder) – kommen mit jedem Update. Eigene Abweichungen in `eigene_regeln.toml` im Datenordner |
| `regeln_alt/` | frühere Standardregeln – zum einmaligen Übernehmen eigener Änderungen aus einer alten `config.toml` |
| `build.bat` | baut beide Ausgaben auf einem Windows-PC |
| `.github/workflows/build-windows.yml` | baut beide Ausgaben in der Cloud und legt ein Release mit zwei ZIPs an |
| `LIESMICH_ANWENDER.md`, `LIESMICH_LIGHT.md` | Kurzanleitungen für die Kolleg*innen |

## Bauen

**GitHub Actions** (empfohlen): Jeder Push auf `main` baut beide Ausgaben, startet sie im Selbsttest und legt ein Release
mit `BAB-O-BOX-Pro-Windows.zip` und `BAB-O-BOX-Light-Windows.zip` an.

**Lokal**: Python 3.11+ und Tesseract (UB Mannheim, mit deutschem Sprachpaket) installieren, `build.bat` doppelklicken.
Ergebnis: `dist\BAB-O-BOX-Pro\` und `dist\BAB-O-BOX-Light\`.

## Hinweise
- Pro braucht **Ninox Cloud oder Private Cloud** (die lokale Mac/iPad-App hat keine API). Zuerst gegen die Entwicklungsdatenbank mit Testlauf.
- Optional **lokale KI** über Ollama (nur lokal, keine Ollama-Cloud – Sozialdaten).
- Der **API-Schlüssel** steht im Klartext in `einstellungen.json` – nur auf geschütztem Arbeits-PC mit eigenem Windows-Konto nutzen.
- **SmartScreen**: Beim ersten Start einer unsignierten `.exe` → „Weitere Informationen → Trotzdem ausführen“.
- Lizenzen: Tesseract (Apache 2.0), pypdfium2/PDFium (Apache/BSD), pypdf (BSD), Pillow (HPND).
