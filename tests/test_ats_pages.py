"""Stage 7 wave 2 task B2 (26.10.2026): page-based job systems in ats_pages.py. Fixtures in tests/fixtures/ats_pages are
trimmed real responses read on 26.10.2026 (Phenom BCG, Prospective Helvetia, softgarden Craftworks and Volksbank,
onlyfy Magenta and Generali, AEM Wüstenrot, Oracle Ipsos). No network: ats_pages._read is replaced by fixture routes,
and the robots.txt check is tested with a stubbed reader."""
import json
import re
import sys
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ats_pages as A  # noqa: E402
import fetch_posting  # noqa: E402
from fetch_companies import is_austrian  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "ats_pages"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def company(**kw):
    c = dict(id="test-co", name="Test Co", niche="test", top=False, ats="test", feed_url="")
    c.update(kw)
    return c


class Reads:
    """Stands in for ats_pages._read: the first route whose key is in the URL answers; every URL is recorded."""

    def __init__(self, routes):
        self.routes, self.asked = routes, []

    def __call__(self, url, accept="text/html"):
        self.asked.append(url)
        for key, answer in self.routes:
            if key in url:
                return answer(url) if callable(answer) else answer
        raise AssertionError(f"unexpected read: {url}")


class AdapterTest(unittest.TestCase):

    def setUp(self):
        self._gap, A.GAP = A.GAP, 0  # no sleeping in tests
        A._LAST.clear()

    def tearDown(self):
        A.GAP = self._gap

    def run_adapter(self, fn, c, routes):
        reads = Reads(routes)
        with mock.patch.object(A, "_read", reads):
            return fn(c), reads


class PhenomTest(AdapterTest):
    FEED = "https://careers.bcg.com/global/en/search-results?qcountry=Austria"

    def test_whole_board_in_one_read(self):
        feed, reads = self.run_adapter(A.phenom, company(feed_url=self.FEED),
                                       [("search-results", fixture("phenom_bcg_austria.html"))])
        self.assertEqual(len(reads.asked), 1)
        self.assertIn("size=100", reads.asked[0])
        self.assertEqual(len(feed), 4)
        self.assertFalse(feed.capped)
        self.assertEqual(feed[0]["url"], "https://careers.bcg.com/global/en/job/57995")
        self.assertTrue(all(is_austrian(j["location"], j["title"]) for j in feed))

    def test_austrian_place_first_survives_the_200_char_cut(self):
        feed, _ = self.run_adapter(A.phenom, company(feed_url=self.FEED),
                                   [("search-results", fixture("phenom_bcg_austria.html"))])
        job_60265 = next(j for j in feed if j["url"].endswith("/job/60265"))
        self.assertTrue(job_60265["location"].startswith("Vienna, Austria"), job_60265["location"])

    def test_board_longer_than_read_is_capped(self):
        page = fixture("phenom_bcg_austria.html").replace('"totalHits": 4', '"totalHits": 6')
        feed, reads = self.run_adapter(A.phenom, company(feed_url=self.FEED), [("search-results", page)])
        self.assertTrue(feed.capped)
        self.assertEqual(len(reads.asked), A.MAX_PAGES)
        self.assertIn("from=4", reads.asked[1])

    def test_layout_change_raises(self):
        with self.assertRaises(ValueError):
            self.run_adapter(A.phenom, company(feed_url=self.FEED), [("search-results", "<html>no blob</html>")])


class ProspectiveTest(AdapterTest):

    def test_country_and_towns_form_the_location(self):
        feed, reads = self.run_adapter(
            A.prospective, company(feed_url="https://ohws.prospective.ch/public/v1/medium/1002532/jobs?offset=0"),
            [("ohws.prospective.ch", fixture("prospective_helvetia_p1.json"))])
        self.assertEqual(len(reads.asked), 1)
        self.assertEqual([j["location"] for j in feed], ["Wien, Österreich", "Villach, Österreich", "Schwyz, Schweiz"])
        self.assertEqual([is_austrian(j["location"], j["title"]) for j in feed], [True, True, False])
        self.assertEqual(feed[0]["url"], "https://jobs.helvetia.com/successfactors/EMEA_PREMIUM/Helvetia/austria/de/apply?id=19888")
        self.assertFalse(feed.capped)


class SoftgardenTest(AdapterTest):

    def test_board_rows_of_the_wicket_career_board(self):
        feed, _ = self.run_adapter(A.softgarden, company(feed_url="https://craftworksgmbh.softgarden.io/en/vacancies"),
                                   [("vacancies", fixture("softgarden_craftworks_vacancies.html"))])
        self.assertEqual(len(feed), 1)
        self.assertEqual(feed[0]["title"], "Talent")
        self.assertEqual(feed[0]["location"], "Wien")
        self.assertEqual(feed[0]["url"], "https://craftworksgmbh.softgarden.io/job/29812044/Talent?jobDbPVId=280474048&l=en")
        self.assertFalse(feed.capped)

    def test_board_without_job_list_raises(self):
        with self.assertRaises(ValueError):
            self.run_adapter(A.softgarden, company(feed_url="https://craftworksgmbh.softgarden.io/en/vacancies"),
                             [("vacancies", "<html>Career board</html>")])

    def test_json_feed_items_and_places(self):
        feed, _ = self.run_adapter(A.softgarden, company(feed_url="https://jobs.volksbankwien.at/jobs.feed.json"),
                                   [("feed.json", fixture("softgarden_volksbank_feed.json"))])
        self.assertEqual(len(feed), 5)
        self.assertEqual(feed[0]["url"], "https://jobs.volksbank.at/jobs/51127498/Initiativbewerbung/")
        self.assertEqual(feed[1]["location"], "Innsbruck, Tirol, Österreich")
        self.assertEqual(feed[0]["posted"], "2025-05-22")
        self.assertFalse(feed.capped)

    def test_json_feed_shorter_than_its_count_is_capped(self):
        data = json.loads(fixture("softgarden_volksbank_feed.json"))
        data["numberOfItems"] = 9
        feed, _ = self.run_adapter(A.softgarden, company(feed_url="https://jobs.volksbankwien.at/jobs.feed.json"),
                                   [("feed.json", json.dumps(data))])
        self.assertTrue(feed.capped)


class OnlyfyTest(AdapterTest):
    FEED = "https://magenta.onlyfy.jobs/en"

    def test_cards_of_the_board(self):
        empty_page = '<span data-testid="pagination-items-count"><b>11-18</b> out of <b>18 jobs</b></span>'
        feed, reads = self.run_adapter(A.onlyfy, company(feed_url=self.FEED),
                                       [("page=2", empty_page), ("magenta.onlyfy.jobs", fixture("onlyfy_magenta_p1.html"))])
        self.assertEqual(len(feed), 3)
        first, second, third = feed
        self.assertEqual(first["title"], "Regional Sales Manager (w/m/d) Field & Fiber Sales NÖ Nord")
        self.assertEqual(first["location"], "Vienna")
        self.assertEqual(first["employment"], "Full-time employee")
        self.assertEqual(first["posted"], "2026-10-14")
        self.assertEqual(first["url"], "https://magenta.onlyfy.jobs/en/job/cg730lwfbz2qn9j3r65e7xlmu15elkj")
        self.assertEqual(second["location"], "Graz")
        self.assertEqual(third["employment"], "Part-time / full-time")
        self.assertTrue(feed.capped)  # 3 of the 18 the page counts
        self.assertIn("page=2", reads.asked[1])

    def test_page_without_count_raises(self):
        with self.assertRaises(ValueError):
            self.run_adapter(A.onlyfy, company(feed_url=self.FEED), [("magenta.onlyfy.jobs", "<html>no list</html>")])

    def test_empty_board_is_an_empty_uncapped_feed(self):
        """wikifolio on 04.11.2026: onlyfy's own empty state, no card and no count (fixture trimmed from the real page)."""
        feed, reads = self.run_adapter(A.onlyfy, company(feed_url="https://wikifolio.onlyfy.jobs/en"),
                                       [("wikifolio.onlyfy.jobs", fixture("onlyfy_wikifolio_empty.html"))])
        self.assertEqual(len(reads.asked), 1)
        self.assertEqual(len(feed), 0)
        self.assertFalse(feed.capped)

    def test_empty_state_on_a_later_page_still_raises(self):
        with self.assertRaises(ValueError):
            self.run_adapter(A.onlyfy, company(feed_url=self.FEED),
                             [("page=2", fixture("onlyfy_wikifolio_empty.html")),
                              ("magenta.onlyfy.jobs", fixture("onlyfy_magenta_p1.html"))])

    def test_empty_words_with_a_job_card_are_not_an_empty_board(self):
        card = re.findall(r'<a [^>]*data-testid="job-card".*?</a>', fixture("onlyfy_magenta_p1.html"), re.S)[0]
        page = '<div data-testid="jobs-empty-state">No job openings right now</div>' + card
        with self.assertRaises(ValueError):
            self.run_adapter(A.onlyfy, company(feed_url=self.FEED), [("magenta.onlyfy.jobs", page)])


class AemTest(AdapterTest):
    FEED = ("https://www.wuestenrot.at/de/ueber-uns/karriere/offene-stellen/_jcr_content/root/container/"
            "container_469680561/job_overview.results.html")

    def test_result_items_of_the_company_list(self):
        no_more = '<span class="job-results__numTotalResults">50</span>'
        feed, _ = self.run_adapter(A.aem_results, company(feed_url=self.FEED),
                                   [("job_overview", lambda url: no_more if "page=2" in url
                                     else fixture("aem_wuestenrot_p1.html"))])
        self.assertEqual(len(feed), 3)
        self.assertEqual(feed[0]["title"], "Mitarbeiter:in für 1st und 2nd Level IT-Support (m/w/d)")
        self.assertEqual(feed[0]["location"], "Salzburg")
        self.assertEqual(feed[0]["url"], "https://www.wuestenrot.at/de/ueber-uns/karriere/offene-stellen/"
                                         "mitarbeiter-in-fuer-1st-und-2nd-level-it-support-m-w-d.html")
        self.assertTrue(feed.capped)  # 3 of 50


class OracleTest(AdapterTest):
    FEED = "https://ecqf.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/IpsosCareers"

    def test_board_without_austrian_jobs(self):
        feed, reads = self.run_adapter(A.oracle, company(feed_url=self.FEED, site_number="CX_2001"),
                                       [("recruitingCEJobRequisitions", fixture("oracle_ipsos_p1.json"))])
        self.assertEqual(len(reads.asked), 1)
        self.assertEqual(len(feed), 3)
        self.assertEqual(feed[0]["url"], self.FEED + "/job/9446")
        self.assertFalse(feed.capped)
        self.assertFalse(any(is_austrian(j["location"], j["title"]) for j in feed))

    def test_country_code_at_counts_as_austria(self):  # synthetic: no Austrian job on the real board today
        data = json.loads(fixture("oracle_ipsos_p1.json"))
        req = data["items"][0]["requisitionList"][0]
        req.update(PrimaryLocation="Vienna", PrimaryLocationCountry="AT", secondaryLocations=[])
        feed, _ = self.run_adapter(A.oracle, company(feed_url=self.FEED, site_number="CX_2001"),
                                   [("recruitingCEJobRequisitions", json.dumps(data))])
        self.assertEqual(feed[0]["location"], "Vienna; Österreich")
        self.assertTrue(is_austrian(feed[0]["location"], feed[0]["title"]))

    def test_answer_without_total_raises(self):
        with self.assertRaises(ValueError):
            self.run_adapter(A.oracle, company(feed_url=self.FEED, site_number="CX_2001"),
                             [("recruitingCEJobRequisitions", '{"items": [{}]}')])


class ReadRulesTest(unittest.TestCase):

    def test_disallowed_page_is_never_read(self):
        with mock.patch.object(fetch_posting, "_robots_ok", return_value=False), \
                mock.patch.object(A, "_fetch") as fetch:
            with self.assertRaises(PermissionError):
                A._read("https://careers.bcg.com/global/en/search-results?qcountry=Austria")
            fetch.assert_not_called()

    def test_refused_redirect_targets(self):
        req = urllib.request.Request("https://careers.bcg.com/global/en/search-results")
        for target in ("https://www.indeed.com/jobs", "http://127.0.0.1/admin", "ftp://careers.bcg.com/x"):
            with self.subTest(target=target), self.assertRaises(urllib.error.URLError):
                A._Hop().redirect_request(req, None, 302, "Found", {}, target)


def phenom_jobs():
    return A._json_after(fixture("phenom_bcg_austria.html"), "phApp.ddo = ")["eagerLoadRefineSearch"]["data"]["jobs"]


def phenom_page(total, jobs):
    blob = {"eagerLoadRefineSearch": {"totalHits": total, "data": {"jobs": jobs}}}
    return "<script>phApp.ddo = " + json.dumps(blob) + ";</script>"


def oracle_page(total, reqs):
    return json.dumps({"items": [{"TotalJobsCount": total, "requisitionList": reqs}]})


class FakeClock:
    """Stands in for ats_pages._clock; the test moves the time forward."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class TimeLimitTest(AdapterTest):
    BCG = "https://careers.bcg.com/global/en/search-results?qcountry=Austria"

    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        cache = mock.patch.dict(fetch_posting._ROBOTS)  # the real reader caches each host's robots.txt once
        cache.start()
        self.addCleanup(cache.stop)
        for target, name, value in ((A, "_clock", self.clock), (fetch_posting, "_robots_ok", self.fake_robots)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def fake_robots(url):
        parts = urllib.parse.urlsplit(url)
        fetch_posting._ROBOTS.setdefault(f"{parts.scheme}://{parts.netloc}", object())
        return True

    def test_slow_pages_stop_at_the_company_limit_and_stay_capped(self):
        page = fixture("phenom_bcg_austria.html").replace('"totalHits": 4', '"totalHits": 9')
        calls = []

        def slow(url, accept, timeout):
            calls.append(timeout)
            self.clock.now += 25
            return page

        with mock.patch.object(A, "_fetch", slow):
            feed = A.phenom(company(feed_url=self.BCG))
        self.assertEqual(calls, [40, 35, 10])  # 60 s in all: each read gets min(40, time left)
        self.assertTrue(feed.capped)
        self.assertEqual(len(feed), 4)

    def test_no_robots_read_without_time_for_it(self):
        robots = mock.Mock(return_value=True)
        page = fixture("phenom_bcg_austria.html").replace('"totalHits": 4', '"totalHits": 9')

        def slow(url, accept, timeout):
            self.clock.now += 45  # 15 s left: too little for an uncached robots.txt read (fetch_posting.DEADLINE)
            return page

        with mock.patch.object(fetch_posting, "_robots_ok", robots), mock.patch.object(A, "_fetch", slow):
            feed = A.phenom(company(feed_url=self.BCG))
        self.assertEqual(robots.call_count, 1)
        self.assertTrue(feed.capped)

    def test_single_read_out_of_time_returns_an_empty_capped_feed(self):
        with mock.patch.object(A, "_read", side_effect=A.OutOfTime("late")):
            feed = A.softgarden(company(feed_url="https://jobs.volksbankwien.at/jobs.feed.json"))
        self.assertEqual(len(feed), 0)
        self.assertTrue(feed.capped)


class PagingTotalTest(AdapterTest):
    """A later page may report total 0 (Workday does), and an empty page may come before the total: neither may end
    the read as complete. The first page's total is the one that counts."""
    BCG = "https://careers.bcg.com/global/en/search-results?qcountry=Austria"

    def test_phenom_later_total_zero_does_not_lower_the_first(self):
        first, later = phenom_page(6, phenom_jobs()), phenom_page(0, [])
        feed, _ = self.run_adapter(A.phenom, company(feed_url=self.BCG),
                                   [("from=4", later), ("search-results", first)])
        self.assertEqual(len(feed), 4)
        self.assertTrue(feed.capped)

    def test_oracle_empty_page_before_total_is_capped(self):
        base = json.loads(fixture("oracle_ipsos_p1.json"))["items"][0]["requisitionList"][0]
        first = oracle_page(60, [dict(base, Id=str(9000 + i)) for i in range(50)])
        feed, _ = self.run_adapter(A.oracle, company(feed_url=OracleTest.FEED, site_number="CX_2001"),
                                   [("offset=50", oracle_page(0, [])), ("recruitingCEJobRequisitions", first)])
        self.assertEqual(len(feed), 50)
        self.assertTrue(feed.capped)

    def test_prospective_empty_page_before_total_is_capped(self):
        job_0 = json.loads(fixture("prospective_helvetia_p1.json"))["jobs"][0]
        first = json.dumps({"total": 150, "jobs": [dict(job_0, id=str(i)) for i in range(100)]})
        feed, _ = self.run_adapter(A.prospective, company(feed_url="https://ohws.prospective.ch/public/v1/medium/1002532/jobs?offset=0"),
                                   [("offset=100", json.dumps({"total": 0, "jobs": []})),
                                    ("ohws.prospective.ch", first)])
        self.assertEqual(len(feed), 100)
        self.assertTrue(feed.capped)

    def test_aem_empty_page_before_total_is_capped(self):
        feed, _ = self.run_adapter(A.aem_results, company(feed_url=AemTest.FEED),
                                   [("page=2", '<span class="job-results__numTotalResults">0</span>'),
                                    ("job_overview", fixture("aem_wuestenrot_p1.html"))])
        self.assertEqual(len(feed), 3)
        self.assertTrue(feed.capped)

    def test_onlyfy_later_zero_total_is_capped(self):
        empty = '<span data-testid="pagination-items-count"><b>1-0</b> out of <b>0 jobs</b></span>'
        feed, _ = self.run_adapter(A.onlyfy, company(feed_url="https://magenta.onlyfy.jobs/en"),
                                   [("page=2", empty), ("magenta.onlyfy.jobs", fixture("onlyfy_magenta_p1.html"))])
        self.assertEqual(len(feed), 3)
        self.assertTrue(feed.capped)

    def test_adapters_registered_for_the_merge(self):
        self.assertEqual(set(A.ADAPTERS), {"phenom", "prospective", "oracle", "softgarden", "onlyfy", "aem_results"})
        self.assertTrue(all(callable(fn) for fn in A.ADAPTERS.values()))


if __name__ == "__main__":
    unittest.main()
