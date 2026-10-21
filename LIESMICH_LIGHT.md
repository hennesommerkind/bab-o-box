# BAB-O-BOX Light – Kurzanleitung

BAB-O-BOX Light liest eingescannte BAB-Schreiben aus und **schlägt** Kundennummer, Name, Haus, Art und
Bescheiddatum vor. Jedes Dokument wird von dir geprüft und freigegeben – erst dann wird es benannt und abgelegt.
Es gibt keine Verbindung zu Ninox: Das Hochladen in Ninox machst du wie gewohnt selbst.

## Starten
Den Ordner `BAB-O-BOX-Light` auf den PC kopieren und **BAB-O-BOX-Light.exe** starten. Keine Installation nötig.

## Einmal einrichten (Reiter „Einstellungen“)
1. **Ordner**: „Scan-Eingang“ auf den Ordner setzen, in den der Scanner speichert. Die übrigen Ordner werden automatisch angelegt.
2. **Häuserliste laden**: den CSV-Export der Häuser-Tabelle aus Ninox wählen. Damit erkennt die App das Haus an der
   Anschrift und bietet die Häuser zur Auswahl an.
3. **Speichern**. Unter „Allgemein“ bei Bedarf **Mit Windows starten** einschalten.

## Im Alltag
1. Scannen. Die App holt neue PDFs aus dem Eingangsordner (Übersicht → „Überwachung starten“ oder „Jetzt verarbeiten“).
2. Reiter **Prüfen**: oben die Liste, unten die Vorschau des Dokuments, rechts die Vorschläge.
   - Jede Angabe mit der Vorschau vergleichen und bei Bedarf korrigieren. Blättern mit ◀ ▶, vergrößern mit + und −.
   - Fehlt etwas („fehlt: Haus“) oder ist etwas unsicher, steht es in der Liste.
   - Häkchen **„Ich habe alle Angaben mit dem Dokument verglichen“** setzen, dann **Freigeben**.
     Wird danach noch etwas geändert, verschwindet das Häkchen wieder.
3. Freigegebene Dokumente liegen benannt im Ordner **Verarbeitet**, z. B. `022P425485_BAB-Bewilligung_Mustermann_Max.pdf`.
   Von dort in Ninox hochladen.

Arten: Bewilligung, Änderung (BAföG, TS), Ablehnung, Aufhebung, Anhörung, Mitwirkung, Anforderungsschreiben.

## Datenschutz
Texterkennung (nur Deutsch) und Auswertung laufen vollständig auf diesem PC; es wird nichts ins Internet gesendet.
Einstellungen und Verarbeitungsliste liegen unter `%APPDATA%\BAB-O-BOX Light`.
