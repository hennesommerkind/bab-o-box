# BAB-O-BOX Pro – Kurzanleitung

Die Pro-Ausgabe erkennt Bescheide und legt sie automatisch in Ninox ab. Wer jedes Dokument selbst prüfen und
freigeben möchte, nimmt **BAB-O-BOX Light** (eigenes ZIP, ohne Ninox-Anbindung).

## Starten
Den Ordner `BAB-O-BOX-Pro` auf den PC kopieren (z. B. `C:\Programme\BAB-O-BOX-Pro` oder ein Netzlaufwerk) und
**BAB-O-BOX-Pro.exe** starten. Eine Installation ist nicht nötig.

## Einmal einrichten (Reiter „Einstellungen“)
1. **Ordner**: „Scan-Eingang“ auf den Ordner setzen, in den der Scanner speichert. Die übrigen Ordner werden automatisch angelegt.
2. **Ninox**: API-Schlüssel eintragen (in Ninox: Integrationen → API-Schlüssel erzeugen).
   Team-ID und Datenbank-ID leer lassen und **Verbindung testen** klicken – die App zeigt die verfügbaren IDs zum Übernehmen.
   Ist die Datenbank-ID eingetragen, zeigt „Testen“ die Tabellen und Felder.
3. **Speichern**.
4. Unter „Allgemein“ bei Bedarf **Mit Windows starten** und **Überwachung beim Programmstart automatisch starten** einschalten.

Weichen Tabellen- oder Feldnamen in eurem Ninox ab, listet „Verbindung testen“ sie auf. Nur die Abweichungen in
**Eigene Regeln bearbeiten** (Datei `eigene_regeln.toml`) eintragen, dann „Konfiguration neu laden“. Die Standardregeln
kommen mit jedem Update automatisch, eigene Regeln werden nie überschrieben.

5. **Stammdaten laden**: holt Häuser, Kostensätze und Rechtslage aus Ninox (danach automatisch einmal täglich).
   Ohne API-Zugang: **… aus CSV-Export** und nacheinander den Export der Häuser, der Kostensätze und (optional) der Rechtslage wählen.

## Testen
1. Reiter **Analyse** → „PDF auswählen …“ mit einem echten Bescheid. Zeigt, was erkannt wurde – es wird nichts verändert.
2. Auf der Übersicht **Testlauf** angehakt lassen und „Überwachung starten“. Dateien werden sortiert und umbenannt, aber nichts an Ninox gesendet.
3. Passt alles: Testlauf abhaken. Ab dann werden Bescheide in Ninox abgelegt.

## Im Alltag
- Einfach scannen. Die App holt neue PDFs selbstständig aus dem Eingangsordner.
- **Übersicht** zeigt alles Verarbeitete. Ein Klick zeigt rechts die ausgelesenen Angaben, ein Doppelklick öffnet das PDF.
  **Auswahl entfernen** (oder Entf) nimmt Zeilen aus der Liste, **Liste leeren …** leert sie ganz. Nur die Liste ändert sich – PDFs und Ninox bleiben unberührt.
- Dateinamen enthalten den Namen aus dem Bescheid (z. B. `022P425485_BAB-Änderung_Mustermann_Max.pdf`). Der Name dient nur dem Dateinamen – in Ninox wird immer über Kundennummer und Geburtsdatum zugeordnet.
- **Prüfen (n)**: Bescheide, die nicht eindeutig erkannt wurden. Auswählen, Angaben rechts prüfen oder korrigieren, dann **Mit diesen Angaben ablegen**.
  Fehlte nur die Kundennummer in Ninox: dort nachtragen und **Erneut automatisch versuchen**.
- Der Bescheid wird an den **Antrag mit passendem Antragsdatum** gehängt (±14 Tage). Die Antragsart (EB, WB …) kommt deshalb vom Antrag in Ninox.
- Kostensatz: Die App rechnet „Bedarf für den Lebensunterhalt“ minus Pauschale für sonstige Bedürfnisse (aus der Rechtslage) und sucht den passenden Kostensatz des Hauses.
- **Anhörung, Mitwirkung und Anforderungsschreiben** werden erkannt, aber nicht automatisch abgelegt – sie landen unter „Prüfen“.
- Dateien in **Fehler** haben ein technisches Problem (z. B. kein Netz). Danach zurück in den Eingangsordner legen.

## Bescheide pseudonymisieren (Reiter „Schwärzen“)
Zum Weitergeben an die Entwicklung ohne personenbezogene Daten. Braucht die lokale KI (Ollama, siehe Einstellungen).
1. Ordner mit PDF-Bescheiden und einen Ausgabeordner wählen (nicht innerhalb des Bescheid-Ordners).
2. „Schnell“ angehakt lassen: Die KI bekommt dann nur das Anschriftenfeld und die Zeilen mit Wörtern, die nicht zum
   Standardtext der Agentur gehören (etwa ein Drittel des Textes) – rund dreimal schneller. Je mehr Bescheide in einem
   Durchgang, desto besser erkennt das Programm den Standardtext.
3. „Pseudonymisieren“ – Namen, Kundennummern, Geburtsdaten, IBAN, private Anschriften, Betriebe, Telefon und E-Mail werden durch Fantasiewerte im gleichen Format ersetzt.
4. Die Textdateien in `zum_Senden` lesen (Warnungen stehen in `bericht.txt`) und erst dann weitergeben.
5. `NUR_LOKAL\zuordnung.csv` enthält die Zuordnung zu den echten Daten – nie weitergeben, nach der Kontrolle löschen.

## Nur ein Rechner gleichzeitig (Sperre)
Beim Start der Überwachung (oder „Jetzt verarbeiten“) legt das Programm im Scan-Ordner die Datei `BAB-O-BOX.lock` an –
mit Computername, Benutzer*in und Uhrzeit. Beim Stoppen und beim Beenden wird sie wieder gelöscht.
Versucht ein zweiter Rechner zu starten, erscheint „Der Scan-Ordner wird gerade bearbeitet von …“. Die Sperre lässt sich
nach einer Sicherheitsabfrage übernehmen – nur tun, wenn dort sicher nicht mehr gearbeitet wird (z. B. nach einem
Absturz). Der andere Rechner stoppt dann beim nächsten Durchlauf und bekommt einen Hinweis. Eine Sperre vom eigenen
Rechner, dessen Programm nicht mehr läuft, wird automatisch übernommen.

## Datenschutz
Die Texterkennung läuft vollständig auf diesem PC. Dokumente gehen nur an Ninox.
Einstellungen (inkl. API-Schlüssel), Protokoll und Verarbeitungsliste liegen unter `%APPDATA%\BAB-O-BOX`.
Das Original-Archiv regelmäßig leeren oder das Feld „Original-Archiv“ leer lassen.
