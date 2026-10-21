# BAB-O-BOX Light – Kurzanleitung

BAB-O-BOX Light liest eingescannte BAB-Schreiben aus und **schlägt** Kundennummer, Name, Haus, Art und
Bescheiddatum vor. Jedes Dokument wird von dir geprüft und freigegeben – erst dann wird es benannt und abgelegt.
Es gibt keine Verbindung zu Ninox: Das Hochladen in Ninox machst du wie gewohnt selbst.

## Starten
Den Ordner `BAB-O-BOX-Light` auf den PC kopieren und **BAB-O-BOX-Light.exe** starten. Keine Installation nötig.

## Einmal einrichten (Reiter „Einstellungen“)
1. **Ordner**: „Scan-Eingang“ auf den Ordner setzen, in den der Scanner speichert. Die übrigen Ordner werden automatisch angelegt.
2. **Stammdaten laden …**: nacheinander die CSV-Exporte aus Ninox wählen – Häuser (nötig), Kostensätze und Rechtslage
   (beide optional, Abbrechen = überspringen). Damit erkennt die App das Haus an der Anschrift bzw. am Kostensatz.
   Ist auf dem PC schon BAB-O-BOX Pro eingerichtet, übernimmt Light deren Stammdaten beim ersten Start automatisch.
3. **Beschreibung zum Kopieren**: ankreuzen, was im Feld „Beschreibung“ stehen soll (Vorsatz, Art, Bescheiddatum,
   Zeitraum, Haus, Name, Kundennummer, Betrag) und in welchem Datumsformat – z. B. „BAB Änderungsbescheid 12/25“.
4. **Namen von Mitarbeitenden** (unter „Allgemein“): Nachnamen der Kolleg*innen, an die Abtretungsschreiben gehen.
   Diese Namen werden nie als Bewohner*in erkannt.
5. **Speichern**. Bei Bedarf **Mit Windows starten** einschalten.

## Im Alltag
1. Scannen. Die App holt neue PDFs aus dem Eingangsordner (Übersicht → „Überwachung starten“ oder „Jetzt verarbeiten“).
2. Reiter **Prüfen**: oben die Liste, unten die Vorschau aller Seiten (Mausrad oder ▲ ▼, vergrößern mit + und −),
   rechts die Vorschläge.
   - Jede Angabe mit der Vorschau vergleichen und bei Bedarf korrigieren. Das Bescheiddatum geht auch über den Kalender 📅.
   - Fehlt etwas („fehlt: Haus“) oder ist etwas unsicher, steht es orange in der Liste.
   - **Beschreibung** mit 📋 in die Zwischenablage kopieren und in Ninox einfügen.
   - **▸ Weitere Informationen aus der Erkennung** klappt dezent auf, was die ausführliche Erkennung noch gefunden hat
     (Antragsdatum, Zeiträume, Beträge, Kostensatz, Hinweise) – nur zum Nachsehen.
   - **Freigeben**: Danach ist das nächste Dokument ausgewählt.
   - **Alle neu erkennen**: z. B. nachdem die Stammdaten geladen wurden.
3. Freigegebene Dokumente liegen benannt im Ordner **Verarbeitet**, z. B. `022P425485_BAB-Bewilligung_Mustermann_Max.pdf`.
   Von dort in Ninox hochladen.

Arten: Bewilligung, Änderung (BAföG, TS), Ablehnung, Aufhebung, Anhörung, Mitwirkung, Anforderungsschreiben.

## Datenschutz
Texterkennung (nur Deutsch) und Auswertung laufen vollständig auf diesem PC; es wird nichts ins Internet gesendet.
Einstellungen und Verarbeitungsliste liegen unter `%APPDATA%\BAB-O-BOX Light`.
