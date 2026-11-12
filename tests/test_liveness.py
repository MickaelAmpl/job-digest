"""Stage 9 (28.10.2026): the weekly check of the vacancies Misha marked Интересно (liveness.py). No network: pages come
from a fake fetch, or from the real opener with a fake transport behind it, so every redirect hop runs through the real
guard. Each closed rule, each refusal, the time budget and the Sunday step in run.sh are covered here."""
import contextlib
import datetime
import email
import http.client
import io
import json
import re
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import urllib.response
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot  # noqa: E402
import fetch_posting  # noqa: E402
import liveness  # noqa: E402
import store  # noqa: E402

TODAY = datetime.date(2026, 10, 28)
SMART = "https://jobs.smartrecruiters.com/Sportradar/744000144749410"
UNIQA = ("https://careers.uniqagroup.com/job/Wien-Studentischer-Mitarbeiterin-People-Transformation%2C-Governance-"
         "&-AI-%2825-30-Stunden%29/1444595733/")
PODERO = "https://careers.podero.com/jobs/8526102-entrepreneur-in-residence-intern"
LINKEDIN = "https://www.linkedin.com/jobs/view/4478547474/"


def page(text, extra=""):
    return f"<html><head>{extra}</head><body><header>menu</header><main><h1>Job</h1><p>{text}</p></main></body></html>"


class Net:
    """A fake fetch: url -> (status, final_url, content_type, body) or an exception to raise. Records every URL asked."""

    def __init__(self, pages=None):
        self.pages = pages or {}
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, url):
        with self._lock:
            self.calls.append(url)
        answer = self.pages.get(url, (200, url, "text/html", page("Wir suchen dich. Jetzt bewerben.")))
        if isinstance(answer, BaseException):
            raise answer
        return answer


class Db(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(store, "DB", Path(self.dir.name) / "data" / "state.db")
        self.patch.start()
        self.con = store.connect()

    def tearDown(self):
        self.con.close()
        self.patch.stop()
        self.dir.cleanup()

    def add(self, jid, company="Acme", title="Praktikum Marketing", url="", open_url=None, verdict="interested",
            status=None, closed=0, data=None):
        job = {"company": company, "title": title, "url": url}
        self.con.execute("insert into jobs (id, key, company, title, url, open_url, source, first_seen, verdict,"
                         " verdict_at, status, closed, data) values (?,?,?,?,?,?,'adzuna','2026-10-20T06:00:00',?,"
                         "'2026-10-21T09:00:00',?,?,?)",
                         (jid, store.dedupe_key(job) + "#" + jid, company, title, url, open_url, verdict, status, closed,
                          json.dumps(data or job, ensure_ascii=False)))
        self.con.commit()

    def row(self, jid):
        return self.con.execute("select * from jobs where id=?", (jid,)).fetchone()

    def checks(self):
        return {r["job_id"]: (r["result"], r["reason"]) for r in self.con.execute(
            "select job_id, result, reason from liveness_checks order by id")}

    def run_with(self, net, **kw):
        return liveness.run(self.con, today=TODAY, fetch=net, **kw)


class Selection(Db):
    def test_only_interested_unsent_open_rows_are_checked(self):
        self.add("a", url="https://www.acme-example.at/job/1")
        self.add("b", verdict=None, url="https://www.acme-example.at/job/2")
        self.add("c", status="applied", url="https://www.acme-example.at/job/3")
        self.add("d", closed=1, url="https://www.acme-example.at/job/4")
        self.add("e", verdict="rejected", url="https://www.acme-example.at/job/5")
        net = Net()
        res = self.run_with(net)
        self.assertEqual(list(self.checks()), ["a"])
        self.assertEqual(net.calls, ["https://www.acme-example.at/job/1"])
        self.assertEqual(res["checked"], 1)

    def test_no_url_and_every_job_board_are_unknown_without_a_request(self):
        self.add("none", url="", open_url=None)
        self.add("li", url=LINKEDIN)
        self.add("karriere", url="https://www.karriere.at/jobs/123")
        self.add("ch", url="https://www.jobs.ch/de/stellenangebote/detail/1/")
        net = Net()
        self.run_with(net)
        self.assertEqual(self.checks(), {"none": ("unknown", "нет ссылки"),
                                         "li": ("unknown", "доска объявлений - проверь сам"),
                                         "karriere": ("unknown", "доска объявлений - проверь сам"),
                                         "ch": ("unknown", "доска объявлений - проверь сам")})
        self.assertEqual(net.calls, [])

    def test_an_ats_host_is_fetched(self):
        self.add("smart", url=SMART)
        net = Net({SMART: (200, SMART, "text/html", page("Apply now"))})
        self.run_with(net)
        self.assertEqual(net.calls, [SMART])
        self.assertEqual(self.checks()["smart"], ("open", ""))

    def test_open_url_is_preferred_over_url(self):
        self.add("x", url=LINKEDIN, open_url="https://www.acme-example.at/job/9")
        net = Net()
        self.run_with(net)
        self.assertEqual(net.calls, ["https://www.acme-example.at/job/9"])

    def test_a_row_misha_reopened_is_skipped_and_never_fetched(self):
        self.add("back", url="https://www.acme-example.at/job/7",
                 data={"company": "Acme", "liveness": {"at": "2026-10-21T09:00:00", "reopened_at": "2026-10-25T10:00:00"}})
        net = Net({"https://www.acme-example.at/job/7": (404, "", "text/html", "")})
        res = self.run_with(net)
        self.assertEqual(self.checks()["back"], ("skipped", "вернул Миша"))
        self.assertEqual(net.calls, [])
        self.assertIsNone(self.row("back")["closed_by"])
        self.assertEqual(res["skipped"], 1)


class ClosedRules(Db):
    def close_by(self, answer, jid="j", url="https://www.acme-example.at/job/1"):
        self.add(jid, url=url)
        self.run_with(Net({url: answer}))
        return self.checks()[jid], self.row(jid)

    def test_http_404_and_410_close_the_job(self):
        url = "https://www.acme-example.at/job/1"
        (result, reason), r = self.close_by(urllib.error.HTTPError(url, 404, "Not Found", None, None), "j404", url)
        self.assertEqual((result, reason), ("closed", "страница не найдена (404)"))
        self.assertEqual((r["closed"], r["closed_by"], r["promote"]), (1, "liveness", 0))
        (result, reason), _ = self.close_by(urllib.error.HTTPError(url, 410, "Gone", None, None), "j410", url)
        self.assertEqual((result, reason), ("closed", "страница удалена (410)"))

    def test_expired_json_ld_validthrough_closes_and_a_future_one_does_not(self):
        past = page("Apply", ld({"@type": "JobPosting", "title": "Praktikum Marketing", "validThrough": "2026-10-01"}))
        future = page("Apply", ld({"@type": "JobPosting", "title": "Praktikum Marketing", "validThrough": "2026-12-31"}))
        self.assertEqual(self.close_by((200, "u", "text/html", past), "jpast")[0],
                         ("closed", "срок вакансии истёк (validThrough)"))
        self.assertEqual(self.close_by((200, "u", "text/html", future), "jfuture")[0], ("open", ""))

    def test_phrases_from_the_real_pages_close_the_job(self):
        cases = {
            "Sorry, this job has expired": "Sportradar, 28.10",
            "Diese Stelle wurde leider bereits besetzt": "UNIQA, 28.10",
            "Diese Stelle ist nicht mehr verfügbar": "CHECK24, 28.10",
            "Die Stelle wurde bereits vergeben.": "Stelle wurde (bereits) vergeben",
            "Es konnte leider nichts gefunden werden.": "search without result",
            "Job nicht gefunden": "job not found",
            "This position has been filled": "position filled",
            "This position is closed": "position closed",
            "No longer open for applications": "no longer open",
            "This role is no longer active": "no longer active",
            "Stellenanzeige ist abgelaufen": "CLOSED list of fetch_posting",
        }
        for i, (text, label) in enumerate(cases.items()):
            with self.subTest(label):
                url = f"https://www.acme-example.at/job/{i}"
                self.add(f"p{i}", url=url)
                self.run_with(Net({url: (200, url, "text/html", page(text))}))
                result, reason = self.checks()[f"p{i}"]
                self.assertEqual(result, "closed", text)
                self.assertIn("на странице:", reason)

    def test_plain_words_that_only_look_similar_stay_open(self):
        for i, text in enumerate(("We have expired contracts for the team.", "Wir suchen eine Stelle im Team, jetzt bewerben.",
                                  "Our apply form is closed on Sundays, call us.")):
            with self.subTest(text):
                url = f"https://www.acme-example.at/neg/{i}"
                self.add(f"n{i}", url=url)
                self.run_with(Net({url: (200, url, "text/html", page(text))}))
                self.assertEqual(self.checks()[f"n{i}"][0], "open")

    def test_a_closed_row_gets_its_reason_time_and_url_in_data(self):
        url = "https://www.acme-example.at/job/22"
        self.add("c", url=url)
        self.run_with(Net({url: (200, url, "text/html", page("Sorry, this job has expired"))}))
        r = self.row("c")
        data = json.loads(r["data"])
        self.assertEqual((r["closed"], r["closed_by"], r["promote"]), (1, "liveness", 0))
        self.assertEqual(data["liveness"]["url"], url)
        self.assertIn("expired", data["liveness"]["reason"])
        self.assertTrue(data["liveness"]["at"].startswith(datetime.date.today().isoformat()[:4]))
        self.assertEqual(data["company"], "Acme")  # the rest of the job record stays

    def test_an_open_page_is_open_and_its_row_is_untouched(self):
        self.add("o", url=PODERO)
        self.run_with(Net({PODERO: (200, PODERO, "text/html", page("Entrepreneur in residence, apply now"))}))
        self.assertEqual(self.checks()["o"], ("open", ""))
        self.assertEqual((self.row("o")["closed"], self.row("o")["closed_by"]), (0, None))


def ld(*nodes):
    return "".join(f'<script type="application/ld+json">{json.dumps(n, ensure_ascii=False)}</script>' for n in nodes)


class JsonLdMatch(Db):
    """QA minor (28.10): only the JobPosting of THIS job decides by validThrough, matched by title as fetch_posting does."""

    def test_an_expired_other_posting_on_a_listing_does_not_close_our_live_job(self):
        url = "https://www.acme-example.at/listing/1"
        body = page("Our posting", ld({"@type": "JobPosting", "title": "Praktikum Marketing (m/w/d)", "validThrough": "2026-12-31"},
                                      {"@type": "JobPosting", "title": "Sales Manager", "validThrough": "2026-10-01"}))
        self.add("live", title="Praktikum Marketing", url=url)
        self.run_with(Net({url: (200, url, "text/html", body)}))
        self.assertEqual(self.checks()["live"], ("open", ""))
        self.assertEqual(self.row("live")["closed"], 0)

    def test_our_own_expired_posting_closes_even_next_to_a_valid_other_one(self):
        url = "https://www.acme-example.at/listing/2"
        body = page("Listing", ld({"@type": "JobPosting", "title": "Praktikum Marketing", "validThrough": "2026-10-01"},
                                  {"@type": "JobPosting", "title": "Sales Manager", "validThrough": "2026-12-31"}))
        self.add("ours", title="Praktikum Marketing", url=url)
        self.run_with(Net({url: (200, url, "text/html", body)}))
        self.assertEqual(self.checks()["ours"], ("closed", "срок вакансии истёк (validThrough)"))

    def test_an_expired_posting_with_another_title_is_ignored_when_none_matches_ours(self):
        url = "https://www.acme-example.at/listing/3"
        body = page("Listing", ld({"@type": "JobPosting", "title": "Sales Manager", "validThrough": "2026-10-01"}))
        self.add("none", title="Praktikum Marketing", url=url)
        self.run_with(Net({url: (200, url, "text/html", body)}))
        self.assertEqual(self.checks()["none"], ("open", ""))

    def test_a_matching_posting_without_validthrough_is_not_expired(self):
        url = "https://www.acme-example.at/listing/4"
        body = page("Listing", ld({"@type": "JobPosting", "title": "Praktikum Marketing"}))
        self.add("open", title="Praktikum Marketing", url=url)
        self.run_with(Net({url: (200, url, "text/html", body)}))
        self.assertEqual(self.checks()["open"], ("open", ""))


class Racy:
    """A connection that changes the row right before the closing UPDATE, as a button press of Misha could (QA minor)."""

    def __init__(self, con, jid, sql):
        self.con = con
        self.jid = jid
        self.sql = sql
        self.fired = False

    def execute(self, sql, params=()):
        if not self.fired and sql.startswith(self.sql):
            self.fired = True
            self.con.execute("update jobs set status='applied', applied_at=? where id=?",
                             ("2026-10-27T10:00:00", self.jid))
        return self.con.execute(sql, params)

    def commit(self):
        return self.con.commit()

    def __getattr__(self, name):
        return getattr(self.con, name)


class RowChanged(Db):
    """QA minor (28.10): a verdict or status pressed between the fetch and the closing write is never overwritten."""

    def test_a_status_pressed_before_the_closing_write_stops_the_close(self):
        url = "https://www.acme-example.at/job/31"
        self.add("r", url=url)
        net = Net({url: (200, url, "text/html", page("Sorry, this job has expired"))})
        liveness.run(Racy(self.con, "r", "update jobs set closed=1, closed_by='liveness'"), today=TODAY, fetch=net)
        self.assertEqual(self.checks()["r"], ("skipped", "изменилось во время проверки"))
        r = self.row("r")
        self.assertEqual((r["status"], r["applied_at"], r["closed"], r["closed_by"]),
                         ("applied", "2026-10-27T10:00:00", 0, None))

    def test_a_change_during_the_fetch_is_seen_at_the_record_step(self):
        url = "https://www.acme-example.at/job/32"
        self.add("v", url=url)

        def fetch(u):  # a worker thread: its own connection, the test's one belongs to the main thread
            c = sqlite3.connect(store.DB)
            c.execute("update jobs set verdict='rejected' where id='v'")
            c.commit()
            c.close()
            raise urllib.error.HTTPError(u, 404, "Not Found", None, None)

        self.run_with(fetch)
        self.assertEqual(self.checks()["v"][0], "skipped")
        self.assertEqual((self.row("v")["verdict"], self.row("v")["closed"]), ("rejected", 0))


class Failures(Db):
    def test_refusals_timeouts_and_server_errors_are_unknown_with_a_short_reason(self):
        cases = {
            "f403": (urllib.error.HTTPError("u", 403, "Forbidden", None, None), "сайт не пустил (403)"),
            "f429": (urllib.error.HTTPError("u", 429, "Too Many", None, None), "сайт не пустил (429)"),
            "f503": (urllib.error.HTTPError("u", 503, "Unavailable", None, None), "сайт ответил с ошибкой (503)"),
            "fto": (socket.timeout("timed out"), "не открылся (таймаут)"),
            "fos": (OSError("no route"), "не открылся"),
            "fcrash": (RuntimeError("bug in a page"), "не открылся"),
        }
        for jid, (answer, reason) in cases.items():
            self.add(jid, url=f"https://www.acme-example.at/{jid}")
        net = Net({f"https://www.acme-example.at/{jid}": answer for jid, (answer, _) in cases.items()})
        res = self.run_with(net)
        for jid, (_, reason) in cases.items():
            self.assertEqual(self.checks()[jid], ("unknown", reason), jid)
        self.assertEqual(res["unknown"], len(cases))
        self.assertEqual(res["closed"], 0)  # an unknown page never closes a job


class Guards(unittest.TestCase):
    """The real opener and redirect guard (liveness.fetch_page) with a fake transport: no socket is ever opened."""

    def setUp(self):
        self.patches = [mock.patch.object(fetch_posting, "_public_host", lambda parsed: True),
                        mock.patch.object(fetch_posting, "_robots_ok", lambda url: True),
                        mock.patch.object(fetch_posting, "_sleep", lambda s: None)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    @staticmethod
    def opener(routes):
        transport = Transport(routes)
        op = urllib.request.OpenerDirector()
        for h in (liveness._HopGuard(time.monotonic() + 1e6), transport, urllib.request.HTTPErrorProcessor(),
                  urllib.request.HTTPDefaultErrorHandler(), urllib.request.UnknownHandler()):
            op.add_handler(h)
        return op, transport

    def test_a_deny_host_in_a_redirect_chain_is_refused_and_never_requested(self):
        first = "https://careers.acme-example.at/job/1"
        op, t = self.opener({first: (302, LINKEDIN, b"")})
        with self.assertRaises(fetch_posting._Refused) as ctx:
            liveness.fetch_page(first, opener=op)
        self.assertEqual(ctx.exception.reason, "доска объявлений - проверь сам")
        self.assertEqual(t.requested, [first])

    def test_a_redirect_into_a_job_board_is_refused(self):
        first = "https://careers.acme-example.at/job/2"
        op, t = self.opener({first: (302, "https://www.jobs.at/job/77", b"")})
        with self.assertRaises(fetch_posting._Refused):
            liveness.fetch_page(first, opener=op)
        self.assertEqual(t.requested, [first])

    def test_a_redirect_to_a_private_address_is_refused(self):
        first = "https://careers.acme-example.at/job/3"
        op, t = self.opener({first: (302, "http://10.1.2.3/admin", b"")})
        with self.assertRaises(fetch_posting._Refused) as ctx:
            liveness.fetch_page(first, opener=op)
        self.assertEqual(ctx.exception.reason, "адрес не публичный")
        self.assertEqual(t.requested, [first])

    def test_robots_txt_that_refuses_is_unknown(self):
        first = "https://careers.acme-example.at/job/4"
        op, t = self.opener({first: (200, b"<html>x</html>")})
        with mock.patch.object(fetch_posting, "_robots_ok", lambda url: False):
            with self.assertRaises(fetch_posting._Refused) as ctx:
                liveness.fetch_page(first, opener=op)
        self.assertEqual(ctx.exception.reason, "robots.txt запрещает")
        self.assertEqual(t.requested, [])

    def test_linkedin_as_the_first_url_is_never_requested(self):
        op, t = self.opener({})
        with self.assertRaises(fetch_posting._Refused):
            liveness.fetch_page(LINKEDIN, opener=op)
        self.assertEqual(t.requested, [])

    def test_a_404_after_a_redirect_is_an_http_error_for_the_caller_to_close_on(self):
        first = "https://careers.acme-example.at/job/5"
        final = "https://careers.acme-example.at/jobs/5-gone"
        op, t = self.opener({first: (301, final, b""), final: (404, None, b"<p>nope</p>")})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            liveness.fetch_page(first, opener=op)
        self.assertEqual(ctx.exception.code, 404)
        self.assertEqual(t.requested, [first, final])

    def test_a_real_html_page_is_read_and_a_phrase_closes_it(self):
        url = "https://careers.acme-example.at/job/6"
        op, _ = self.opener({url: (200, page("Sorry, this job has expired").encode("utf-8"))})
        status, final, ctype, body = liveness.fetch_page(url, opener=op)
        self.assertEqual((status, ctype), (200, "text/html"))
        self.assertIn("Sorry, this job has expired", body)

    def test_an_ats_page_is_fetched_through_the_guard(self):
        op, t = self.opener({SMART: (200, page("Apply").encode("utf-8"))})
        liveness.fetch_page(SMART, opener=op)
        self.assertEqual(t.requested, [SMART])


class Transport(urllib.request.BaseHandler):
    """Answers every http(s) request from routes: url -> (code, body) or (302, location, b'')."""
    handler_order = 100

    def __init__(self, routes):
        self.routes = routes
        self.requested = []

    def http_open(self, req):
        self.requested.append(req.full_url)
        answer = self.routes.get(req.full_url, (404, b""))
        if len(answer) == 3:
            code, location, body = answer
        else:
            code, body = answer
            location = None
        hdrs = http.client.HTTPMessage()
        hdrs["Content-Type"] = "text/html; charset=utf-8"
        if location:
            hdrs["Location"] = location
        resp = urllib.response.addinfourl(io.BytesIO(body), hdrs, req.full_url, code)
        resp.msg = "OK"
        return resp

    https_open = http_open


class Budget(Db):
    def test_a_zero_budget_fetches_nothing_and_marks_every_row_not_reached(self):
        self.add("a", url="https://www.acme-example.at/job/1")
        self.add("b", url="https://www.acme-example.at/job/2")
        net = Net()
        res = self.run_with(net, budget=0)
        self.assertEqual(net.calls, [])
        self.assertEqual(self.checks(), {"a": ("unknown", "не успел"), "b": ("unknown", "не успел")})
        self.assertEqual(res["unknown"], 2)

    def test_at_most_four_pages_are_in_flight(self):
        for i in range(10):
            self.add(f"w{i}", url=f"https://www.acme-example.at/w/{i}")
        lock = threading.Lock()
        state = {"now": 0, "max": 0}

        def slow(url):
            with lock:
                state["now"] += 1
                state["max"] = max(state["max"], state["now"])
            time.sleep(0.05)
            with lock:
                state["now"] -= 1
            return (200, url, "text/html", page("Apply now"))

        res = self.run_with(slow)
        self.assertLessEqual(state["max"], liveness.WORKERS)
        self.assertEqual(res["checked"], 10)
        self.assertEqual(res["open"], 10)


class Summary(Db):
    def test_main_prints_one_summary_line_and_exits_zero(self):
        self.add("a", url="https://www.acme-example.at/job/1")
        self.add("b", url=LINKEDIN)
        out = io.StringIO()
        with mock.patch.object(liveness, "fetch_page", lambda url: (200, url, "text/html", page("Apply now"))), \
                contextlib.redirect_stdout(out):
            code = liveness.main()
        self.assertEqual(code, 0)
        self.assertRegex(out.getvalue().strip(),
                         r"^liveness: checked 2, open 1, closed 0, unknown 1, skipped 0$")

    def test_liveness_checks_rows_carry_the_time_result_reason_and_url(self):
        self.add("a", url="https://www.acme-example.at/job/1")
        self.run_with(Net({"https://www.acme-example.at/job/1": (200, "u", "text/html", page("Sorry, this job has expired"))}))
        r = self.con.execute("select * from liveness_checks where job_id='a'").fetchone()
        self.assertEqual((r["result"], r["url"]), ("closed", "https://www.acme-example.at/job/1"))
        self.assertTrue(r["checked_at"])
        self.assertIn("на странице:", r["reason"])

    def test_the_table_and_its_index_exist(self):
        cols = [r[1] for r in self.con.execute("pragma table_info(liveness_checks)")]
        self.assertEqual(cols, ["id", "job_id", "checked_at", "result", "reason", "url"])
        self.assertIn("liveness_checks_at", [r[1] for r in self.con.execute("pragma index_list('liveness_checks')")])


class Interesting(Db):
    def test_closed_rows_leave_the_interesting_list(self):
        self.add("open", company="Openco", title="Analyst", url="https://www.acme-example.at/job/1")
        self.add("gone", company="Goneco", title="Designer", url="https://www.acme-example.at/job/2")
        self.run_with(Net({"https://www.acme-example.at/job/1": (200, "u", "text/html", page("Apply")),
                           "https://www.acme-example.at/job/2": urllib.error.HTTPError("u", 404, "Not Found", None, None)}))
        lines, _ = bot.interesting_list(self.con)
        text = " ".join(lines)
        self.assertIn("Openco", text)
        self.assertNotIn("Goneco", text)


class RunSh(unittest.TestCase):
    def test_sunday_only_step_after_score_and_before_health_finish(self):
        text = (ROOT / "run.sh").read_text(encoding="utf-8")
        self.assertEqual(subprocess_check(["bash", "-n", str(ROOT / "run.sh")]), 0)
        block = ('if [ "$(date +%u)" = 7 ]; then\n'
                 '      step --timeout 660 python3 liveness.py || true\n'
                 '    fi')
        self.assertIn(block, text)
        self.assertLess(text.index("step python3 score.py"), text.index(block))
        self.assertLess(text.index(block), text.index("python3 health.py finish"))


def subprocess_check(cmd):
    import subprocess
    return subprocess.run(cmd, capture_output=True).returncode


if __name__ == "__main__":
    unittest.main()
