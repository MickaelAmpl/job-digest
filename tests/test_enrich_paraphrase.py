"""Stage 7 step 1 (16.10.2026): enrich takes the posting text from our own fetch, keeps the facts a search read as
a paraphrase (never as the description), records a closed or dated posting even when no text is found; score caps
an old posting; the digest card says '(пересказ)' for a paraphrase. No network, no claude: subprocess and the
fetch_posting module are fakes."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import digest  # noqa: E402
import enrich  # noqa: E402
import score  # noqa: E402
import store  # noqa: E402

POSTING = "Wir suchen eine:n Product Owner. Aufgaben: Backlog, Stakeholder, Roadmap. " * 5
JOB = {"id": "e1", "title": "Product Owner", "company": "TechTalk", "location": "Wien", "description": "",
       "enrich_tries": 0}
TRUSTED = "https://www.techtalk.at/jobs/1234567"  # company label 'techtalk' + a job number: trusted when official
SCORE = {"score": 70, "why_ru": "ok", "german": "none", "hours": "part-time", "level": "junior", "skill_gaps": []}


class FakeFetch:
    """Stands in for fetch_posting: returns the queued (text, reason) results and records the URLs asked for."""

    def __init__(self, *results):
        self.results, self.urls = list(results), []

    def fetch_description(self, url, job, timeout=20):
        self.urls.append(url)
        return self.results.pop(0) if self.results else ("", "fail: none queued")

    def module(self):
        return types.SimpleNamespace(fetch_description=self.fetch_description)


def apply(j, answer, *fetch_results):
    """What the worker does (fetch_page when found), then the pure apply_result."""
    f = FakeFetch(*fetch_results)
    with mock.patch.dict(sys.modules, {"fetch_posting": f.module()}):
        fetched = enrich.fetch_page(j, answer) if answer.get("found") else None
    return enrich.apply_result(j, answer, fetched), f.urls


def days_ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


class ApplyResult(unittest.TestCase):
    def test_closed_when_not_found_and_the_answer_names_a_link(self):
        """Raiffeisen Digital Bank case: 'closed' was lost when the text was not found."""
        j = dict(JOB)
        self.assertEqual(apply(j, {"found": False, "still_open": "no",
                                   "url": "https://www.karriere.at/jobs/7654321"})[0], (False, True))
        self.assertTrue(j["closed"])
        self.assertFalse(j.get("description"))

    def test_closure_without_any_link_is_ignored(self):
        """Item 3 (QA): 'closed' with found=false and no link is no evidence."""
        j = dict(JOB)
        self.assertEqual(apply(j, {"found": False, "still_open": "no"})[0], (False, False))
        self.assertNotIn("closed", j)

    def test_closed_when_found_but_no_text_and_no_facts(self):
        j = dict(JOB)
        out, _ = apply(j, {"found": True, "still_open": "no", "url": TRUSTED, "official": True, "facts": {}},
                       ("", "fail: 404"))
        self.assertEqual(out, (False, True))
        self.assertTrue(j["closed"])

    def test_still_open_unknown_is_not_closed(self):
        j = dict(JOB)
        apply(j, {"found": False, "still_open": "unknown"})
        self.assertNotIn("closed", j)

    def test_posted_from_the_page_when_the_job_has_none(self):
        j = dict(JOB)
        apply(j, {"found": False, "posted": days_ago(5)})
        self.assertEqual(j["posted"], days_ago(5))

    def test_posted_from_the_source_is_not_overwritten(self):
        j = dict(JOB, posted="2026-01-05")
        apply(j, {"found": False, "posted": days_ago(5)})
        self.assertEqual(j["posted"], "2026-01-05")

    def test_invalid_or_future_posted_is_ignored(self):
        for value in ("", "30.09.2026", "2026-09-30T10:00", "yesterday", days_ago(-3), None):
            j = dict(JOB)
            apply(j, {"found": False, "posted": value})
            self.assertNotIn("posted", j, repr(value))

    def test_page_text_becomes_the_description(self):
        text = POSTING * 3
        j = dict(JOB)
        out, urls = apply(j, {"found": True, "url": TRUSTED, "official": True, "facts": {},
                              "summary_ru": "Роль: PO\nhttps://evil.example"}, (text, "ok: 200"))
        self.assertEqual(out, (True, False))
        self.assertEqual(j["description"], text.strip())
        self.assertEqual(j["description_source"], "page")
        self.assertEqual(j["original_url"], TRUSTED)
        self.assertNotIn("evil.example", j["summary_ru"])
        self.assertEqual(urls, [TRUSTED])

    def test_page_text_must_pass_the_posting_check(self):
        """A short or note-like page text is not a posting, even when the fetch says ok."""
        j = dict(JOB)
        out, _ = apply(j, {"found": True, "url": TRUSTED, "official": True, "facts": {}}, ("too short", "ok: 200"))
        self.assertEqual(out, (False, False))
        self.assertFalse(j.get("description"))

    def test_failed_fetch_with_facts_gives_a_paraphrase(self):
        j = dict(JOB)
        facts = {"tasks": ["Backlog pflegen"], "requirements": ["Deutsch B1", "Erfahrung"], "german": "B1",
                 "hours": "19,25", "salary": "2 745 EUR"}
        out, _ = apply(j, {"found": True, "url": TRUSTED, "official": True, "facts": facts,
                           "summary_ru": "- Роль: PO\n- Немецкий B1"}, ("", "fail: 404"))
        self.assertEqual(out, (True, False))
        self.assertEqual(j["description_source"], "paraphrase")
        self.assertEqual(j["paraphrase"]["german"], "B1")
        self.assertEqual(j["paraphrase"]["requirements"], ["Deutsch B1", "Erfahrung"])
        self.assertEqual(j["summary_ru"], "- Роль: PO\n- Немецкий B1")

    def test_paraphrase_is_never_written_into_description(self):
        j = dict(JOB)
        apply(j, {"found": True, "url": TRUSTED, "official": True,
                  "facts": {"tasks": ["Backlog pflegen", "Roadmap mitgestalten"]}, "description": POSTING * 5}, ("", "fail: 404"))
        self.assertEqual(j.get("description", ""), "")
        self.assertEqual(j["description_source"], "paraphrase")

    def test_no_text_and_no_facts_is_not_found(self):
        j = dict(JOB)
        out, _ = apply(j, {"found": True, "url": TRUSTED, "official": True, "facts": {}}, ("", "fail: 404"))
        self.assertEqual(out, (False, False))
        self.assertNotIn("paraphrase", j)
        self.assertNotIn("description_source", j)
        self.assertNotIn("summary_ru", j)

    def test_one_fact_is_not_enough_for_a_paraphrase(self):
        j = dict(JOB)
        out, _ = apply(j, {"found": True, "facts": {"tasks": ["Backlog"], "german": "B2", "salary": "2000"}},
                       ("", "fail: 404"))
        self.assertEqual(out, (False, False))
        self.assertNotIn("paraphrase", j)

    def test_two_facts_from_requirements_and_tasks_count_together(self):
        j = dict(JOB)
        out, _ = apply(j, {"found": True, "facts": {"tasks": ["Backlog pflegen"], "requirements": ["Erfahrung im Team"]}},
                       ("", "fail: 404"))
        self.assertEqual(out, (True, False))

    def test_facts_are_capped(self):
        j = dict(JOB)
        big = ["x" * 1000 for _ in range(20)]
        apply(j, {"found": True, "facts": {"tasks": big, "requirements": ["r"], "location": "y" * 1000}},
              ("", "fail: 404"))
        self.assertEqual(len(j["paraphrase"]["tasks"]), enrich.FACT_ITEMS)
        self.assertTrue(all(len(t) <= enrich.FACT_CHARS for t in j["paraphrase"]["tasks"]))
        self.assertEqual(len(j["paraphrase"]["location"]), enrich.FACT_CHARS)

    def test_fetch_exception_is_no_text(self):
        j = dict(JOB)
        answer = {"found": True, "url": TRUSTED, "official": True, "facts": {"tasks": ["Backlog pflegen", "Roadmap mitgestalten"]}}
        fetch = mock.Mock(side_effect=RuntimeError("boom"))
        with mock.patch.dict(sys.modules, {"fetch_posting": types.SimpleNamespace(fetch_description=fetch)}):
            fetched = enrich.fetch_page(j, answer)
        self.assertEqual(fetched["text"], "")
        out = enrich.apply_result(j, answer, fetched)
        self.assertEqual(out, (True, False))
        self.assertEqual(j["description_source"], "paraphrase")

    def test_trusted_link_is_the_one_fetched(self):
        j = dict(JOB)
        _, urls = apply(j, {"found": True, "url": TRUSTED, "official": True, "facts": {}}, (POSTING * 3, "ok: 200"))
        self.assertEqual(urls, [TRUSTED])

    def test_untrusted_reported_link_is_passed_to_the_fetch_module_only(self):
        """No button for it, and the fetch module (company sites only) decides whether it may open it."""
        j = dict(JOB)
        reported = "https://evil.example/apply/1234567"
        _, urls = apply(j, {"found": True, "url": reported, "official": False, "facts": {}})
        self.assertEqual(urls, [reported])
        self.assertNotIn("original_url", j)

    def test_missing_fetch_module_still_gives_the_paraphrase(self):
        j = dict(JOB)
        answer = {"found": True, "url": TRUSTED, "official": True, "facts": {"tasks": ["Backlog pflegen", "Roadmap mitgestalten"]}}
        with mock.patch.dict(sys.modules, {"fetch_posting": None}), contextlib.redirect_stderr(io.StringIO()):
            fetched = enrich.fetch_page(j, answer)
        out = enrich.apply_result(j, answer, fetched)
        self.assertEqual(out, (True, False))
        self.assertEqual(j["description_source"], "paraphrase")

    def test_apply_result_makes_no_network_call(self):
        """The page is read in the worker thread; apply_result only folds the answer in."""
        j = dict(JOB)
        with mock.patch.dict(sys.modules, {"fetch_posting": None}):  # any import of the fetch module would fail
            out = enrich.apply_result(j, {"found": True, "url": TRUSTED, "official": True,
                                          "facts": {"tasks": ["Backlog pflegen", "Roadmap mitgestalten"]}})
        self.assertEqual(out, (True, False))

    def test_no_url_means_no_fetch(self):
        j = dict(JOB)
        _, urls = apply(j, {"found": True, "url": "", "facts": {"tasks": ["Backlog pflegen", "Roadmap mitgestalten"]}})
        self.assertEqual(urls, [])
        self.assertEqual(j["description_source"], "paraphrase")


LONG = {"tasks": ["Backlog pflegen", "Roadmap mitgestalten"]}


class StrictAnswer(unittest.TestCase):
    """QA of 16.10 (items 1-5): strict parsing of the model's answer, closure evidence, link stripping, page closure."""

    def test_found_counts_only_true_or_the_text_true(self):
        for v in (True, "true", "TRUE", " True "):
            j = dict(JOB)
            self.assertEqual(enrich.apply_result(j, {"found": v, "facts": LONG}), (True, False), repr(v))
        for v in ("false", "no", "yes", 1, 0, "", None, ["true"]):
            j = dict(JOB)
            self.assertEqual(enrich.apply_result(j, {"found": v, "facts": LONG}), (False, False), repr(v))
            self.assertNotIn("paraphrase", j, repr(v))

    def test_still_open_parser(self):
        url = "https://www.karriere.at/jobs/7654321"
        for v in ("No", "no", " closed ", "expired", False):
            j = dict(JOB)
            enrich.apply_result(j, {"found": False, "still_open": v, "url": url})
            self.assertTrue(j.get("closed"), repr(v))
        for v in ("yes", True, ["no"], "maybe", None):
            j = dict(JOB)
            enrich.apply_result(j, {"found": False, "still_open": v, "url": url})
            self.assertNotIn("closed", j, repr(v))

    def test_closure_with_found_false_needs_a_usable_link(self):
        for url in ("", "   ", "javascript:alert(1)", "not a link"):
            j = dict(JOB)
            self.assertEqual(enrich.apply_result(j, {"found": False, "still_open": "no", "url": url}), (False, False), url)
            self.assertNotIn("closed", j, url)

    def test_closure_with_found_false_needs_a_board_ats_or_company_domain(self):
        """Round 2: any URL is no evidence; only a job board, an ATS on that list, or the company's own domain."""
        for url in ("https://evil.example/jobs/1234567", "https://www.wikipedia.org/wiki/Product_owner"):
            j = dict(JOB)
            enrich.apply_result(j, {"found": False, "still_open": "no", "url": url})
            self.assertNotIn("closed", j, url)
        j = dict(JOB)  # the company's own domain: the official link of the company's name
        enrich.apply_result(j, {"found": False, "still_open": "no", "url": "https://careers.techtalk.at/jobs/7654321",
                                "official": True})
        self.assertTrue(j.get("closed"))
        j = dict(JOB)  # a board
        enrich.apply_result(j, {"found": False, "still_open": "no", "url": "https://www.jobs.at/job/7654321"})
        self.assertTrue(j.get("closed"))
        j = dict(JOB)  # an ATS portal (on the board list)
        enrich.apply_result(j, {"found": False, "still_open": "no",
                                "url": "https://boards.greenhouse.io/techtalk/jobs/7654321"})
        self.assertTrue(j.get("closed"))

    def test_prompt_asks_for_the_closure_page(self):
        """Live test: the model said 'no' with url '' while it had seen the 410 page."""
        self.assertIn('If still_open is "no", url must be the exact page that says the vacancy is closed, '
                      'expired or removed (also when found is false).', enrich.PROMPT)

    def test_tech_names_stay_and_links_go(self):
        """Round 2: Node.js, Vue.js and ASP.NET are kept; links, handles, mail and bare domains are not."""
        j = dict(JOB)
        enrich.apply_result(j, {"found": True, "facts": LONG,
                                "summary_ru": "Node.js, Vue.js и ASP.NET. Ссылки: evil.com/login, example.at, "
                                              "t.me/x, hr@acme.at, https://x.example/z"})
        for kept in ("Node.js", "Vue.js", "ASP.NET"):
            self.assertIn(kept, j["summary_ru"])
        for gone in ("evil.com", "example.at", "t.me", "hr@acme", "x.example", "https"):
            self.assertNotIn(gone, j["summary_ru"], gone)

    def test_summary_loses_bare_domains_handles_and_mail_addresses(self):
        j = dict(JOB)
        enrich.apply_result(j, {"found": True, "facts": LONG,
                                "summary_ru": "Подача: evil.com/login и www.evil.at, t.me/evil_jobs, "
                                              "hr@acme.at, https://x.example/z, Роль: PO."})
        for token in ("evil", "t.me", "acme.at", "hr@", "http", "www", "x.example"):
            self.assertNotIn(token, j["summary_ru"], token)
        self.assertIn("Роль: PO", j["summary_ru"])

    def test_page_that_says_closed_closes_the_job(self):
        for reason in ("http 410", "expired (validThrough)"):
            j = dict(JOB)
            out = enrich.apply_result(j, {"found": True, "facts": LONG, "url": ""},
                                      {"text": "", "reason": reason, "posted": "", "closed": True})
            self.assertEqual(out, (True, True), reason)
            self.assertTrue(j["closed"], reason)

    def test_closed_phrase_is_only_no_text(self):
        """Round 2: 'closed phrase' means just no text; the paraphrase still applies, no closure."""
        j = dict(JOB)
        out = enrich.apply_result(j, {"found": True, "facts": LONG, "url": ""},
                                  {"text": "", "reason": "closed phrase", "posted": "", "closed": False})
        self.assertEqual(out, (True, False))
        self.assertNotIn("closed", j)
        self.assertEqual(j["description_source"], "paraphrase")

    def test_page_posted_beats_the_models_and_the_source_wins(self):
        j = dict(JOB)
        enrich.apply_result(j, {"found": True, "facts": LONG, "posted": days_ago(3)},
                            {"text": "", "reason": "fail: 404", "posted": days_ago(10)})
        self.assertEqual(j["posted"], days_ago(10))
        j = dict(JOB, posted="2026-01-05")
        enrich.apply_result(j, {"found": True, "facts": LONG}, {"text": "", "reason": "x", "posted": days_ago(10)})
        self.assertEqual(j["posted"], "2026-01-05")

    def test_fetch_posting_info_is_used_when_the_module_has_it(self):
        info = mock.Mock(return_value={"text": POSTING * 3, "reason": "ok:jsonld", "posted": days_ago(10),
                                       "valid_through": ""})
        boom = mock.Mock(side_effect=AssertionError("fetch_description must not be used"))
        module = types.SimpleNamespace(fetch_posting_info=info, fetch_description=boom)
        with mock.patch.dict(sys.modules, {"fetch_posting": module}):
            fetched = enrich.fetch_page(dict(JOB), {"found": True, "url": TRUSTED, "official": True})
        self.assertEqual(fetched["text"], (POSTING * 3).strip())
        self.assertEqual((fetched["reason"], fetched["posted"]), ("ok:jsonld", days_ago(10)))
        info.assert_called_once()
        boom.assert_not_called()

    def test_fetch_posting_info_closed_page(self):
        info = mock.Mock(return_value={"text": "", "reason": "http 410", "posted": "", "closed": True})
        with mock.patch.dict(sys.modules, {"fetch_posting": types.SimpleNamespace(fetch_posting_info=info)}):
            fetched = enrich.fetch_page(dict(JOB), {"found": True, "url": TRUSTED, "official": True})
        self.assertEqual(fetched["reason"], "http 410")
        self.assertTrue(fetched["closed"])
        j = dict(JOB)
        self.assertEqual(enrich.apply_result(j, {"found": True, "facts": LONG}, fetched), (True, True))

    def test_facts_need_two_items_of_eight_characters(self):
        for facts in ({"tasks": ["Backlog"], "requirements": ["kurz", "auch"]},   # short items
                      {"tasks": "Backlog pflegen und Roadmap"},                   # one string = one item
                      {"tasks": [{"a": "Backlog pflegen"}], "requirements": [["Erfahrung im Team"]]},  # wrong shapes
                      {"requirements": ["Erfahrung im Team"], "german": "B2"}):
            j = dict(JOB)
            self.assertEqual(enrich.apply_result(j, {"found": True, "facts": facts}), (False, False), repr(facts))
        j = dict(JOB)
        self.assertEqual(enrich.apply_result(j, {"found": True, "facts": {"tasks": "Backlog pflegen und Roadmap",
                                                                          "requirements": ["Erfahrung im Team"]}}),
                         (True, False))


class DirectPage(unittest.TestCase):
    """The job's own link is read before any search when fetch_posting allows it (Uni Wien, company feeds)."""

    UNI = "https://jobs.careercenter.univie.ac.at/job/7654321"

    def run_main(self, jobs, info, allowed=lambda url, job: True, find=None):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "data").mkdir()
            (d / "out").mkdir()
            (d / "out" / "to_score.json").write_text(json.dumps(jobs))
            fake = types.SimpleNamespace(allowed=allowed, fetch_posting_info=info)
            search = mock.Mock(side_effect=find or (lambda j: {"found": False, "still_open": "unknown"}))
            with mock.patch.object(store, "DB", d / "data" / "state.db"), mock.patch.object(store, "OUT", d / "out"), \
                    mock.patch.object(enrich, "PATH", d / "out" / "to_score.json"), \
                    mock.patch.object(enrich, "find_original", search), \
                    mock.patch.dict(sys.modules, {"fetch_posting": fake}), mock.patch.object(enrich.health, "record") as rec, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                enrich.main()
            saved = {j["id"]: j for j in json.loads((d / "out" / "to_score.json").read_text())}
        return saved, search, rec.call_args.kwargs

    def test_own_page_gives_the_posting_and_no_search_runs(self):
        info = mock.Mock(return_value={"text": POSTING * 3, "reason": "ok:jsonld", "posted": days_ago(4),
                                       "closed": False})
        saved, search, stats = self.run_main([dict(JOB, id="u1", url=self.UNI)], info)
        search.assert_not_called()
        j = saved["u1"]
        self.assertEqual((j["description_source"], j["posted"]), ("page", days_ago(4)))
        self.assertEqual(j["enrich_tries"], 0)  # a direct read is not a search try
        self.assertEqual((stats["direct"], stats["found_page"], stats["not_found"]), (1, 1, 0))

    def test_linkedin_link_is_never_read_directly(self):
        info = mock.Mock(return_value={"text": POSTING * 3, "reason": "ok:html", "posted": "", "closed": False})
        saved, search, stats = self.run_main([dict(JOB, id="l1", url="https://www.linkedin.com/jobs/view/4012345678/")],
                                             info, allowed=lambda url, job: "linkedin" not in url)
        info.assert_not_called()
        search.assert_called_once()

    def test_failed_direct_read_runs_the_search(self):
        info = mock.Mock(return_value={"text": "", "reason": "http 404", "posted": "", "closed": False})
        saved, search, stats = self.run_main([dict(JOB, id="f1", url=self.UNI)], info)
        search.assert_called_once()
        self.assertEqual(stats["direct"], 0)

    def test_closed_own_page_closes_the_job_without_a_search(self):
        info = mock.Mock(return_value={"text": "", "reason": "http 410", "posted": "", "closed": True})
        saved, search, stats = self.run_main([dict(JOB, id="c1", url=self.UNI)], info)
        search.assert_not_called()
        self.assertTrue(saved["c1"]["closed"])
        self.assertEqual(stats["closed"], 1)

    def test_direct_page_uses_open_url_too(self):
        info = mock.Mock(return_value={"text": POSTING * 3, "reason": "ok:html", "posted": "", "closed": False})
        saved, search, stats = self.run_main([dict(JOB, id="o1", url="", open_url=self.UNI)], info)
        search.assert_not_called()
        self.assertEqual(saved["o1"]["description_source"], "page")


class FetchRefusal(unittest.TestCase):
    """Item 4 of stage 7: fetch_posting refuses LinkedIn, the StepStone click links and the job boards before any
    network call. The real module is used; urllib is patched to fail if it is ever asked for a page."""

    def test_banned_hosts_are_refused_without_a_request(self):
        import fetch_posting
        boom = mock.Mock(side_effect=AssertionError("network used"))
        job = {"title": "Product Owner", "company": "TechTalk", "company_id": ""}
        cases = {"https://www.linkedin.com/jobs/view/4012345678/": "banned host",
                 "https://click.stepstone.at/track?x=1": "banned host",
                 "https://www.stepstone.at/jobs/product-owner/1234567.html": "banned host",
                 "https://www.karriere.at/jobs/7654321": "banned host"}
        with mock.patch.object(fetch_posting.urllib.request, "build_opener", boom), \
                mock.patch.object(fetch_posting.urllib.request, "urlopen", boom):
            for url, reason in cases.items():
                self.assertEqual(fetch_posting.fetch_description(url, job), ("", reason), url)
            # through enrich: the search answer names LinkedIn; the worker's fetch returns no text
            fetched = enrich.fetch_page(dict(job), {"found": True, "url": "https://www.linkedin.com/jobs/view/4012345678/",
                                                    "official": True, "facts": {}})
        self.assertEqual(fetched["text"], "")
        boom.assert_not_called()


class EnrichStats(unittest.TestCase):
    """enrich.main on four jobs: a page, a paraphrase, a closed one and a not found one."""

    ANSWERS = {
        "Page Job": {"found": True, "url": TRUSTED, "official": True, "still_open": "yes", "facts": {},
                     "summary_ru": "Роль: PO"},
        "Para Job": {"found": True, "url": "", "still_open": "unknown", "facts": {"tasks": ["Backlog pflegen", "Roadmap mitgestalten"]},
                     "summary_ru": "Роль: PO"},
        "Closed Job": {"found": False, "still_open": "no", "url": "https://www.karriere.at/jobs/7654321"},
        "Nothing Job": {"found": False, "still_open": "unknown"},
    }

    def test_stats_and_saved_file(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "data").mkdir()
            (d / "out").mkdir()
            jobs = [dict(JOB, id=t.replace(" ", "-"), title=t) for t in self.ANSWERS]
            (d / "out" / "to_score.json").write_text(json.dumps(jobs))

            def run(cmd, **kw):
                prompt = cmd[2]
                answer = next(v for k, v in self.ANSWERS.items() if f'"title": "{k}"' in prompt)
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"is_error": False,
                                                                       "result": json.dumps(answer)}), "")

            def fetch(url, job, timeout=20):
                return (POSTING * 3, "ok: 200") if job["title"] == "Page Job" else ("", "fail: 404")
            fake = types.SimpleNamespace(fetch_description=fetch)
            with mock.patch.object(store, "DB", d / "data" / "state.db"), mock.patch.object(store, "OUT", d / "out"), \
                    mock.patch.object(enrich, "PATH", d / "out" / "to_score.json"), \
                    mock.patch.object(enrich.subprocess, "run", run), \
                    mock.patch.dict(sys.modules, {"fetch_posting": fake}), \
                    mock.patch.object(enrich.health, "record") as record, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                enrich.main()
            stats = record.call_args.kwargs
            saved = {j["title"]: j for j in json.loads((d / "out" / "to_score.json").read_text())}

        self.assertEqual(stats["todo"], 4)
        self.assertEqual((stats["found"], stats["found_page"], stats["found_paraphrase"]), (2, 1, 1))
        self.assertEqual((stats["not_found"], stats["closed"], stats["failed"]), (2, 1, 0))
        self.assertEqual(saved["Page Job"]["description_source"], "page")
        self.assertEqual(saved["Para Job"]["description_source"], "paraphrase")
        self.assertEqual(saved["Para Job"].get("description", ""), "")
        self.assertTrue(saved["Closed Job"]["closed"])
        self.assertNotIn("closed", saved["Nothing Job"])


class ScoreFields(unittest.TestCase):
    def test_paraphrase_and_source_go_to_claude_and_the_rules_say_so(self):
        self.assertIn("paraphrase", score.FIELDS)
        self.assertIn("description_source", score.FIELDS)
        self.assertIn("`paraphrase`", score.RULES)
        self.assertIn("45 days", score.RULES)

    def test_paraphrase_counts_as_a_description_for_the_german_cap(self):
        s = {"score": 80, "why_ru": "ok", "german": "ad_in_german"}
        self.assertEqual(score.bounds({"description": "", "paraphrase": {"tasks": ["a"]}}, s)["score"], 80)
        self.assertEqual(score.bounds({"description": ""}, s)["score"], 55)

    def test_rfc_2822_and_german_dates_are_read_too(self):
        """Item 6 (QA): the sources also write 'Thu, 01 Oct 2026 10:00:00 +0000' and '01.10.2026'."""
        from email.utils import format_datetime
        from datetime import datetime
        d = date.today() - timedelta(days=46)
        for posted in (format_datetime(datetime(d.year, d.month, d.day, 10)), d.strftime("%d.%m.%Y")):
            s = score.bounds({"description": "x" * 200, "posted": posted}, {"score": 80, "why_ru": "Подходит"})
            self.assertLessEqual(s["score"], 30, posted)
            self.assertTrue(s["why_ru"].startswith("Старое объявление"), posted)
        d = date.today() - timedelta(days=44)
        s = {"score": 80, "why_ru": "Подходит"}
        for posted in (format_datetime(datetime(d.year, d.month, d.day, 10)), d.strftime("%d.%m.%Y")):
            self.assertEqual(score.bounds({"description": "x" * 200, "posted": posted}, s), s, posted)

    def test_posting_46_days_old_is_capped_with_the_russian_prefix(self):
        posted = days_ago(46)
        s = score.bounds({"description": "x" * 200, "posted": posted}, {"score": 80, "why_ru": "Подходит"})
        d = date.today() - timedelta(days=46)
        self.assertLessEqual(s["score"], 30)
        self.assertEqual(s["why_ru"], f"Старое объявление (опубликовано {d:%d.%m.%Y}). Подходит")

    def test_posting_44_days_old_is_unchanged(self):
        s = {"score": 80, "why_ru": "Подходит"}
        self.assertEqual(score.bounds({"description": "x" * 200, "posted": days_ago(44)}, s), s)

    def test_no_date_is_unchanged(self):
        s = {"score": 80, "why_ru": "Подходит"}
        self.assertEqual(score.bounds({"description": "x" * 200, "posted": "soon"}, s), s)


class Saved(unittest.TestCase):
    """score.save and the digest card read the same row; a scratch DB is enough."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "data").mkdir()
        self.patches = [mock.patch.object(store, "DB", d / "data" / "state.db"),
                        mock.patch.object(store, "OUT", d)]
        for p in self.patches:
            p.start()
        self.con = store.connect()

    def tearDown(self):
        self.con.close()
        for p in reversed(self.patches):
            p.stop()
        self.dir.cleanup()

    def add(self, jid, **cols):
        cols = {"id": jid, "key": f"ACME|PM|{jid}", "company": "ACME", "title": "Product Manager", "url": "",
                "source": "linkedin", "first_seen": store.now(), "data": json.dumps({"title": "Product Manager"}),
                **cols}
        self.con.execute(f"insert into jobs ({','.join(cols)}) values ({','.join('?' * len(cols))})",
                         list(cols.values()))
        self.con.commit()

    def test_has_desc_is_2_for_a_paraphrase_only_job(self):
        self.add("p")
        self.add("q")
        self.add("t")
        score.save(self.con, [{"id": "p", "title": "PM", "paraphrase": {"tasks": ["Backlog pflegen", "Roadmap mitgestalten"]}}], [dict(SCORE, id="p")])
        score.save(self.con, [{"id": "q", "title": "PM", "description": "x" * 300}], [dict(SCORE, id="q")])
        score.save(self.con, [{"id": "t", "title": "PM"}], [dict(SCORE, id="t")])
        got = {r["id"]: r["has_desc"] for r in self.con.execute("select id, has_desc from jobs")}
        self.assertEqual(got, {"p": 2, "q": 1, "t": 0})

    def test_card_for_a_paraphrase_says_so_and_is_not_title_only(self):
        self.add("p", has_desc=2, score_json=json.dumps(SCORE), scored_at=store.now(), rule="keep", score=70,
                 summary="- Роль: PO")
        job, sc, _ = digest.card_item(self.con.execute("select * from jobs where id='p'").fetchone())
        self.assertFalse(job["title_only"])
        self.assertEqual(job["description_source"], "paraphrase")
        text, _ = digest.job_message(job, sc)
        self.assertIn("<b>О вакансии (пересказ)</b>", text)

    def test_unscored_fallback_on_an_old_posting_is_capped_too(self):
        """Item 2: with Claude down the fallback 55 must not stay on a 46-day-old posting."""
        job = {"id": "s1", "title": "PM", "company": "ACME", "description": "x" * 200, "posted": days_ago(46)}
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "scoring").mkdir()
            (d / "scoring" / "profile.md").write_text("profile", encoding="utf-8")
            (d / "out").mkdir()
            self.add("s1", title="PM")
            (d / "out" / "to_score.json").write_text(json.dumps([job]))
            with mock.patch.object(store, "OUT", d / "out"), mock.patch.object(score, "ROOT", d), \
                    mock.patch.object(score, "ask_claude", side_effect=RuntimeError("login")), \
                    mock.patch.object(score.health, "record"), contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                score.main()
        row = self.con.execute("select score, why, score_tries from jobs where id='s1'").fetchone()
        self.assertEqual(row["score"], 30)
        self.assertTrue(row["why"].startswith("Старое объявление"))
        self.assertEqual(row["score_tries"], 1)  # still a fallback: queued again

    def test_paraphrase_card_without_summary_says_something_from_the_facts(self):
        """Item 7 (QA): an empty summary_ru on a paraphrase card is replaced by a line built from the facts."""
        job = {"id": "x", "title": "PO", "company": "A", "description_source": "paraphrase",
               "paraphrase": {"german": "B2", "hours": "20 h", "level": "junior", "tasks": ["Backlog pflegen"]}}
        text, _ = digest.job_message(job, SCORE)
        self.assertIn("<b>О вакансии (пересказ)</b>", text)
        self.assertIn("Немецкий B2, часы: 20 h, уровень: junior", text)
        self.assertEqual(digest.facts_line({"german": "unknown", "hours": "unknown"}), "")

    def test_card_lifted_by_the_hours_rescore_says_so(self):
        job = {"id": "x", "title": "PO", "company": "A", "summary_ru": "Роль: PO", "promoted": 3}
        text, _ = digest.job_message(job, SCORE)
        self.assertIn("Пересчитал по новому правилу", text)
        self.assertNotIn("Вчера была в списке отсеянных", text)

    def test_card_with_a_page_text_has_the_plain_heading(self):
        job = {"id": "x", "title": "PO", "company": "A", "summary_ru": "Роль: PO", "description_source": "page"}
        text, _ = digest.job_message(job, SCORE)
        self.assertIn("<b>О вакансии</b>", text)
        self.assertNotIn("(пересказ)", text)
        text, _ = digest.job_message(dict(job, description_source="paraphrase"), SCORE)
        self.assertIn("<b>О вакансии (пересказ)</b>", text)


if __name__ == "__main__":
    unittest.main()
