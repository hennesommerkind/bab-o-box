# BAB-Eingang – Kurzanleitung

## Starten
Copy the `BAB-Eingang` folder to your PC (e.g. `C:\Programme\BAB-Eingang` or a network drive) and launch **BAB-Eingang.exe**. No installation is needed.

## Set up once (tab "Einstellungen")
1. **Ordner**: set "Scan-Eingang" to the folder the scanner saves to. The other folders are created automatically.
2. **Ninox**: enter the API key (in Ninox: Integrationen → API-Schlüssel generieren).
   Leave Team-ID and Datenbank-ID empty and click **Verbindung testen**. The app then shows the available IDs to copy in.
   Once the Datenbank-ID is filled in, "Testen" shows the tables and their fields. You need these for the rules file (see below).
3. **Speichern**.
4. Under "Allgemein", optionally enable **Mit Windows starten** and **Überwachung beim Programmstart automatisch starten**.

The table and field names in Ninox (resident table, documents table, category field …) are set in the **rules file**: "Regeldatei bearbeiten (config.toml)", then "Konfiguration neu laden".

## Testing
1. Tab **Analyse** → "PDF auswählen …" with a real notice. This shows what was recognized. Nothing is changed.
2. On the Übersicht tab, leave **Testlauf** ticked and click "Überwachung starten". Files get sorted and renamed, but nothing is sent to Ninox.
3. Once everything fits: untick Testlauf. From then on, notices are filed in Ninox.

## Day-to-day
- Just scan. The app picks up new PDFs from the input folder on its own.
- **Übersicht** shows everything that was processed. Double-clicking a row opens the PDF.
- **Prüfen (n)**: notices that weren't recognized clearly. Select one, check or correct the fields on the right, then **Mit diesen Angaben ablegen**.
  If the customer number was simply missing in Ninox, add it there and click **Erneut automatisch versuchen**.
- Files in **Fehler** have a technical problem (e.g. no network). Once it's fixed, move them back into the input folder.

## Data protection
Text recognition runs entirely on this PC. Documents are only sent to Ninox.
Settings (including the API key), the log and the processing record are stored under `%APPDATA%\BAB-Eingang`. The PC should be used by authorized staff only.
Delete the original archive regularly, or leave the "Original-Archiv" field empty.
