"""Stage 7 wave 2 part 2 glue (26.10.2026): the new adapter modules load into fetch_companies, a module that
fails to load is reported instead of stopping the run, Austrian district towns count as Austria, and open
applications listed on company boards never pass the filter.

QA fixes 26.10.2026 (docs/qa-stage7-wave2b-3.md, Dev D): a global fetch deadline that records the companies it
did not reach as failed, a redirect guard for company feeds, foreign countries that make a location foreign,
and the bad-records note threshold. No network: the redirect tests use a local server or a fake resolver."""

import contextlib
import http.client
import http.server
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from unittest import mock

import fetch_companies
import fetch_posting
import health
import prefilter
import prepare
import store


class ExtraAdaptersTest(unittest.TestCase):
    def test_all_new_modules_load(self):
        self.assertEqual(fetch_companies.load_extra_adapters(), {})
        for name in ("join", "workday", "erecruiter", "personio_json", "phenom", "onlyfy", "softgarden", "site"):
            self.assertIn(name, fetch_companies.ADAPTERS)

    def test_a_broken_module_is_reported_not_raised(self):
        with mock.patch.object(fetch_companies, "EXTRA_MODULES", ("no_such_adapter_module",)):
            failed = fetch_companies.load_extra_adapters()
        self.assertIn("no_such_adapter_module", failed)
        self.assertIn("ModuleNotFoundError", failed["no_such_adapter_module"])


class AustrianTownsTest(unittest.TestCase):
    def test_district_towns_are_austrian(self):
        for place in ("Saint Pölten", "Sankt Pölten", "Horn", "Bruck an der Mur", "Amstetten", "Gumpoldskirchen",
                      "Voitsberg", "Pinzgau", "Weiz", "St. Johann im Pongau", "Waidhofen an der Thaya",
                      "Voecklabruck", "Vöcklabruck", "Wals-Siezenheim", "Zell am See", "Pasching, Oberösterreich"):
            self.assertTrue(fetch_companies.is_austrian(place), place)

    def test_german_namesakes_and_foreign_places_are_not(self):
        for place in ("Feldkirchen bei München", "Neunkirchen (Saar)", "Schwäbisch Gmünd", "Munich, DE",
                      "Remote - Germany", "Prague"):
            self.assertFalse(fetch_companies.is_austrian(place), place)


class OpenApplicationsTest(unittest.TestCase):
    def test_open_applications_are_dropped(self):
        for title in ("Initiativbewerbung", "INITIATIV Application General", "Initiativ-Bewerbung (m/w/d)",
                      "Open Application - Product", "Spontaneous Application", "Talent Pool Product Management"):
            self.assertEqual(prefilter.verdict({"title": title, "description": ""}), "excluded_field", title)

    def test_initiative_in_a_real_title_still_passes(self):
        job = {"title": "Product Manager - Climate Initiative", "description": ""}
        self.assertEqual(prefilter.verdict(job), "keep")


class FetchDeadlineTest(unittest.TestCase):
    """QA minor 2: the run is killed at 1200 s and writes nothing before its end, so main() stops starting companies
    FETCH_DEADLINE seconds after its start. A company not started is a failure, never an empty answer: it is not in
    totals, so prepare.py neither counts it as answered nor closes its jobs. Fake clock: every feed takes self.step."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root, self.out = Path(tmp.name), Path(tmp.name) / "out"
        companies = [{"id": f"co-{x}", "name": f"Firma {x.upper()}", "niche": "test", "top": False, "ats": "fake",
                      "feed_url": f"https://{x}.example.at/jobs"} for x in "abcd"]
        (self.root / "companies.json").write_text(json.dumps(companies), encoding="utf-8")
        self.clock, self.step = [0.0], 500.0

        def fake_feed(c):
            self.clock[0] += self.step
            return fetch_companies.Feed([fetch_companies.job(c, "Projektmanager", "Wien, AT",
                                                             f"https://{c['id']}.example.at/1")])
        for p in (mock.patch.object(fetch_companies, "ROOT", self.root),
                  mock.patch.object(fetch_companies, "OUT", self.out),
                  mock.patch.object(fetch_companies, "load_extra_adapters", return_value={}),
                  mock.patch.object(fetch_companies, "_clock", lambda: self.clock[0]),
                  mock.patch.dict(fetch_companies.ADAPTERS, {"fake": fake_feed}),
                  mock.patch.object(fetch_companies.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)

    def run_main(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            fetch_companies.main()
        return json.loads((self.out / "company_feeds.json").read_text(encoding="utf-8"))

    def test_deadline_is_900_seconds(self):
        self.assertEqual(fetch_companies.FETCH_DEADLINE, 900)

    def test_companies_not_started_before_the_deadline_are_failed(self):
        status = self.run_main()  # 500 s each: a and b start before 900 s, c and d do not
        self.assertEqual(sorted(status["totals"]), ["co-a", "co-b"])
        self.assertEqual(sorted(status["failed"]), ["co-c", "co-d"])
        self.assertEqual(status["failed"]["co-c"], "Firma C: не успел (лимит времени)")
        self.assertEqual(status["asked"], 4)

    def test_all_companies_run_when_they_fit_in_the_deadline(self):
        self.step = 100.0
        status = self.run_main()
        self.assertEqual(sorted(status["totals"]), ["co-a", "co-b", "co-c", "co-d"])
        self.assertEqual(status["failed"], {})

    def test_prepare_does_not_count_a_skipped_company_as_answered(self):
        self.run_main()
        with mock.patch.object(store, "OUT", self.out):
            answered, _ = prepare.answered_companies([])
        self.assertEqual(answered, {"co-a", "co-b"})


class FeedRedirectGuardTest(unittest.TestCase):
    """QA minor 8: get() follows a redirect only to a public host that is not a job board, checked on every hop with
    fetch_posting's guards (DENY, scheme and host form, public address). The resolver is faked: no network."""

    PUBLIC = ("93.184.216.34",)

    def hop(self, newurl, resolves_to=PUBLIC):
        req = urllib.request.Request("https://feeds.example.at/jobs")
        with mock.patch.object(fetch_posting, "_resolve", lambda host, port: list(resolves_to)):
            return fetch_companies._GuardedRedirect().redirect_request(req, None, 302, "Found", {}, newurl)

    def test_redirect_to_a_job_board_is_refused(self):
        for newurl in ("https://www.linkedin.com/jobs/view/1", "https://de.indeed.com/job/2", "https://karriere.at/x",
                       "https://www.stepstone.de/y"):
            with self.assertRaises(urllib.error.URLError, msg=newurl):
                self.hop(newurl)

    def test_redirect_to_a_private_or_local_address_is_refused(self):
        for newurl in ("http://127.0.0.1:8080/x", "http://10.0.0.5/x", "http://localhost/x", "http://intranet.lan/x",
                       "file:///etc/passwd"):
            with self.assertRaises(urllib.error.URLError, msg=newurl):
                self.hop(newurl)
        with self.assertRaises(urllib.error.URLError):  # a public-looking name that resolves inside the network
            self.hop("https://feeds.example.at/v2", resolves_to=("10.0.0.7",))

    def test_redirect_to_a_public_host_is_followed(self):
        req = self.hop("https://feeds.example.at/v2/jobs")
        self.assertEqual(req.full_url, "https://feeds.example.at/v2/jobs")

    def test_get_refuses_redirects_to_a_job_board_or_a_private_address(self):
        """A local server redirects to a job board and to its own /secret. Every other connection is blocked here, so
        a failing guard shows up as a recorded attempt, never as a real request to the job board."""
        hits, attempts = [], []
        real_connect = http.client.HTTPConnection.connect

        class Redirects(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                target = {"/feed": "https://www.linkedin.com/jobs/", "/private": f"http://127.0.0.1:{PORT}/secret"}
                if self.path in target:
                    self.send_response(302)
                    self.send_header("Location", target[self.path])
                    self.end_headers()
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *args):
                pass

        def connect(conn):
            if conn.host != "127.0.0.1":
                attempts.append(conn.host)
                raise urllib.error.URLError("connection blocked in tests")
            return real_connect(conn)

        server = http.server.HTTPServer(("127.0.0.1", 0), Redirects)
        PORT = server.server_port
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with mock.patch.object(http.client.HTTPConnection, "connect", connect), \
                mock.patch.object(http.client.HTTPSConnection, "connect", connect):
            for path in ("/feed", "/private"):
                with self.assertRaises(urllib.error.URLError, msg=path) as cm:
                    fetch_companies.get(f"http://127.0.0.1:{PORT}{path}")
                self.assertIn("refused", str(cm.exception), path)
        self.assertEqual(attempts, [])  # no connection to the job board
        self.assertEqual(hits, ["/feed", "/private"])  # /secret was never requested


class AustrianCountryRuleTest(unittest.TestCase):
    """QA minor 6: a foreign country named without Austria makes a location foreign, even when a town in it is an
    Austrian one; 'Baden' is only the Lower Austrian town; the new district towns count; 'Rum' only with Tirol."""

    def check(self, location, expected, title=""):
        self.assertIs(fetch_companies.is_austrian(location, title), expected,
                      f"location={location!r} title={title!r}")

    def test_foreign_country_without_austria_is_not_austrian(self):
        for loc in ("Horn, Germany", "Wolfsberg, Germany", "Vienna, Virginia", "Zürich, CH",
                    "Bratislava, Slovakia", "Milano, Italy", "Remote - United Kingdom", "Munich, DE",
                    "Mobiles Arbeiten, Germany"):
            self.check(loc, False)

    def test_austria_named_next_to_a_foreign_country_is_austrian(self):
        for loc in ("Wien, Austria", "Wien, Austria, Germany", "Horn, Niederösterreich", "Wolfsberg, Österreich"):
            self.check(loc, True)

    def test_foreign_country_in_the_title_counts_when_there_is_no_location(self):
        self.check("", False, title="Sales Manager Germany (m/w/d)")
        self.check("", True, title="Sales Manager, Wien")

    def test_new_district_towns_are_austrian(self):
        for loc in ("Brunn am Gebirge", "Premstätten", "Schladming", "Parndorf", "Seiersberg", "Gratkorn",
                    "Mondsee", "Ternitz", "Hennersdorf"):
            self.check(loc, True)

    def test_rum_counts_only_with_its_qualifier(self):
        self.check("Rum bei Innsbruck", True)
        self.check("Rum, Tirol", True)
        self.check("Rum", False)

    def test_baden_is_the_lower_austrian_town_only(self):
        self.check("Baden", True)
        self.check("Baden bei Wien", True)
        self.check("Baden, Wien", True)
        for loc in ("Baden-Baden", "Baden - Baden", "Baden Württemberg", "Baden, Germany", "Baden, Switzerland",
                    "Baden (AG)", "Baden, DE"):
            self.check(loc, False)

    def test_capital_de_and_it_in_titles_are_not_country_codes(self):
        self.check("Chef de Projet, Wien", True)
        self.check("Wien | IT Consultant", True)


class BadRecordsNoteTest(unittest.TestCase):
    """QA minor 7: the 'битые записи' note names a company only with at least 2 bad records and at least 10 % of
    its feed (good plus bad records); a stray record stays silent."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)
        for p in (mock.patch.object(store, "OUT", self.out),
                  mock.patch.object(health, "load", lambda: {"day": date.today().isoformat(), "finished": True,
                                                             "collect": {}})):
            p.start()
            self.addCleanup(p.stop)

    def notes_for(self, totals, bad):
        feeds = {"at": store.now(), "totals": totals, "capped": [], "failed": {}, "bad_records": bad, "asked": 9}
        (self.out / "company_feeds.json").write_text(json.dumps(feeds), encoding="utf-8")
        _, notes = health._today_problems()
        return [n for n in notes if "битые записи" in n]

    def test_stray_and_thin_bad_records_stay_silent(self):
        notes = self.notes_for({"co-stray": 40, "co-one": 0, "co-thin": 30},
                               {"co-stray": 1, "co-one": 1, "co-thin": 2})  # 2 of 32 is 6 %
        self.assertEqual(notes, [])

    def test_two_or_more_bad_records_at_ten_percent_are_named(self):
        notes = self.notes_for({"co-stray": 40, "co-tenth": 27, "co-half": 2, "co-bigbad": 100, "co-thin": 30},
                               {"co-stray": 1, "co-tenth": 3, "co-half": 2, "co-bigbad": 12, "co-thin": 2})
        self.assertEqual(len(notes), 1)
        self.assertIn("co-bigbad 12, co-half 2, co-tenth 3", notes[0])
        self.assertNotIn("co-stray", notes[0])
        self.assertNotIn("co-thin", notes[0])


class SegmentRuleTest(unittest.TestCase):
    """QA N1 (re-check 26.10): ';', '/', '|', 'oder' and 'or' split a location into segments, and one Austrian segment
    is enough. Rule for a segment with a foreign country and no Austria mention: it is Austrian only when it also names
    a big Austrian city (Wien, Vienna, Graz, Linz, Salzburg, Innsbruck) AND Germany (Germany, Deutschland, DE).
    Smaller names shared with German towns (Horn, Wolfsberg) never count. 'VA' (Virginia) is a foreign code."""

    def check(self, location, expected):
        self.assertIs(fetch_companies.is_austrian(location, ""), expected, f"location={location!r}")

    def test_one_austrian_segment_is_enough(self):
        for loc in ("Wien; Berlin, DE", "Wien oder München (Deutschland)", "Graz / Zürich, Schweiz", "Wien | Zürich, CH",
                    "Berlin, DE or Wien", "Remote - Germany / Austria"):
            self.check(loc, True)

    def test_big_austrian_city_with_germany_in_one_segment_counts(self):
        self.check("Vienna, Munich, Germany", True)
        self.check("Linz, Deutschland", True)
        self.check("Horn, Germany", False)
        self.check("Wolfsberg, Germany", False)

    def test_foreign_segments_stay_foreign(self):
        for loc in ("Vienna, Virginia", "Vienna, VA", "Bolzano, South Tyrol, Italy", "Berlin, DE; Zürich, CH",
                    "Munich / Zürich, CH", "Remote or Germany"):
            self.check(loc, False)


class SmartRecruitersDeadlineTest(unittest.TestCase):
    """QA N6 (re-check 26.10): SmartRecruiters paging has the same 60 s per-company deadline as the other readers.
    A deadline that stops paging gives capped=True; each request waits at most min(40, remaining) seconds.
    A timeout that comes at the deadline stops the read; an error before the deadline still raises.
    Fake clock: every request takes per_call seconds, every sleep advances the clock."""

    def read(self, counts, total, per_call, fail=None):
        now, calls = [0.0], []

        def fake_get(url, accept="application/json", max_bytes=0, timeout=40):
            calls.append(timeout)
            now[0] += per_call
            if fail and len(calls) == fail[0]:
                now[0] += fail[1]
                raise TimeoutError("timed out")
            n = counts[len(calls) - 1] if len(calls) <= len(counts) else 100
            content = [{"id": f"{len(calls)}-{k}", "name": f"Job {len(calls)}-{k}",
                        "location": {"city": "Wien", "country": "at"}, "releasedDate": "2026-10-01T00:00:00Z"}
                       for k in range(n)]
            return json.dumps({"totalFound": total, "content": content}).encode()

        def fake_sleep(seconds):
            now[0] += seconds

        c = {"id": "acme", "name": "Acme", "niche": "test", "top": False, "ats": "smartrecruiters",
             "feed_url": "https://api.smartrecruiters.com/v1/companies/acme/postings"}
        with mock.patch.object(fetch_companies, "get", fake_get), \
                mock.patch.object(fetch_companies, "_clock", lambda: now[0]), \
                mock.patch.object(fetch_companies.time, "sleep", fake_sleep):
            feed = fetch_companies.smartrecruiters(c)
        return feed, calls

    def test_a_slow_board_stops_at_the_deadline_and_is_capped(self):
        feed, calls = self.read(counts=[100] * 10, total=1000, per_call=35)
        self.assertEqual(calls, [40, 24])  # second request waits for what is left of the 60 s, not 40
        self.assertEqual(len(feed), 200)
        self.assertTrue(feed.capped)

    def test_a_fast_complete_board_is_not_capped(self):
        feed, calls = self.read(counts=[100, 50], total=150, per_call=1)
        self.assertEqual(calls, [40, 40])
        self.assertEqual(len(feed), 150)
        self.assertFalse(feed.capped)

    def test_a_timeout_at_the_deadline_keeps_what_was_read(self):
        feed, calls = self.read(counts=[100, 100], total=1000, per_call=35, fail=(2, 25))
        self.assertEqual(len(feed), 100)
        self.assertTrue(feed.capped)

    def test_an_error_before_the_deadline_still_raises(self):
        def broken(url, accept="application/json", max_bytes=0, timeout=40):
            raise urllib.error.URLError("no route")
        c = {"id": "acme", "name": "Acme", "niche": "test", "top": False, "ats": "smartrecruiters",
             "feed_url": "https://api.smartrecruiters.com/v1/companies/acme/postings"}
        with mock.patch.object(fetch_companies, "get", broken), mock.patch.object(fetch_companies.time, "sleep"):
            with self.assertRaises(urllib.error.URLError):
                fetch_companies.smartrecruiters(c)


class AvatureCappedTest(unittest.TestCase):
    """QA N4 (re-check 26.10): UniCredit's Avature RSS returns exactly 20 items, and jobRecordsPerPage and jobOffset
    are ignored. It is a slice of the board (20 of 25), so it is capped like the 20-item SuccessFactors RSS and never
    closes jobs. Same rule as successfactors, nothing else changes."""

    def feed(self, n, ats):
        items = "".join(f"<item><title>AT - EXT - UCBA - Retail Network - BestStart - Ort {i} - 2026</title>"
                        f"<link>https://careers.unicredit.eu/jobsuche/JobDetail/x-{i}/{64000 + i}</link>"
                        f"<pubDate>Tue, 06 Oct 2026 00:00:00 +0000</pubDate></item>" for i in range(n))
        xml = f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{items}</channel></rss>'.encode()
        c = {"id": "x", "name": "X", "niche": "banks", "top": True, "ats": ats, "feed_url": "https://example.at/feed"}
        with mock.patch.object(fetch_companies, "get", return_value=xml):
            return fetch_companies.rss(c)

    def test_an_avature_feed_of_exactly_the_page_size_is_capped(self):
        self.assertTrue(self.feed(20, "avature").capped)
        self.assertFalse(self.feed(19, "avature").capped)

    def test_the_successfactors_rule_is_unchanged(self):
        self.assertTrue(self.feed(20, "successfactors").capped)
        self.assertFalse(self.feed(19, "successfactors").capped)


class ForeignCityRuleTest(unittest.TestCase):
    """QA S7-3 (round 3): a big Austrian city (Wien, Vienna, Graz, Linz, Salzburg, Innsbruck) in a part whose other
    parts name foreign countries is Austrian. Exception: a US namesake in the same part (any US state name or code,
    USA, United States, US) makes the part foreign. 'DE' is Germany, not Delaware, so it stays a foreign country."""

    def check(self, location, expected, title=""):
        self.assertIs(fetch_companies.is_austrian(location, title), expected, f"location={location!r} title={title!r}")

    def test_big_austrian_city_with_any_foreign_country_is_austrian(self):
        for loc in ("Wien, Italien", "Wien, Poland", "Wien, England", "Wien, Slovakia, Czechia",
                    "Innsbruck, Munich, CH", "Vienna, Munich, Germany", "Linz, Deutschland"):
            self.check(loc, True)

    def test_us_namesakes_make_the_part_foreign(self):
        for loc in ("Vienna, Illinois", "Vienna, Georgia", "Vienna, VA", "Vienna, Virginia", "Vienna, US",
                    "Vienna, USA", "Vienna, United States", "Graz, Ohio", "Salzburg, New York", "Vienna, West Virginia",
                    "Linz, WV"):
            self.check(loc, False)

    def test_two_letter_codes_count_only_as_the_last_token_of_a_comma_part(self):
        """Vienna abbreviations (MA 48 = Magistratsabteilung) and German or English words are not US codes."""
        for loc in ("Wien, MA 48", "Stadt Wien - MA 10", "Wien (MA 01)", "Wien, OR Abteilung"):
            self.check(loc, True)
        for loc in ("Vienna, VA", "Vienna, VA 22180", "Vienna, VA, Germany", "Vienna, Virginia", "Vienna, Illinois"):
            self.check(loc, False)

    def test_smaller_towns_with_foreign_countries_stay_foreign(self):
        for loc in ("Horn, Germany", "Wolfsberg, Germany", "Bolzano, South Tyrol, Italy", "Horn, Illinois"):
            self.check(loc, False)

    def test_german_de_is_not_delaware(self):
        self.check("Vienna, DE", True)
        self.check("Munich, DE", False)

    def test_title_fallback_follows_the_same_rule(self):
        self.check("", False, title="Sales Manager Vienna, Illinois")
        self.check("", True, title="Sales Manager, Wien, Italien")


if __name__ == "__main__":
    unittest.main()
