"""fetch_posting: which pages we may open (no private targets, no job boards), robots.txt, the page deadline,
how the posting text is taken out, and every refusal reason. No network, no real waiting: DNS, the page opener,
robots and the clock are replaced."""
import datetime
import json
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import enrich  # noqa: E402
import fetch_posting as fp  # noqa: E402

JOB = {"title": "Product Owner (m/w/d)", "company": "Acme GmbH", "company_id": ""}
BANKING = {"title": "Product Owner Banking", "company": "Acme GmbH"}
PUBLIC = ["93.184.216.34"]
LONG = ("Wir suchen eine Product Owner Person, die unser Produkt gemeinsam mit Entwicklung und Kunden "
        "weiterentwickelt. Du sammelst Anforderungen, priorisierst das Backlog und begleitest Releases. ") * 3


def html_page(body, title="Product Owner (m/w/d) - Acme GmbH", head=""):
    return f"<html><head><title>{title}</title>{head}</head><body>{body}</body></html>"


def jsonld(obj):
    return f'<script type="application/ld+json">{json.dumps(obj, ensure_ascii=False)}</script>'


def posting(description, title="Product Owner (m/w/d)", **extra):
    return dict({"@context": "https://schema.org", "@type": "JobPosting", "title": title,
                 "description": description}, **extra)


class FakeResp:
    def __init__(self, url, body, ctype="text/html", charset="utf-8"):
        self._url, self._body, self._pos = url, body, 0
        self.reads = []
        self.headers = Message()
        self.headers["Content-Type"] = f"{ctype}; charset={charset}" if charset else ctype

    def geturl(self):
        return self._url

    def read(self, n=-1):
        self.reads.append(n)
        if n is None or n < 0:
            n = len(self._body) - self._pos
        out = self._body[self._pos:self._pos + n]
        self._pos += len(out)
        return out

    def read1(self, n=-1):
        return self.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Net(unittest.TestCase):
    """DNS answers a public address, sleeping is a no-op, caches are empty for each test."""

    def setUp(self):
        fp._ROBOTS.clear()
        fp._HOST_LAST.clear()
        fp._HOST_GAP.clear()
        for name, value in (("_resolve", lambda host, port: PUBLIC), ("_sleep", lambda s: None)):
            patcher = mock.patch.object(fp, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class FetchCase(Net):
    """Robots is not read (no file, so allowed); the company's own domain is acme.com."""

    def setUp(self):
        super().setUp()
        for target, value in ((fp, "_fetch_robots"),):
            patcher = mock.patch.object(target, value, side_effect=OSError("no robots"))
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(enrich, "_own_domains", return_value={"acme.com"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def serve(self, body, url="https://careers.acme.com/job/12345", ctype="text/html", final=None):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        return mock.patch.object(fp, "_open_page",
                                 return_value=(final or url, ctype, "utf-8", raw if ctype == "text/html" else None))

    def run_fetch(self, body, job=JOB, url="https://careers.acme.com/job/12345", **kw):
        with self.serve(body, url=url, **kw):
            return fp.fetch_description(url, job)

    def run_info(self, body, job=JOB, url="https://careers.acme.com/job/12345", **kw):
        with self.serve(body, url=url, **kw):
            return fp.fetch_posting_info(url, job)


class Allowed(Net):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(enrich, "_own_domains", return_value=set())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_deny_list_beats_everything(self):
        with mock.patch.object(enrich, "_own_domains", return_value={"karriere.at", "linkedin.com"}):
            for url in ("https://karriere.at/jobs/1", "https://www.karriere.at/jobs/1", "https://jobs.karriere.at/x",
                        "https://jobs.at/i/1", "https://www.linkedin.com/jobs/view/1", "https://de.linkedin.com/x",
                        "https://www.stepstone.at/job/1", "https://www.stepstone.de/job/1", "https://indeed.com/x",
                        "https://www.glassdoor.com/x", "https://www.willhaben.at/x", "https://www.derstandard.at/x",
                        "https://www.adzuna.at/x", "https://www.xing.com/x", "https://www.monster.at/x",
                        "https://karriere.at@acme.com/x"):
                self.assertFalse(fp.allowed(url, JOB), url)

    def test_lookalikes_are_not_allowed(self):
        for url in ("https://karriere.at.evil.com/jobs/12345", "https://karriere-at.com/jobs/1",
                    "https://linkedin.com.evil.example/jobs/1", "https://acme.com.evil.example/jobs/12345"):
            self.assertFalse(fp.allowed(url, JOB), url)

    def test_ats_domains_are_allowed(self):
        for url in ("https://acme.jobs.personio.de/job/2139923", "https://boards.greenhouse.io/acme/jobs/1",
                    "https://jobs.lever.co/acme/abc-123", "https://apply.workable.com/j/1F19EC0B52",
                    "https://job-boards.eu.greenhouse.io/bitpanda/jobs/4721614101",
                    "https://acme.myworkdayjobs.com/en-US/careers/job/1", "https://acme.bamboohr.com/careers/184"):
            self.assertTrue(fp.allowed(url, JOB), url)

    def test_own_company_domain_is_allowed(self):
        with mock.patch.object(enrich, "_own_domains", return_value={"acme.com"}):
            self.assertTrue(fp.allowed("https://www.acme.com/careers/product-owner", JOB))
            self.assertTrue(fp.allowed("https://careers.acme.com/jobs/12345", JOB))

    def test_rule_c_official_domain_matching_company(self):
        erste = {"title": "Praktikum Produktmanagement", "company": "Erste Group Bank AG", "company_id": ""}
        self.assertTrue(fp.allowed("https://www.erstegroup.com/en/careers/job/123456", erste))
        self.assertFalse(fp.allowed("https://www.erste-evil.com/en/careers/job/123456", erste))

    def test_unknown_domain_and_schemes_are_not_allowed(self):
        for url in ("https://example-shop.com/job/123", "ftp://acme.com/job/1", "javascript:alert(1)",
                    "https://127.0.0.1/job/1", "https://localhost/job/1", "", "not a url"):
            self.assertFalse(fp.allowed(url, JOB), url)

    def test_job_board_is_not_rule_c(self):
        self.assertFalse(fp.allowed("https://acme.ams.at/jobs/12345", JOB))

    def test_local_names_and_odd_ports_are_refused(self):
        with mock.patch.object(enrich, "_own_domains", return_value={"acme.com", "acme.localhost", "acme.lan"}):
            for url in ("https://careers.acme.com:8443/job/12345", "http://careers.acme.com:22/job/1",
                        "https://careers.acme.localhost/job/1", "https://acme.lan/job/12345",
                        "https://printer.local/job/1", "https://wiki.internal/job/1"):
                self.assertFalse(fp.allowed(url, JOB), url)
            self.assertTrue(fp.allowed("https://careers.acme.com:443/job/12345", JOB))
            self.assertTrue(fp.allowed("http://careers.acme.com:80/job/12345", JOB))


class Ssrf(FetchCase):
    """Every hop, the final URL and robots.txt must resolve to public addresses only."""

    def test_private_and_special_addresses_are_refused(self):
        for addr in ("10.0.0.5", "127.0.0.1", "169.254.169.254", "100.64.0.1", "::1", "fc00::1", "fe80::1",
                     "::ffff:10.0.0.1", "0.0.0.0", "224.0.0.1"):
            with mock.patch.object(fp, "_resolve", return_value=[addr]):
                self.assertEqual(fp._refusal("https://careers.acme.com/job/1", JOB), "not allowed host", addr)

    def test_one_private_address_among_public_ones_refuses(self):
        with mock.patch.object(fp, "_resolve", return_value=["93.184.216.34", "10.1.2.3"]):
            self.assertEqual(fp._refusal("https://careers.acme.com/job/1", JOB), "not allowed host")

    def test_public_address_passes(self):
        self.assertIsNone(fp._refusal("https://careers.acme.com/job/1", JOB))

    def test_redirect_hop_to_private_address_is_refused(self):
        guard = fp._RedirectGuard(JOB, deadline=10 ** 12)
        req = urllib.request.Request("https://careers.acme.com/job/1")
        with mock.patch.object(fp, "_resolve", return_value=["127.0.0.1"]):
            with self.assertRaises(fp._Refused) as cm:
                guard.redirect_request(req, None, 302, "Found", {}, "https://careers.acme.com/admin")
        self.assertEqual(cm.exception.reason, "not allowed host")

    def test_final_url_on_private_address_is_refused(self):
        with mock.patch.object(fp, "_resolve", side_effect=lambda h, p: ["10.0.0.9"] if h == "careers.acme.com"
                               else PUBLIC):
            with mock.patch.object(fp, "_open_page", return_value=("https://careers.acme.com/x", "text/html",
                                                                    "utf-8", b"<main>x</main>")):
                self.assertEqual(fp.fetch_description("https://careers.acme.com/x", JOB), ("", "not allowed host"))

    def test_port_is_refused_before_any_request(self):
        with mock.patch.object(fp, "_open_page") as opener:
            self.assertEqual(fp.fetch_description("https://careers.acme.com:8080/job/1", JOB),
                             ("", "not allowed host"))
            opener.assert_not_called()

    def test_robots_redirect_off_host_is_refused(self):
        guard = fp._RobotsRedirect("careers.acme.com")
        req = urllib.request.Request("https://careers.acme.com/robots.txt")
        with self.assertRaises(fp._Refused):
            guard.redirect_request(req, None, 301, "Moved", {}, "https://other.example.com/robots.txt")


class Refusals(FetchCase):
    def test_banned_first_url_is_refused_without_fetch(self):
        with mock.patch.object(fp, "_open_page") as opener:
            self.assertEqual(fp.fetch_description("https://www.karriere.at/jobs/7879696", JOB), ("", "banned host"))
            opener.assert_not_called()

    def test_not_allowed_first_url(self):
        with mock.patch.object(fp, "_open_page") as opener:
            self.assertEqual(fp.fetch_description("https://example-shop.com/job/1", JOB), ("", "not allowed host"))
            opener.assert_not_called()

    def test_redirect_to_banned_host_is_refused(self):
        body = html_page(f"<main>{'<p>' + LONG + '</p>'}</main>")
        self.assertEqual(self.run_fetch(body, final="https://www.karriere.at/jobs/7879696"), ("", "banned host"))

    def test_redirect_guard_refuses_banned_hop(self):
        guard = fp._RedirectGuard(JOB, deadline=10 ** 12)
        req = urllib.request.Request("https://careers.acme.com/job/1")
        with self.assertRaises(fp._Refused) as cm:
            guard.redirect_request(req, None, 302, "Found", {}, "https://www.linkedin.com/jobs/view/1")
        self.assertEqual(cm.exception.reason, "banned host")

    def test_redirect_guard_refuses_unknown_host(self):
        guard = fp._RedirectGuard(JOB, deadline=10 ** 12)
        req = urllib.request.Request("https://careers.acme.com/job/1")
        with self.assertRaises(fp._Refused) as cm:
            guard.redirect_request(req, None, 301, "Moved", {}, "https://example-shop.com/x")
        self.assertEqual(cm.exception.reason, "not allowed host")

    def test_robots_disallow(self):
        with mock.patch.object(fp, "_fetch_robots", return_value=fp._Rules("User-agent: *\nDisallow: /")), \
                mock.patch.object(fp, "_open_page") as opener:
            self.assertEqual(fp.fetch_description("https://careers.acme.com/job/12345", JOB), ("", "robots"))
            opener.assert_not_called()

    def test_not_html(self):
        self.assertEqual(self.run_fetch(LONG, ctype="application/json"), ("", "not html"))
        self.assertEqual(self.run_fetch(LONG, ctype="application/pdf"), ("", "not html"))

    def test_http_404_and_410_give_exact_reasons(self):
        for code in (404, 410):
            err = urllib.error.HTTPError("https://careers.acme.com/job/1", code, "x", {}, None)
            with mock.patch.object(fp, "_open_page", side_effect=err):
                self.assertEqual(fp.fetch_description("https://careers.acme.com/job/1", JOB), ("", f"http {code}"))
                info = fp.fetch_posting_info("https://careers.acme.com/job/1", JOB)
            self.assertEqual(info["closed"], code == 410, code)

    def test_only_410_and_expired_validthrough_close_a_job(self):
        page = html_page(jsonld(posting(f"<p>{LONG}</p>", validThrough="2020-01-01")))
        self.assertTrue(self.run_info(page)["closed"])
        self.assertTrue(self.run_info(html_page(f"<main><h1>Product Owner (m/w/d)</h1><p>{LONG}</p></main>"),
                                      url="https://careers.acme.com/job/1")["closed"] is False)
        for phrase in ("Position has been filled", "Job expired", "Page not found"):
            info = self.run_info(html_page(f"<main><h1>Product Owner (m/w/d)</h1><p>{phrase}. {LONG}</p></main>"))
            self.assertEqual(info["reason"], "closed phrase", phrase)
            self.assertFalse(info["closed"], phrase)
            self.assertEqual(info["text"], "", phrase)

    def test_timeouts(self):
        with mock.patch.object(fp, "_open_page", side_effect=TimeoutError("timed out")):
            self.assertEqual(fp.fetch_description("https://careers.acme.com/job/1", JOB), ("", "error: TimeoutError"))
        with mock.patch.object(fp, "_open_page", side_effect=urllib.error.URLError(TimeoutError("timed out"))):
            self.assertEqual(fp.fetch_description("https://careers.acme.com/job/1", JOB), ("", "error: TimeoutError"))

    def test_other_errors_are_named(self):
        with mock.patch.object(fp, "_open_page", side_effect=ValueError("bad")):
            self.assertEqual(fp.fetch_description("https://careers.acme.com/job/1", JOB), ("", "error: ValueError"))


class Robots(unittest.TestCase):
    """robots.txt rules, read from text."""

    def test_star_group_disallow_and_empty_disallow(self):
        self.assertFalse(fp._Rules("User-agent: *\nDisallow: /jobs/").can_fetch("https://x.at/jobs/1"))
        self.assertTrue(fp._Rules("User-agent: *\nDisallow:\n").can_fetch("https://x.at/jobs/1"))

    def test_own_group_beats_star_group(self):
        text = "User-agent: job-digest-bot\nDisallow: /\n\nUser-agent: *\nAllow: /"
        self.assertFalse(fp._Rules(text).can_fetch("https://x.at/jobs/1"))
        text = "User-agent: Googlebot\nDisallow: /\n\nUser-agent: *\nDisallow: /private"
        self.assertTrue(fp._Rules(text).can_fetch("https://x.at/jobs/1"))

    def test_other_bot_only_is_allowed(self):
        self.assertTrue(fp._Rules("User-agent: Googlebot\nDisallow: /\n").can_fetch("https://x.at/jobs/1"))

    def test_longest_rule_wins_and_allow_wins_ties(self):
        text = "User-agent: *\nDisallow: /jobs\nAllow: /jobs/123456"
        self.assertTrue(fp._Rules(text).can_fetch("https://x.at/jobs/123456"))
        self.assertFalse(fp._Rules(text).can_fetch("https://x.at/jobs/999"))
        self.assertTrue(fp._Rules("User-agent: *\nDisallow: /a\nAllow: /a").can_fetch("https://x.at/a"))

    def test_wildcard_and_end_anchor(self):
        rules = fp._Rules("User-agent: *\nDisallow: /*123456\nDisallow: /*.pdf$")
        self.assertFalse(rules.can_fetch("https://x.at/jobs/123456"))
        self.assertFalse(rules.can_fetch("https://x.at/files/a.pdf"))
        self.assertTrue(rules.can_fetch("https://x.at/files/a.pdf?y=1"))

    def test_query_and_case(self):
        rules = fp._Rules("User-agent: *\nDisallow: /*?")
        self.assertFalse(rules.can_fetch("https://x.at/jobs/123456?x=1"))
        self.assertTrue(fp._Rules("User-agent: *\nDisallow: /JOBS/").can_fetch("https://x.at/jobs/1"))

    def test_bom_crlf_comments_and_crawl_delay(self):
        self.assertFalse(fp._Rules("﻿User-agent: *\r\nDisallow: /\r\n").can_fetch("https://x.at/jobs/1"))
        self.assertTrue(fp._Rules("User-agent: *\nCrawl-delay: 30\nDisallow: /nothing # note").can_fetch(
            "https://x.at/jobs/1"))

    def test_not_a_robots_file_allows(self):
        self.assertTrue(fp._Rules("<html>not a robots file</html>").can_fetch("https://x.at/jobs/1"))

    def test_disallow_all_flag(self):
        self.assertFalse(fp._Rules(disallow_all=True).can_fetch("https://x.at/"))


class RobotsHttp(Net):
    """How robots.txt answers over HTTP decides the whole host."""

    def check(self, exc):
        opener = mock.Mock()
        opener.open.side_effect = exc
        with mock.patch.object(fp.urllib.request, "build_opener", return_value=opener):
            return fp._robots_ok("https://careers.acme.com/job/12345")

    def test_401_403_and_5xx_disallow_everything(self):
        for code in (401, 403, 500, 503):
            fp._ROBOTS.clear()
            err = urllib.error.HTTPError("https://careers.acme.com/robots.txt", code, "x", {}, None)
            self.assertFalse(self.check(err), code)

    def test_404_410_timeouts_and_network_errors_allow(self):
        for exc in (urllib.error.HTTPError("u", 404, "x", {}, None), urllib.error.HTTPError("u", 410, "x", {}, None),
                    TimeoutError("timed out"), urllib.error.URLError("down")):
            fp._ROBOTS.clear()
            self.assertTrue(self.check(exc), repr(exc))

    def test_robots_fetch_uses_the_guarded_opener(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError("down")
        with mock.patch.object(fp.urllib.request, "build_opener", return_value=opener) as build:
            with self.assertRaises(urllib.error.URLError):
                fp._fetch_robots("https://careers.acme.com/robots.txt")
        self.assertTrue(any(isinstance(h, fp._RobotsRedirect) for h in build.call_args.args))

    def test_robots_read_has_its_own_deadline(self):
        now = [0.0]

        class Slow(FakeResp):
            def read1(self, n=-1):
                now[0] += 31.0  # one chunk takes longer than the 30 s deadline
                return b"x" * 10

            def read(self, n=-1):
                return self.read1(n)

        opener = mock.Mock()
        opener.open.return_value = Slow("https://careers.acme.com/robots.txt", b"")
        with mock.patch.object(fp, "_clock", lambda: now[0]), \
                mock.patch.object(fp.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(fp._PageTimeout):
                fp._fetch_robots("https://careers.acme.com/robots.txt")
            with self.assertRaises(fp._PageTimeout):
                fp._robots_ok("https://careers.acme.com/job/1")
        self.assertNotIn("https://careers.acme.com", fp._ROBOTS)

    def test_robots_read_once_per_host(self):
        with mock.patch.object(fp, "_fetch_robots", side_effect=OSError("down")) as rob:
            self.assertTrue(fp._robots_ok("https://careers.acme.com/job/1"))
            self.assertTrue(fp._robots_ok("https://careers.acme.com/job/2"))
            self.assertEqual(rob.call_count, 1)


class Politeness(Net):
    def setUp(self):
        super().setUp()
        self.now = [100.0]
        self.sleeps = []
        for name, value in (("_clock", lambda: self.now[0]), ("_sleep", self._fake_sleep)):
            patcher = mock.patch.object(fp, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _fake_sleep(self, s):
        self.sleeps.append(s)
        self.now[0] += s

    def test_same_host_waits_until_one_second(self):
        fp._polite("jobs.acme.com")
        self.now[0] += 0.25
        fp._polite("jobs.acme.com")
        self.assertEqual(len(self.sleeps), 1)
        self.assertAlmostEqual(self.sleeps[0], 0.75)

    def test_no_wait_after_the_gap_or_for_another_host(self):
        fp._polite("jobs.acme.com")
        self.now[0] += 2.0
        fp._polite("jobs.acme.com")
        fp._polite("careers.other.com")
        self.assertEqual(self.sleeps, [])

    def test_page_requests_are_throttled(self):
        page = FakeResp("https://careers.acme.com/job/1", b"<html></html>")
        opener = mock.Mock()
        opener.open.return_value = page
        with mock.patch.object(fp.urllib.request, "build_opener", return_value=opener):
            fp._open_page("https://careers.acme.com/job/1", JOB, 5)
            fp._open_page("https://careers.acme.com/job/2", JOB, 5)
        self.assertEqual(len(self.sleeps), 1)


class Clock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += seconds


class HostQueue(Net):
    """Eight jobs on one host with Crawl-delay 10 s and four workers: robots is read once, every page waits its
    turn and then gets its own 30 s, so all eight succeed and the page requests are 10 s apart."""

    def test_eight_concurrent_jobs_on_one_host_all_succeed(self):
        clock = Clock()
        page = ("<html><head><title>Product Owner (m/w/d)</title></head><body><main><h1>Product Owner (m/w/d)</h1>"
                f"<p>{LONG}</p></main></body></html>").encode()
        seen = {"robots": 0, "pages": []}

        class Site:
            def open(self, req, timeout=None):
                url = req.full_url
                if url.endswith("/robots.txt"):
                    seen["robots"] += 1
                    return FakeResp(url, b"User-agent: *\nCrawl-delay: 10\n", "text/plain")
                seen["pages"].append(clock.now)
                return FakeResp(url, page)

        with mock.patch.object(fp, "_clock", lambda: clock.now), \
                mock.patch.object(fp, "_sleep", clock.sleep), \
                mock.patch.object(fp.urllib.request, "build_opener", side_effect=lambda *handlers: Site()), \
                mock.patch.object(enrich, "_own_domains", return_value={"acme.com"}):
            urls = [f"https://careers.acme.com/job/{i}" for i in range(8)]
            with ThreadPoolExecutor(4) as pool:
                results = list(pool.map(lambda url: fp.fetch_posting_info(url, JOB), urls))
        self.assertEqual([r["reason"] for r in results], ["ok:html"] * 8)
        self.assertEqual(seen["robots"], 1)
        starts = sorted(seen["pages"])
        self.assertEqual(len(starts), 8)
        self.assertGreaterEqual(starts[0], 10 - 1e-9)  # the first page waited for the Crawl-delay gap
        self.assertTrue(all(b - a >= 10 - 1e-9 for a, b in zip(starts, starts[1:])), starts)


class Deadline(FetchCase):
    def setUp(self):
        super().setUp()
        self.now = [0.0]
        patcher = mock.patch.object(fp, "_clock", lambda: self.now[0])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_body_read_stops_at_the_deadline(self):
        class Drip(FakeResp):
            def read(self, n=-1):
                self.now[0] += 1.0  # each read takes a second
                return b"x" * min(n, 10)

        resp = Drip("https://careers.acme.com/job/1", b"", )
        resp.now = self.now
        with self.assertRaises(TimeoutError):
            fp._read_capped(resp, deadline=5.0)

    def test_body_is_capped_and_read_in_chunks(self):
        class Endless(FakeResp):
            def read(self, n=-1):
                self.reads.append(n)
                return b"x" * min(n, 10_000_000)

        resp = Endless("https://careers.acme.com/job/1", b"")
        body = fp._read_capped(resp, deadline=10 ** 12)
        self.assertEqual(len(body), fp.MAX_BYTES)
        self.assertTrue(all(0 < n <= fp.CHUNK for n in resp.reads))

    def test_redirect_after_the_deadline_is_refused(self):
        self.now[0] = 50.0
        guard = fp._RedirectGuard(JOB, deadline=10.0)
        with self.assertRaises(TimeoutError):
            guard.redirect_request(urllib.request.Request("https://careers.acme.com/a"), None, 302, "F", {},
                                   "https://careers.acme.com/b")

    def test_page_past_deadline_gives_timeout_reason(self):
        with mock.patch.object(fp, "DEADLINE", -1):
            with mock.patch.object(fp, "_open_page", side_effect=TimeoutError("page deadline")):
                self.assertEqual(fp.fetch_description("https://careers.acme.com/job/1", JOB),
                                 ("", "error: TimeoutError"))

    def test_default_deadline_is_thirty_seconds(self):
        self.assertEqual(fp.DEADLINE, 30)


class Extraction(FetchCase):
    def test_jsonld_single(self):
        page = html_page(jsonld(posting(f"<p>{LONG}</p>")))
        text, reason = self.run_fetch(page)
        self.assertEqual(reason, "ok:jsonld")
        self.assertTrue(text.startswith("Product Owner (m/w/d)\n"))
        self.assertIn("Wir suchen eine Product Owner Person", text)
        self.assertNotIn("<", text)

    def test_jsonld_in_list(self):
        page = html_page(jsonld([{"@type": "Organization", "name": "Acme"}, posting(f"<p>{LONG}</p>")]))
        self.assertEqual(self.run_fetch(page)[1], "ok:jsonld")

    def test_jsonld_in_graph_with_type_list(self):
        node = posting(f"<p>{LONG}</p>")
        node["@type"] = ["Thing", "JobPosting"]
        page = html_page(jsonld({"@context": "https://schema.org", "@graph": [{"@type": "WebPage"}, node]}))
        self.assertEqual(self.run_fetch(page)[1], "ok:jsonld")

    def test_jsonld_html_description_becomes_text(self):
        desc = (f"<p>{LONG}</p><ul><li>Erfahrung &amp; Wissen</li><li>Deutsch C1</li></ul>"
                "Zeile eins<br>Zeile zwei")
        text, reason = self.run_fetch(html_page(jsonld(posting(desc))))
        self.assertEqual(reason, "ok:jsonld")
        self.assertIn("- Erfahrung & Wissen\n- Deutsch C1", text)
        self.assertIn("Zeile eins\nZeile zwei", text)
        self.assertNotIn("&amp;", text)
        self.assertNotIn("<", text)

    def test_jsonld_double_escaped_description(self):
        text, reason = self.run_fetch(html_page(jsonld(posting("&lt;p&gt;" + LONG + "&lt;/p&gt;"))))
        self.assertEqual(reason, "ok:jsonld")
        self.assertNotIn("<", text)

    def test_jsonld_description_as_list(self):
        text, reason = self.run_fetch(html_page(jsonld(posting([LONG, LONG]))))
        self.assertEqual(reason, "ok:jsonld")

    def test_jsonld_title_is_unescaped(self):
        text, reason = self.run_fetch(html_page(jsonld(posting(f"<p>{LONG}</p>", title="Product Owner &amp; Banking",
                                                               )), title="Product Owner & Banking - Acme GmbH"),
                                      job={"title": "Product Owner & Banking", "company": "Acme GmbH"})
        self.assertEqual(reason, "ok:jsonld")
        self.assertTrue(text.startswith("Product Owner & Banking\n"), text[:60])

    def test_jsonld_posting_for_another_job_is_not_used(self):
        other = posting(LONG * 3, title="Cleaner")
        page = html_page(jsonld(other) + f"<main><h1>Product Owner (m/w/d)</h1><p>{LONG}</p></main>")
        text, reason = self.run_fetch(page)
        self.assertEqual(reason, "ok:html")
        self.assertNotIn("Cleaner", text)

    def test_jsonld_picks_the_matching_posting_not_the_longest(self):
        page = html_page(jsonld([posting(LONG * 5, title="Sales Rep"), posting(f"<p>{LONG}</p>")]))
        text, reason = self.run_fetch(page)
        self.assertEqual(reason, "ok:jsonld")
        self.assertTrue(text.startswith("Product Owner (m/w/d)\n"))

    def test_jsonld_without_title_is_not_used(self):
        page = html_page(jsonld({"@type": "JobPosting", "description": LONG}) + f"<main><p>{LONG}</p></main>")
        self.assertEqual(self.run_fetch(page)[1], "ok:html")

    def test_short_jsonld_loses_to_longer_html(self):
        short = posting("Short teaser only.")
        page = html_page(jsonld(short) + f"<main><h1>Product Owner (m/w/d)</h1><p>{LONG}</p></main>")
        text, reason = self.run_fetch(page)
        self.assertEqual(reason, "ok:html")
        self.assertIn("Wir suchen eine Product Owner Person", text)

    def test_longer_jsonld_beats_html(self):
        page = html_page(jsonld(posting(f"<p>{LONG}</p>")) + "<main><h1>Product Owner (m/w/d)</h1><p>short</p></main>")
        self.assertEqual(self.run_fetch(page)[1], "ok:jsonld")

    def test_dates_come_from_jsonld(self):
        info = self.run_info(html_page(jsonld(posting(f"<p>{LONG}</p>", datePosted="2026-10-01T09:00:00+02:00",
                                                       validThrough="2026-12-31"))))
        self.assertEqual(info["reason"], "ok:jsonld")
        self.assertEqual(info["posted"], "2026-10-01")
        self.assertEqual(info["valid_through"], "2026-12-31")

    def test_validthrough_in_the_past_is_closed(self):
        page = html_page(jsonld(posting(f"<p>{LONG}</p>", validThrough="2020-01-01")))
        info = self.run_info(page)
        self.assertEqual(info["reason"], "expired (validThrough)")
        self.assertTrue(info["closed"])
        self.assertEqual(info["text"], "")
        self.assertEqual(info["valid_through"], "2020-01-01")

    def test_validthrough_today_or_later_is_open(self):
        today = datetime.date.today().isoformat()
        self.assertEqual(self.run_fetch(html_page(jsonld(posting(f"<p>{LONG}</p>", validThrough=today))))[1],
                         "ok:jsonld")

    def test_info_and_description_agree(self):
        page = html_page(jsonld(posting(f"<p>{LONG}</p>")))
        info = self.run_info(page)
        self.assertEqual(set(info), {"text", "reason", "posted", "valid_through", "closed"})
        with self.serve(page):
            self.assertEqual(fp.fetch_description("https://careers.acme.com/job/12345", JOB),
                             (info["text"], info["reason"]))

    def test_html_fallback_prefers_main_and_drops_chrome(self):
        page = html_page(
            "<nav>MENU ITEMS</nav><header>HEADER BAR</header>"
            "<script>var junk = 1;</script><style>.x{}</style>"
            f"<main><h1>Product Owner (m/w/d)</h1><p>{LONG}</p><form>FORM LABEL</form></main>"
            "<footer>FOOTER LINKS</footer>",
            head="<script>var head = 'junk';</script>")
        text, reason = self.run_fetch(page)
        self.assertEqual(reason, "ok:html")
        self.assertIn("Wir suchen eine Product Owner Person", text)
        for junk in ("MENU ITEMS", "HEADER BAR", "junk", "FOOTER LINKS", "FORM LABEL"):
            self.assertNotIn(junk, text)

    def test_html_fallback_uses_article(self):
        page = html_page(f"<div>Cookies</div><article><h1>Product Owner</h1><p>{LONG}</p></article>")
        self.assertEqual(self.run_fetch(page)[1], "ok:html")

    def test_short_main_is_not_the_posting(self):
        page = html_page(f"<main>Product Owner</main><div><h1>Product Owner (m/w/d)</h1><p>{LONG}</p></div>")
        text, reason = self.run_fetch(page)
        self.assertEqual(reason, "ok:html")
        self.assertIn("Wir suchen", text)

    def test_long_form_is_kept_short_form_is_dropped(self):
        long_form = f"<form><p>{LONG}</p></form>"
        text, _ = self.run_fetch(html_page(f"<main><h1>Product Owner (m/w/d)</h1>{long_form}</main>"))
        self.assertIn("Wir suchen", text)
        text, _ = self.run_fetch(html_page(f"<main><h1>Product Owner (m/w/d)</h1><p>{LONG}</p>"
                                           "<form>Jetzt bewerben</form></main>"))
        self.assertNotIn("Jetzt bewerben", text)

    def test_closed_menu_is_dropped_unclosed_menu_is_kept(self):
        text, _ = self.run_fetch(html_page(
            "<header><div>Mein Profil</div></header><main><h1>Product Owner (m/w/d)</h1>"
            f"<p>{LONG}</p></main>"))
        self.assertNotIn("Mein Profil", text)
        text, _ = self.run_fetch(html_page(
            "<div><h1>Product Owner (m/w/d)</h1><nav>Mein Profil</div>"
            f"<p>{LONG}</p>"))
        self.assertIn("Mein Profil", text)

    def test_h1_inside_a_closed_header_is_kept(self):
        page = html_page("<header><h1>Product Owner (m/w/d)</h1><div>Menu</div></header>"
                         f"<div><p>{LONG}</p></div>")
        text, _ = self.run_fetch(page)
        self.assertIn("Product Owner (m/w/d)", text)
        self.assertNotIn("Menu", text)

    def test_cookie_box_is_dropped_but_body_and_lookalikes_are_not(self):
        box = "<div class='cookie-banner'>Wir verwenden Cookies " + "x " * 50 + "</div>"
        text, _ = self.run_fetch(html_page(f"<body class='cookie-consent-enabled'><main><h1>Product Owner (m/w/d)"
                                           f"</h1>{box}<p>{LONG}</p></main></body>"))
        self.assertNotIn("Wir verwenden Cookies", text)
        text, reason = self.run_fetch(html_page(
            "<div id='page-no-cookie-banner'><main><h1>Product Owner (m/w/d)</h1>"
            f"<p>{LONG}</p></main></div>"))
        self.assertEqual(reason, "ok:html")
        self.assertIn("Wir suchen", text)

    def test_meta_charset_is_used_when_header_has_none(self):
        raw = ("<html><head><meta charset='iso-8859-1'><title>Product Owner (m/w/d)</title></head><body><main>"
               "<h1>Product Owner (m/w/d)</h1><p>Für Größe Qualität Prüfung " + LONG + "</p></main></body></html>")
        with mock.patch.object(fp, "_open_page", return_value=("https://careers.acme.com/job/1", "text/html", None,
                                                               raw.encode("latin-1"))):
            info = fp.fetch_posting_info("https://careers.acme.com/job/1", JOB)
        self.assertEqual(info["reason"], "ok:html")
        self.assertIn("Größe", info["text"])

    def test_result_is_cut_to_limit(self):
        text, reason = self.run_fetch(html_page(jsonld(posting("<p>" + LONG * 40 + "</p>"))))
        self.assertEqual(reason, "ok:jsonld")
        self.assertEqual(len(text), fp.DESC_LIMIT)

    def test_lists_keep_line_breaks(self):
        self.assertEqual(fp._html_text("<ul><li>Eins</li><li>Zwei</li></ul>"), "- Eins\n- Zwei")


class Checks(FetchCase):
    def test_too_short(self):
        page = html_page(jsonld(posting("<p>Product Owner, Wien, 20 Stunden.</p>")))
        self.assertEqual(self.run_fetch(page), ("", "too short"))

    def test_no_title_match(self):
        cooking = ("Rezept fuer Gemuesesuppe mit Karotten, Zwiebeln und frischen Kraeutern. Man schneidet alles "
                   "klein und laesst es langsam kochen, bis das Gemuese weich ist. ") * 4
        page = html_page(f"<main><p>{cooking}</p></main>", title="Rezepte")
        self.assertEqual(self.run_fetch(page), ("", "no title match"))

    def test_title_words_must_be_whole_words(self):
        page = html_page(f"<main><h1>Owner of a house</h1><p>{LONG}</p></main>", title="Haus")
        self.assertEqual(self.run_fetch(page, job=BANKING), ("", "no title match"))

    def test_company_name_alone_is_not_a_match(self):
        text = "Bei Acme GmbH arbeitest du in einem kleinen Team an Werkzeugen fuer Kunden. " * 8
        self.assertEqual(self.run_fetch(html_page(f"<main><p>{text}</p></main>", title="Stellen"),
                                        job={"title": "Office Assistant", "company": "Acme GmbH"}),
                         ("", "no title match"))

    def test_title_match_in_h1_or_jsonld_or_title_tag(self):
        self.assertEqual(self.run_fetch(html_page(f"<main><h1>Product Owner Banking</h1><p>{LONG}</p></main>",
                                                  title="Stellen"), job=BANKING)[1], "ok:html")
        self.assertEqual(self.run_fetch(html_page(f"<main><p>{LONG}</p></main>", title="Product Owner Banking | Karriere"),
                                        job=BANKING)[1], "ok:html")
        self.assertEqual(self.run_fetch(html_page(jsonld(posting(f"<p>{LONG}</p>", title="Product Owner Banking")),
                                                  title="Karriere"), job=BANKING)[1], "ok:jsonld")

    def test_multi_tenant_ats_needs_the_company_on_the_page(self):
        url = "https://boards.greenhouse.io/acme/jobs/1"
        with_company = html_page(f"<main><h1>Product Owner (m/w/d)</h1><p>Acme GmbH sucht. {LONG}</p></main>",
                                 title="Stellen")
        without = html_page(f"<main><h1>Product Owner (m/w/d)</h1><p>Some other firm. {LONG}</p></main>",
                            title="Stellen")
        self.assertEqual(self.run_fetch(with_company, url=url)[1], "ok:html")
        self.assertEqual(self.run_fetch(without, url=url), ("", "no company match"))

    def test_company_word_may_be_shorter_than_the_job_record_name(self):
        url = "https://navax.jobs.personio.com/job/2457596"
        navax = {"title": "Product Owner (m/w/d)", "company": "NAVAX Consulting", "company_id": ""}
        page = html_page(f"<main><h1>Product Owner (m/w/d)</h1><p>Die NAVAX Gruppe sucht. {LONG}</p></main>",
                         title="Stellen")
        self.assertEqual(self.run_fetch(page, url=url, job=navax)[1], "ok:html")
        other = html_page(f"<main><h1>Product Owner (m/w/d)</h1><p>Another firm. {LONG}</p></main>", title="Stellen")
        self.assertEqual(self.run_fetch(other, url=url, job=navax), ("", "no company match"))

    def test_closed_page(self):
        body = ("This job is no longer available. Please browse our other open positions. " + LONG)
        self.assertEqual(self.run_fetch(html_page(jsonld(posting(f"<p>{body}</p>")))), ("", "closed phrase"))

    def test_closed_marker_after_600_chars_is_ignored(self):
        page = html_page(jsonld(posting(f"<p>{LONG * 3}</p><p>Position has been filled.</p>")))
        self.assertEqual(self.run_fetch(page)[1], "ok:jsonld")

    def test_german_closed_marker(self):
        page = html_page(f"<main><p>Diese Stelle ist bereits besetzt. {LONG}</p></main>")
        self.assertEqual(self.run_fetch(page), ("", "closed phrase"))


class ClosedPhrases(FetchCase):
    def closed(self, phrase, title="Product Owner (m/w/d) - Acme GmbH"):
        return self.run_fetch(html_page(f"<main><h1>Product Owner (m/w/d)</h1><p>{phrase}. {LONG}</p></main>",
                                        title=title))

    def test_full_closed_phrases_give_closed_phrase_without_closing(self):
        for phrase in ("Job posting has expired", "Job expired", "Stellenanzeige ist abgelaufen",
                       "This job is no longer available", "Diese Seite ist nicht mehr verfügbar",
                       "We are no longer accepting applications", "Position has been filled",
                       "Diese Stelle ist bereits besetzt", "Stelle ist vergeben", "Page not found",
                       "Seite nicht gefunden", "404 Not Found"):
            self.assertEqual(self.closed(phrase), ("", "closed phrase"), phrase)
            info = self.run_info(html_page(f"<main><h1>Product Owner (m/w/d)</h1><p>{phrase}. {LONG}</p></main>"))
            self.assertFalse(info["closed"], phrase)

    def test_live_postings_with_common_words_are_kept(self):
        # the two phrases from the live QA: a real posting, not a closed one
        self.assertEqual(self.closed("Handling of expired contracts is part of the role")[1], "ok:html")
        self.assertEqual(self.closed("This position is closed to external candidates")[1], "ok:html")
        for phrase in ("Our expired-certificate monitoring", "wir betreuen abgelaufene Konten",
                       "Abgelaufene Produkte werden entsorgt", "Job 404 Product Owner"):
            self.assertEqual(self.closed(phrase)[1], "ok:html", phrase)

    def test_phrase_in_the_title_or_h1_counts(self):
        self.assertEqual(self.closed("Some text", title="Page not found")[1], "closed phrase")
        page = html_page(f"<main><h1>Position has been filled</h1><p>{LONG}</p></main>")
        self.assertEqual(self.run_fetch(page), ("", "closed phrase"))

    def test_phrase_after_the_first_200_chars_is_ignored(self):
        page = html_page(f"<main><h1>Product Owner (m/w/d)</h1><p>{LONG}</p><p>Position has been filled.</p></main>")
        self.assertEqual(self.run_fetch(page)[1], "ok:html")


class Trim(FetchCase):
    def test_fold_umlauts_for_title_words(self):
        self.assertEqual(enrich._fold("Produktmanagerin Kapitalmärkte"), "produktmanagerin kapitalmaerkte")

    def test_start_at_title_unit(self):
        job = {"title": "Product Owner (m/w/d)", "company": "Acme GmbH"}
        self.assertEqual(fp._start_at_title("Menu\nProduct Owner\nBody", job), "Product Owner\nBody")
        self.assertEqual(fp._start_at_title("Menu\nBody", job), "Menu\nBody")

    def test_menu_above_the_title_is_dropped(self):
        menu = "".join(f"<div>{x}</div>" for x in ("Zum Hauptinhalt", "Mein Profil", "Stellenangebote",
                                                    "Jobwelt Bau", "Initiativbewerbung"))
        page = html_page(f"{menu}<div><h1>Mitarbeiter:in Product Owner</h1><p>{LONG}</p></div>",
                         title="Stellen - Acme")
        text, reason = self.run_fetch(page)
        self.assertEqual(reason, "ok:html")
        self.assertTrue(text.startswith("Mitarbeiter:in Product Owner"), text[:80])


if __name__ == "__main__":
    unittest.main()
