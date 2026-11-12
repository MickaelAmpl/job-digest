"""Stage 7 step 3 (16.10.2026): Uni Wien Career Center alert mails ('Jobalarm'). Fixtures in tests/fixtures/uniwien are
two of the four real mails, sanitized (query strings cut, no personal data). The four session samples are read when
the scratchpad exists and skipped otherwise, like the LinkedIn samples."""
import email
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import collect  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "uniwien"
SAMPLES = Path("<scratch>"
               "/scratchpad/s7/linkedin_mails")
EXPECTED = {"uniwien-04": 7, "uniwien-05": 5, "uniwien-06": 6, "uniwien-07": 3}  # jobs per real mail
HEADER_FOOTER = ("Hallo", "Es gibt neue Ergebnisse", "Alle Jobs anzeigen", "Jobalarm ändern", "Jobportal Career Center")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def mails(folder):
    return [(p.stem, p.read_text(encoding="utf-8")) for p in sorted(folder.glob("uniwien-*.txt"))]


class UniwienParseTest(unittest.TestCase):

    def test_fixture_mails_give_expected_jobs(self):
        found = mails(FIXTURES)
        self.assertEqual(len(found), 2)
        for name, body in found:
            jobs = collect.parse_uniwien(body)
            self.assertEqual(len(jobs), EXPECTED[name], name)
            for job in jobs:
                for key in ("title", "company", "location", "posted"):
                    self.assertTrue(job[key], f"{name}: empty {key} in {job}")
                self.assertRegex(job["posted"], ISO_DATE)

    def test_known_jobs_from_fixtures(self):
        jobs = collect.parse_uniwien(dict(mails(FIXTURES))["uniwien-04"])
        first = jobs[0]
        self.assertEqual(first["title"], "Office Manager:in für Ingenieurbüro – Vollzeit/Karenzvertretung")
        self.assertEqual(first["company"], "NWU Biologie GmbH")
        self.assertEqual(first["location"], "Wien, Österreich")
        self.assertEqual(first["posted"], "2026-10-13")
        vig = next(j for j in jobs if j["company"] == "Vienna Insurance Group")  # '&amp;' in the title is unescaped
        self.assertEqual(vig["title"], "Data & Analytics - Data Engineering Lead (all genders)")
        self.assertEqual(vig["posted"], "2026-10-14")

    def test_location_with_country_only_is_kept(self):
        jobs = collect.parse_uniwien(dict(mails(FIXTURES))["uniwien-07"])
        contai = next(j for j in jobs if j["company"].startswith("CONTAINEX"))
        self.assertEqual(contai["location"], "2351, Österreich")
        self.assertEqual(contai["posted"], "2026-10-12")

    def test_parser_leaves_url_empty_and_keeps_only_the_click_link(self):
        for name, body in mails(FIXTURES):
            for job in collect.parse_uniwien(body):
                self.assertEqual(job["url"], "", name)
                self.assertEqual(job["ext_id"], "", name)
                self.assertTrue(job["link"].startswith("https://u49249119.ct.eu.sendgrid.net/"), name)

    @unittest.skipUnless(SAMPLES.is_dir(), "real Uni Wien samples are in the session scratchpad only")
    def test_all_four_real_mails(self):
        found = mails(SAMPLES)
        self.assertEqual(sorted(name for name, _ in found), sorted(EXPECTED))
        total = 0
        for name, body in found:
            jobs = collect.parse_uniwien(body)
            self.assertEqual(len(jobs), EXPECTED[name], name)
            total += len(jobs)
            for job in jobs:
                self.assertTrue(all(job[k] for k in ("title", "company", "location", "posted")), name)
        self.assertGreaterEqual(total, 7)

    def test_header_and_footer_are_never_jobs(self):
        for name, body in mails(FIXTURES) + (mails(SAMPLES) if SAMPLES.is_dir() else []):
            for job in collect.parse_uniwien(body):
                self.assertFalse(job["title"].lower().startswith(tuple(h.lower() for h in HEADER_FOOTER)),
                                 f"{name}: {job['title']!r}")
                self.assertNotIn("|", job["title"], name)
        footer_only = ("Hallo ,<br> Es gibt neue Ergebnisse für Ihre gespeicherte Suche\n\n"
                       "( https://u49249119.ct.eu.sendgrid.net/ls/click )\n\n"
                       "Alle Jobs anzeigen ( https://u49249119.ct.eu.sendgrid.net/ls/click )\n\n"
                       "Jobalarm ändern oder löschen ( https://u49249119.ct.eu.sendgrid.net/ls/click )\n\n"
                       "Jobportal Career Center Universität Wien ( https://u49249119.ct.eu.sendgrid.net/ls/click )\n")
        self.assertEqual(collect.parse_uniwien(footer_only), [])

    def test_title_without_company_line_is_skipped(self):
        body = ("Product Owner (m/w/d) ( https://u49249119.ct.eu.sendgrid.net/ls/click )\n\n"
                "Alle Jobs anzeigen ( https://u49249119.ct.eu.sendgrid.net/ls/click )\n")
        self.assertEqual(collect.parse_uniwien(body), [])

    def test_bad_or_missing_date_keeps_the_job_without_posted_and_empty_title_is_skipped(self):
        body = ("Product Owner ( https://u49249119.ct.eu.sendgrid.net/ls/click )\n"
                "ACME GmbH | Wien, Österreich | 31.02.2026\n\n"
                "Sales Lead ( https://u49249119.ct.eu.sendgrid.net/ls/click )\n"
                "ACME GmbH | Wien, Österreich\n\n"
                "( https://u49249119.ct.eu.sendgrid.net/ls/click )\n"
                "ACME GmbH | Wien, Österreich | 14.10.2026\n")
        jobs = collect.parse_uniwien(body)
        self.assertEqual([(j["title"], j["location"], j["posted"]) for j in jobs],
                         [("Product Owner", "Wien, Österreich", ""), ("Sales Lead", "Wien, Österreich", "")])

    def test_markdown_link_and_university_link_are_read(self):
        body = ("[Senior Analyst (m/w/d)](https://jobs.careercenter.univie.ac.at/job/1)\n"
                "ACME GmbH | 1010 Wien, Österreich | 14.10.2026\n")
        jobs = collect.parse_uniwien(body)
        self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs[0]["title"], jobs[0]["company"], jobs[0]["posted"]),
                         ("Senior Analyst (m/w/d)", "ACME GmbH", "2026-10-14"))
        self.assertEqual(jobs[0]["link"], "https://jobs.careercenter.univie.ac.at/job/1")
        self.assertEqual(jobs[0]["url"], "")  # the parser makes no requests; resolve_uniwien fills url

    def test_very_long_line_is_cut_not_slow(self):
        body = "x" * 300_000 + " ( https://u49249119.ct.eu.sendgrid.net/ls/click )\nACME | Wien | 14.10.2026\n"
        self.assertEqual(collect.parse_uniwien(body), [])

    def test_parser_is_registered(self):
        self.assertIs(collect.PARSERS["uniwien"], collect.parse_uniwien)

    def test_digest_label(self):
        import report  # noqa: E402
        self.assertEqual(report.SOURCE_LABELS["uniwien"], "Uni Wien")

    def test_only_the_exact_sender_address_is_uniwien(self):
        self.assertEqual(collect.source_of("Career Center <office@careercenter-univie.at>"), "uniwien")
        self.assertEqual(collect.source_of("OFFICE@CareerCenter-Univie.at"), "uniwien")
        for lookalike in ('"office@careercenter-univie.at" <evil@attacker.test>',
                          "Uni Wien <office@careercenter-univie.at.evil.test>",
                          "office@careercenter-univie.at.example.test",
                          "noreply@careercenter-univie.at",
                          "office@mail.careercenter-univie.at",
                          "office@careercenter-univie.co"):
            self.assertIsNone(collect.source_of(lookalike), lookalike)

    def test_dmarc_failure_still_counts_as_spoofed(self):
        failed = email.message_from_string("Authentication-Results: mx.google.com; dmarc=fail\n\nbody")
        passed = email.message_from_string("Authentication-Results: mx.google.com; dmarc=pass\n\nbody")
        self.assertTrue(collect.spoofed(failed))
        self.assertFalse(collect.spoofed(passed))


if __name__ == "__main__":
    unittest.main()
