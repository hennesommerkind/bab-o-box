# Feedback-Protokoll (Entwurf)

Ziel: Beim Freigeben festhalten, was die Erkennung vorgeschlagen hat und was am Ende gilt.
Daraus entstehen Fehlerquoten je Feld und Testfälle für die Regeln. Die Daten bleiben lokal.

## Wo es greift

- **Light:** `Verarbeitung.freigeben(pfad, werte)` in `bab_kern.py` (manuelle Freigabe aus „Prüfen“).
- **Pro:** dieselbe Stelle in `bab_gui.py` (`_pruefen_freigeben`), die Freigabe läuft über `self.v.freigeben(pfad, w)`.
- Das Protokoll wird neben `protokoll.csv` im Ordner `ordner.protokoll` abgelegt, als `feedback.jsonl`.

## Eine Zeile je freigegebenem Dokument

```json
{
  "zeit": "2026-10-29 16:50:00",
  "version": "1.0.25",
  "edition": "pro",
  "hinweise": ["kostensatz_mehrdeutig", "datum_eindeutig"],
  "regeln": ["kategorie:kopf:Änderung", "datum:labelled"],
  "felder": {
    "kategorie":     {"erkannt": "Bewilligung", "final": "Änderung", "geaendert": true,  "bestaetigt": true},
    "bescheiddatum": {"erkannt": "2026-03-06",  "final": "2026-03-06", "geaendert": false, "bestaetigt": true},
    "kundennummer":  {"erkannt": null,          "final": "900X000001", "geaendert": true,  "bestaetigt": true}
  }
}
```

- `erkannt` ist der Vorschlag der Erkennung, `final` der freigegebene Wert.
- `geaendert` ist wahr, wenn das Feld im Formular verändert wurde.
- `bestaetigt` ist wahr, wenn das Feld verändert oder bewusst abgehakt wurde.

## Bestätigungs-Regel

- Ein Feld zählt nur dann in die Quote, wenn `bestaetigt` wahr ist.
- Ohne bewusste Bestätigung würde jede unveränderte Freigabe als „richtig“ gelten, und die Fehlerquote wäre zu niedrig.
- Umsetzung in der Oberfläche: je Feld ein Häkchen „✓ geprüft“, das beim Ändern automatisch gesetzt wird.
  Freigabe ohne Häkchen ist möglich, aber dann bleibt das Feld unbestätigt.

## Datenschutz

- Keine Namen, keine Kundennummern, keine Adressen, keine Texte im Protokoll.
- Ungeklärt: Betrag und Datum gemeinsam mit dem Haus können zusammen identifizierend sein.
  Vorschlag: Beträge nur als Verhältnis speichern (erkannt == final ja/nein), das Datum nur mit Monat.
- Der Export geht nur über die bestehende Schwärzen-Funktion, und nur für Zeilen mit mindestens einer Korrektur.

## Auswertung (Bericht)

Je Feld:

- `n`: bestätigte Fälle
- `korrigiert`: Anzahl mit `geaendert = true`
- `quote`: korrigiert / n
- Häufigste Hinweise bei korrigierten Fällen

Ausgabe in der Übersicht oder als CSV. Der Bericht rechnet nur bestätigte Felder.

## Offen

1. Soll das Protokoll auch bei Light-Freigaben gelten, oder nur bei Pro? (Light hat nur fünf Felder, aber jede Freigabe ist ein Soll-Wert.)
2. Welche Hinweis-Codes? Heute sind Hinweise Fließtext. Für die Auswertung braucht es kurze Codes, die parallel zum Text gespeichert werden.
3. Datenschutzstelle klären, bevor Export und Auswertung in Betrieb gehen.
