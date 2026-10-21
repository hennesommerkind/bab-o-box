# BAB-O-BOX – Kurzanleitung

## Starten
Copy the `BAB-O-BOX` folder to your PC (e.g. `C:\Programme\BAB-O-BOX` or a network drive) and launch **BAB-O-BOX.exe**. No installation is needed.

## Set up once (tab "Einstellungen")
1. **Ordner**: set "Scan-Eingang" to the folder the scanner saves to. The other folders are created automatically.
2. **Ninox**: enter the API key (in Ninox: Integrationen → API-Schlüssel generieren).
   Leave Team-ID and Datenbank-ID empty and click **Verbindung testen**. The app then shows the available IDs to copy in.
   Once the Datenbank-ID is filled in, "Testen" shows the tables and their fields. You need these for the rules file (see below).
3. **Speichern**.
4. Under "Allgemein", optionally enable **Mit Windows starten** and **Überwachung beim Programmstart automatisch starten**.

If table or field names in your Ninox differ from the defaults, "Verbindung testen" lists them. Enter only the differences in **Eigene Regeln bearbeiten** (file `eigene_regeln.toml`), then "Konfiguration neu laden". The standard rules arrive with every update automatically; your own rules are never overwritten.
5. **Stammdaten laden (Häuser, Kostensätze)**: reads houses with addresses and cost rates from Ninox (afterwards automatically once a day). The app then recognizes the house from the address in the notice and picks that house's cost rate for each approval period.
Tip: if the resident table has a **Geburtsdatum** field, enter its name there. The app then also finds residents whose customer number is unreadable, and it checks that customer number and birth date match – this prevents a notice from being filed under the wrong person.

## Testing
1. Tab **Analyse** → "PDF auswählen …" with a real notice. This shows what was recognized. Nothing is changed.
2. On the Übersicht tab, leave **Testlauf** ticked and click "Überwachung starten". Files get sorted and renamed, but nothing is sent to Ninox.
3. Once everything fits: untick Testlauf. From then on, notices are filed in Ninox.

## Day-to-day
- Just scan. The app picks up new PDFs from the input folder on its own.
- **Übersicht** shows everything that was processed. Clicking a row shows what was read out on the right (name, customer number, dates, approval periods, notes); double-clicking opens the PDF.
  **Auswahl entfernen** (or the Del key) removes selected rows from the list, **Liste leeren …** clears it completely. Only the list changes – PDFs and Ninox stay untouched; a backup of the old list is kept in the data folder.
- File names contain the name as read from the notice (e.g. `022P425485_BAB-Änderung_Mustermann_Max.pdf`). The name is only used for the file name – matching in Ninox always runs via customer number and birth date.
- **Prüfen (n)**: notices that weren't recognized clearly. Select one, check or correct the fields on the right, then **Mit diesen Angaben ablegen**. The reason tells you what's missing – e.g. "Kundennummer nicht eindeutig: 022P425485, 022P426485" means the number was read in two different ways; look it up in the PDF and enter it.
  If the customer number was simply missing in Ninox, add it there and click **Erneut automatisch versuchen**.
- The notice is attached to the **Antrag whose date matches** "auf dem am … gestellten Antrag" (±14 days). The Antragsart (EB, WB, …) therefore comes from the Antrag in Ninox. If no matching Antrag exists and there are several, the notice goes to "Prüfen".
- Notices that say benefits after a certain date are only paid once the last exam day is proven get a note in the Bescheid's "Notiz" field.
- Files in **Fehler** have a technical problem (e.g. no network). Once it's fixed, move them back into the input folder.

## Data protection
Text recognition runs entirely on this PC. Documents are only sent to Ninox.
Settings (including the API key), the log and the processing record are stored under `%APPDATA%\BAB-O-BOX`. The PC should be used by authorized staff only.
Delete the original archive regularly, or leave the "Original-Archiv" field empty.
