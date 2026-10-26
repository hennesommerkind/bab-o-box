# BAB-O-BOX Light – Kurzanleitung

BAB-O-BOX Light liest eingescannte BAB-Schreiben aus und **schlägt** Kundennummer, Name, Haus, Art und
Bescheiddatum vor. Jedes Dokument wird von dir geprüft und freigegeben – erst dann wird es benannt und abgelegt.
Es gibt keine Verbindung zu Ninox: Das Hochladen in Ninox machst du wie gewohnt selbst.

## Starten
Den Ordner `BAB-O-BOX-Light` auf den PC kopieren und **BAB-O-BOX-Light.exe** starten. Keine Installation nötig.

## Einmal einrichten (Reiter „Einstellungen“)
1. **Ordner**: „Scan-Eingang“ auf den Ordner setzen, in den der Scanner speichert. Die übrigen Ordner werden automatisch angelegt.
2. **Stammdaten**: mit **Häuser laden …**, **Kostensätze laden …** und **Rechtslage laden …** jeweils den CSV-Export
   aus Ninox wählen – einzeln, z. B. nur die Kostensätze, wenn sich etwas geändert hat. Zuerst die Häuser laden.
   Damit erkennt die App das Haus an der Anschrift bzw. am Kostensatz.
   Ist auf dem PC schon BAB-O-BOX Pro eingerichtet, übernimmt Light deren Stammdaten beim ersten Start automatisch.
3. **Beschreibung zum Kopieren**: ankreuzen, was im Feld „Beschreibung“ stehen soll (Vorsatz, Art, Bescheiddatum,
   Zeitraum, Haus, Name, Kundennummer, Betrag) und in welchem Datumsformat – z. B. „BAB Änderungsbescheid 12/25“.
4. **Dateiname**: Muster für die Benennung, z. B. `{kundennummer}_BAB-{art}_{name}` oder
   `{haus}_{datum}_{art_lang}_{name}`, dazu das Datumsformat. Unabhängig von der Beschreibung; ein Beispiel wird angezeigt.
5. **Namen von Mitarbeitenden** (unter „Allgemein“): Nachnamen der Kolleg*innen, an die Abtretungsschreiben gehen.
   Diese Namen werden nie als Bewohner*in erkannt.
6. **Speichern**. Bei Bedarf **Mit Windows starten** einschalten.

## Im Alltag
1. Scannen. Die App holt neue PDFs aus dem Eingangsordner (Übersicht → „Überwachung starten“ oder „Jetzt verarbeiten“).
2. Reiter **Prüfen**: oben die Liste, unten die Vorschau aller Seiten (Mausrad oder ▲ ▼, vergrößern mit + und −),
   rechts die Vorschläge.
   - Jede Angabe mit der Vorschau vergleichen und bei Bedarf korrigieren. Das Bescheiddatum geht auch über den Kalender 📅.
   - Fehlt etwas („fehlt: Haus“) oder ist etwas unsicher, steht es orange in der Liste.
   - **Beschreibung** mit 📋 in die Zwischenablage kopieren und in Ninox einfügen.
   - **▸ Weitere Informationen aus der Erkennung** klappt dezent auf, was die ausführliche Erkennung noch gefunden hat
     (Antragsdatum, Zeiträume, Beträge, Kostensatz, Hinweise) – nur zum Nachsehen.
   - **Freigeben**: Die Datei wird nach dem Muster benannt und in **Verarbeitet** gelegt, die **Kundennummer** landet in
     der Zwischenablage (kurze Einblendung unten rechts), und der Ordner **Verarbeitet** öffnet sich mit der markierten
     Datei – ist er schon offen, wird dieses Fenster nach vorn geholt. Danach ist das nächste Dokument ausgewählt.
   - **Zuletzt freigegeben – für Ninox** (rechts): 1 Kundennummer, 2 Beschreibung, 3 Datei. Jedes Feld lässt sich mit
     der Maus direkt in Ninox ziehen (Text ins Textfeld, die Datei ins Dateifeld) oder mit 📋 kopieren.
   - **🗑 Löschen …** (oder Entf): ausgewählte Dokumente (mehrere mit Strg/Umschalt) aus „Prüfen“ endgültig löschen –
     nach einer deutlichen Warnung. Sie kommen nicht in den Papierkorb; nur der Scan im Original-Archiv bleibt (falls eingerichtet).
     In der Übersicht bleibt ein Eintrag „gelöscht“.
   - Oben stehen wie auf der Übersicht Status, „Jetzt verarbeiten“, Start/Stopp und die Ordner-Knöpfe.
   - Nach dem Laden von Stammdaten bietet die App an, die offenen Dokumente neu zu erkennen.
3. In Ninox: Bewohner*in über die Kundennummer suchen (Strg+V), Beschreibung und Datei hineinziehen.

Die abgelegten PDFs sind so groß wie der Scan: Die erkannte Schrift wird nur als unsichtbare Textebene hinzugefügt
(die Datei bleibt durchsuchbar).

Arten: Bewilligung, Änderung (BAföG, TS), Ablehnung, Aufhebung, Anhörung, Mitwirkung, Anforderungsschreiben.

## Nur ein Rechner gleichzeitig (Sperre)
Beim Start der Überwachung (oder „Jetzt verarbeiten“) legt das Programm im Scan-Ordner die Datei `BAB-O-BOX.lock` an –
mit Computername, Benutzer*in und Uhrzeit. Beim Stoppen und beim Beenden wird sie wieder gelöscht.
Versucht ein zweiter Rechner zu starten, erscheint „Der Scan-Ordner wird gerade bearbeitet von …“. Die Sperre lässt sich
nach einer Sicherheitsabfrage übernehmen – nur tun, wenn dort sicher nicht mehr gearbeitet wird (z. B. nach einem
Absturz). Der andere Rechner stoppt dann beim nächsten Durchlauf und bekommt einen Hinweis. Eine Sperre vom eigenen
Rechner, dessen Programm nicht mehr läuft, wird automatisch übernommen.

## Datenschutz
Texterkennung (nur Deutsch) und Auswertung laufen vollständig auf diesem PC; es wird nichts ins Internet gesendet.
Einstellungen und Verarbeitungsliste liegen unter `%APPDATA%\BAB-O-BOX Light`.
