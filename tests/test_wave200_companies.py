"""Wave 200 companies.json checks (stage 12, 10.11.2026). No network: reads companies.json and the adapter map only."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import fetch_companies  # noqa: E402

# every record has these; "triage" is required only for wave 200 records (75 older records have no triage key)
REQUIRED = {"id", "name", "niche", "top", "careers_url", "ats", "feed_url", "feed_ok"}
# niche keys already used in companies.json before wave 200 (new keys are mapped to these in the spec)
KNOWN_NICHES = {"agency", "ai", "banks", "consulting", "crypto", "ecommerce", "edtech", "fintech", "gaming",
                "insurance", "intl", "market_research", "public", "saas", "startups", "telecom"}
# research/ is not deployed: on the server the wave 200 list is absent and the two list tests are skipped
DECISIONS = ROOT / "research" / "wave200_decisions.json"
WAVE200_READABLE = (json.loads(DECISIONS.read_text(encoding="utf-8"))["readable"] if DECISIONS.exists() else [])


def not_read(c):
    """Listed as not read: no working feed on purpose (unreadable note), dropped, or feed_ok false."""
    return bool(c.get("unreadable")) or c.get("triage") == "drop" or c.get("feed_ok") is False


def load_companies():
    return json.loads((ROOT / "companies.json").read_text(encoding="utf-8"))


class CompaniesWave200Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.companies = load_companies()
        cls.failed_modules = fetch_companies.load_extra_adapters()
        cls.adapters = set(fetch_companies.ADAPTERS)

    def test_every_record_has_required_keys(self):
        for c in self.companies:
            with self.subTest(id=c.get("id")):
                self.assertTrue(REQUIRED <= set(c), f"missing keys: {sorted(REQUIRED - set(c))}")
                if c["id"] in WAVE200_READABLE:
                    self.assertIn("triage", c)

    def test_ids_are_unique(self):
        ids = [c["id"] for c in self.companies]
        self.assertEqual(len(ids), len(set(ids)), "duplicate ids in companies.json")

    def test_ats_is_known_adapter_or_not_read(self):
        self.assertEqual(self.failed_modules, {}, "extra adapter modules must load")
        for c in self.companies:
            with self.subTest(id=c["id"]):
                if c["ats"] in self.adapters:
                    continue
                self.assertTrue(not_read(c), f"ats {c['ats']!r} has no adapter and is not listed as not read")

    def test_feed_url_set_when_feed_ok(self):
        for c in self.companies:
            if c.get("feed_ok") is True and not not_read(c):
                with self.subTest(id=c["id"]):
                    self.assertTrue(c.get("feed_url"), "feed_ok is true but feed_url is empty")

    @unittest.skipUnless(DECISIONS.exists(), "research/ is not deployed")
    def test_wave200_readable_records_are_present_with_shape(self):
        by_id = {c["id"]: c for c in self.companies}
        for cid in WAVE200_READABLE:
            with self.subTest(id=cid):
                self.assertIn(cid, by_id, "readable company missing from companies.json")
                c = by_id[cid]
                self.assertIn(c["niche"], KNOWN_NICHES, f"unknown niche {c['niche']!r}")
                self.assertIs(c["top"], False)
                self.assertIs(c["feed_ok"], True)
                self.assertEqual(c["triage"], "keep")
                self.assertTrue(c["feed_url"])
                self.assertIn(c["ats"], self.adapters)

    @unittest.skipUnless(DECISIONS.exists(), "research/ is not deployed")
    def test_wave200_records_are_appended_in_decision_order(self):
        ids = [c["id"] for c in self.companies]
        positions = [ids.index(cid) for cid in WAVE200_READABLE]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(positions, list(range(positions[0], positions[0] + len(positions))), "records not contiguous")


if __name__ == "__main__":
    unittest.main()
