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
    def test_zeichen_pro_zeile_wird_zusammengesetzt_und_gelesen(self):
        inhalt = ("Agentur für Arbeit Musterstadt Datum: 03. März 2026 Kundennummer: 900X000001 "
                  "Bescheid für Max Muster, geb. am 01.01.2000 Änderungsbescheid zum Bescheid vom 30.07.2025")
        e = lesen("\n".join(list(inhalt)))
        self.assertIn("zusammengesetzt", e.hinweise)
        self.assertEqual(e.kundennummer, "900X000001")
        self.assertEqual(e.bescheiddatum, "2026-03-03")
        self.assertEqual(e.kategorie, "Änderung")

    def test_zusammengeklebter_text_ohne_leerzeichen_wird_gemeldet(self):
        inhalt = "AgenturfuerArbeitMusterstadtDatum:03.03.2026Kundennummer:900X000001" * 3
        e = lesen("\n".join(list(inhalt)))
        self.assertIn("Scan-Text unbrauchbar", e.hinweise)

    def test_normaler_text_wird_nicht_veraendert(self):
        text = KOPF + "Bescheid für Max Muster, geb. am 01.01.2000\n" + TABELLE
        self.assertEqual(bab_kern.einzelzeichen_reparieren(text), (text, False))

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


class NeueArtenUndDatenTests(unittest.TestCase):
    def test_widerspruchsverfahren_ist_kein_bescheid(self):
        text = KOPF + "Rechtsbehelfsstelle\nWiderspruchsverfahren von Herrn Muster wegen des Anspruchsbeginns\n"
        self.assertEqual(lesen(text).kategorie, "Widerspruch")

    def test_rechtsbehelfs_url_im_bescheid_loest_nichts_aus(self):
        text = (KOPF + "Bescheid für Max Muster, geb. am 01.01.2000\n" + TABELLE +
                "Informationen unter: www.arbeitsagentur.de/rechtsbehelfsstellen).\n")
        self.assertEqual(lesen(text).kategorie, "Bewilligung")

    def test_abhilfebescheid_ist_aenderung(self):
        text = KOPF + "Abhilfebescheid im Widerspruchsverfahren\nWiderspruchs-Nr: W-1\n" + TABELLE
        self.assertTrue((lesen(text).kategorie or "").startswith("Änderung"))

    def test_berechnungsbogen_allein(self):
        text = "Berechnungsbogen BAB zum Bescheid vom 19.02.2026\nKundenname Erle Muster 900X000002\n"
        self.assertEqual(lesen(text).kategorie, "Berechnungsbogen")

    def test_berechnungsbogen_hinten_im_bescheid_bleibt_bewilligung(self):
        text = (KOPF + "Bescheid für Max Muster, geb. am 01.01.2000\n" + TABELLE +
                "\nBerechnungsbogen BAB zum Bescheid vom 03.03.2026\n")
        self.assertEqual(lesen(text).kategorie, "Bewilligung")

    def test_ocr_komma_im_datum(self):
        self.assertEqual(lesen("Datum: 30, September 2025\n" + TABELLE).bescheiddatum, "2025-09-30")

    def test_gleichstand_datum_vor_bescheid_ueberschrift_gewinnt(self):
        text = ("Datum: 17. Juli 2024\nAnschreiben ohne Bescheid\n\n"
                "Datum: 14. August 2024\nÄnderungsbescheid gemäß § 48 SGB X\n" + TABELLE)
        self.assertEqual(lesen(text).bescheiddatum, "2024-08-14")

    def test_bescheid_vom_in_klartext_loest_gleichstand(self):
        text = ("Datum: 22. April 2026\nals Bevollmächtigte erhalten Sie den Bescheid vom 24. März 2026.\n"
                "Datum: 24.03.2026 13:31\n" + TABELLE)
        self.assertEqual(lesen(text).bescheiddatum, "2026-03-24")

    def test_tabelle_spaltenweise_zerlegt(self):
        text = (KOPF + "Berufsausbildungsbeihilfe\n07. 08. 2023\n01. 01. 2024\n01. 08. 2024\n31. 12, 2023\n"
                "31. 07. 2024\n31.01.2025\nmtl. Betrag\n1. 949,00\n1.998,00\n2. 017, 00\n")
        e = lesen(text)
        self.assertEqual(e.zeitraum_bis, "2025-01-31")
        self.assertEqual(e.betrag, "2.017,00")
        self.assertEqual(len(e.abschnitte), 3)


if __name__ == "__main__":
    unittest.main()
