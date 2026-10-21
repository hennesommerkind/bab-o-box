# BAB-O-BOX – Desktop app for BAB notices

Windows app with a graphical interface: it watches the scan folder, runs local OCR on the notices (Tesseract), reads out the customer number, category, date, approval period and amount, names the file, and files it in Ninox. Anything uncertain lands under "Prüfen", where it can be corrected and filed with one click.

## Files

| File | Purpose |
|---|---|
| `bab_gui.py` | Interface (tkinter) |
| `bab_kern.py` | Core logic – also runs on its own on the command line (`python bab_kern.py --einmal`) |
| `bab_schwaerzen.py` | Pseudonymization of notices (fixed patterns + local AI via Ollama), tab "Schwärzen" |
| `config_standard.toml` | Default rules (search patterns, categories, Ninox fields) – bundled, updated with every release. Local differences go into `%APPDATA%\BAB-O-BOX\eigene_regeln.toml` |
| `regeln_alt/` | Earlier default rule files – used once to extract own changes from an old full `config.toml` |
| `build.bat` | Builds the `.exe` on a Windows PC |
| `.github/workflows/build-windows.yml` | Alternative: builds the `.exe` in the cloud via GitHub Actions |
| `LIESMICH_ANWENDER.md` | Short guide for colleagues – ships with the build |

## Building the .exe – option A: locally (once, on any Windows PC)

1. Install **Python 3.11+** from python.org ("Add to PATH").
2. Install **Tesseract** from UB Mannheim (github.com/UB-Mannheim/tesseract/wiki) and select **German** under "Additional language data".
3. Put all the files in one folder and double-click `build.bat`.
4. Result: `dist\BAB-O-BOX\` containing `BAB-O-BOX.exe` and Tesseract (~100 MB). Zip the folder and pass it on – the target PCs don't need anything installed.

## Building the .exe – option B: GitHub Actions (without a Windows build PC)

Push the files to a (private) GitHub repo, then go to **Actions → Windows-Build → Run workflow**. After about 5 minutes, the ZIP is under "Artifacts".

## Notes

- **Ninox Cloud or Private Cloud** required (the API isn't available in the local Mac/iPad app).
- **Ghostscript/OCRmyPDF is no longer needed**: the app renders the PDF pages itself and creates the searchable PDF directly with Tesseract. If the scanner already delivers PDFs with a text layer, OCR is skipped automatically.
- Optionally, **local AI** via Ollama (ollama.com) on the same PC or on the network – set this in the app under Einstellungen.
- The **API key** is stored in plain text in `%APPDATA%\BAB-O-BOX\einstellungen.json` – use the app on a protected work PC with your own Windows account.
- **SmartScreen**: an unsigned `.exe` triggers "Der Computer wurde durch Windows geschützt" on first launch → "Weitere Informationen → Trotzdem ausführen". If your IT requires it, the `.exe` can be code-signed.
- Licences: Tesseract (Apache 2.0), pypdfium2/PDFium (Apache/BSD), pypdf (BSD) – may be bundled for internal use.
