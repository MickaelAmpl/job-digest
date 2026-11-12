"""Task B1 adapters (ats_api.py) parsed from trimmed real responses in tests/fixtures/ats_api/. No network:
get() and the Workday POST helper are replaced by the fixtures or by canned replies."""
import json
import sys
import unittest
import urllib.error
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ats_api  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "ats_api"


def fixture(name):
    return (FIX / name).read_bytes()


def company(ats, feed_url, **extra):
    return {"id": "test-co", "name": "Test Co", "niche": "test", "top": False, "ats": ats,
            "feed_url": feed_url, **extra}


class Join(unittest.TestCase):
    def test_reads_jobs_from_the_page_state(self):
        c = company("join", "https://join.com/companies/21bitcoin")
        with mock.patch.object(ats_api, "get", return_value=fixture("join_21bitcoin.html")):
            feed = ats_api.join(c)
        self.assertFalse(feed.capped)
        self.assertEqual(len(feed), 4)
        first = feed[0]
        self.assertEqual(first["title"], "AFC Operations Specialist")
        self.assertEqual(first["url"], "https://join.com/companies/21bitcoin/16756353-afc-operations-specialist")
        self.assertEqual(first["posted"], "2023-11-06")
        self.assertEqual(first["location"], "Vienna, Austria, AT")
        self.assertEqual([j["location"] for j in feed][2], "Salzburg, Austria, AT")

    def test_empty_board_is_an_uncapped_empty_feed(self):
        c = company("join", "https://join.com/companies/brickwise")
        with mock.patch.object(ats_api, "get", return_value=fixture("join_brickwise_empty.html")):
            feed = ats_api.join(c)
        self.assertEqual(len(feed), 0)
        self.assertFalse(feed.capped)

    def test_more_jobs_than_the_page_state_holds_is_capped(self):
        c = company("join", "https://join.com/companies/21bitcoin")
        html = fixture("join_21bitcoin.html").decode("utf-8").replace('"total": 4', '"total": 9')
        with mock.patch.object(ats_api, "get", return_value=html.encode("utf-8")):
            feed = ats_api.join(c)
        self.assertTrue(feed.capped)
        self.assertEqual(len(feed), 4)

    def test_new_page_shape_is_read(self):
        # JOIN moved the job list to pageProps.pageData (21bitcoin, from 30.10); same jobs, new idParam
        c = company("join", "https://join.com/companies/21bitcoin")
        with mock.patch.object(ats_api, "get", return_value=fixture("join_21bitcoin_pagedata.html")):
            feed = ats_api.join(c)
        self.assertFalse(feed.capped)
        self.assertEqual(len(feed), 4)
        self.assertEqual(feed[0]["url"], "https://join.com/companies/21bitcoin/16867206-afc-operations-specialist")
        self.assertEqual(feed[0]["location"], "Vienna, Austria, AT")

    def test_page_that_counts_a_job_but_lists_none_is_capped(self):
        # enliteai (from 31.10): the page says 1 open position and lists none, so one job cannot be read
        c = company("join", "https://join.com/companies/enlite")
        with mock.patch.object(ats_api, "get", return_value=fixture("join_enlite_pagedata.html")):
            feed = ats_api.join(c)
        self.assertEqual(len(feed), 0)
        self.assertTrue(feed.capped)

    def test_page_with_neither_state_shape_raises(self):
        html = b'<html><script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"x":1}}}</script></html>'
        with mock.patch.object(ats_api, "get", return_value=html):
            with self.assertRaises(ValueError):
                ats_api.join(company("join", "https://join.com/companies/x"))

    def test_missing_state_raises(self):
        with mock.patch.object(ats_api, "get", return_value=b"<html>changed layout</html>"):
            with self.assertRaises(ValueError):
                ats_api.join(company("join", "https://join.com/companies/x"))


def make_postings(n):
    """n Workday postings with the fields the adapter reads (titles and links are unique)."""
    return [{"title": f"Job {i}", "externalPath": f"/job/Wien/Job-{i}_R{i}", "locationsText": "Wien",
             "postedOn": "Posted Today"} for i in range(n)]


def place_facets(places):
    """A board's location facet (no Austrian value) with the given (descriptor, count) pairs."""
    return [{"facetParameter": "locationMainGroup", "values": [
        {"facetParameter": "locations", "descriptor": "Locations", "values": [
            {"descriptor": d, "id": f"id-{i}", "count": n} for i, (d, n) in enumerate(places)]}]}]


class FakeBoard:
    """A Workday board as the cxs API answers it: the total is sent on offset 0 only (later pages send 0),
    a page holds at most 20 postings (larger limits are refused with HTTP 400), offsets past serve_until
    come back empty, and send_total=False leaves the total out."""

    def __init__(self, postings, serve_until=None, send_total=True, facets=None, total=None):
        self.postings, self.send_total = postings, send_total
        self.total = len(postings) if total is None else total
        self.serve_until = len(postings) if serve_until is None else serve_until
        self.facets = facets if facets is not None else [{"facetParameter": "locationMainGroup", "values": [
            {"facetParameter": "locationCountry", "descriptor": "Country", "values": [
                {"descriptor": "Austria", "id": "AT1", "count": len(postings)}]}]}]
        self.timeouts = []

    def post(self, url, body, timeout=40):
        self.timeouts.append(timeout)
        if body["limit"] == 1 and not body.get("appliedFacets"):
            return {"total": self.total, "facets": self.facets, "jobPostings": []}
        if body["limit"] > 20:
            raise urllib.error.HTTPError(url, 400, "Bad Request", {}, None)
        off = body["offset"]
        end = min(off + body["limit"], self.serve_until, len(self.postings))
        reply = {"jobPostings": self.postings[off:end] if off < self.serve_until else []}
        if self.send_total:  # real Workday: the total on offset 0, 0 on the later pages
            reply["total"] = self.total if off == 0 else 0
        return reply


class Workday(unittest.TestCase):
    API = "https://santander.wd3.myworkdayjobs.com/SantanderCareers"

    def run_board(self, board):
        with mock.patch.object(ats_api, "_post_json", side_effect=board.post), mock.patch("ats_api.time.sleep"):
            return ats_api.workday(company("workday", self.API))

    def test_board_of_120_is_read_in_full(self):
        # QA B1: the total arrives on offset 0 only; the old code stopped after page 2 with 40 jobs, uncapped
        feed = self.run_board(FakeBoard(make_postings(120)))
        self.assertEqual(len(feed), 120)
        self.assertFalse(feed.capped)

    def test_board_of_43_like_accenture_is_read_in_full(self):
        feed = self.run_board(FakeBoard(make_postings(43)))
        self.assertEqual(len(feed), 43)
        self.assertFalse(feed.capped)

    def test_board_over_the_page_limit_is_capped(self):
        feed = self.run_board(FakeBoard(make_postings(250)))
        self.assertEqual(len(feed), 200)
        self.assertTrue(feed.capped)

    def test_empty_page_before_the_total_is_capped(self):
        # 54 postings promised, the board stops answering at 40: never complete
        feed = self.run_board(FakeBoard(make_postings(54), serve_until=40))
        self.assertEqual(len(feed), 40)
        self.assertTrue(feed.capped)

    def test_missing_total_is_capped(self):
        feed = self.run_board(FakeBoard(make_postings(7), send_total=False))
        self.assertEqual(len(feed), 7)
        self.assertTrue(feed.capped)

    def test_deadline_stops_and_caps_with_a_shrinking_timeout(self):
        clock = [0.0]
        board = FakeBoard(make_postings(120))

        def slow_post(url, body, timeout=40):
            clock[0] += 25.0  # every request takes 25 s
            return board.post(url, body, timeout=timeout)
        with mock.patch.object(ats_api, "_post_json", side_effect=slow_post), \
                mock.patch("ats_api.time.sleep"), mock.patch("ats_api.time.monotonic", side_effect=lambda: clock[0]):
            feed = ats_api.workday(company("workday", self.API))
        self.assertTrue(feed.capped)
        self.assertLess(len(feed), 120)
        self.assertTrue(all(t <= 40 for t in board.timeouts))
        self.assertLess(board.timeouts[-1], 40)  # the last request got only the time that was left

    def test_redirect_is_refused_not_followed(self):
        self.assertIsNone(ats_api._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://x.test/"))
        err = urllib.error.HTTPError("https://x.test/", 302, "Found", {}, None)
        with mock.patch.object(ats_api._OPENER, "open", side_effect=err):
            with self.assertRaises(urllib.error.HTTPError):
                ats_api._post_json("https://x.test/wday/cxs/x/y/jobs", {"limit": 1})

    def test_postings_read_from_the_santander_fixture(self):
        head = json.loads(fixture("workday_santander_facets.json"))
        page = json.loads(fixture("workday_santander_austria_page.json"))
        # the fixture holds 4 of the board's 13 Austrian postings; the board is silent after them
        feed = self.run_board(FakeBoard(page["jobPostings"], serve_until=4, total=13, facets=head["facets"]))
        self.assertEqual(len(feed), 4)
        self.assertTrue(feed.capped)
        self.assertEqual(feed[0]["location"], "Wien")
        self.assertEqual(feed[0]["url"], "https://santander.wd3.myworkdayjobs.com/SantanderCareers/job/Wien/"
                                         "Initiativbewerbung_Req0896311")
        self.assertEqual(feed[1]["location"], "Eisenstadt")
        self.assertEqual(feed[2]["posted"], (date.today() - timedelta(days=6)).isoformat())
        self.assertEqual(feed[0]["posted"], "")  # 'Posted 30+ Days Ago' is not a date

    def test_empty_filtered_first_page_is_a_complete_empty_board(self):
        # the Austria facet is there and the first page says total 0 with no postings: complete, nothing open
        board = FakeBoard([], facets=[{"facetParameter": "locationMainGroup", "values": [
            {"facetParameter": "locationCountry", "descriptor": "Country", "values": [
                {"descriptor": "Austria", "id": "AT1", "count": 0}]}]}])
        feed = self.run_board(board)
        self.assertEqual(len(feed), 0)
        self.assertFalse(feed.capped)

    def test_board_without_an_austria_facet_but_with_postings_stays_capped(self):
        # Tricentis-like (4.11): 21 postings worldwide, none in Austria, no 'AT - Vienna' facet. The place counts
        # cover the whole board, but a missing Austria facet is not proof of an empty Austrian board: capped.
        board = FakeBoard(make_postings(21), facets=place_facets([("UK - London", 3), ("SG - Singapore", 3),
                                                                  ("CZ - Prague", 3), ("Austin, Texas", 2),
                                                                  ("PH - Manila", 2), ("US - Atlanta", 2),
                                                                  ("US - Austin", 2), ("PH - Remote", 3),
                                                                  ("IN - Pune", 4), ("IE - Cork", 3)]))
        feed = self.run_board(board)
        self.assertEqual(len(feed), 0)
        self.assertTrue(feed.capped)
        self.assertEqual(len(board.timeouts), 1)  # only the facet call

    def test_empty_board_with_no_facets_is_complete(self):
        feed = self.run_board(FakeBoard([], facets=[]))
        self.assertEqual(len(feed), 0)
        self.assertFalse(feed.capped)

    def test_austria_facet_is_taken_from_the_board(self):
        head = json.loads(fixture("workday_santander_facets.json"))
        self.assertEqual(ats_api._wd_austria_facet(head["facets"]),
                         {"locationCountry": ["d004c0d1a6c84511ab048669fcdf9fd7"]})

    def test_board_without_an_austrian_facet_reads_nothing_and_stays_capped(self):
        board = FakeBoard(make_postings(5), facets=[{"facetParameter": "locationMainGroup", "values": [
            {"facetParameter": "locationCountry", "descriptor": "Country", "values": [
                {"descriptor": "Germany", "id": "aa", "count": 5}]}]}])
        feed = self.run_board(board)
        self.assertEqual(len(feed), 0)
        self.assertTrue(feed.capped)
        self.assertEqual(len(board.timeouts), 1)  # only the facet call

    def test_place_facet_is_used_when_there_is_no_country_facet(self):
        facets = [{"facetParameter": "locationMainGroup", "values": [
            {"facetParameter": "locations", "descriptor": "Locations", "values": [
                {"descriptor": "Amstetten", "id": "a1", "count": 4},
                {"descriptor": "Austria Remote Work", "id": "a2", "count": 3},
                {"descriptor": "Cologne", "id": "d1", "count": 2}]}]}]
        self.assertEqual(ats_api._wd_austria_facet(facets), {"locations": ["a1", "a2"]})

    def test_location_rules(self):
        self.assertEqual(ats_api._wd_location("8 Locations"), "Austria")
        self.assertEqual(ats_api._wd_location("AT - Vienna"), "AT - Vienna")
        self.assertEqual(ats_api._wd_location("Amstetten"), "Amstetten, Austria")
        self.assertEqual(ats_api._wd_location("Wien"), "Wien")

    def test_posted_relative_dates(self):
        today = date.today()
        self.assertEqual(ats_api._wd_posted("Posted Today"), today.isoformat())
        self.assertEqual(ats_api._wd_posted("Posted Yesterday"), (today - timedelta(days=1)).isoformat())
        self.assertEqual(ats_api._wd_posted("Posted 3 Days Ago"), (today - timedelta(days=3)).isoformat())
        self.assertEqual(ats_api._wd_posted("Posted 30+ Days Ago"), "")


class ERecruiter(unittest.TestCase):
    def test_jobs_list_model_is_read(self):
        c = company("erecruiter", "https://karriere.hdi.at/Jobs")
        with mock.patch.object(ats_api, "get", return_value=fixture("erecruiter_hdi_model.html")):
            feed = ats_api.erecruiter(c)
        self.assertEqual(len(feed), 3)
        self.assertTrue(feed.capped)  # the model says 11 jobs, the fixture holds 3
        self.assertEqual(feed[0]["title"], "IT Tester:in")
        self.assertEqual(feed[0]["url"], "https://karriere.hdi.at/Job/1340")
        self.assertEqual(feed[0]["location"], "Wien")
        self.assertEqual(feed[0]["posted"], "2026-10-22")  # 10/22/2026 (US order in this model)

    def test_model_with_every_job_is_uncapped(self):
        html = fixture("erecruiter_hdi_model.html").decode("utf-8").replace('"TotalJobsCount": 11',
                                                                            '"TotalJobsCount": 3')
        with mock.patch.object(ats_api, "get", return_value=html.encode("utf-8")):
            feed = ats_api.erecruiter(company("erecruiter", "https://karriere.hdi.at/Jobs"))
        self.assertFalse(feed.capped)

    def test_location_missing_takes_default_location(self):
        html = fixture("erecruiter_hdi_model.html").decode("utf-8").replace('"Location": "Wien"', '"Location": ""', 1)
        html = html.replace('"TotalJobsCount": 11', '"TotalJobsCount": 3')
        with mock.patch.object(ats_api, "get", return_value=html.encode("utf-8")):
            feed = ats_api.erecruiter(company("erecruiter", "https://karriere.hdi.at/Jobs", default_location="Wien"))
        self.assertEqual(feed[0]["location"], "Wien")

    def test_static_table_rows_are_read(self):
        c = company("erecruiter", "https://stellenangebote.raiffeisenbank.jobs/Jobs")
        with mock.patch.object(ats_api, "get", return_value=fixture("erecruiter_raiffeisen_table.html")):
            feed = ats_api.erecruiter(c)
        self.assertEqual(len(feed), 3)
        self.assertTrue(feed.capped)  # the page says 15 Ergebnisse
        self.assertEqual(feed[0]["title"], "Expert:in Zins-, Markt- und Liquiditätsrisikomanagement")
        self.assertEqual(feed[0]["url"], "https://stellenangebote.raiffeisenbank.jobs/de/Job/107319/"
                                         "Expertin-Zins--Markt--und-Liquiditaetsrisikomanagement")
        self.assertEqual(feed[0]["location"], "1020 Wien")
        self.assertEqual(feed[0]["posted"], "2026-10-23")

    def test_table_count_equal_to_rows_is_uncapped(self):
        html = fixture("erecruiter_raiffeisen_table.html").decode("utf-8").replace("15 Ergebnisse", "3 Ergebnisse")
        with mock.patch.object(ats_api, "get", return_value=html.encode("utf-8")):
            feed = ats_api.erecruiter(company("erecruiter", "https://stellenangebote.raiffeisenbank.jobs/Jobs"))
        self.assertFalse(feed.capped)

    def test_page_without_list_raises(self):
        with mock.patch.object(ats_api, "get", return_value=b"<html>maintenance</html>"):
            with self.assertRaises(ValueError):
                ats_api.erecruiter(company("erecruiter", "https://karriere.hdi.at/Jobs"))


class BambooHR(unittest.TestCase):
    def test_list_is_read_and_checked_against_total(self):
        c = company("bamboohr", "https://woom.bamboohr.com/careers/list")
        with mock.patch.object(ats_api, "get", return_value=fixture("bamboohr_list.json")):
            feed = ats_api.bamboohr(c)
        self.assertEqual(len(feed), 6)  # meta.totalCount is 6
        self.assertFalse(feed.capped)
        self.assertEqual(feed[1]["title"], "Email &Lifecycle Management Specialist")
        self.assertEqual(feed[1]["location"], "Austin, Texas")
        self.assertEqual(feed[0]["location"], "Vienna")
        self.assertEqual(feed[0]["url"], "https://woom.bamboohr.com/careers/104")


class GoHire(unittest.TestCase):
    def test_cards_are_read_and_capped_against_the_page_count(self):
        c = company("gohire", "https://jobs.gohire.io/cryptonow-3coot9wf/")
        with mock.patch.object(ats_api, "get", return_value=fixture("gohire_cards.html")):
            feed = ats_api.gohire(c)
        self.assertEqual(len(feed), 3)
        self.assertTrue(feed.capped)  # the page says 20 open jobs
        self.assertEqual(feed[0]["title"], "Marketing Manager")
        self.assertEqual(feed[0]["location"], "Hybrid Remote, Vienna, Austria")
        self.assertEqual(feed[0]["url"], "https://jobs.gohire.io/cryptonow-3coot9wf/marketing-manager-304971/")
        self.assertEqual(feed[0]["posted"], "2026-10-22")

    def test_no_cards_raises(self):
        with mock.patch.object(ats_api, "get", return_value=b"<html>no cards</html>"):
            with self.assertRaises(ValueError):
                ats_api.gohire(company("gohire", "https://jobs.gohire.io/x/"))


class Kula(unittest.TestCase):
    def test_cards_are_read_and_always_capped(self):
        c = company("kula", "https://careers.kula.ai/journi")
        with mock.patch.object(ats_api, "get", return_value=fixture("kula_journi.html")):
            feed = ats_api.kula(c)
        self.assertEqual(len(feed), 2)
        self.assertTrue(feed.capped)
        self.assertEqual(feed[0]["title"], "Initiative Application (all genders)")
        self.assertEqual(feed[0]["location"], "Vienna, Austria")
        self.assertEqual(feed[0]["url"], "https://careers.kula.ai/journi/19650-initiative-application-all-genders")

    def test_no_cards_raises(self):
        with mock.patch.object(ats_api, "get", return_value=b"<html></html>"):
            with self.assertRaises(ValueError):
                ats_api.kula(company("kula", "https://careers.kula.ai/journi"))


class Factorial(unittest.TestCase):
    def test_rows_are_read_with_the_city_key_as_location(self):
        c = company("factorial", "https://finmatics.factorial.com/")
        with mock.patch.object(ats_api, "get", return_value=fixture("factorial_rows.html")):
            feed = ats_api.factorial(c)
        self.assertEqual(len(feed), 2)
        self.assertTrue(feed.capped)  # the page states no total
        self.assertEqual(feed[0]["title"], "Senior Sales Development Representative (m/w/d)")
        self.assertEqual(feed[0]["location"], "Wien")
        self.assertEqual(feed[0]["url"], "https://finmatics.factorial.com/job_posting/"
                                         "senior-sales-development-representative-m-w-d-316906")

    def test_no_rows_raises(self):
        with mock.patch.object(ats_api, "get", return_value=b"<html></html>"):
            with self.assertRaises(ValueError):
                ats_api.factorial(company("factorial", "https://finmatics.factorial.com/"))


class PersonioJson(unittest.TestCase):
    """Personio boards: /search.json lists the positions, the board page links the same /job/<id> pages."""

    def fake(self, search, board):
        def get_(url, accept="application/json", timeout=40):
            return search if url.endswith("/search.json") else board
        return get_

    def test_positions_are_read_and_match_the_board(self):
        search, board = fixture("personio_amina_search.json"), fixture("personio_amina_board.html")
        c = company("personio_json", "https://amina.jobs.personio.com/")
        with mock.patch.object(ats_api, "_get", side_effect=self.fake(search, board)), mock.patch("ats_api.time.sleep"):
            feed = ats_api.personio_json(c)
        self.assertEqual(len(feed), 19)
        self.assertFalse(feed.capped)
        self.assertEqual(feed[0]["title"], "Business Development Manager")
        self.assertEqual(feed[0]["location"], "Abu Dhabi")
        self.assertEqual(feed[0]["url"], "https://amina.jobs.personio.com/job/2424698")
        austria = [j for j in feed if "Austria" in j["location"]]
        self.assertEqual([j["title"] for j in austria], ["Payments Strategy & Business Development"])

    def test_office_prefix_is_removed(self):
        search, board = fixture("personio_own3d_search.json"), fixture("personio_own3d_board.html")
        c = company("personio_json", "https://own3d.jobs.personio.de/")
        with mock.patch.object(ats_api, "_get", side_effect=self.fake(search, board)), mock.patch("ats_api.time.sleep"):
            feed = ats_api.personio_json(c)
        self.assertEqual(len(feed), 5)
        self.assertFalse(feed.capped)
        self.assertIn("AT - Steyr", feed[1]["location"])
        self.assertNotIn("Office:", feed[1]["location"])

    def test_board_showing_an_extra_job_is_capped(self):
        search, board = fixture("personio_own3d_search.json"), fixture("personio_own3d_board.html")
        board = board + b'\n<a href="/job/999999">extra</a>'
        c = company("personio_json", "https://own3d.jobs.personio.de/")
        with mock.patch.object(ats_api, "_get", side_effect=self.fake(search, board)), mock.patch("ats_api.time.sleep"):
            feed = ats_api.personio_json(c)
        self.assertTrue(feed.capped)
        self.assertEqual(len(feed), 5)

    def test_position_missing_from_the_board_is_capped(self):
        search = json.loads(fixture("personio_own3d_search.json"))
        search.append(dict(search[0], id=111111, name="Hidden position"))
        c = company("personio_json", "https://own3d.jobs.personio.de/")
        with mock.patch.object(ats_api, "_get", side_effect=self.fake(json.dumps(search).encode(),
                                                                     fixture("personio_own3d_board.html"))), \
                mock.patch("ats_api.time.sleep"):
            feed = ats_api.personio_json(c)
        self.assertTrue(feed.capped)
        self.assertEqual(len(feed), 6)

    def test_slow_search_skips_the_board_page_and_caps(self):
        clock, calls = [0.0], []
        search = fixture("personio_own3d_search.json")

        def slow(url, accept="application/json", timeout=40):
            calls.append(url)
            clock[0] += 59.0  # the search reply alone uses almost the whole company deadline
            return search
        c = company("personio_json", "https://own3d.jobs.personio.de/")
        with mock.patch.object(ats_api, "_get", side_effect=slow), mock.patch("ats_api.time.sleep"), \
                mock.patch("ats_api.time.monotonic", side_effect=lambda: clock[0]):
            feed = ats_api.personio_json(c)
        self.assertEqual(len(feed), 5)
        self.assertTrue(feed.capped)
        self.assertEqual(len(calls), 1)

    def test_reply_that_is_not_a_list_raises(self):
        c = company("personio_json", "https://amina.jobs.personio.com/")
        with mock.patch.object(ats_api, "_get", side_effect=self.fake(b'{"error": "gone"}', b"<html></html>")), \
                mock.patch("ats_api.time.sleep"):
            with self.assertRaises(ValueError):
                ats_api.personio_json(c)


class Registry(unittest.TestCase):
    def test_adapter_names_match_companies_json_ats_values(self):
        self.assertEqual(set(ats_api.ADAPTERS), {"join", "workday", "erecruiter", "bamboohr", "gohire",
                                                 "kula", "factorial", "personio_json", "mokahr", "jibe"})
        self.assertTrue(all(callable(f) for f in ats_api.ADAPTERS.values()))


if __name__ == "__main__":
    unittest.main()
