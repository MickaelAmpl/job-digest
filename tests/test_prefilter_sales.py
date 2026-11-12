"""Sales roles (owner, 04.11.2026): SDR/BDR, business development, account executive and manager, partnerships
are kept; field, retail, call-centre and German 'Vertrieb' titles stay out. No network."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import prefilter  # noqa: E402


def v(title, description=""):
    return prefilter.verdict({"title": title, "description": description})


def why(title):
    return prefilter.reason({"title": title})


SALES_TITLES = ["Sales Development Representative (SDR / BDR) - Fintech Payouts", "Business Development Representative",
                "Account Executive (m/f/d)", "Account Manager (w/m/d) Logistics",
                "Sales Manager - Native Russian Speaking (m/f/x)", "Partner Manager, Consulting Partnerships",
                "Inside Sales Specialist", "BDR", "Key Account Manager"]


class SalesKept(unittest.TestCase):
    def test_sales_titles_keep_under_sales(self):
        """Owner 04.11.2026: the sales group passes these; no other rule is credited first."""
        for t in SALES_TITLES:
            with self.subTest(t):
                self.assertEqual(v(t), "keep")
                self.assertEqual(why(t), "sales")

    def test_target_still_credited_first(self):
        for t in ["Junior Sales Consultant", "Junior Account Executive"]:
            with self.subTest(t):
                self.assertEqual(v(t), "keep")
                self.assertEqual(why(t), "target")

    def test_graduate_sales_programme(self):
        self.assertEqual(v("Graduate Sales Programme"), "keep")
        self.assertEqual(why("Graduate Sales Programme"), "graduate")

    def test_ai_sales_is_ai_edge_not_hard_excluded(self):
        """AI_EDGE is checked before groups, so the title is credited to ai_edge."""
        self.assertEqual(v("AI Sales Representative"), "keep")
        self.assertEqual(why("AI Sales Representative"), "ai_edge")


class SalesSenior(unittest.TestCase):
    def test_senior_sales_titles(self):
        for t in ["Senior Account Executive", "Head of Sales", "Sales Director Europe", "Sales Expert (w/m/d)"]:
            with self.subTest(t):
                self.assertEqual(v(t), "senior")


class SalesExcluded(unittest.TestCase):
    def test_field_retail_call_centre_and_soft_excluded(self):
        for t in ["Sales Trainee im Außendienst (w/m/d) - Wien", "Verkäufer:in Filiale", "Call Center Sales Agent",
                  "Sales Business Analyst", "Customer Service Sales Representative"]:
            with self.subTest(t):
                self.assertEqual(v(t), "excluded_field")


class SalesNotTargetRole(unittest.TestCase):
    def test_german_vertrieb_and_name_look_alikes_stay_out(self):
        """'Vertrieb' is not added (insurance field sales in our feeds); 'presales' and 'salesforce' are not 'sales'."""
        for t in ["Vertriebsmitarbeiter:in Wien", "Vertriebsassistent (w|m|d)", "Gebietsleiter:in Vertrieb",
                  "Presales Solution Designer Cyber Security (w/m/d)", "Salesforce Administrator"]:
            with self.subTest(t):
                self.assertEqual(v(t), "not_target_role")
                self.assertIsNone(why(t))

    def test_switching_sales_group_off_drops_its_titles(self):
        """Owner can switch the group off after the preview: the sales titles then drop as not_target_role."""
        pattern, _ = prefilter.ROLE_GROUPS["sales"]
        with mock.patch.dict(prefilter.ROLE_GROUPS, {"sales": (pattern, False)}):
            self.assertEqual(v("Account Executive (m/f/d)"), "not_target_role")
            self.assertIsNone(why("Account Executive (m/f/d)"))


class SalesGerman(unittest.TestCase):
    def test_required_german_drops_sales_title(self):
        self.assertEqual(v("Account Executive", "Fließendes Deutsch und sehr gutes Englisch"), "german_required")

    def test_adjective_endings_count_as_required(self):
        """04.11.2026: 'fließendes/sehr gutes Deutsch' were missed; the endings -e, -en, -es, -em now match."""
        for text in ["Fließendes Deutsch und sehr gutes Englisch", "Sehr gutes Deutsch in Wort und Schrift",
                     "verhandlungssicheres Deutsch", "mit sehr guten Deutschkenntnissen"]:
            with self.subTest(text):
                self.assertTrue(prefilter.german_required(text))
                self.assertEqual(v("Account Executive", text), "german_required")

    def test_softened_and_english_only_stay_keep(self):
        for text in ["Fließendes Deutsch von Vorteil", "sehr gute Englischkenntnisse"]:
            with self.subTest(text):
                self.assertFalse(prefilter.german_required(text))
                self.assertEqual(v("Account Executive", text), "keep")


class SalesRound2(unittest.TestCase):
    def test_verkaeufer_and_back_office_sales_stay_out(self):
        """04.11.2026 round 2: German 'Verkäufer' titles and back-office sales support are excluded."""
        for t in ["A1 B2B Outbound Sales Verkäufer:in Salzburg (w/m/d)", "Sales Assistant",
                  "Sales Office Specialist (m/w/d)", "Spezialist:in (w/m/d) im Central Sales Support"]:
            with self.subTest(t):
                self.assertEqual(v(t), "excluded_field")


class SalesGroupRegistry(unittest.TestCase):
    def test_sales_is_the_last_group(self):
        """The group is added last, so no earlier group changes its credit order."""
        self.assertEqual(list(prefilter.ROLE_GROUPS)[-1], "sales")
        self.assertTrue(prefilter.ROLE_GROUPS["sales"][1])


if __name__ == "__main__":
    unittest.main()
