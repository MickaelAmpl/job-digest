"""Production titles are not product roles (stage 12, 10.11.2026). TARGET matched 'product|produkt' as a prefix, so
'Produktionsmitarbeiter' and 'Stock Production Member' passed as product work. No network; verdicts only."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import prefilter  # noqa: E402

PRODUCTION_TITLES = [
    "Produktionsmitarbeiter (m/w/d)",
    "ProduktionsmitarbeiterIn im Schichtbetrieb",
    "Produktionsplaner (m/w/d)",
    "Produktionsmitarbeiter:in Papierfabrik",
    "Stock Production Member",
    "(Junior) Leather Production Specialist",
]

PRODUCT_TITLES = [
    "Produktmanager:in",
    "Product Manager",
    "Product Owner",
    "Junior Produktmanager",
    "Product Operations Manager",
    "Produktmanagement Trainee",
    "Global Product Owner",
]


def v(title):
    return prefilter.verdict({"title": title, "description": ""})


class ProductionTitlesTest(unittest.TestCase):
    def test_production_titles_are_not_keep(self):
        for title in PRODUCTION_TITLES:
            with self.subTest(title=title):
                self.assertNotEqual(v(title), "keep")

    def test_product_titles_still_keep(self):
        for title in PRODUCT_TITLES:
            with self.subTest(title=title):
                self.assertEqual(v(title), "keep")


if __name__ == "__main__":
    unittest.main()
