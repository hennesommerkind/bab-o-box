"""Regeltests für die Pseudonymisierung (feste Muster, Anschriften, Restprüfung).

Alle Beispiele sind SYNTHETISCH – keine echten Namen, Adressen oder Kennungen.
Ausführen: python3 -m unittest tests/test_schwaerzen_regeln.py  (im Projektordner)
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bab_schwaerzen as s  # noqa: E402

import tomllib  # noqa: E402

with open(Path(__file__).resolve().parents[1] / "config_standard.toml", "rb") as f:
    CFG = tomllib.load(f)
CFG.setdefault("ki", {})


def laufe(text: str) -> s.Ergebnis:
    return s.pseudonymisieren(text, CFG, s.Zuordnung(), "test.pdf", mit_ki=False)


class Anschriften(unittest.TestCase):
    def test_einzeilig_mit_zimmer(self):
        e = laufe("Anschrift: - Zimmer Nr. 127 Beispielweg 7, 12345 Testhausen")
        self.assertNotIn("Beispielweg", e.text)
        self.assertNotIn("Testhausen", e.text)
        self.assertNotIn("127", e.text)
        self.assertIn("Pseudoweg", e.text)

    def test_strasse_darueber_plz_darunter(self):
        e = laufe("Absender:\nMusterperson\nBeispielweg 7\n(Straße, Hausnummer, Postfach)\n12345 Testhausen\n")
        self.assertNotIn("Beispielweg", e.text)
        self.assertNotIn("Testhausen", e.text)
        self.assertIn("Pseudostadt", e.text)

    def test_zeile_mit_zusatztext_rechts(self):
        e = laufe("Beispielweg 7 Servicerufnummer für Rückfragen\n12345 Testhausen 0800 4 5555 00\n")
        self.assertNotIn("Beispielweg", e.text)
        self.assertNotIn("Testhausen", e.text)
        self.assertIn("0800 4 5555 00", e.text)  # Servicenummer bleibt

    def test_strassen_mit_bereich_und_punkt(self):
        e = laufe("Beispiel-Str. 14-18\n12345 Testhausen\n")
        self.assertNotIn("Beispiel-Str.", e.text)

    def test_behoerdenadresse_bleibt(self):
        e = laufe("Besucheradresse\nSonnenallee 282\n12057 Berlin\n")
        self.assertIn("Sonnenallee 282", e.text)
        self.assertIn("12057 Berlin", e.text)

    def test_schon_pseudonymisiert_bleibt(self):
        e = laufe("Pseudoweg 12, 99012 Pseudostadt")
        self.assertEqual(e.text.count("Pseudostadt"), 1)
        self.assertNotIn("99012 Pseudostadt Pseudostadt", e.text)

    def test_plz_ohne_strasse_wird_gemeldet(self):
        e = laufe("Herr Muster\n12345 Testhausen\n")
        self.assertTrue(any("PLZ mit Ort" in w for w in e.warnungen), e.warnungen)


class Geburtsdaten(unittest.TestCase):
    def test_ocr_lesefehler_im_geburtsdatum(self):
        e = laufe("Bescheid für Max Mustermann, geb. am 10:02.2006 |")
        self.assertNotIn("10:02", e.text)
        self.assertIn("01.01.2006", e.text)

    def test_normales_geburtsdatum_unveraendert_ersetzt(self):
        e = laufe("geb. am 14.09.2001")
        self.assertIn("01.01.2001", e.text)

    def test_nicht_ersetzt_wird_gemeldet(self):
        # Restprüfung für einen Lesefehler, den die Ersetzung nicht erreicht
        z = s.Zuordnung()
        self.assertIn("Geburtsdatum (mit Lesefehler) nicht ersetzt",
                      s.restpruefung("geb. am 1O:02.2006", CFG, z))


class Iban(unittest.TestCase):
    def test_iban_mit_ocr_punkten_und_fragezeichen(self):
        e = laufe("Konto DE12 3456 7890 1234.56? 78 bei Musterbank")
        self.assertNotIn("3456 7890", e.text)

    def test_ba_konto_bleibt(self):
        e = laufe("IBAN: DE50 7600 0000 0076 0016 17")
        self.assertIn("DE50 7600 0000 0076 0016 17", e.text)

    def test_iban_mit_leerzeichen_wird_ersetzt(self):
        e = laufe("IBAN: DE12 3456 7890 1234 5678 90")
        self.assertNotIn("3456 7890", e.text)


class Zimmer(unittest.TestCase):
    def test_zimmer_nummer_wird_ersetzt(self):
        e = laufe("Zimmer Nr. 127 im Wohnheim")
        self.assertNotIn("127", e.text)
        self.assertIn("Zimmer Nr.", e.text)


class Behoerdentexte(unittest.TestCase):
    def test_standardtext_bleibt_unveraendert(self):
        text = "Die Berechnung der Berufsausbildungsbeihilfe ist der beigefügten Anlage zu entnehmen.\n" \
               "Für Zahlungen wird das Konto (DE00 0000 0000 0000 0003 00) verwendet.\n"
        e = laufe(text)
        self.assertIn("Berufsausbildungsbeihilfe ist der beigefügten Anlage", e.text)


class Hausanschriften(unittest.TestCase):
    """Kolping-Häuser bleiben stehen, auch bei OCR-Fehlern im Straßennamen."""
    CFG_HAUS = dict(CFG)
    CFG_HAUS["_stammdaten"] = {"haeuser": [{"name": "Kolping Testhausen gGmbH", "strasse": "Beispielweg 3",
                                            "plz": "12345", "ort": "Testhausen"}]}

    def _laufe(self, text):
        return s.pseudonymisieren(text, self.CFG_HAUS, s.Zuordnung(), "test.pdf", mit_ki=False)

    def test_haus_bleibt_stehen(self):
        e = self._laufe("Anschrift: Beispielweg 3, 12345 Testhausen")
        self.assertIn("Beispielweg 3", e.text)

    def test_haus_mit_ocr_fehler_bleibt_stehen(self):
        e = self._laufe("Anschrift: Beispielwek 3, 12345 Testhausen")
        self.assertIn("Beispielwek 3", e.text)

    def test_fremde_adresse_wird_ersetzt(self):
        e = self._laufe("Anschrift: Anderer Weg 9, 54321 Fremdhausen")
        self.assertNotIn("Anderer Weg", e.text)


if __name__ == "__main__":
    unittest.main()
