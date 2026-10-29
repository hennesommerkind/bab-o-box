"""Regressionstests für die Dokumenterkennung (bab_kern.regeln_anwenden).
Alle Texte sind synthetisch – keine echten Bescheide im Repo."""
import pathlib
import tomllib
import unittest

import bab_kern

ROOT = pathlib.Path(__file__).resolve().parent.parent
with open(ROOT / "config_standard.toml", "rb") as f:
    CFG = tomllib.load(f)
CFG.setdefault("_stammdaten", None)


def lesen(text):
    return bab_kern.regeln_anwenden(text, CFG)


KOPF = "Agentur für Arbeit Musterstadt\n\nDatum: 03. März 2026\n\n"
TABELLE = "von bis mtl. Betrag\n\n01.04.2026 30.09.2026 1.000,00 €\n"


class KategorieTests(unittest.TestCase):
    def test_aenderung_gewinnt_gegen_bewilligungsbescheid_im_anschreiben(self):
        text = (KOPF + "Anlage\n\nBewilligungsbescheid\n\n"
                "Änderungsbescheid zum Bescheid vom 26.06.2026; gemäß § 45 SGB X\n" + TABELLE)
        self.assertEqual(lesen(text).kategorie, "Änderung")

    def test_plain_bewilligung_mit_bescheid_fuer_kopf(self):
        text = KOPF + "Bescheid für Max Muster, geb. am 01.01.2000\n" + TABELLE
        self.assertEqual(lesen(text).kategorie, "Bewilligung")

    def test_bewilligungsbescheid_als_hinweis_ohne_kopf(self):
        text = KOPF + "Hiermit erhalten Sie den Bewilligungsbescheid.\n" + TABELLE
        self.assertEqual(lesen(text).kategorie, "Bewilligung")


class DatumTests(unittest.TestCase):
    def test_ocr_fehler_fuenfstelliges_jahr_wird_ignoriert(self):
        text = ("Datum: 06. März 20226\n"
                "Berechnungsbogen BAB zum Bescheid vom 06.03.2026\n"
                "Änderungsbescheid zum Bescheid vom 30.07.2025\n" + TABELLE)
        self.assertEqual(lesen(text).bescheiddatum, "2026-03-06")

    def test_referenz_auf_altbescheid_zaehlt_nicht(self):
        text = "Änderungsbescheid zum Bescheid vom 30.07.2025\n" + TABELLE
        self.assertIsNone(lesen(text).bescheiddatum)

    def test_unplausibles_jahr_wird_ignoriert(self):
        text = "Datum: 06. März 1926\nDatum: 03.03.2026\n" + TABELLE
        self.assertEqual(lesen(text).bescheiddatum, "2026-03-03")


class HinweisTests(unittest.TestCase):
    def test_zeichen_pro_zeile_wird_als_unbrauchbar_gemeldet(self):
        text = "\n".join(list("Agentur für Arbeit Musterstadt Bescheid ") * 2)
        e = lesen(text)
        self.assertIn("Scan-Text unbrauchbar", e.hinweise)

    def test_vertreter_schreiben_wird_gemeldet(self):
        text = KOPF + "Kundennummer: 900X000001\nMein Zeichen: 021-900X000002\nals Vertreter erhalten Sie den Bescheid vom 03.03.2026.\n" + TABELLE
        self.assertIn("Bevollmächtigte/Vertreter", lesen(text).hinweise)

    def test_ocr_tolerant_ale_bevollmaechtigte(self):
        text = KOPF + "Kundennummer: 900X000001\nMein Zeichen: 021-900X000002\nale Bevollmächtigte erhalten Sie den Bescheid.\n" + TABELLE
        self.assertIn("Bevollmächtigte/Vertreter", lesen(text).hinweise)

    def test_letzter_abschnitt_null_euro(self):
        text = KOPF + ("von bis mtl. Betrag\n\n01.04.2026 30.06.2026 1.792,00 €\n"
                       "01.07.2026 30.09.2027 0,00 €\n")
        self.assertIn("Letzter Abschnitt hat 0,00", lesen(text).hinweise)

    def test_standardtext_bevollmaechtigte_ohne_zweite_nummer_kein_hinweis(self):
        text = KOPF + "Kundennummer: 900X000001\nals Bevollmächtigte erhalten Sie den Bescheid.\n" + TABELLE
        self.assertNotIn("Bevollmächtigte/Vertreter", lesen(text).hinweise)

    def test_normaler_bescheid_ohne_hinweise_auf_scan_oder_vertreter(self):
        text = KOPF + "Bescheid für Max Muster, geb. am 01.01.2000\n" + TABELLE
        hinweise = lesen(text).hinweise
        self.assertNotIn("Scan-Text", hinweise)
        self.assertNotIn("Bevollmächtigte", hinweise)


if __name__ == "__main__":
    unittest.main()
