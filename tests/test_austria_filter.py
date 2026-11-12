"""QA round 2 (09.10.2026), pipeline2-05 / realdata2-03: the Austria check for company feeds. Full SuccessFactors
boards carry foreign 'Remote' jobs, and the old case-insensitive regex matched 'remote' and the English word 'at'.
Pure function tests, no network."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import fetch_companies  # noqa: E402


class AustriaFilter(unittest.TestCase):
    def check(self, location, expected, title=""):
        self.assertIs(fetch_companies.is_austrian(location, title), expected,
                      f"location={location!r} title={title!r}")

    def test_foreign_remote_is_not_austrian(self):
        self.check("Remote, GB", False)
        self.check("Remote Worker, GB", False)
        self.check("Munich, DE", False)
        self.check("DevOps Engineer with French | Remote, RO", False)

    def test_austrian_places_and_codes(self):
        self.check("Wien, AT", True)
        self.check("Vienna, Austria", True)
        self.check("Remote, AT", True)
        self.check("Remote - Österreich", True)
        self.check("Remote (Österreich)", True)
        self.check("Graz", True)
        self.check("AT-1030 Wien", True)
        self.check("Wien / München", True)
        self.check("St. Pölten", True)
        self.check("Steyr, at", True)  # SmartRecruiters country code, lowercase, last token (Deloitte Austria)
        self.check("Kärnten", True)

    def test_remote_hybrid_home_office_alone_is_austrian(self):
        # All companies.json employers are Austrian: a location that names no country counts as Austrian.
        self.check("Remote", True)
        self.check("Hybrid", True)
        self.check("Home office", True)
        self.check("Remote Worker", True)
        self.check("Remote - Germany", False)
        self.check("Remote in EU", False)

    def test_title_is_read_only_when_location_is_empty(self):
        self.check("", False, title="Work at our office")
        self.check("", True, title="Sachbearbeiter (Wien, AT, 1030)")
        self.check("Remote, GB", False, title="Analyst (Wien)")

    def test_lowercase_at_is_not_the_country_code(self):
        self.check("", False, title="Product Manager at Acme")
        self.check("Berlin at Acme", False)


    def test_more_austrian_towns_and_regions(self):
        """QA round 2 follow-up: towns and states that the old list missed."""
        for loc in ("Villach", "Wels", "Dornbirn", "Eisenstadt", "Wiener Neustadt", "Wiener-Neustadt", "Bregenz",
                    "Krems an der Donau", "Steyr", "Leoben", "Mödling", "Moedling", "Feldkirch", "Klosterneuburg",
                    "Schwechat", "Tirol", "Tyrol", "Styria", "Vorarlberg", "Burgenland", "Österreichweit",
                    "Oesterreichweit"):
            self.check(loc, True)

    def test_baden_is_austrian_only_as_the_town(self):
        """'Baden' also names Baden-Baden and Baden-Württemberg in Germany."""
        self.check("Baden", True)
        self.check("Baden bei Wien", True)
        self.check("Baden-Baden, DE", False)
        self.check("Baden-Württemberg", False)
        self.check("Baden Württemberg", False)
        self.check("Baden, DE", False)

    def test_home_office_words_are_remote(self):
        """Home-Office, Homeoffice, Mobiles Arbeiten, Telearbeit count like bare 'Remote'."""
        for loc in ("Home-Office", "Homeoffice", "Home Office", "Mobiles Arbeiten", "Telearbeit"):
            self.check(loc, True)
        self.check("Home-Office, GB", False)
        self.check("Telearbeit, DE", False)
        self.check("Mobiles Arbeiten, Germany", False)


if __name__ == "__main__":
    unittest.main()
