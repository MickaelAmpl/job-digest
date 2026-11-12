"""Uni Wien job links (16.10.2026): each SendGrid click link is followed once per distinct job, paced 10 s apart, at
most 30 per run. The first URL must be https SendGrid; every redirect hop is checked before it is requested, and the
first hop on the job portal ends the chain. fetch_posting may open the portal host and honours robots.txt
Crawl-delay per host. No network: the opener, time.sleep, robots and the clock are replaced, except the local-server
test, which shows that a refused http link gets no request at all."""
import contextlib
import io
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import collect  # noqa: E402
import fetch_posting as fp  # noqa: E402

PORTAL = "https://jobs.careercenter.univie.ac.at/job/buchhalter-wmd-einer-modernen-steuerberatung-153652"
CLICK = "https://u49249119.ct.eu.sendgrid.net/ls/click?upn=SECRET-TOKEN-123"
CLICK2 = "https://u1.ct.eu.sendgrid.net/ls/click?upn=SECRET-TOKEN-456"
UW_JOB = {"title": "Buchhalter (m/w/d)", "company": "NWU Biologie GmbH"}


def uw(link, title="Buchhalter (m/w/d)", posted="2026-10-13"):
    return {"source": "uniwien", "title": title, "company": "NWU Biologie GmbH", "location": "Wien, Österreich",
            "posted": posted, "url": "", "ext_id": "", "link": link, "extra": "", "query": ""}


class Resp:
    def __init__(self, final):
        self._final = final

    def geturl(self):
        return self._final

    def read(self, n=-1):  # the body must never be read
        raise AssertionError("body read")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Stands in for build_opener(...): records every request it is asked to make, answers with script(req)."""

    def __init__(self, script):
        self.script = script
        self.methods, self.urls = [], []

    def open(self, req, timeout=None):
        self.methods.append(req.get_method())
        self.urls.append(req.full_url)
        return self.script(req)


def hop(target, base=CLICK):
    """What _ClickRedirects does with one redirect to `target`: a Request, or an exception."""
    req = urllib.request.Request(base, method="HEAD")
    return collect._ClickRedirects().redirect_request(req, None, 302, "Found", {}, target)


class ResolveTest(unittest.TestCase):

    def setUp(self):
        patcher = mock.patch.object(collect.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def run_resolve(self, jobs, follow):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            calls = collect.resolve_uniwien(jobs, follow=follow)
        return calls, err.getvalue()

    def test_portal_job_page_gives_url_and_id(self):
        self.assertEqual(collect.jobportal_url(PORTAL + "?utm_source=mail#top"), (PORTAL, "uw:153652"))

    def test_anything_else_gives_nothing(self):
        for final in ("http://jobs.careercenter.univie.ac.at/job/x-153652",
                      "https://jobs.careercenter.univie.ac.at/jobs/studijob",
                      "https://jobs.careercenter.univie.ac.at/job/no-number",
                      "https://jobs.careercenter.univie.ac.at/job/-12345",
                      "https://jobs.careercenter.univie.ac.at/job/foo bar-12345",
                      "https://evil.example/job/x-153652",
                      "https://jobs.careercenter.univie.ac.at@evil.example/job/x-153652",
                      "https://jobs.careercenter.univie.ac.at:8443/job/x-153652",
                      "https://www.univie.ac.at/job/x-153652", "not a url", ""):
            self.assertEqual(collect.jobportal_url(final), ("", ""), final)

    def test_resolve_sets_url_and_id_and_drops_the_click_link(self):
        jobs = [uw(CLICK)]
        calls, _ = self.run_resolve(jobs, lambda link: PORTAL + "?x=1")
        self.assertEqual(calls, 1)
        self.assertEqual((jobs[0]["url"], jobs[0]["ext_id"]), (PORTAL, "uw:153652"))
        self.assertNotIn("link", jobs[0])

    def test_job_without_date_is_resolved_too(self):
        jobs = [uw(CLICK, posted="")]
        calls, _ = self.run_resolve(jobs, lambda link: PORTAL)
        self.assertEqual(calls, 1)
        self.assertEqual(jobs[0]["url"], PORTAL)

    def test_same_job_in_two_mails_costs_one_request(self):
        jobs = [uw(CLICK), uw(CLICK2)]
        seen = []
        calls, _ = self.run_resolve(jobs, lambda link: seen.append(link) or PORTAL)
        self.assertEqual(calls, 1)
        self.assertEqual(seen, [CLICK])
        self.assertEqual([j["url"] for j in jobs], [PORTAL, PORTAL])
        self.assertEqual(self.sleep.call_count, 0)

    def test_same_title_on_another_date_is_another_job(self):
        jobs = [uw(CLICK, posted="2026-10-13"), uw(CLICK2, posted="2026-10-14")]
        calls, _ = self.run_resolve(jobs, lambda link: PORTAL)
        self.assertEqual(calls, 2)

    def test_job_without_link_gets_no_request(self):
        jobs = [uw("")]
        calls, _ = self.run_resolve(jobs, lambda link: self.fail("must not be called"))
        self.assertEqual(calls, 0)
        self.assertEqual(jobs[0]["url"], "")

    def test_failed_or_foreign_link_keeps_the_job_without_url(self):
        jobs = [uw(CLICK, "Foreign"), uw(CLICK2, "Timeout")]

        def follow(link):
            if link == CLICK:
                return "https://evil.example/job/x-153652"
            raise urllib.error.URLError("timed out")

        _, err = self.run_resolve(jobs, follow)
        self.assertEqual([(j["title"], j["url"], j["ext_id"]) for j in jobs],
                         [("Foreign", "", ""), ("Timeout", "", "")])
        self.assertNotIn("SECRET", err)
        self.assertNotIn("sendgrid", err)

    def test_click_link_is_never_printed_or_stored(self):
        jobs = [uw(CLICK)]

        def follow(link):  # an error whose text contains the link, as a real one could
            raise urllib.error.URLError(link)

        _, err = self.run_resolve(jobs, follow)
        self.assertNotIn("SECRET", err)
        self.assertNotIn("sendgrid", err)
        self.assertNotIn("link", jobs[0])
        self.assertEqual(jobs[0]["url"], "")

    def test_ten_seconds_apart_and_no_wait_before_the_first(self):
        jobs = [uw(CLICK, "A"), uw(CLICK2, "B")]
        calls, _ = self.run_resolve(jobs, lambda link: PORTAL)
        self.assertEqual(calls, 2)
        self.assertEqual(self.sleep.call_args_list, [mock.call(10.0)])

    def test_at_most_thirty_requests_per_run(self):
        jobs = [uw(f"{CLICK}&n={i}", f"Job {i}") for i in range(35)]
        calls, _ = self.run_resolve(jobs, lambda link: PORTAL)
        self.assertEqual(calls, collect.UW_MAX)
        self.assertEqual(self.sleep.call_count, collect.UW_MAX - 1)
        self.assertEqual(sum(1 for j in jobs if j["url"]), collect.UW_MAX)
        self.assertTrue(all("link" not in j for j in jobs))

    def test_other_sources_are_left_alone(self):
        job = {"source": "linkedin", "title": "PM", "url": "https://www.linkedin.com/jobs/view/1/", "ext_id": "li:1"}
        calls, _ = self.run_resolve([job], lambda link: self.fail("must not be called"))
        self.assertEqual(calls, 0)
        self.assertEqual(job["url"], "https://www.linkedin.com/jobs/view/1/")


class ClickRedirectTest(unittest.TestCase):

    def test_sendgrid_hop_is_requested(self):
        req = hop(CLICK2)
        self.assertEqual(req.full_url, CLICK2)

    def test_relative_sendgrid_hop_is_joined_to_the_link(self):
        self.assertEqual(hop("/next?x=1").full_url, "https://u49249119.ct.eu.sendgrid.net/next?x=1")

    def test_first_portal_hop_stops_without_a_request(self):
        with self.assertRaises(collect._PortalReached) as stop:
            hop(PORTAL)
        self.assertEqual(stop.exception.url, PORTAL)

    def test_every_other_hop_is_refused(self):
        for target in ("http://jobs.careercenter.univie.ac.at/job/x-12345",
                       "http://u1.sendgrid.net/x", "ftp://u1.sendgrid.net/x",
                       "https://127.0.0.1/admin", "https://[::1]/x", "https://10.0.0.5/x",
                       "https://u1.sendgrid.net:8443/x", "https://user@u1.sendgrid.net/x",
                       "https://www.stepstone.at/job/1", "https://evil-sendgrid.net/x",
                       "https://sendgrid.net.evil.example/x",
                       "https://jobs.careercenter.univie.ac.at.evil.example/job/x-12345",
                       "https://jobs.careercenter.univie.ac.at:8443/job/x-12345",
                       "https://www.univie.ac.at/x", "https://evil.example/job/x-153652"):
            with self.assertRaises(OSError, msg=target):
                hop(target)


class FollowLinkTest(unittest.TestCase):

    def test_first_url_must_be_https_sendgrid_and_nothing_is_requested_otherwise(self):
        for link in ("http://u1.ct.eu.sendgrid.net/x", "ftp://u1.ct.eu.sendgrid.net/x",
                     "https://127.0.0.1/x", "https://u1.ct.eu.sendgrid.net:8080/x",
                     "https://jobs.careercenter.univie.ac.at/job/x-12345", "https://www.stepstone.at/x"):
            with mock.patch("urllib.request.build_opener") as build:
                with self.assertRaises(OSError, msg=link):
                    collect.follow_link(link)
                build.assert_not_called()

    def test_local_server_link_gets_no_request_at_all(self):
        hits = []

        class Probe(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_HEAD(self):
                hits.append("HEAD")
                self.send_response(200)
                self.end_headers()

            def do_GET(self):
                hits.append("GET")
                self.send_response(200)
                self.end_headers()

        server = HTTPServer(("127.0.0.1", 0), Probe)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        with self.assertRaises(OSError):
            collect.follow_link(f"http://127.0.0.1:{server.server_port}/ls/click?upn=SECRET")
        self.assertEqual(hits, [])

    def test_redirect_to_portal_returns_it_without_requesting_it(self):
        def script(req):
            return hop(PORTAL, base=req.full_url)  # what the redirect handler does with the 302 target

        opener = FakeOpener(script)
        with mock.patch("urllib.request.build_opener", return_value=opener):
            self.assertEqual(collect.follow_link(CLICK), PORTAL)
        self.assertEqual(opener.urls, [CLICK])  # the portal URL itself was never requested
        self.assertEqual(opener.methods, ["HEAD"])

    def test_final_click_host_answer_is_returned_as_is(self):
        def script(req):
            if req.full_url == CLICK:
                return Resp(CLICK2)  # a second click host answered as redirect target
            raise AssertionError(req.full_url)

        opener = FakeOpener(script)
        with mock.patch("urllib.request.build_opener", return_value=opener):
            self.assertEqual(collect.follow_link(CLICK), CLICK2)
        self.assertEqual(opener.urls, [CLICK])

    def test_refused_hop_stops_the_chain_and_no_get_follows(self):
        def script(req):
            raise OSError("redirect refused")  # the handler refused the hop before any request

        opener = FakeOpener(script)
        with mock.patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaises(OSError):
                collect.follow_link(CLICK)
        self.assertEqual(opener.methods, ["HEAD"])

    def test_head_refused_then_get(self):
        def script(req):
            if req.get_method() == "HEAD":
                raise urllib.error.HTTPError(CLICK, 405, "Method Not Allowed", None, None)
            return Resp(PORTAL)

        opener = FakeOpener(script)
        with mock.patch("urllib.request.build_opener", return_value=opener):
            self.assertEqual(collect.follow_link(CLICK), PORTAL)
        self.assertEqual(opener.methods, ["HEAD", "GET"])

    def test_head_success_needs_no_get(self):
        opener = FakeOpener(lambda req: Resp(PORTAL))
        with mock.patch("urllib.request.build_opener", return_value=opener):
            self.assertEqual(collect.follow_link(CLICK), PORTAL)
        self.assertEqual(opener.methods, ["HEAD"])

    def test_not_found_is_an_error_without_get(self):
        def script(req):
            raise urllib.error.HTTPError(CLICK, 404, "Not Found", None, None)

        opener = FakeOpener(script)
        with mock.patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaises(urllib.error.HTTPError):
                collect.follow_link(CLICK)
        self.assertEqual(opener.methods, ["HEAD"])


class PortalFetchTest(unittest.TestCase):
    HOST = "jobs.careercenter.univie.ac.at"

    def setUp(self):
        fp._ROBOTS.clear()
        fp._HOST_LAST.clear()
        fp._HOST_GAP.clear()
        self.addCleanup(fp._ROBOTS.clear)
        self.addCleanup(fp._HOST_LAST.clear)
        self.addCleanup(fp._HOST_GAP.clear)
        self.now, self.sleeps = [100.0], []
        for name, value in (("_clock", lambda: self.now[0]), ("_sleep", self._fake_sleep)):
            patcher = mock.patch.object(fp, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _fake_sleep(self, s):
        self.sleeps.append(s)
        self.now[0] += s

    def test_portal_host_is_allowed(self):
        self.assertTrue(fp.allowed(PORTAL, UW_JOB))

    def test_rest_of_univie_is_not_allowed(self):
        for url in ("https://www.univie.ac.at/karriere/", "https://careercenter.univie.ac.at/agb/",
                    "https://univie.ac.at/jobs/1"):
            self.assertFalse(fp.allowed(url, UW_JOB), url)

    def test_crawl_delay_is_read_for_our_group(self):
        self.assertEqual(fp._Rules("User-agent: *\nCrawl-delay: 10\nDisallow: /api/gmapi").crawl_delay(), 10.0)
        self.assertEqual(fp._Rules("User-agent: *\nCrawl-delay: 20\n\nUser-agent: job-digest-bot\n"
                                   "Crawl-delay: 3").crawl_delay(), 3.0)
        self.assertIsNone(fp._Rules("User-agent: *\nDisallow: /x").crawl_delay())
        self.assertIsNone(fp._Rules("User-agent: *\nCrawl-delay: soon").crawl_delay())
        self.assertIsNone(fp._Rules("User-agent: *\nCrawl-delay: -4").crawl_delay())

    def test_robots_crawl_delay_sets_the_host_gap_within_bounds(self):
        cases = {"Crawl-delay: 10": 10.0, "Crawl-delay: 60": 15.0, "Crawl-delay: 0.2": 1.0, "Disallow: /x": None}
        for i, (text, want) in enumerate(cases.items()):
            host = f"host{i}.example"
            rules = fp._Rules(f"User-agent: *\n{text}\n")
            with mock.patch.object(fp, "_fetch_robots", return_value=rules):
                fp._robots_ok(f"https://{host}/job/1")
            self.assertEqual(fp._HOST_GAP.get(host), want, text)

    def test_pacing_waits_the_host_gap_and_other_hosts_keep_one_second(self):
        fp._HOST_GAP[self.HOST] = 10.0
        fp._polite(self.HOST)
        self.now[0] += 3.0
        fp._polite(self.HOST)
        self.assertEqual(self.sleeps, [7.0])
        fp._polite("careers.other.com")
        self.now[0] += 0.25
        fp._polite("careers.other.com")
        self.assertEqual(self.sleeps, [7.0, 0.75])


if __name__ == "__main__":
    unittest.main()
