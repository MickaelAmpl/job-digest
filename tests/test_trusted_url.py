"""security-06-r2, pipeline2-01, pipeline2-02: which model-found links become the 'Открыть объявление' button.
Lookalike hosts are refused, real employer pages and job boards are accepted. No network."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import enrich  # noqa: E402

ACME = {"company": "Acme GmbH"}


class RegistrableDomain(unittest.TestCase):
    def test_last_two_labels_or_three_under_a_public_suffix(self):
        for host, domain in (("karriere.oenb.at", "oenb.at"), ("jobs.univie.ac.at", "univie.ac.at"),
                             ("WWW.Example.CO.UK", "example.co.uk"), ("www.karriere.at", "karriere.at"),
                             ("karriere.at.evil.example", "evil.example"), ("localhost", "localhost")):
            self.assertEqual(enrich.registrable_domain(host), domain, host)


class TrustedUrl(unittest.TestCase):
    def assertTrusted(self, url, job, official, msg=""):
        self.assertEqual(enrich.trusted_url(url, job, official), url, msg or url)

    def assertNotTrusted(self, url, job, official, msg=""):
        self.assertEqual(enrich.trusted_url(url, job, official), "", msg or url)

    def test_lookalikes_are_not_trusted(self):
        """security-06-r2, pipeline2-02: the name inside a host is not the domain."""
        self.assertNotTrusted("https://karriere.at.evil.example/job/12345678", ACME, False)
        self.assertNotTrusted("https://karriere.at.evil.test/jobs/1234567", ACME, True)
        self.assertNotTrusted("https://wienernetze.evil.example/job/12345678", {"company": "Wiener Netze GmbH"}, True)
        self.assertNotTrusted("https://evilkarriere.at/job/12345678", ACME, False)
        self.assertNotTrusted("https://clever.company/job/12345678", ACME, False)
        self.assertNotTrusted("https://greenhouse.io.attacker.test/x/12345678", ACME, False)
        self.assertNotTrusted("https://glassdoor-phish.ru/job/12345678", ACME, True)
        self.assertNotTrusted("https://erste-login.evil.test/job/12345678", {"company": "Erste Group"}, True)

    def test_real_employer_pages_are_trusted(self):
        """pipeline2-01: the four pages from the QA, plus one hash link from onlyfy."""
        self.assertTrusted("https://karriere.oenb.at/job/5125", {"company": "Oesterreichische Nationalbank"}, True)
        self.assertTrusted("https://karriere.oenb.at/job/5125", {"company": "Oesterreichische Nationalbank"}, False,
                           "companies.json careers host, rule b")
        self.assertTrusted("https://jobs.frequentis.com/de-AT/job/3494", {"company": "Frequentis AG"}, True)
        self.assertTrusted("https://jobs.univie.ac.at/en/job/4711/researcher", {"company": "Universität Wien"}, True)
        self.assertTrusted("https://acme.onlyfy.jobs/job/3f9a1c2b", ACME, False)

    def test_employer_label_must_equal_a_company_label(self):
        """security-06-r2: rule c matches the whole first label, not a word inside it."""
        wiener = {"company": "Wiener Netze GmbH"}
        self.assertNotTrusted("https://wienernetze-evil.example/job/12345678", wiener, True)
        self.assertNotTrusted("https://wienernetzejobs.example/job/12345678", wiener, True)
        self.assertTrusted("https://wienernetze.at/job/12345678", wiener, True)
        self.assertTrusted("https://www.post.at/job/12345678", {"company": "Österreichische Post AG"}, True)
        self.assertTrusted("https://oesterreichischepost.at/job/12345678", {"company": "Österreichische Post AG"}, True)
        self.assertTrusted("https://oenb.at/job/12345678", {"company": "Oesterreichische Nationalbank"}, True)

    def test_employer_page_needs_official_claim(self):
        self.assertNotTrusted("https://jobs.frequentis.com/de-AT/job/3494", {"company": "Frequentis AG"}, False)

    def test_company_careers_host_from_companies_json(self):
        """Rule b: Erste Group's careers host in companies.json, no company word needed."""
        self.assertTrusted("https://www.erstegroup.com/en/career/positions-offered/12345", {"company": "Erste Group"}, False)
        self.assertTrusted("https://www.erstegroup.com/en/career/positions-offered/12345",
                           {"company": "x", "company_id": "erste-group"}, False)

    def test_linkedin_is_never_trusted(self):
        self.assertNotTrusted("https://www.linkedin.com/jobs/view/12345678", {"company": "Wiener Netze"}, True)
        self.assertNotTrusted("https://linkedin.com/jobs/view/12345678", ACME, False)

    def test_unsafe_shapes_are_not_trusted(self):
        for url in ("javascript:alert(1)", "data:text/html,<a href=x>job</a>", "http://1.2.3.4/job/12345678",
                    "https://[::1]/job/12345678", "https://localhost/job/12345678",
                    "https://user@karriere.at/job/12345678", "https://user:pw@karriere.at/job/12345678"):
            self.assertNotTrusted(url, ACME, True)

    def test_board_subdomain_is_trusted(self):
        self.assertTrusted("https://jobs.karriere.at/job/12345678", ACME, False)

    def test_listing_or_start_page_is_not_trusted(self):
        """Kept from the old same_link gate: a careers start page is not the posting (test_pipeline_qafix)."""
        self.assertNotTrusted("https://www.techtalk.at/karriere", {"company": "TechTalk"}, True)
        self.assertNotTrusted("https://www.karriere.at/", ACME, False)


    def test_host_with_percent_encoding_or_backslash_is_refused(self):
        """QA follow-up: 'evil.example%5C.karriere.at' used to pass as a karriere.at board link."""
        self.assertNotTrusted("https://evil.example%5C.karriere.at/job/12345678", ACME, False)
        self.assertNotTrusted("https://karriere.at%2F.evil.example/job/12345678", ACME, False)
        self.assertNotTrusted("https://karriere.at\\.evil.example/jobs/12345678", ACME, False)
        self.assertNotTrusted("https://jobs.karriere.at%00.evil.example/jobs/12345678", ACME, False)

    def test_board_posting_needs_no_numeric_id(self):
        """QA follow-up: a board URL with a slug path is a posting; a bare board page is not."""
        self.assertTrusted("https://speech.talentlyft.com/jobs/product-manager-hardware-innovation-all-genders-welcome-ckeN",
                           {"company": "Speech Design"}, False)
        self.assertTrusted("https://jobs.karriere.at/jobs/product-manager-vienna", ACME, False)
        self.assertNotTrusted("https://jobs.karriere.at/jobs", ACME, False)
        self.assertNotTrusted("https://speech.talentlyft.com/", {"company": "Speech Design"}, False)

    def test_own_company_pages_still_need_a_job_id(self):
        """The relaxed path rule is for job boards only."""
        self.assertNotTrusted("https://jobs.frequentis.com/de-AT/careers", {"company": "Frequentis AG"}, True)

    def test_new_boards(self):
        """QA follow-up: factorial.com, bamboohr.com, lufthansagroup.careers are job boards."""
        self.assertTrusted("https://acme.factorial.com/job/senior-engineer-vienna", ACME, False)
        self.assertTrusted("https://acme.bamboohr.com/careers/12", ACME, False)
        self.assertTrusted("https://www.lufthansagroup.careers/job/vienna-cabin-crew", {"company": "Lufthansa"}, False)
        self.assertNotTrusted("https://www.lufthansagroup.careers/", {"company": "Lufthansa"}, False)

    def test_new_aliases(self):
        """QA follow-up: University of Vienna -> univie, Wiener Netze -> wienerstadtwerke / wienernetze,
        Drees & Sommer -> dreso."""
        self.assertTrusted("https://univie.ac.at/job/12345678", {"company": "University of Vienna"}, True)
        self.assertTrusted("https://wienerstadtwerke.at/job/12345678", {"company": "Wiener Netze GmbH"}, True)
        self.assertTrusted("https://dreso.com/job/12345678", {"company": "Drees & Sommer SE"}, True)
        self.assertNotTrusted("https://dreso.com/job/12345678", {"company": "Drees & Sommer SE"}, False)
        self.assertNotTrusted("https://univie-evil.example/job/12345678", {"company": "University of Vienna"}, True)

    def test_common_single_words_are_not_labels(self):
        """QA follow-up: 'wiener', 'netze', 'bank', 'group' ... never match a domain on their own."""
        self.assertNotTrusted("https://wiener.at/job/12345678", {"company": "Wiener Netze GmbH"}, True)
        self.assertNotTrusted("https://netze.at/job/12345678", {"company": "Wiener Netze GmbH"}, True)
        self.assertNotTrusted("https://bank.at/job/12345678", {"company": "Bank Austria"}, True)
        self.assertNotTrusted("https://group.at/job/12345678", {"company": "Erste Group"}, True)
        self.assertNotTrusted("https://digital.at/job/12345678", {"company": "Acme Digital GmbH"}, True)
        self.assertTrusted("https://bankaustria.at/job/12345678", {"company": "Bank Austria"}, True)
        self.assertTrusted("https://erste.at/job/12345678", {"company": "Erste Group"}, True)


if __name__ == "__main__":
    unittest.main()
