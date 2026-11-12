"""Wave 2 duplicates (26.10.2026): the same vacancy from a LinkedIn alert and from a company's own feed.
The five real pairs on the server (CHECK24 x3, woom, Wüstenrot) must be closed as copies, and the look-alikes
that are different jobs must stay two. No network: rows are built in a temp DB with real dedupe keys."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import store  # noqa: E402

REF_DAY = datetime(2026, 10, 27)  # the day the literal dates below were written


def rel(iso):
    """A literal 2026 date in this file, moved to today by the same number of days: the 15- and 30-day
    windows judge first_seen against the clock, and these tests must not start failing in a month."""
    return (datetime.now() - (REF_DAY - datetime.fromisoformat(iso))).isoformat(timespec="seconds")


FEED_SEEN = rel("2026-10-26T20:35:48")  # the evening company-feed batch
SENT = "2026-10-17T07:30:08"  # Misha already got this one as a card


def days_ago(n):
    """first_seen n days back: the 15-day window is judged from today, so tests must not hard-code dates."""
    return (datetime.now() - timedelta(days=n)).isoformat(timespec="seconds")


class Db(unittest.TestCase):
    """A scratch state.db with the real schema; store.mark_if_duplicate works on it directly."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(store, "DB", Path(self.dir.name) / "data" / "state.db")
        self.patch.start()
        self.con = store.connect()

    def tearDown(self):
        self.con.close()
        self.patch.stop()
        self.dir.cleanup()

    def add(self, jid, company, title, url, open_url=None, first_seen=FEED_SEEN, sent=False, verdict=None,
            source="company", key=None):
        job = {"company": company, "title": title, "url": url}
        self.con.execute("insert into jobs (id, key, company, title, url, open_url, source, first_seen, sent_at,"
                         " verdict, data) values (?,?,?,?,?,?,?,?,?,?,?)",
                         (jid, key or store.dedupe_key(job), company, title, url, open_url, source, first_seen,
                          SENT if sent else None, verdict, json.dumps(job, ensure_ascii=False)))
        self.con.commit()

    def state(self, jid):
        """(closed, closed_by, dup_of) of a row."""
        r = self.con.execute("select closed, closed_by, dup_of from jobs where id=?", (jid,)).fetchone()
        return (r["closed"], r["closed_by"], r["dup_of"])


CHECK24_INTERNSHIP = "Internship - Product Management (all genders)"
CHECK24_JUNIOR = "(Junior) Technischer Produktmanager (all genders)"
CHECK24_WERKSTUDENT = "Werkstudent (all genders) im Produktmanagement"
WOOM_TITLE = "eCommerce Data & Analytics Specialist (f/m/d)"
WUESTENROT_TITLE = "Technical Product Manager:in mit Fokus Software Architektur und AI (m/w/d)"
WUESTENROT_PATH = ("https://www.wuestenrot.at/de/ueber-uns/karriere/offene-stellen/"
                   "technical-product-manager-in-mit-fokus-software-architektur-und.html")


class RealPairs(Db):
    """The five pairs found on the server on 26.10.2026."""

    def test_check24_internship_en_link_and_de_link_are_one_job(self):
        self.add("li", "CHECK24", CHECK24_INTERNSHIP, "https://www.linkedin.com/jobs/view/4479863121/",
                 open_url="https://jobs.check24.de/en/jobs/produkt-projektmanagement/"
                          "ref669u-internship-product-management-all-genders",
                 first_seen=rel("2026-10-17T06:34:54"), sent=True, verdict="interested", source="linkedin")
        self.add("feed", "CHECK24 Vergleichsportal Österreich", CHECK24_INTERNSHIP,
                 "https://jobs.check24.de/de/jobs/produkt-projektmanagement/"
                 "ref669u-internship-product-management-all-genders?locations=vienna",
                 open_url="https://jobs.check24.de/de/jobs/produkt-projektmanagement/"
                          "ref669u-internship-product-management-all-genders?locations=vienna")
        self.assertEqual(store.mark_if_duplicate(self.con, "feed"), "feed")
        self.assertEqual(self.state("feed"), (1, "duplicate", "li"))
        self.assertEqual(self.state("li"), (0, None, None))

    def test_check24_junior_technischer_produktmanager(self):
        self.add("li", "CHECK24", CHECK24_JUNIOR, "https://www.linkedin.com/jobs/view/4422688943/",
                 open_url="https://jobs.check24.de/en/jobs/produkt-projektmanagement/"
                          "REF228K-junior-technischer-produktmanager-all-genders/",
                 first_seen=rel("2026-10-11T06:31:45"), sent=True, verdict="interested", source="linkedin")
        self.add("feed", "CHECK24 Vergleichsportal Österreich", CHECK24_JUNIOR,
                 "https://jobs.check24.de/de/jobs/produkt-projektmanagement/"
                 "ref228k-junior-technischer-produktmanager-all-genders?locations=vienna",
                 open_url="https://jobs.check24.de/de/jobs/produkt-projektmanagement/"
                          "ref228k-junior-technischer-produktmanager-all-genders?locations=vienna")
        self.assertEqual(store.mark_if_duplicate(self.con, "feed"), "feed")
        self.assertEqual(self.state("feed"), (1, "duplicate", "li"))

    def test_check24_werkstudent_with_a_different_middle_path_segment(self):
        """The LinkedIn link says produkt-und-projektmanagement, the feed says produkt-projektmanagement."""
        self.add("li", "CHECK24", CHECK24_WERKSTUDENT, "https://www.linkedin.com/jobs/view/4462849721/",
                 open_url="https://jobs.check24.de/en/jobs/produkt-und-projektmanagement/"
                          "REF235Q-werkstudent-all-genders-im-produktmanagement/",
                 first_seen=rel("2026-10-19T06:32:10"), sent=True, verdict="interested", source="linkedin")
        self.add("feed", "CHECK24 Vergleichsportal Österreich", CHECK24_WERKSTUDENT,
                 "https://jobs.check24.de/de/jobs/produkt-projektmanagement/"
                 "ref235q-werkstudent-all-genders-im-produktmanagement?locations=vienna",
                 open_url="https://jobs.check24.de/de/jobs/produkt-projektmanagement/"
                          "ref235q-werkstudent-all-genders-im-produktmanagement?locations=vienna")
        self.assertEqual(store.mark_if_duplicate(self.con, "feed"), "feed")
        self.assertEqual(self.state("feed"), (1, "duplicate", "li"))

    def test_woom_bikes_and_woom_are_one_employer(self):
        """The links do not match (/careers/184 vs /careers/184/detail), so only the company rule can see it."""
        self.add("adz", "woom Bikes", WOOM_TITLE, "https://www.adzuna.at/details/5908295645?utm_medium=api",
                 open_url="https://woom.bamboohr.com/careers/184/detail", first_seen=rel("2026-10-08T14:41:38"),
                 sent=True, source="adzuna")
        self.add("feed", "woom", WOOM_TITLE, "https://woom.bamboohr.com/careers/184",
                 open_url="https://woom.bamboohr.com/careers/184")
        self.assertEqual(store.mark_if_duplicate(self.con, "feed"), "feed")
        self.assertEqual(self.state("feed"), (1, "duplicate", "adz"))

    def test_wuestenrot_same_posting_path_on_the_same_host(self):
        self.add("adz", "Wüstenrot Gruppe", WUESTENROT_TITLE,
                 "https://www.adzuna.at/land/ad/5913589559?utm_medium=api", open_url=WUESTENROT_PATH,
                 first_seen=rel("2026-10-07T21:07:43"), sent=True, source="adzuna")
        self.add("feed", "Bausparkasse Wüstenrot", WUESTENROT_TITLE, WUESTENROT_PATH, open_url=WUESTENROT_PATH)
        self.assertEqual(store.mark_if_duplicate(self.con, "feed"), "feed")
        self.assertEqual(self.state("feed"), (1, "duplicate", "adz"))


class Negatives(Db):
    """Look-alikes that are two jobs and must stay two."""

    def test_raiffeisen_bank_and_raiffeisenlandesbank_are_two_employers(self):
        """A shared first word is not the same employer."""
        self.add("rbi", "Raiffeisen Bank International", "Praktikum Marketing",
                 "https://www.rbinternational.com/karriere/praktikum-marketing", first_seen=rel("2026-10-20T06:30:00"))
        self.add("rlb", "Raiffeisenlandesbank NÖ-Wien", "Praktikum Marketing",
                 "https://www.raiffeisenlandesbank.at/karriere/praktikum-marketing")
        self.assertIsNone(store.mark_if_duplicate(self.con, "rlb"))
        self.assertIsNone(store.mark_if_duplicate(self.con, "rbi"))
        self.assertEqual(self.state("rlb"), (0, None, None))
        self.assertEqual(self.state("rbi"), (0, None, None))

    def test_locale_change_with_different_postings_is_two_jobs(self):
        """/en/ and /de/ with different last segments: two postings, not one."""
        self.add("en", "CHECK24", "Junior Product Manager (all genders)",
                 "https://jobs.check24.de/en/jobs/produkt-projektmanagement/ref111111-junior-product-manager",
                 first_seen=rel("2026-10-11T06:31:45"), sent=True, verdict="interested", source="linkedin")
        self.add("de", "CHECK24", "Product Manager (all genders)",
                 "https://jobs.check24.de/de/jobs/produkt-projektmanagement/ref222222-product-manager?locations=vienna")
        self.assertIsNone(store.mark_if_duplicate(self.con, "de"))
        self.assertEqual(self.state("de"), (0, None, None))

    def test_locale_change_with_identical_title_and_other_company_is_two_jobs(self):
        self.add("en", "ACME Jobs", "Product Manager (all genders)",
                 "https://jobs.example.at/en/jobs/ref111111-product-manager-vienna", sent=True, verdict="interested")
        self.add("de", "Globex", "Product Manager (all genders)",
                 "https://jobs.example.at/de/jobs/ref222222-product-manager-vienna")
        self.assertIsNone(store.mark_if_duplicate(self.con, "de"))
        self.assertEqual(self.state("de"), (0, None, None))

    def test_same_title_on_one_site_with_different_job_numbers_is_two_jobs(self):
        """GoStudent (found by the 26.10 dry run on the server): 'Center Manager' in each city is its own job, the
        page is the same job-position page and only the gh_jid in the query tells them apart."""
        self.add("wien", "GoStudent", "LernQuadrat - Center Manager (w/m/d) in Wien",
                 "https://www.gostudent.org/en-gb/job-position/?gh_jid=4838320101")
        self.add("dornbirn", "GoStudent", "LernQuadrat - Center Manager (w/m/d) in Dornbirn (Vorarlberg)",
                 "https://www.gostudent.org/en-gb/job-position/?gh_jid=4913620101", sent=True, verdict="interested")
        self.assertIsNone(store.mark_if_duplicate(self.con, "wien"))
        self.assertEqual(self.state("wien"), (0, None, None))

    def test_same_title_with_different_job_ids_on_one_site_is_two_jobs(self):
        """SumUp (found by the dry run): m/w/x and m/w/d with two different gh_jid. The second row carries the
        stale key a rekey left behind, so only the company rule could see the pair."""
        self.add("d", "SumUp", "Erfahrener Außendienstmitarbeiter (m/w/d)",
                 "https://sumup.com/careers/positions/8874567002?gh_jid=8874567002")
        self.add("x", "SumUp", "Erfahrener Außendienstmitarbeiter (m/w/x)",
                 "https://sumup.com/careers/positions/8559397002?gh_jid=8559397002",
                 key="sumup|erfahrener außendienstmitarbeiter m w x")
        self.assertIsNone(store.mark_if_duplicate(self.con, "x"))
        self.assertEqual(self.state("x"), (0, None, None))

    def test_generic_job_position_page_is_not_a_posting(self):
        """'job-position' is 12 letters, but it is a page, not one posting: two of them never link."""
        self.add("p1", "Acme Austria", "Product Manager (all genders)",
                 "https://jobs.example-career.at/en-gb/job-position/", sent=True, verdict="interested")
        self.add("p2", "Globex", "Product Manager (all genders) Vienna",
                 "https://www.jobs.example-career.at/de/job-position/")
        self.assertIsNone(store.mark_if_duplicate(self.con, "p2"))
        self.assertEqual(self.state("p2"), (0, None, None))

    def test_copy_with_a_verdict_is_not_marked(self):
        """Misha already pressed a button on the feed copy: it stays as he left it."""
        self.add("li", "CHECK24", CHECK24_INTERNSHIP, "https://www.linkedin.com/jobs/view/4479863121/",
                 open_url="https://jobs.check24.de/en/jobs/produkt-projektmanagement/"
                          "ref669u-internship-product-management-all-genders",
                 first_seen=rel("2026-10-17T06:34:54"), sent=True, verdict="interested", source="linkedin")
        self.add("feed", "CHECK24 Vergleichsportal Österreich", CHECK24_INTERNSHIP,
                 "https://jobs.check24.de/de/jobs/produkt-projektmanagement/"
                 "ref669u-internship-product-management-all-genders?locations=vienna", verdict="later")
        self.assertIsNone(store.mark_if_duplicate(self.con, "feed"))
        self.assertIsNone(store.mark_if_duplicate(self.con, "li"))
        self.assertEqual(self.state("feed"), (0, None, None))

    def test_a_duplicate_is_never_the_original(self):
        """The only same-employer row is a closed duplicate, even though it was sent: nothing is an original, so
        the new row stays as it is."""
        self.add("dup", "woom Bikes", WOOM_TITLE, "https://www.adzuna.at/details/5908295645",
                 first_seen=rel("2026-10-01T06:00:00"), sent=True, source="adzuna")
        self.con.execute("update jobs set closed=1, closed_by='duplicate', dup_of='gone' where id='dup'")
        self.con.commit()
        self.add("new", "woom", WOOM_TITLE, "https://woom.bamboohr.com/careers/199")
        self.assertIsNone(store.mark_if_duplicate(self.con, "new"))
        self.assertEqual(self.state("new"), (0, None, None))


class Rules(Db):
    def test_same_posting_link_alone_merges_when_company_names_differ(self):
        """The link rule does not depend on the company name: same host, same posting slug, similar title."""
        self.add("sent", "Acme Austria", "Product Manager (all genders)",
                 "https://jobs.example-career.at/en/jobs/ref987654-product-manager-vienna",
                 first_seen=rel("2026-10-11T06:31:45"), sent=True, verdict="interested")
        self.add("copy", "Globex", "Product Manager (all genders) Vienna",
                 "https://www.jobs.example-career.at/de/jobs/ref987654-product-manager-vienna")
        self.assertEqual(store.mark_if_duplicate(self.con, "copy"), "copy")
        self.assertEqual(self.state("copy"), (1, "duplicate", "sent"))

    def test_both_unsent_and_both_shown_the_earlier_first_seen_is_the_original(self):
        self.add("old", "woom", "German Copywriter (f/m/d)", "https://woom.bamboohr.com/careers/182",
                 first_seen=days_ago(5))
        self.add("new", "woom Bikes", "German Copywriter (f/m/d)", "https://www.adzuna.at/details/111222333",
                 first_seen=days_ago(1))
        self.assertEqual(store.mark_if_duplicate(self.con, "new"), "new")
        self.assertEqual(self.state("new"), (1, "duplicate", "old"))

    def test_same_pair_marks_the_copy_from_either_side(self):
        """The LinkedIn row is scored first: marking from its side still closes the feed copy."""
        self.add("li", "CHECK24", CHECK24_INTERNSHIP, "https://www.linkedin.com/jobs/view/4479863121/",
                 open_url="https://jobs.check24.de/en/jobs/produkt-projektmanagement/"
                          "ref669u-internship-product-management-all-genders",
                 first_seen=rel("2026-10-17T06:34:54"), sent=True, verdict="interested", source="linkedin")
        self.add("feed", "CHECK24 Vergleichsportal Österreich", CHECK24_INTERNSHIP,
                 "https://jobs.check24.de/de/jobs/produkt-projektmanagement/"
                 "ref669u-internship-product-management-all-genders?locations=vienna")
        self.assertEqual(store.mark_if_duplicate(self.con, "li"), "feed")
        self.assertEqual(self.state("feed"), (1, "duplicate", "li"))
        self.assertEqual(self.state("li"), (0, None, None))


class ShownWins(Db):
    """Window rule (Misha's list 26.10, pair 6): when neither row was sent, the row still shown in the window is the
    original, whatever the first_seen says. Same employer, same title: Statistik Austria."""

    def add_statistik(self):
        # S7-1: the employer evidence needs a shared company host (the real Adzuna row has none, so in prod this pair
        # no longer merges); this test is about the window ranking, so the row carries the statistik.at link.
        self.add("old", "Bundesanstalt Statistik Österreich", "Senior Java Backend Developer:in",
                 "https://www.adzuna.at/land/ad/5917488997?utm_medium=api", first_seen=days_ago(20), source="adzuna",
                 open_url="https://www.statistik.at/ueber-uns/karriere/jobangebote/bewerbung/job/940")
        self.add("new", "Statistik Austria", "Senior Java Backend Developer:in",
                 "https://www.statistik.at/ueber-uns/karriere/jobangebote/bewerbung/job/940", first_seen=days_ago(0))

    def test_shown_feed_row_stays_open_and_the_old_unsent_adzuna_row_becomes_the_duplicate(self):
        self.add_statistik()
        self.assertEqual(store.mark_if_duplicate(self.con, "new"), "old")
        self.assertEqual(self.state("old"), (1, "duplicate", "new"))
        self.assertEqual(self.state("new"), (0, None, None))

    def test_the_same_pair_settles_the_same_way_from_the_other_side(self):
        self.add_statistik()
        self.assertEqual(store.mark_if_duplicate(self.con, "old"), "old")
        self.assertEqual(self.state("new"), (0, None, None))

    def test_both_outside_the_window_the_earlier_first_seen_is_the_original(self):
        self.add("older", "woom", "German Copywriter (f/m/d)", "https://woom.bamboohr.com/careers/182",
                 first_seen=days_ago(25))
        self.add("newer", "woom Bikes", "German Copywriter (f/m/d)", "https://www.adzuna.at/details/111222333",
                 first_seen=days_ago(20), source="adzuna")
        self.assertEqual(store.mark_if_duplicate(self.con, "newer"), "newer")
        self.assertEqual(self.state("newer"), (1, "duplicate", "older"))

    def test_a_sent_row_outside_the_window_still_wins(self):
        self.add("sent", "woom Bikes", "German Copywriter (f/m/d)", "https://www.adzuna.at/details/111222333",
                 first_seen=days_ago(25), sent=True, source="adzuna")
        self.add("shown", "woom", "German Copywriter (f/m/d)", "https://woom.bamboohr.com/careers/182")
        self.assertEqual(store.mark_if_duplicate(self.con, "shown"), "shown")
        self.assertEqual(self.state("shown"), (1, "duplicate", "sent"))


class CompanyGuards(Db):
    """Generic words and group brands never name one employer alone (Misha's list 26.10)."""

    def test_raiffeisen_bank_international_and_steiermark_stay_two_with_the_same_title(self):
        self.add("rbi", "Raiffeisen Bank International", "Praktikum Marketing",
                 "https://www.rbinternational.com/karriere/praktikum-marketing", first_seen=days_ago(1))
        self.add("rbs", "Raiffeisen Bank Steiermark", "Praktikum Marketing",
                 "https://www.raiffeisen-steiermark.at/karriere/praktikum-marketing")
        self.assertIsNone(store.mark_if_duplicate(self.con, "rbs"))
        self.assertIsNone(store.mark_if_duplicate(self.con, "rbi"))
        self.assertEqual(self.state("rbs"), (0, None, None))
        self.assertEqual(self.state("rbi"), (0, None, None))

    def test_post_still_merges_with_osterreichische_post(self):
        self.add("sent", "Österreichische Post", "Zusteller:in (m/w/d)", "https://www.adzuna.at/details/9988776655",
                 first_seen=days_ago(10), sent=True, source="adzuna")
        self.add("feed", "Post", "Zusteller:in (m/w/d)", "https://jobs.post.at/job/4455667788")
        self.assertEqual(store.mark_if_duplicate(self.con, "feed"), "feed")
        self.assertEqual(self.state("feed"), (1, "duplicate", "sent"))

    def test_group_brand_alone_is_not_an_employer(self):
        self.add("hypo", "Hypo", "Praktikum Marketing", "https://www.hypo-example.at/karriere/praktikum-marketing")
        self.add("tirol", "Hypo Tirol Bank", "Praktikum Marketing", "https://www.hypotirol.at/karriere/praktikum")
        self.assertIsNone(store.mark_if_duplicate(self.con, "hypo"))
        self.assertEqual(self.state("hypo"), (0, None, None))


class EmployerEvidence(Db):
    """S7-1 (27.10.2026): the company rule also needs evidence that two rows are one employer: a shared company host,
    or the words the longer name adds are only descriptors. Group entities and subsidiaries stay apart."""

    def test_group_names_without_a_shared_host_stay_two_jobs(self):
        """VIG vs Donau, 'Austrian' vs 'Austrian Power Grid', Siemens vs Siemens Energy, Mondi vs Mondi Gronau."""
        pairs = [("Donau Versicherung Vienna Insurance Group", "Donau Versicherung"), ("Austrian Power Grid", "Austrian"),
                 ("Siemens Energy", "Siemens"), ("Mondi Gronau", "Mondi")]
        for n, (long_name, short_name) in enumerate(pairs):
            self.add(f"o{n}", long_name, "Praktikum Marketing", f"https://www.adzuna.at/details/10{n}1", sent=True,
                     source="adzuna")
            self.add(f"c{n}", short_name, "Praktikum Marketing", f"https://careers{n}.example.at/job/{n}")
            self.assertIsNone(store.mark_if_duplicate(self.con, f"c{n}"), long_name)
            self.assertEqual(self.state(f"c{n}"), (0, None, None), long_name)

    def test_wiener_staedtische_and_versicherung_ag_are_one_employer(self):
        """The 'Versicherung' descriptor (27.10.2026) brings the derStandard copy back to its feed original."""
        self.add("orig", "Wiener Städtische", "Versicherungsmathematiker für Aktuarielle Modellierung (w|m|d)",
                 "https://dvinci.wienerstaedtische.at/de/p/guterjob_at/jobs/380", first_seen=days_ago(5))
        self.add("copy", "WIENER STÄDTISCHE Versicherung AG",
                 "Versicherungsmathematiker für Aktuarielle Modellierung (w|m|d)",
                 "https://jobs.derstandard.at/detail/06ff2a17-5cf7-3673-8ea4-b03d1fb458a6", first_seen=days_ago(1))
        self.assertEqual(store.mark_if_duplicate(self.con, "copy"), "copy")

    def test_vienna_insurance_group_and_donau_versicherung_stay_two_jobs(self):
        """'donau' is no descriptor: the parent group and the subsidiary are two employers, even with 'Versicherung'."""
        self.add("vig", "Vienna Insurance Group", "Praktikum Marketing", "https://www.adzuna.at/details/660001",
                 sent=True, source="adzuna")
        self.add("donau", "DONAU Versicherung AG Vienna Insurance Group", "Praktikum Marketing",
                 "https://dvinci.donauversicherung.at/de/jobs/229")
        self.assertIsNone(store.mark_if_duplicate(self.con, "donau"))
        self.assertEqual(self.state("donau"), (0, None, None))

    def test_the_same_company_host_counts_when_the_names_differ(self):
        self.add("orig", "Siemens Energy", "Praktikum Marketing", "https://www.adzuna.at/details/555111",
                 open_url="https://jobs.siemens.com/job/1", sent=True, source="adzuna")
        self.add("copy", "Siemens", "Praktikum Marketing", "https://jobs.siemens.com/job/77")
        self.assertEqual(store.mark_if_duplicate(self.con, "copy"), "copy")
        self.assertEqual(self.state("copy"), (1, "duplicate", "orig"))

    def test_a_shared_ats_platform_domain_alone_is_no_evidence(self):
        """Two different tenants on personio.com are two employers."""
        self.add("orig", "Mondi", "Praktikum Marketing", "https://mondi.jobs.personio.com/job/1", sent=True,
                 source="adzuna")
        self.add("copy", "Mondi Gronau", "Praktikum Marketing", "https://gronau.jobs.personio.com/job/2")
        self.assertIsNone(store.mark_if_duplicate(self.con, "copy"))

    def test_the_same_ats_tenant_counts_as_the_same_employer(self):
        self.add("orig", "Acme Power", "Praktikum Marketing", "https://acme.jobs.personio.com/job/1", sent=True,
                 source="adzuna")
        self.add("copy", "Acme", "Praktikum Marketing", "https://acme.jobs.personio.com/job/2")
        self.assertEqual(store.mark_if_duplicate(self.con, "copy"), "copy")

    def test_descriptor_words_are_evidence_and_bank_only_without_a_group_brand(self):
        self.add("o1", "Schoeller Bank", "Praktikum Marketing", "https://www.adzuna.at/details/901", sent=True,
                 source="adzuna")
        self.add("c1", "Schoeller", "Praktikum Marketing", "https://careers.schoeller.example.at/job/1")
        self.assertEqual(store.mark_if_duplicate(self.con, "c1"), "c1")
        self.add("o2", "Acme Digital Services", "Praktikum Marketing", "https://www.adzuna.at/details/902", sent=True,
                 source="adzuna")
        self.add("c2", "Acme", "Praktikum Marketing", "https://careers.acme.example.at/job/2")
        self.assertEqual(store.mark_if_duplicate(self.con, "c2"), "c2")


class RepostWindow(Db):
    """S7-2 (27.10.2026): a row merges with another only while it is still in the window or first seen within 30
    days. An old sent row no longer suppresses a repost: the new row is a new posting."""

    def test_an_old_sent_row_does_not_suppress_a_company_repost(self):
        self.add("old", "Acme Labs", "Praktikum Marketing", "https://www.adzuna.at/details/77001",
                 first_seen=days_ago(45), sent=True, source="adzuna")
        self.add("new", "Acme", "Praktikum Marketing", "https://careers.acme.example.at/job/5", first_seen=days_ago(0))
        self.assertIsNone(store.mark_if_duplicate(self.con, "new"))
        self.assertEqual(self.state("new"), (0, None, None))

    def test_an_old_sent_row_with_the_same_link_suppresses_the_repost(self):
        """The link rule is not age-gated (27.10 QA): the same posting link is the same job, whatever its age."""
        self.add("old", "Example Labs", "Product Manager", "https://jobs.example-career.at/en/jobs/ref987654-product-manager",
                 first_seen=days_ago(60), sent=True, source="adzuna")
        self.add("new", "Other", "Product Manager", "https://www.jobs.example-career.at/de/jobs/ref987654-product-manager",
                 first_seen=days_ago(0))
        self.assertEqual(store.mark_if_duplicate(self.con, "new"), "new")

    def judged_original(self, jid, days, url):
        """What Misha applied to: sent, verdict interested, status applied."""
        self.add(jid, "Acme Labs", "Product Manager", url, first_seen=days_ago(days), sent=True, verdict="interested",
                 source="adzuna")
        self.con.execute("update jobs set status='applied', applied_at=? where id=?", (days_ago(days), jid))
        self.con.commit()

    def test_qa_a_judged_original_with_the_same_adzuna_link_suppresses_the_copy_at_31_days(self):
        self.judged_original("orig", 31, "https://www.adzuna.at/details/5908295645?utm_medium=api")
        self.add("copy", "Acme", "Product Manager", "https://www.adzuna.at/details/5908295645?utm_source=feed")
        self.assertEqual(store.mark_if_duplicate(self.con, "copy"), "copy")
        self.assertEqual(self.state("copy"), (1, "duplicate", "orig"))

    def test_qa_a_judged_original_with_the_same_adzuna_link_suppresses_the_copy_at_40_days(self):
        self.judged_original("orig", 40, "https://www.adzuna.at/details/5908295700?utm_medium=api")
        self.add("copy", "Acme", "Product Manager", "https://www.adzuna.at/details/5908295700?utm_source=feed")
        self.assertEqual(store.mark_if_duplicate(self.con, "copy"), "copy")
        self.assertEqual(self.state("copy"), (1, "duplicate", "orig"))

    def test_a_closed_unjudged_old_original_with_another_link_is_a_new_posting(self):
        """The company-name rule keeps S7-2: an old row that is neither shown nor judged does not suppress."""
        self.add("old", "Acme Labs", "Product Manager", "https://careers.acme-example.at/job/10", first_seen=days_ago(45))
        self.con.execute("update jobs set closed=1, closed_by='feed' where id='old'")
        self.con.commit()
        self.add("new", "Acme", "Product Manager", "https://careers.acme-example.at/job/11")
        self.assertIsNone(store.mark_if_duplicate(self.con, "new"))
        self.assertEqual(self.state("new"), (0, None, None))

    def test_a_judged_old_row_counts_as_recent_for_the_company_rule(self):
        self.judged_original("old", 45, "https://www.adzuna.at/details/77003")
        self.add("new", "Acme", "Product Manager", "https://careers.acme.example.at/job/12")
        self.assertEqual(store.mark_if_duplicate(self.con, "new"), "new")

    def test_the_same_link_at_60_days_suppresses_the_copy(self):
        self.add("old", "Acme Labs", "Product Manager", "https://www.adzuna.at/details/5908295800", first_seen=days_ago(60),
                 sent=True, source="adzuna")
        self.add("new", "Acme", "Product Manager", "https://www.adzuna.at/details/5908295800?utm_source=x")
        self.assertEqual(store.mark_if_duplicate(self.con, "new"), "new")

    def test_a_sent_row_within_30_days_still_suppresses(self):
        self.add("old", "Acme Labs", "Praktikum Marketing", "https://www.adzuna.at/details/77002",
                 first_seen=days_ago(20), sent=True, source="adzuna")
        self.add("new", "Acme", "Praktikum Marketing", "https://careers.acme.example.at/job/6", first_seen=days_ago(0))
        self.assertEqual(store.mark_if_duplicate(self.con, "new"), "new")


class Helpers(unittest.TestCase):
    def test_same_employer(self):
        self.assertTrue(store.same_employer("CHECK24", "CHECK24 Vergleichsportal Österreich"))
        self.assertTrue(store.same_employer("woom", "woom Bikes"))
        self.assertTrue(store.same_employer("Bausparkasse Wüstenrot", "Wüstenrot Gruppe"))
        self.assertFalse(store.same_employer("Raiffeisen Bank International", "Raiffeisenlandesbank NÖ-Wien"))
        self.assertFalse(store.same_employer("Bo", "Bo Bikes"), "shorter name under 4 letters never matches")
        self.assertFalse(store.same_employer("", "woom"))

    def test_posting_link(self):
        """host (www. ignored) + last path segment; a leading locale and the query do not count."""
        self.assertEqual(store.posting_link("https://jobs.check24.de/en/jobs/produkt-projektmanagement/"
                                            "ref669u-internship-product-management-all-genders"),
                         store.posting_link("https://www.jobs.check24.de/de/jobs/produkt-projektmanagement/"
                                            "ref669u-internship-product-management-all-genders/?locations=vienna"))
        self.assertEqual(store.posting_link("https://www.adzuna.at/details/5908295645?utm_medium=api"),
                         "adzuna.at/5908295645")
        self.assertEqual(store.posting_link("https://woom.bamboohr.com/careers/184"), "", "3-digit id is not specific")
        self.assertEqual(store.posting_link("https://woom.bamboohr.com/careers/184/detail"), "", "'detail' is generic")
        self.assertEqual(store.posting_link("https://jobs.example.at/en/jobs"), "")
        self.assertEqual(store.posting_link("https://jobs.example.at/de"), "")
        self.assertEqual(store.posting_link("https://www.gostudent.org/en-gb/job-position/"), "",
                         "12 letters, but one word-pair only: a page name")
        self.assertEqual(store.posting_link("https://jobs.example.at/en_GB/jobs/ref12345"), "jobs.example.at/ref12345",
                         "en_GB is a locale too")

    def test_generic_words_and_group_brands_alone_are_no_employer(self):
        """Each listed word, on its own, is not a company (legal forms are already dropped by company_part)."""
        for word in ("bank", "versicherung", "versicherungen", "group", "gruppe", "holding", "international",
                     "austria", "österreich", "österreichische", "oesterreichische", "wiener", "wien",
                     "raiffeisen", "volksbank", "sparkasse", "hypo"):
            self.assertTrue(store.is_generic_company(word), word)
        for word in ("bank", "versicherung", "versicherungen", "wiener", "wien", "raiffeisen", "volksbank",
                     "sparkasse", "hypo"):
            self.assertFalse(store.same_employer(word, f"{word} Labs"), word)
        self.assertFalse(store.same_employer("Volksbank Wien", "Volksbank Wien Immobilien"))
        self.assertFalse(store.same_employer("Bank Austria", "UniCredit Bank Austria"))

    def test_real_names_are_not_generic(self):
        for name in ("CHECK24", "woom", "Post", "Statistik Austria", "Wiener Städtische", "NAVAX", "Wüstenrot"):
            self.assertFalse(store.is_generic_company(name), name)

    def test_title_part_is_the_title_half_of_dedupe_key(self):
        """The company rule compares this part; it must be computed exactly like dedupe_key does."""
        for title in (CHECK24_INTERNSHIP, CHECK24_JUNIOR, WOOM_TITLE, "Praktikum Marketing", "Außendienst (m/w/d)",
                      "Product Owner*in Businessplattformen", "Менеджер продукта"):
            self.assertEqual(store.title_part(title), store.dedupe_key({"company": "X", "title": title}).split("|")[1],
                             title)


if __name__ == "__main__":
    unittest.main()
