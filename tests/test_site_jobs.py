"""site_jobs: the generic company-site adapter. No network: fetch_posting's refusal check, the HTML reader and the JSON
reader are replaced. Fixtures are trimmed real responses from the task C companies, except jsonld_joblist_synthetic.html
(no company of task C publishes JobPosting JSON-LD, so that case is built to the schema.org shape)."""
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import fetch_companies as fc  # noqa: E402
import fetch_posting as fp  # noqa: E402
import site_jobs as sj  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "site_jobs"
GUARD_ID = "acme"


def fixture(name):
    return (FIX / name).read_bytes()


def company(**over):
    c = {"id": GUARD_ID, "name": "Acme GmbH", "niche": "agency", "top": False, "ats": "site",
         "feed_url": "https://acme.example/karriere/"}
    c.update(over)
    return c


class SiteTest(unittest.TestCase):
    """Pages and JSON come from dicts; the refusal check allows everything unless a test says otherwise."""

    def setUp(self):
        self.pages, self.json_pages, self.refused = {}, {}, set()
        self.fetched, self.bodies, self.timeouts = [], [], []
        for patch in (mock.patch.object(fp, "_refusal", side_effect=self._refusal),
                      mock.patch.object(fp, "_open_page", side_effect=self._open_page),
                      mock.patch.object(sj, "_json", side_effect=self._json)):
            patch.start()
            self.addCleanup(patch.stop)

    def _refusal(self, url, guard):
        return "banned host" if url in self.refused else None

    def _open_page(self, url, guard, timeout):
        self.fetched.append(url)
        self.timeouts.append(timeout)
        return url, "text/html", "utf-8", self.pages[url]

    def _json(self, url, guard, budget, body=None):
        budget.spend()
        self.fetched.append(url)
        self.bodies.append(body)
        return self.json_pages[url]() if callable(self.json_pages[url]) else self.json_pages[url]


class ParsingTest(SiteTest):
    def test_jsonld_postings_place_remote_and_denied_link(self):
        """schema.org JobPosting: address parts give the place, TELECOMMUTE gives Remote, a LinkedIn link is never kept."""
        url = "https://acme.example/karriere/"
        self.pages[url] = fixture("jsonld_joblist_synthetic.html")
        feed = sj.site(company(feed_url=url))
        self.assertEqual(feed.source, "jsonld")
        self.assertEqual([(j["title"], j["location"]) for j in feed],
                         [("Marketing Manager (m/w/d)", "Wien, AT"), ("Remote Analyst", "Remote")])
        self.assertEqual(feed.bad, 1)
        self.assertEqual(feed[0]["posted"], "2026-10-20")
        self.assertTrue(feed.capped)  # no complete flag: the page is not shown to be the whole board

    def test_cards_title_pattern_place_and_unsolicited_application(self):
        """smatrics: title before 'Vollzeit', place in brackets; 'Initiativbewerbung' is not a vacancy."""
        url = "https://smatrics.com/de/jobs"
        self.pages[url] = fixture("smatrics_cards.html")
        feed = sj.site(company(feed_url=url, link_pattern=r"smatrics\.com/de/jobs/[A-Za-z0-9-]+$",
                               title_pattern=r"([^|]+?)\s*\|\s*(?:Vollzeit|Teilzeit)",
                               location_pattern=r"([^|]*\(Österreich\))"))
        titles = [j["title"] for j in feed]
        self.assertIn("LOCI Entwickler:in (m/w/d)", titles)
        self.assertIn("Produkt Manager:in E-Mobilität (m/w/d)", titles)
        self.assertNotIn("Initiativbewerbung", titles)
        self.assertEqual(feed.bad, 1)
        loci = next(j for j in feed if j["title"] == "LOCI Entwickler:in (m/w/d)")
        self.assertEqual(loci["location"], "Wien (Österreich)")
        self.assertTrue(fc.is_austrian(loci["location"], loci["title"]))
        rollout = next(j for j in feed if j["title"].startswith("Rollout"))
        self.assertEqual(rollout["location"], "")  # no place on the card: not counted as Austrian

    def test_title_and_place_after_the_link(self):
        """oberbank /jobs: the link says 'Jobdetails anzeigen'; the title and the place follow it in the card."""
        url = "https://www.oberbank.at/jobs"
        self.pages[url] = fixture("oberbank_jobs.html")
        feed = sj.site(company(feed_url=url, link_pattern=r"oberbank\.at/jobs/[a-z0-9-]+-\d+$",
                               title_pattern=r"^([^|]+)", location_pattern=r"^[^|]+\|\s*([^|]+)", location_after=True))
        self.assertEqual(len(feed), 16)
        self.assertEqual((feed[0]["title"], feed[0]["location"]),
                         ("Junior Financial Risk Manager (w/m/d)", "Linz"))
        self.assertIn("Leitung Filiale Krems (w/m/d)", [j["title"] for j in feed])
        self.assertNotIn("Jobdetails anzeigen", [j["title"] for j in feed])

    def test_statistik_placeholder_dropped_and_complete_board(self):
        """Statistik Austria: 16 links, one is a placeholder /job/0; the page states 15 open positions."""
        url = "https://www.statistik.at/ueber-uns/karriere/jobangebote"
        self.pages[url] = fixture("statistik_table.html")
        cfg = dict(feed_url=url, link_pattern=r"statistik\.at/ueber-uns/karriere/jobangebote/bewerbung/job/[1-9]\d*$",
                   title_pattern=r"([^|]+?)\s*\|\s*Stellenbeschreibung", default_location="Österreich",
                   count_pattern=r"Derzeit gibt es (\d+) offene Stellen")
        feed = sj.site(company(**cfg))
        self.assertEqual(len(feed), 15)
        self.assertEqual(feed.bad, 0)  # the placeholder /job/0 is not a card of the list
        self.assertTrue(all(not j["url"].endswith("/job/0") for j in feed))
        self.assertFalse(feed.capped)
        self.assertEqual(feed[0]["title"], "Data Scientist:in für die Bildungsstatistik (befristet bis 30. Juni 2028)")
        self.assertTrue(all(j["location"] == "Österreich" for j in feed))

    def test_default_location_only_when_the_card_names_no_place(self):
        url = "https://smatrics.com/de/jobs"
        self.pages[url] = fixture("smatrics_cards.html")
        feed = sj.site(company(feed_url=url, link_pattern=r"smatrics\.com/de/jobs/[A-Za-z0-9-]+$",
                               title_pattern=r"([^|]+?)\s*\|\s*(?:Vollzeit|Teilzeit)",
                               location_pattern=r"([^|]*\(Österreich\))", default_location="Wien"))
        rollout = next(j for j in feed if j["title"].startswith("Rollout"))
        self.assertEqual(rollout["location"], "Wien")
        loci = next(j for j in feed if j["title"].startswith("LOCI"))
        self.assertEqual(loci["location"], "Wien (Österreich)")


class JsonFeedTest(SiteTest):
    def test_paging_reads_until_total_and_stops_at_the_request_limit(self):
        """a1 shows 6 jobs per page and 54 in total; a run may spend 6 requests, so it reads 6 pages (36 jobs) and is capped."""
        base = fixture("a1_filter_jobs_page1.json")
        first = json.loads(base.decode("utf-8"))
        url = "https://jobs.a1.com/job-search/"
        feed_url = "https://jobs.a1.com/wp-json/a1-group/v1/filter-jobs?country=Austria"

        def page(p):
            # each page gets its own job links, as on the live endpoint
            data = json.loads(base.decode("utf-8"))
            for it in data["items"]:
                it["url"] = f"{it['url']}?p={p}"
                it["id"] = f"{it['id']}-{p}"
            return data
        self.json_pages[feed_url + "&page=1"] = lambda: page(1)
        for p in range(2, 10):
            self.json_pages[feed_url + f"&page={p}"] = (lambda q: lambda: page(q))(p)
        spec = {"url": feed_url, "items": "items", "title": "title", "link": "url", "location": "location",
                "country": "Österreich", "total": "totalItems", "page_param": "page", "page_start": 1, "page_step": 1}
        feed = sj.site(company(feed_url=url, json_feed=spec))
        self.assertEqual(feed.source, "json")
        self.assertEqual(len(feed), 36)  # 6 pages x 6 jobs; the list of 54 is not finished within the request cap
        self.assertTrue(feed.capped)
        self.assertEqual(feed.requests, 6)
        self.assertTrue(all(fc.is_austrian(j["location"], j["title"]) for j in feed))
        self.assertIn(f"{feed_url}&page=2", self.fetched)
        self.assertEqual(first["totalItems"], 54)

    def test_country_suffix_names_austria_for_towns_the_matcher_misses(self):
        """Stockerau and Pasching are not in fetch_companies' list of Austrian places; the endpoint is filtered to Austria."""
        feed_url = "https://jobs.a1.com/wp-json/a1-group/v1/filter-jobs?country=Austria"
        self.json_pages[feed_url] = json.loads(fixture("a1_filter_jobs_page1.json").decode("utf-8"))
        spec = {"url": feed_url, "items": "items", "title": "title", "link": "url", "location": "location",
                "country": "Österreich"}
        feed = sj.site(company(feed_url="https://jobs.a1.com/job-search/", json_feed=spec))
        self.assertEqual(feed[0]["location"], "Stockerau, Österreich")
        self.assertTrue(fc.is_austrian(feed[0]["location"], feed[0]["title"]))
        self.assertTrue(feed.capped)  # one read of an unpaged list, no complete flag

    def test_json_list_read_in_full_is_complete(self):
        """Microsoft careers API: 4 positions and count 4 for the Vienna search, so the whole list was read."""
        feed_url = "https://apply.careers.microsoft.com/api/pcsx/search?domain=microsoft.com&query=&location=Vienna%2C%20Austria&start=0"
        self.json_pages[sj._with_param(feed_url, "start", 0)] = json.loads(fixture("microsoft_pcsx_vienna.json").decode("utf-8"))
        spec = {"url": feed_url, "items": "data.positions", "title": "name", "link": "positionUrl",
                "location": "locations", "posted": "postedTs", "total": "data.count", "page_param": "start",
                "page_start": 0, "page_step": 10}
        feed = sj.site(company(id="microsoft-austria", name="Microsoft Austria",
                               feed_url="https://apply.careers.microsoft.com/careers", json_feed=spec))
        self.assertEqual(len(feed), 4)
        self.assertFalse(feed.capped)
        self.assertTrue(feed[0]["url"].startswith("https://apply.careers.microsoft.com/careers/job/"))
        self.assertEqual(feed[0]["posted"], fc.day(json.loads(fixture("microsoft_pcsx_vienna.json").decode("utf-8"))
                                                    ["data"]["positions"][0]["postedTs"]))


class RulesTest(SiteTest):
    def test_no_vacancies_on_the_page_raises(self):
        """An empty answer must not reach the feed: an uncapped empty feed would close every job of the company."""
        url = "https://acme.example/karriere/"
        self.pages[url] = b"<html><body><p>Derzeit keine offenen Stellen.</p></body></html>"
        with self.assertRaises(sj.SiteError):
            sj.site(company(feed_url=url, link_pattern=r"acme\.example/jobs/[0-9]+$"))

    def test_refused_list_page_is_never_fetched(self):
        url = "https://www.karriere.at/jobs"
        self.refused.add(url)
        with self.assertRaises(sj.SiteError) as cm:
            sj.site(company(feed_url=url, link_pattern=r"karriere\.at/jobs/"))
        self.assertIn("banned host", str(cm.exception))
        self.assertEqual(self.fetched, [])

    def test_request_budget_ends_paging_as_a_capped_feed(self):
        """Once the budget is spent, the paged read stops and the feed is capped, so nothing is closed on it."""
        budget = sj._Budget()
        budget.used = sj.MAX_REQUESTS
        self.assertFalse(budget.ok())
        with self.assertRaises(sj.SiteError):
            budget.spend()

    def test_helpers(self):
        self.assertEqual(sj._place("Graz | Graz | Munich | Munich "), "Graz, Munich")
        self.assertEqual(sj._place(""), "")
        self.assertEqual(sj._dig({"data": {"count": 4}}, "data.count"), 4)
        self.assertIsNone(sj._dig({"data": 1}, "data.count"))
        self.assertEqual(sj._with_param("https://x.example/a?page=1&q=2", "page", 3), "https://x.example/a?q=2&page=3")
        self.assertFalse(sj._keep({"title": "Initiativbewerbung (m/w/x) Graz", "url": "https://x.example/1"}))
        self.assertFalse(sj._keep({"title": "Data Analyst", "url": "https://www.linkedin.com/jobs/view/1"}))
        self.assertTrue(sj._keep({"title": "Data Analyst", "url": "https://x.example/1"}))

    def test_adapter_registered_for_fetch_companies(self):
        self.assertEqual(sj.ADAPTERS, {"site": sj.site})


class BudgetTest(unittest.TestCase):
    def test_default_is_six_requests_and_thirty_seconds(self):
        b = sj._Budget()
        self.assertEqual((b.max_requests, b.seconds), (6, 30))

    def test_company_value_scales_seconds_and_has_a_ceiling(self):
        self.assertEqual((sj._Budget(10).max_requests, sj._Budget(10).seconds), (10, 50))
        self.assertEqual((sj._Budget(20).max_requests, sj._Budget(20).seconds), (12, 60))
        self.assertEqual(sj._Budget(0).max_requests, 6)


def synthetic_pages(pages, per_page):
    """Pages of a JSON list in the shape of a1's filter-jobs endpoint: items and totalItems, 1-based page parameter."""
    def make(p):
        return {"items": [{"id": f"{p}-{i}", "title": f"Job {p}-{i}", "url": f"https://jobs.example.com/job/{p}-{i}",
                           "location": "Wien"} for i in range(per_page)], "totalItems": pages * per_page}
    return make


class CompletenessTest(SiteTest):
    def a1_spec(self, feed_url):
        return {"url": feed_url, "items": "items", "title": "title", "link": "url", "location": "location",
                "total": "totalItems", "page_param": "page", "page_start": 1, "page_step": 1}

    def test_list_read_in_full_within_the_company_limit_is_not_capped(self):
        feed_url = "https://jobs.example.com/list?country=Austria"
        make = synthetic_pages(9, 6)  # 54 jobs in 9 pages
        for p in range(1, 10):
            self.json_pages[sj._with_param(feed_url, "page", p)] = (lambda q: lambda: make(q))(p)
        feed = sj.site(company(feed_url="https://jobs.example.com/", json_feed=self.a1_spec(feed_url), max_requests=10))
        self.assertEqual(len(feed), 54)
        self.assertFalse(feed.capped)
        self.assertEqual(feed.requests, 9)

    def test_same_list_with_the_default_limit_stops_after_six_pages_and_is_capped(self):
        feed_url = "https://jobs.example.com/list?country=Austria"
        make = synthetic_pages(9, 6)
        for p in range(1, 10):
            self.json_pages[sj._with_param(feed_url, "page", p)] = (lambda q: lambda: make(q))(p)
        feed = sj.site(company(feed_url="https://jobs.example.com/", json_feed=self.a1_spec(feed_url)))
        self.assertEqual(len(feed), 36)
        self.assertTrue(feed.capped)
        self.assertEqual(feed.requests, 6)

    def test_dynatrace_search_post_is_read_in_one_request_and_is_complete(self):
        """Dynatrace's careers list is one POST to its own search endpoint (anonymous JSON, 109 jobs in total)."""
        feed_url = "https://www.dynatrace.com/api/coveo/search/"
        self.json_pages[feed_url] = json.loads(fixture("dynatrace_coveo_search.json").decode("utf-8"))
        body = {"q": "", "numberOfResults": 1000, "wildcards": True, "fieldsToInclude": ["country"], "facets": []}
        spec = {"url": feed_url, "method": "POST", "body": body, "items": "results", "title": "title", "link": "uri",
                "location": "raw.country", "total": "totalCount"}
        feed = sj.site(company(id="dynatrace", name="Dynatrace", feed_url="https://www.dynatrace.com/careers/jobs/",
                               json_feed=spec))
        self.assertEqual(self.bodies, [body])
        self.assertEqual(len(feed), 109)
        self.assertFalse(feed.capped)
        austria = [j for j in feed if fc.is_austrian(j["location"], j["title"])]
        self.assertEqual(len(austria), 15)
        self.assertTrue(all(j["url"].startswith("https://www.dynatrace.com/careers/jobs/") for j in austria))


class TitleTest(SiteTest):
    def test_heading_inside_the_link_is_the_title_not_the_teaser(self):
        """check24: the link holds an <h3> with the job title and a share button labelled 'Teilen'."""
        url = "https://jobs.check24.de/de/jobs/?locations=vienna"
        self.pages[url] = fixture("check24_list.html")
        feed = sj.site(company(id="check24-oesterreich", name="CHECK24", feed_url=url,
                               link_pattern=r"jobs\.check24\.de/de/jobs/[a-z-]+/ref[a-z0-9]+-", default_location="Wien"))
        self.assertEqual(len(feed), 5)
        self.assertEqual(feed[0]["title"], "Internship - Product Management (all genders)")
        self.assertTrue(all("Teilen" not in j["title"] for j in feed))
        self.assertTrue(all(j["location"] == "Wien" for j in feed))

    def test_title_clean_drops_the_numbering_prefix(self):
        """wild: 'J / 005 Senior Digital Designer (f/m/d) Remote' -> the job title; 'Remote' stays as the place."""
        url = "https://wild.as/jobs"
        self.pages[url] = fixture("wild_jobs.html")
        feed = sj.site(company(id="wild", name="Wild", feed_url=url, link_pattern=r"wild\.as/jobs/[a-z0-9-]+$",
                               location_pattern=r"(Remote|Wien|Vienna)", count_pattern=r"See (\d+) Job openings?",
                               title_clean=r"^J / \d+\s+(.*?)(?:\s+Remote)?$"))
        self.assertEqual(len(feed), 1)
        self.assertEqual(feed[0]["title"], "Senior Digital Designer (f/m/d)")
        self.assertEqual(feed[0]["location"], "Remote")
        self.assertFalse(feed.capped)


def production(cid):
    """The company's row in companies.json: the config the morning run actually uses."""
    rows = json.loads((ROOT / "companies.json").read_text(encoding="utf-8"))
    return next(r for r in rows if r["id"] == cid)


def slug_matches(url, title, minimum=0.99):
    """Share of the title's words (3+ letters) that appear in the URL's slug; 1.0 when the card and the link agree."""
    import re
    slug = re.sub(r"-\d+$", "", url.rsplit("/", 1)[1])
    words = [w for w in re.findall(r"[a-z]+", sj.enrich._fold(title)) if len(w) > 2]
    return bool(words) and sum(w in slug for w in words) / len(words) >= minimum


class ProductionConfigTest(SiteTest):
    """Tests the companies.json rows of the seven companies re-verified in the QA round."""

    def test_oberbank_title_and_place_come_from_the_same_card(self):
        """M1: the title is the link's own title="Jobdetails zu ..."; the place is read from the card before the link."""
        doc = fixture("oberbank_jobs.html").decode("utf-8")
        jobs, bad = sj._from_links(doc, production("oberbank"), "https://www.oberbank.at/jobs")
        self.assertEqual(len(jobs), 16)
        self.assertEqual(bad, 0)
        self.assertTrue(all(slug_matches(j["url"], j["title"]) for j in jobs),
                        [(j["title"], j["url"]) for j in jobs if not slug_matches(j["url"], j["title"])][:3])
        self.assertEqual((jobs[1]["title"], jobs[1]["location"]), ("Junior Financial Risk Manager (w/m/d)", "Linz"))
        self.assertEqual((jobs[4]["title"], jobs[4]["location"]), ("Leitung Filiale Krems (w/m/d)", "Krems"))
        self.assertTrue(jobs[5]["title"].startswith("Spezialist:in Geldwäscheprävention & Terrorismusfinanzierung (w/m/d)"))
        self.assertEqual(jobs[5]["location"], "Linz")
        self.assertTrue(all(fc.is_austrian(j["location"], j["title"]) for j in jobs))

    def test_denizbank_title_is_the_job_title_not_the_card_text(self):
        """Minor 9: the card's 'Vollzeit Wien, Zentrale Einzelheiten' is not part of the title."""
        doc = fixture("denizbank_list.html").decode("utf-8")
        jobs, _ = sj._from_links(doc, production("denizbank-austria"), "https://www.denizbank.at/at/uber-uns/karriere/stellenangebote")
        self.assertEqual(len(jobs), 10)
        self.assertEqual(jobs[0]["title"], "Corporate Banking Coordinator (m/f/d)")
        self.assertTrue(all("Vollzeit" not in j["title"] and "Einzelheiten" not in j["title"] for j in jobs))
        # the card's place ('Vollzeit Wien, Zentrale') moved out of the title with the clean-up: it must be read as the place
        self.assertEqual([j["location"] for j in jobs][:4], ["Wien", "Wien", "Wien", "Wien"])
        self.assertTrue(all(fc.is_austrian(j["location"], j["title"]) for j in jobs))

    def test_statistik_complete_only_when_the_stated_count_matches(self):
        """Minor 1: 'Derzeit gibt es 15 offene Stellen' is checked against the 15 vacancies read."""
        row = production("statistik-austria")
        url = row["feed_url"]
        self.pages[url] = fixture("statistik_table.html")
        self.assertFalse(sj.site(row).capped)
        self.pages[url] = fixture("statistik_table.html").replace(b"Derzeit gibt es 15", b"Derzeit gibt es 16")
        self.assertTrue(sj.site(row).capped)
        self.pages[url] = fixture("statistik_table.html").replace(b"Derzeit gibt es 15 offene Stellen", b"")
        self.assertTrue(sj.site(row).capped)  # no count on the page: capped

    def test_wild_complete_only_when_the_stated_count_matches(self):
        row = production("wild")
        url = row["feed_url"]
        self.pages[url] = fixture("wild_jobs.html")
        self.assertFalse(sj.site(row).capped)
        self.pages[url] = fixture("wild_jobs.html").replace(b"See 1 Job openings", b"See 2 Job openings")
        self.assertTrue(sj.site(row).capped)

    def test_bearingpoint_counts_the_unsolicited_card_as_listed(self):
        """'18 Stellen verfügbar' = 17 vacancies + 1 Initiativbewerbung card: complete when all 18 cards are accounted for."""
        row = production("bearingpoint-austria")
        url = row["feed_url"]
        self.pages[url] = fixture("bearingpoint_list.html")
        feed = sj.site(row)
        self.assertEqual(len(feed), 17)
        self.assertFalse(feed.capped)
        self.pages[url] = fixture("bearingpoint_list.html").replace(b"18 Stellen verf", b"19 Stellen verf")
        self.assertTrue(sj.site(row).capped)


class PagingCompletenessTest(SiteTest):
    def a1_spec(self, feed_url, **extra):
        spec = {"url": feed_url, "items": "items", "title": "title", "link": "url", "location": "location",
                "total": "totalItems", "page_param": "page", "page_start": 1, "page_step": 1}
        spec.update(extra)
        return spec

    def test_empty_page_before_the_total_is_capped_not_complete(self):
        """M2: a1 page 3 comes back empty with totalItems 54: 12 read, not complete, so nothing is closed."""
        feed_url = "https://jobs.example.com/list?country=Austria"
        make = synthetic_pages(9, 6)
        self.json_pages[sj._with_param(feed_url, "page", 1)] = lambda: make(1)
        self.json_pages[sj._with_param(feed_url, "page", 2)] = lambda: make(2)
        self.json_pages[sj._with_param(feed_url, "page", 3)] = {"items": [], "totalItems": 54}
        feed = sj.site(company(feed_url="https://jobs.example.com/", json_feed=self.a1_spec(feed_url), max_requests=10))
        self.assertEqual(len(feed), 12)
        self.assertTrue(feed.capped)

    def test_without_a_total_complete_when_the_last_page_is_shorter_than_page_size(self):
        feed_url = "https://jobs.example.com/list"
        self.json_pages[sj._with_param(feed_url, "page", 1)] = lambda: {"items": synthetic_pages(1, 6)(1)["items"]}
        self.json_pages[sj._with_param(feed_url, "page", 2)] = lambda: {"items": synthetic_pages(1, 4)(2)["items"]}
        spec = {"url": feed_url, "items": "items", "title": "title", "link": "url", "location": "location",
                "page_param": "page", "page_start": 1, "page_step": 1, "page_size": 6}
        feed = sj.site(company(feed_url="https://jobs.example.com/", json_feed=spec))
        self.assertEqual(len(feed), 10)
        self.assertFalse(feed.capped)
        self.assertEqual(feed.requests, 2)


class TimeLimitTest(SiteTest):
    def test_each_page_read_gets_at_most_the_time_left(self):
        """Minor 2: the per-request timeout is min(default, time left) of the company's budget."""
        url = "https://acme.example/karriere/"
        self.pages[url] = b"<html><body><p>x</p></body></html>"
        budget = sj._Budget()
        budget.started -= sj.SECONDS_PER_REQUEST * budget.max_requests - 3  # 3 s left
        sj._html(url, company(), budget)
        self.assertLessEqual(self.timeouts[-1], 3.0)


class RetryTest(unittest.TestCase):
    """Minor 7: the Microsoft API answers 429: wait Retry-After (max 20 s, else 10 s) and retry once."""

    class Resp:
        def __init__(self, body):
            self.body = body
            self.headers = mock.Mock(get_content_type=lambda: "application/json")

        def geturl(self):
            return "https://apply.careers.microsoft.com/api/pcsx/search?start=0"

        def read1(self, n=-1):
            body, self.body = self.body, b""
            return body

        def read(self, n=-1):
            return self.read1(n)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    @staticmethod
    def too_many(retry_after=None):
        from email.message import Message
        m = Message()
        if retry_after is not None:
            m["Retry-After"] = retry_after
        return m

    def run_json(self, replies):
        """replies: a body (bytes) for a 200, or a header Message for a 429. Returns (result or exception, sleep mock)."""
        import io
        from email.message import Message
        queue = list(replies)
        opener = mock.Mock()

        def open_(req, timeout=None):
            reply = queue.pop(0)
            if isinstance(reply, Message):
                raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", reply, io.BytesIO(b""))
            return self.Resp(reply)
        opener.open.side_effect = open_
        with mock.patch.object(fp, "_refusal", return_value=None), mock.patch.object(fp, "_polite"), \
                mock.patch("urllib.request.build_opener", return_value=opener), \
                mock.patch("site_jobs.time.sleep") as sleep:
            try:
                data = sj._json("https://apply.careers.microsoft.com/api/pcsx/search?start=0",
                                {"company": "Microsoft", "company_id": "microsoft-austria", "title": ""}, sj._Budget())
            except Exception as e:  # the test asserts on the exception type
                return e, sleep
        return data, sleep

    def test_429_is_retried_once_after_retry_after_seconds(self):
        ok = b'{"data": {"positions": []}}'
        data, sleep = self.run_json([self.too_many("3"), ok])
        self.assertEqual(data, {"data": {"positions": []}})
        sleep.assert_called_once_with(3)

    def test_without_retry_after_wait_10_seconds_and_cap_at_20(self):
        ok = b'{"data": {"positions": []}}'
        _, sleep = self.run_json([self.too_many(), ok])
        sleep.assert_called_once_with(10)
        _, sleep = self.run_json([self.too_many("99"), ok])
        sleep.assert_called_once_with(20)

    def test_second_429_is_raised(self):
        result, sleep = self.run_json([self.too_many("1"), self.too_many("1")])
        self.assertIsInstance(result, urllib.error.HTTPError)
        self.assertEqual(sleep.call_count, 1)

class RoundTwoTest(SiteTest):
    """N2: a later page's total (0 or missing) never ends the read; N5: a place is read from its own card however long."""

    def a1_spec(self, feed_url):
        return {"url": feed_url, "items": "items", "title": "title", "link": "url", "location": "location",
                "total": "totalItems", "page_param": "page", "page_start": 1, "page_step": 1}

    def test_later_page_reporting_total_zero_does_not_end_the_read(self):
        """N2: total is 54 on page 1 and 0 on page 2: the read goes on and, within the 6-request limit, is capped."""
        feed_url = "https://jobs.example.com/list?country=Austria"
        make = synthetic_pages(9, 6)
        for p in range(1, 10):
            data = make(p)
            if p > 1:
                data["totalItems"] = 0
            self.json_pages[sj._with_param(feed_url, "page", p)] = (lambda d: lambda: d)(data)
        feed = sj.site(company(feed_url="https://jobs.example.com/", json_feed=self.a1_spec(feed_url)))
        self.assertEqual(len(feed), 36)
        self.assertTrue(feed.capped)
        self.assertEqual(feed.requests, 6)

    def test_total_only_on_page_one_still_reads_the_whole_list(self):
        """N2 with the QA mock: the total comes on page 1 only; all 54 jobs within 10 requests are read, not capped."""
        feed_url = "https://jobs.example.com/list?country=Austria"
        make = synthetic_pages(9, 6)
        for p in range(1, 10):
            data = make(p)
            if p > 1:
                data.pop("totalItems")
            self.json_pages[sj._with_param(feed_url, "page", p)] = (lambda d: lambda: d)(data)
        feed = sj.site(company(feed_url="https://jobs.example.com/", json_feed=self.a1_spec(feed_url), max_requests=10))
        self.assertEqual(len(feed), 54)
        self.assertFalse(feed.capped)

    def card_html(self, title, place, description_chars, slug):
        head = f"{place} | MY JOB | " if place else ""
        desc = ("Beschreibung der Stelle, ein langer Text zu den Aufgaben. " * (description_chars // 55 + 1))[:description_chars]
        return (f'<li><span class="x">{title} | {head}{desc}</span>'
                f'<a href="https://www.oberbank.at/jobs/{slug}" class="dt-a-arrow" '
                f'title="Jobdetails zu {title}">Jobdetails anzeigen</a></li>')

    def test_place_comes_from_a_six_thousand_character_card(self):
        """N5: the place sits at the start of a 6000-character card, before its link; it is still read."""
        doc = "<ul>" + self.card_html("Junior Analyst (w/m/d)", "Linz", 6000, "junior-analyst-100") + "</ul>"
        row = production("oberbank")
        jobs, bad = sj._from_links(doc, row, "https://www.oberbank.at/jobs")
        self.assertEqual([(j["title"], j["location"]) for j in jobs], [("Junior Analyst (w/m/d)", "Linz")])
        self.assertEqual(bad, 0)

    def test_card_without_its_own_place_is_counted_as_bad_not_kept(self):
        """N5: Oberbank's board is not Austria-only (country pages for Germany, Czechia, Slovakia, Hungary): a card
        without a place is not kept with a made-up place; it is counted as bad."""
        doc = ("<ul>" + self.card_html("Junior Analyst (w/m/d)", "Linz", 300, "junior-analyst-100")
               + '<li><span class="x">Product Owner (w/m/d) | Beschreibung der Stelle</span>'
                 '<a href="https://www.oberbank.at/jobs/product-owner-200" class="dt-a-arrow" '
                 'title="Jobdetails zu Product Owner (w/m/d)">Jobdetails anzeigen</a></li></ul>')
        jobs, bad = sj._from_links(doc, production("oberbank"), "https://www.oberbank.at/jobs")
        self.assertEqual([j["title"] for j in jobs], ["Junior Analyst (w/m/d)"])
        self.assertEqual(bad, 1)


class UniCreditPagesTest(SiteTest):
    """N4 / round 2: UniCredit's Avature careers search lists 25 UCBA jobs, 15 per page; the page states 'N - M of T
    results'. The read is complete only when all T links were read; an empty page before T is capped."""
    PAGE1 = "https://careers.unicredit.eu/en_GB/jobsuche/SearchJobs/?search=UCBA"
    PAGE2 = "https://careers.unicredit.eu/en_GB/jobsuche/SearchJobs/UCBA?jobRecordsPerPage=15&jobOffset=15"
    PAGE3 = "https://careers.unicredit.eu/en_GB/jobsuche/SearchJobs/UCBA?jobRecordsPerPage=15&jobOffset=30"

    def setUp(self):
        super().setUp()
        self.pages[self.PAGE1] = fixture("unicredit_list_p1.html")
        self.pages[self.PAGE2] = fixture("unicredit_list_p2.html")
        self.pages[self.PAGE3] = b"<html><body><p>no results</p></body></html>"  # reached only when the count says 26

    def test_both_pages_read_25_of_25_and_not_capped(self):
        feed = sj.site(production("unicredit-bank-austria"))
        self.assertEqual(len(feed), 25)
        self.assertEqual(len({j["url"] for j in feed}), 25)
        self.assertFalse(feed.capped)
        self.assertEqual(feed.requests, 2)
        self.assertTrue(all(j["location"].startswith("Austria") for j in feed))
        self.assertTrue(all(j["title"] and j["title"] != "View More" for j in feed))
        self.assertEqual(feed[0]["location"], "Austria , Vienna")

    @staticmethod
    def count_in_html(raw, total):
        """The page's 'N - M of T results' counter, whose cells are split by tags in the raw HTML (T replaced; None removes it)."""
        import re as regex
        pattern = rb"(of(?:\s|<[^>]*>)*)25((?:\s|<[^>]*>)*results)"
        if total is None:
            return regex.sub(rb"of(?:\s|<[^>]*>)*25(?:\s|<[^>]*>)*results", b"", raw)
        return regex.sub(pattern, lambda m: m.group(1) + str(total).encode() + m.group(2), raw)

    def test_count_stated_as_26_is_not_complete(self):
        self.pages[self.PAGE1] = self.count_in_html(fixture("unicredit_list_p1.html"), 26)
        feed = sj.site(production("unicredit-bank-austria"))
        self.assertEqual(len(feed), 25)
        self.assertTrue(feed.capped)

    def test_empty_second_page_is_capped_not_complete(self):
        self.pages[self.PAGE2] = b"<html><body><p>no results</p></body></html>"
        feed = sj.site(production("unicredit-bank-austria"))
        self.assertEqual(len(feed), 15)
        self.assertTrue(feed.capped)

    def test_no_count_on_the_page_reads_the_first_page_only_and_is_capped(self):
        self.pages[self.PAGE1] = self.count_in_html(fixture("unicredit_list_p1.html"), None)
        feed = sj.site(production("unicredit-bank-austria"))
        self.assertEqual(len(feed), 15)
        self.assertTrue(feed.capped)
        self.assertEqual(feed.requests, 1)

    def test_request_limit_stops_paging_capped(self):
        feed = sj.site(dict(production("unicredit-bank-austria"), max_requests=1))
        self.assertEqual(len(feed), 15)
        self.assertTrue(feed.capped)


class UrlRewriteTest(SiteTest):
    """The UniCredit portal moved its job links from /jobsuche/JobDetail/ to /en_GB/jobsuche/JobDetail/. Rows stored
    before the move carry the old form: url_rewrite keeps the links the same, so the rows are matched, not re-sent."""
    OLD = "https://careers.unicredit.eu/jobsuche/JobDetail/AT-INT-EXT-UCBA-Professional-Firmenkundenbetreuer-Senior-Medium-Linz-2026/64392"
    NEW = "https://careers.unicredit.eu/en_GB/jobsuche/JobDetail/AT-INT-EXT-UCBA-Professional-Firmenkundenbetreuer-Senior-Medium-Linz-2026/64392"

    def test_rewrite_gives_the_stored_form_and_leaves_other_companies_alone(self):
        self.assertEqual(sj._url(production("unicredit-bank-austria"), self.NEW), self.OLD)
        self.assertEqual(sj._url(production("oberbank"), "https://www.oberbank.at/jobs/x-1"), "https://www.oberbank.at/jobs/x-1")

    def test_read_produces_the_stored_urls(self):
        self.pages["https://careers.unicredit.eu/en_GB/jobsuche/SearchJobs/?search=UCBA"] = fixture("unicredit_list_p1.html")
        self.pages["https://careers.unicredit.eu/en_GB/jobsuche/SearchJobs/UCBA?jobRecordsPerPage=15&jobOffset=15"] = fixture("unicredit_list_p2.html")
        self.pages["https://careers.unicredit.eu/en_GB/jobsuche/SearchJobs/UCBA?jobRecordsPerPage=15&jobOffset=30"] = b"<html></html>"
        feed = sj.site(production("unicredit-bank-austria"))
        urls = {j["url"] for j in feed}
        self.assertEqual(len(urls), 25)
        self.assertIn(self.OLD, urls)
        self.assertTrue(all(u.startswith("https://careers.unicredit.eu/jobsuche/JobDetail/") for u in urls))


if __name__ == "__main__":
    unittest.main()
