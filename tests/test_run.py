"""Task 5: the orchestrator end to end with a fake claude, a fake renderer and a fake poppler.
Library mode (09.10.2026): two Claude calls per run (requirements + selection), no verifier."""
import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import store  # noqa: E402
from cv.tailor import library, llm, pdfcheck, run  # noqa: E402
from tests.fakes import FakeRun, envelope  # noqa: E402

BASE = json.loads((ROOT / "cv" / "source" / "content" / "en.json").read_text(encoding="utf-8"))
LIB = json.loads(library.LIBRARY.read_text(encoding="utf-8"))
AD = ("We are looking for a Junior Product Owner (20 h/week) to manage the backlog in Jira, write product specs "
      "and run market research. English required, German a plus. Minimum salary EUR 2,800 gross/month for "
      "38.5 h/week (full-time), overpayment possible. ") * 2
REQS = {"must": ["backlog management", "Jira"], "nice": ["German"], "terms": ["Jira", "backlog", "market research"],
        "ad_language": "en", "min_salary": "EUR 2,800 gross/month", "hours": "38.5 h/week (full-time)"}
BASE_IDS = [["pm1", "pm3", "pm4", "pm5", "pm6"], ["fi1", "fi2", "fi3"], ["ra1", "ra2", "ra3", "ra4"], ["fo1", "fo2", "fo3"]]
PM2 = LIB["roles"][0]["bullets"]["pm2"]   # 'Turned market research and positioning into the roadmap and specs ...'


def chosen(role0=("pm1", "pm3", "pm4", "pm5", "pm2"), summary="A_product", swaps=None, skills=None, roles=None):
    """A selection answer; by default pm2 (market research) replaces pm6."""
    r = copy.deepcopy(roles or BASE_IDS)
    r[0] = list(role0)
    return {"summary": summary, "roles": r, "swaps": swaps or [], "skills": skills or copy.deepcopy(BASE["skills"]),
            "gaps": [{"requirement": "German C1", "why": "B1 only"}],
            "terms": [{"term": t, "claimable": True} for t in REQS["terms"]]}


def ans(obj):
    return envelope(json.dumps(obj))


def fake_pdf(path, pages):
    Path(path).write_bytes(b"%%PDF-1.4\n1 0 obj << /Type /Pages /Count %d /Kids [] >> endobj PTSerif" % pages)
    return Path(path)


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.saved = (store.DB, llm.RUN, run.BUILD, run.CHECK, os.environ.get("JOBDIGEST_CV_DIR"), library.LIBRARY)
        store.DB = t / "state.db"
        os.environ["JOBDIGEST_CV_DIR"] = str(t / "cv")
        con = store.connect()
        for jid, company, title, desc in (
                ("job1", "Raiffeisen Bank International (RBI)", "Student Job - Agile IT Delivery", AD),
                ("job2", "Tito & Friends", "Product Intern", "")):
            con.execute("insert into jobs (id, key, company, title, data) values (?,?,?,?,?)",
                        (jid, jid, company, title, json.dumps({"description": desc})))
        con.commit()
        con.close()
        self.pages = lambda content, n: 1          # renderer: pages as a function of content and build count
        self.builds = 0

        def build(content_path, pdf_path, html_dir=None):
            self.builds += 1
            content = json.loads(Path(content_path).read_text(encoding="utf-8"))
            return fake_pdf(pdf_path, self.pages(content, self.builds))

        def check(pdf, expected_text=None):
            data = Path(pdf).read_bytes()
            return {"pages": pdfcheck.page_count(data), "size": len(data), "text": "", "issues": []}

        run.BUILD, run.CHECK = build, check

    def tearDown(self):
        store.DB, llm.RUN, run.BUILD, run.CHECK, cv, library.LIBRARY = self.saved
        if cv is None:
            os.environ.pop("JOBDIGEST_CV_DIR", None)
        else:
            os.environ["JOBDIGEST_CV_DIR"] = cv
        self.tmp.cleanup()

    def rows(self):
        con = store.connect()
        try:
            return [dict(r) for r in con.execute("select * from cv_versions order by id")]
        finally:
            con.close()

    def content(self, r):
        return json.loads((Path(r["pdf"]).parent / "content.json").read_text(encoding="utf-8"))


class Orchestrator(Env):
    def test_ok_path_writes_row_and_files(self):
        fake = FakeRun(ans(REQS), ans(chosen()))
        llm.RUN = fake
        r = run.tailor_job("job1")
        self.assertEqual(r["status"], "ok", r["reason"])
        self.assertEqual([c["model"] for c in fake.calls], ["sonnet", "sonnet"])  # requirements + selection, no verifier
        pdf = Path(r["pdf"])
        self.assertTrue(pdf.is_file())
        self.assertEqual(pdf.parent, Path(os.environ["JOBDIGEST_CV_DIR"]) / "job1" / "1")
        self.assertTrue((pdf.parent / "content.json").is_file())
        self.assertTrue((pdf.parent / "result.json").is_file())
        self.assertEqual(self.content(r)["experience"][0]["bullets"][4], PM2)
        self.assertEqual(r["coverage"], [0.33, 0.67])      # claimable terms: Jira, backlog, market research
        self.assertEqual(r["coverage_all"], [0.33, 0.67])
        self.assertEqual([c["reason"] for c in r["changes"]], ["library bullet pm2", "dropped pm6 for this ad"])
        self.assertEqual(r["gaps"], [{"requirement": "German C1", "why": "B1 only"}])
        self.assertEqual(r["reverted"], [])
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "ok")
        self.assertEqual(rows[0]["job_id"], "job1")
        self.assertIn("Turned market research", rows[0]["content"])
        self.assertIn("1,454.55", r["answers"])  # 2,800 * 20 / 38.5, pro-rated, cents kept
        self.assertEqual(r["reason"], "")
        self.assertIn("ÖSD", r["answers"])

    def test_every_bullet_is_library_text(self):
        llm.RUN = FakeRun(ans(REQS), ans(chosen(summary="B_research", roles=[[], ["fi2", "fi1"], ["ra1", "ra5", "ra7"], ["fo1", "fo3"]],
                                                swaps=[{"bullet": "pm4", "from": "tickets", "to": "Jira tickets"}],
                                                role0=("pm3", "pm4", "pm2", "pm1"))))
        r = run.tailor_job("job1")
        self.assertEqual(r["status"], "ok", r["reason"])
        content = self.content(r)
        lib_texts = {t for role in LIB["roles"] for t in role["bullets"].values()}
        swapped = LIB["roles"][0]["bullets"]["pm4"].replace("tickets", "Jira tickets")
        for e in content["experience"]:
            self.assertTrue(set(e["bullets"]) <= lib_texts | {swapped}, e["bullets"])
        self.assertIn(content["summary"], LIB["summaries"].values())

    def test_second_press_is_a_new_version(self):
        llm.RUN = FakeRun(ans(REQS), ans(chosen()), ans(REQS), ans(chosen()))
        first, second = run.tailor_job("job1"), run.tailor_job("job1")
        self.assertNotEqual(Path(first["pdf"]).parent, Path(second["pdf"]).parent)
        self.assertEqual(Path(second["pdf"]).parent.name, "2")
        self.assertEqual(len(self.rows()), 2)

    def test_two_pages_then_one_after_retry(self):
        self.pages = lambda content, n: 2 if n == 1 else 1
        fake = FakeRun(ans(REQS), ans(chosen()), ans(chosen(role0=("pm3", "pm2", "pm1", "pm4"))))
        llm.RUN = fake
        r = run.tailor_job("job1")
        self.assertEqual(r["status"], "ok", r["reason"])
        self.assertEqual(len(fake.calls), 3)
        self.assertIn("Shorten it", fake.calls[2]["prompt"])
        self.assertIn("experience[0]", fake.calls[2]["prompt"])  # the section that grew
        self.assertEqual(len(self.content(r)["experience"][0]["bullets"]), 4)

    def test_always_two_pages_falls_back_to_base_selection_for_longest_role(self):
        self.pages = lambda content, n: 1 if content["experience"][0]["bullets"] == BASE["experience"][0]["bullets"] else 2
        llm.RUN = FakeRun(ans(REQS), ans(chosen()), ans(chosen()))
        r = run.tailor_job("job1")
        self.assertEqual(r["status"], "ok", r["reason"])
        self.assertEqual(self.content(r)["experience"][0]["bullets"], BASE["experience"][0]["bullets"])
        self.assertIn("experience[0]", [x["field"] for x in r["reverted"]])
        self.assertEqual(r["changes"], [])

    def test_overflow_even_with_base_role_is_fallback(self):
        self.pages = lambda content, n: 1 if content == BASE else 2
        llm.RUN = FakeRun(ans(REQS), ans(chosen(summary="B_research")), llm.LLMError("claude timed out after 300 s"))
        r = run.tailor_job("job1")
        self.assertEqual(r["status"], "fallback")
        self.assertIn("одну страницу", r["reason"])
        self.assertTrue(any("shortening retry failed" in i for i in r["issues"]))
        self.assertTrue(Path(r["pdf"]).is_file())
        self.assertEqual(self.content(r), BASE)

    def test_llm_timeout_falls_back_with_reason(self):
        llm.RUN = FakeRun(ans(REQS), llm.LLMError("claude timed out after 300 s"))
        r = run.tailor_job("job1")
        self.assertEqual(r["status"], "fallback")
        self.assertEqual(r["reason"], "Claude не ответил вовремя")  # plain words; the raw error stays in issues
        self.assertTrue(any("timed out" in i for i in r["issues"]))
        self.assertTrue(Path(r["pdf"]).is_file())
        self.assertTrue(Path(r["pdf"]).name.startswith("Alex-Morgan-CV-"))
        self.assertEqual(self.rows()[0]["status"], "fallback")
        self.assertIn("leave empty if optional", r["answers"])

    def test_broken_json_and_expired_login_fall_back(self):
        for bad in (envelope("Sure! Here is your CV."), envelope("OAuth token has expired", is_error=True)):
            llm.RUN = FakeRun(bad)
            r = run.tailor_job("job1")
            self.assertEqual(r["status"], "fallback")
            self.assertTrue(r["reason"])

    def test_old_free_form_answer_falls_back(self):
        c = copy.deepcopy(BASE)
        c["experience"][0]["bullets"][0] = "Built a 12-company LatAm crypto ranking"
        llm.RUN = FakeRun(ans(REQS), ans({"content": c, "changes": []}))
        r = run.tailor_job("job1")
        self.assertEqual(r["status"], "fallback")
        self.assertIn("не по формату", r["reason"])
        self.assertEqual(self.content(r), BASE)

    def test_locked_fields_cannot_change(self):
        a = chosen()
        a["headline"] = "Senior Product Lead"
        a["experience"] = [{"role": "Head of Product", "org": "Revolut"}]
        llm.RUN = FakeRun(ans(REQS), ans(a))
        r = run.tailor_job("job1")
        self.assertEqual(r["status"], "ok", r["reason"])
        content = self.content(r)
        for key in run.checks.LOCKED_TOP:
            self.assertEqual(content[key], BASE[key])
        self.assertEqual([e["role"] for e in content["experience"]], [e["role"] for e in BASE["experience"]])

    def test_invalid_role_selection_reverted_and_logged(self):
        llm.RUN = FakeRun(ans(REQS), ans(chosen(roles=[[], ["fi3", "fi4"], ["ra1", "ra3", "ra5"], ["fo2"]])))
        r = run.tailor_job("job1")
        self.assertEqual(r["status"], "ok", r["reason"])
        content = self.content(r)
        self.assertEqual([e["bullets"] for e in content["experience"][1:]], [e["bullets"] for e in BASE["experience"][1:]])
        whys = {x["field"]: x["why"] for x in r["reverted"]}
        self.assertIn("exclude each other", whys["experience[1]"])
        self.assertIn("exclude each other", whys["experience[2]"])
        self.assertIn("must keep", whys["experience[3]"])

    def test_build_crash_falls_back_to_approved_pdf(self):
        def broken(*a, **k):
            raise RuntimeError("Chrome crashed")

        run.BUILD = broken
        llm.RUN = FakeRun(ans(REQS), ans(chosen()))
        r = run.tailor_job("job1")
        self.assertEqual(r["status"], "fallback")
        self.assertEqual(Path(r["pdf"]).read_bytes(), run.APPROVED_PDF.read_bytes())

    def test_broken_library_fails_loudly(self):
        bad = json.loads(json.dumps(LIB))
        bad["roles"][0]["bullets"]["pm9"] = "Wrote SQL dashboards for the owners"
        path = Path(self.tmp.name) / "library.json"
        path.write_text(json.dumps(bad), encoding="utf-8")
        library.LIBRARY = path
        llm.RUN = FakeRun()  # no claude call allowed
        with self.assertRaises(library.LibraryError) as ctx:
            run.tailor_job("job1")
        self.assertIn("SQL", str(ctx.exception))
        self.assertEqual(self.rows(), [])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(run.main(["job1"]), 1)
        self.assertIn('"status": "error"', out.getvalue())

    def test_no_description_needs_text(self):
        llm.RUN = FakeRun()  # no claude call allowed
        r = run.tailor_job("job2")
        self.assertEqual(r["status"], "need_text")
        self.assertEqual(self.rows(), [])

    def test_pasted_text_is_used(self):
        fake = FakeRun(ans(REQS), ans(chosen()))
        llm.RUN = fake
        r = run.tailor_job("job2", pasted_text=AD)
        self.assertEqual(r["status"], "ok", r["reason"])
        self.assertIn("Junior Product Owner", fake.calls[0]["prompt"])

    def test_unknown_skill_items_dropped_blocklisted_line_reverted(self):
        skills = copy.deepcopy(BASE["skills"])
        skills[2]["items"] = "Jira (Kanban), Salesforce, Confluence, Notion, Figma"
        skills[3]["items"] = "Claude, ChatGPT, Codex"
        llm.RUN = FakeRun(ans(REQS), ans(chosen(skills=skills)))
        r = run.tailor_job("job1")
        self.assertEqual(r["status"], "ok", r["reason"])
        lines = {s["group"]: s["items"] for s in self.content(r)["skills"]}
        self.assertEqual(lines["Tools"], "Jira (Kanban), Confluence, Notion, Figma")
        self.assertEqual(lines["AI"], "Claude, Codex")
        dropped = {(x["field"], x["text"]): x["why"] for x in r["reverted"]}
        self.assertEqual(dropped[("skills[2]", "Salesforce")], "skill not in cv/skills_allowed.json for this group")
        self.assertEqual(dropped[("skills[3]", "ChatGPT")], "skill not in cv/skills_allowed.json for this group")

    def test_whitelist_not_fragments_dedupe_and_cap(self):
        """QA 09.10: fact-bank fragments passed as skills; items now match the whitelist exactly, per group."""
        skills = copy.deepcopy(BASE["skills"])
        skills[2]["items"] = "Jira (Kanban), Development was led by the CTO, Led, engineering, figma, Figma, Kanban"
        skills[3]["items"] = "Claude, Codex, Cursor, Perplexity, Gemini, Midjourney, Stitch, AI-assisted prototyping, " \
                             "AI-assisted research, Prompt engineering, Claude, Codex"
        skills[0]["items"] = "Roadmapping, " + "Surveys, " * 20000  # 160 KB: capped before any processing
        llm.RUN = FakeRun(ans(REQS), ans(chosen(skills=skills)))
        t = time.monotonic()
        r = run.tailor_job("job1")
        self.assertLess(time.monotonic() - t, 5)
        self.assertEqual(r["status"], "ok", r["reason"])
        lines = {s["group"]: s["items"] for s in self.content(r)["skills"]}
        self.assertEqual(lines["Tools"], "Jira (Kanban), Figma")   # Kanban is listed under Product only
        self.assertEqual(lines["Product"], "Roadmapping, Surveys")
        whys = {x["text"]: x["why"] for x in r["reverted"] if x["field"] == "skills[2]"}
        for bad in ("Development was led by the CTO", "Led", "engineering", "Kanban"):
            self.assertEqual(whys[bad], "skill not in cv/skills_allowed.json for this group")
        self.assertEqual(whys["Figma"], "duplicate skill item")
        self.assertNotIn("CTO", json.dumps(self.content(r)["skills"]))
        ai = self.content(r)["skills"][3]["items"].split(", ")
        self.assertLessEqual(len(ai), 10)

    def test_german_skill_items_dropped(self):
        skills = copy.deepcopy(BASE["skills"])
        skills[0]["items"] = "Roadmapping, Marktforschung, Wireframing"
        llm.RUN = FakeRun(ans(REQS), ans(chosen(skills=skills)))
        r = run.tailor_job("job1")
        self.assertEqual(r["status"], "ok", r["reason"])
        self.assertNotIn("Marktforschung", json.dumps(self.content(r), ensure_ascii=False))
        self.assertEqual(self.content(r)["skills"][0]["items"], "Roadmapping, Wireframing")

    def test_without_poppler_pages_come_from_bytes(self):
        def missing(pdf, expected_text=None):
            raise pdfcheck.ToolMissing("pdfinfo not installed")

        run.CHECK = missing
        llm.RUN = FakeRun(ans(REQS), ans(chosen()))
        r = run.tailor_job("job1")
        self.assertEqual(r["status"], "ok", r["reason"])
        self.assertTrue(any("skipped" in i for i in r["issues"]))

    def test_summary_that_would_wrap_is_cut_before_build(self):
        from tests.test_library import LONG_LIB
        library.LIBRARY = Path(self.tmp.name) / "library.json"
        library.LIBRARY.write_text(json.dumps(LONG_LIB), encoding="utf-8")
        llm.RUN = FakeRun(ans(REQS), ans(chosen(summary="D_long")))  # 4 printed lines; the base page is full
        r = run.tailor_job("job1")
        self.assertEqual(r["status"], "ok", r["reason"])
        self.assertEqual(self.builds, 1)  # no shortening retry needed
        self.assertIn("summary", [x["field"] for x in r["reverted"]])
        self.assertEqual(self.content(r)["summary"], BASE["summary"])

    def test_no_gain_falls_back(self):
        skills = copy.deepcopy(BASE["skills"])
        skills[2]["items"] = "Google Analytics 4 (GA4), Confluence, Notion, Figma, Canva"  # drops Jira
        llm.RUN = FakeRun(ans(REQS), ans(chosen(role0=BASE_IDS[0], skills=skills)))
        saved, run.cover.fill = run.cover.fill, lambda base, lib, result, *a, **k: result  # the guard behind the fill
        try:
            r = run.tailor_job("job1")
        finally:
            run.cover.fill = saved
        self.assertEqual(r["status"], "fallback")
        self.assertEqual(r["reason"], "адаптация не улучшила совпадение")
        self.assertEqual(r["coverage"][0], r["coverage"][1])

    def test_overused_term_loses_latest_changed_unit(self):
        skills = copy.deepcopy(BASE["skills"])
        skills[2]["items"] = "Jira, Jira (Kanban), Confluence, Notion"  # Jira x4 with pm7 and the pm4 swap
        swaps = [{"bullet": "pm4", "from": "tickets", "to": "Jira tickets"}]
        llm.RUN = FakeRun(ans(REQS), ans(chosen(role0=("pm1", "pm3", "pm4", "pm5", "pm7"), swaps=swaps, skills=skills)))
        r = run.tailor_job("job1")
        self.assertEqual(r["status"], "ok", r["reason"])
        content = self.content(r)
        self.assertEqual(run.checks.overused(["Jira"], run.checks.cv_text(content)), [])
        self.assertEqual(content["skills"][2], BASE["skills"][2])                       # latest unit first
        self.assertIn("Jira tickets", content["experience"][0]["bullets"][2])           # earlier units kept
        self.assertEqual(content["experience"][0]["bullets"][4], LIB["roles"][0]["bullets"]["pm7"])
        self.assertIn("term used more than 3 times: Jira", [x["why"] for x in r["reverted"]])

    def test_empty_terms_ok_with_note(self):
        llm.RUN = FakeRun(ans(dict(REQS, terms=[])), ans(chosen()))
        r = run.tailor_job("job1")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["reason"], "не удалось выделить ключевые слова")
        self.assertEqual(r["coverage"], [None, None])
        self.assertEqual(r["coverage_all"], [None, None])
        self.assertNotIn("None", json.dumps([r["reason"], r["answers"]]))

    def test_nothing_claimable_ok_with_note(self):
        t = chosen()
        t["terms"] = [{"term": x, "claimable": False} for x in REQS["terms"]]
        llm.RUN = FakeRun(ans(REQS), ans(t))
        r = run.tailor_job("job1")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["coverage"], [None, None])
        self.assertEqual(r["coverage_all"], [0.33, 0.67])
        self.assertIn("не подтверждается", r["reason"])

    def test_short_paste_does_not_override_description(self):
        fake = FakeRun(ans(REQS), ans(chosen()))
        llm.RUN = fake
        run.tailor_job("job1", pasted_text="see attached")
        self.assertIn("Junior Product Owner", fake.calls[0]["prompt"])
        llm.RUN = FakeRun()
        self.assertEqual(run.tailor_job("job2", pasted_text="too short")["status"], "need_text")

    def test_call_timeouts_capped(self):
        fake = FakeRun(ans(REQS), ans(chosen()))
        llm.RUN = fake
        run.tailor_job("job1")
        self.assertTrue(all(c["timeout"] <= 180 for c in fake.calls))
        self.assertIsNone(llm.DEADLINE)  # reset after the run

    def test_whole_run_deadline_falls_back(self):
        saved = run.RUN_DEADLINE
        run.RUN_DEADLINE = 0
        try:
            llm.RUN = FakeRun()
            r = run.tailor_job("job1")
        finally:
            run.RUN_DEADLINE = saved
        self.assertEqual(r["status"], "fallback")
        self.assertIn("не уложился", r["reason"])

    def test_cursor_and_midjourney_in_skills_kept(self):
        skills = copy.deepcopy(BASE["skills"])
        skills[3]["items"] = "Claude, Codex, Cursor, Midjourney, Perplexity"  # facts.md, 08.10.2026
        llm.RUN = FakeRun(ans(REQS), ans(chosen(skills=skills)))
        r = run.tailor_job("job1")
        self.assertEqual(r["status"], "ok", r["reason"])
        self.assertEqual(self.content(r)["skills"][3]["items"], "Claude, Codex, Cursor, Midjourney, Perplexity")
        self.assertNotIn("skills[3]", [x["field"] for x in r["reverted"]])

    def test_surrogates_do_not_crash(self):
        t = chosen(summary="B_research\ud800")
        t["gaps"] = [{"requirement": "SQL\ud800", "why": "never used\udfff"}]
        raw = json.dumps(t)  # json escapes the lone surrogates, as a model answer would carry them
        llm.RUN = FakeRun(ans(REQS), envelope(raw))
        r = run.tailor_job("job1")
        self.assertEqual(r["status"], "ok", r["reason"])
        self.assertEqual(r["gaps"], [{"requirement": "SQL", "why": "never used"}])
        self.assertEqual(self.content(r)["summary"], LIB["summaries"]["B_research"])
        json.dumps(r, ensure_ascii=False).encode("utf-8")  # no lone surrogate left anywhere

    def test_emoji_skill_line_cleaned(self):
        skills = copy.deepcopy(BASE["skills"])
        skills[3]["items"] = "Claude, Codex \U0001F680\U0001F680"
        llm.RUN = FakeRun(ans(REQS), ans(chosen(skills=skills)))
        r = run.tailor_job("job1")
        self.assertEqual(r["status"], "ok", r["reason"])
        self.assertNotIn("\U0001F680", json.dumps(self.content(r), ensure_ascii=False))
        self.assertTrue(r["reverted"])

    def test_renamed_skill_group_keeps_base_groups(self):
        skills = [{"group": "Tooling", "items": "Jira, Notion"}, {"group": "AI", "items": "Claude, ChatGPT"}]
        llm.RUN = FakeRun(ans(REQS), ans(chosen(skills=skills)))
        r = run.tailor_job("job1")
        content = self.content(r)
        self.assertEqual(sorted(s["group"] for s in content["skills"]), sorted(s["group"] for s in BASE["skills"]))
        self.assertEqual({s["group"]: s["items"] for s in content["skills"]}["AI"], "Claude")  # ChatGPT: not whitelisted

    def test_unknown_job(self):
        with self.assertRaises(LookupError):
            run.tailor_job("nope")



class Names(unittest.TestCase):
    def test_rbi_file_name(self):
        name = run.file_name("Raiffeisen Bank International (RBI)", "Student Job - Agile IT Delivery")
        self.assertTrue(name.startswith("Alex-Morgan-CV-"))
        self.assertTrue(name.endswith(".pdf"))
        self.assertLessEqual(len(name), 80)
        self.assertTrue(name.isascii())
        self.assertNotIn("--", name)
        self.assertIn("Raiffeisen", name)
        self.assertIn("Student-Job-Agile-IT-Delivery", name)

    def test_umlauts_and_gender_marker(self):
        name = run.file_name("Österreichische Post AG", "Werkstudent:in Produktmanagement (m/w/d)")
        self.assertEqual(name, "Alex-Morgan-CV-Osterreichische-Post-AG-Werkstudent-in-Produktmanagement.pdf")

    def test_very_long(self):
        name = run.file_name("A" * 100, "B" * 100)
        self.assertLessEqual(len(name), 80)


class Salary(unittest.TestCase):
    def test_no_salary(self):
        self.assertIn("no minimum", run.salary_line({"min_salary": None}))

    def test_part_time_ad_not_prorated(self):
        line = run.salary_line({"min_salary": "EUR 1.450 brutto/Monat", "hours": "20 h/week"})
        self.assertIn("EUR 1.450", line)
        self.assertNotIn("pro-rated", line)

    def test_german_number_format_keeps_cents(self):
        line = run.salary_line({"min_salary": "EUR 3.200,50 brutto/Monat", "hours": "40 Stunden Vollzeit"})
        self.assertIn("1,600.25", line)

    def test_hourly_never_prorated(self):
        line = run.salary_line({"min_salary": "EUR 14,50 brutto/Stunde", "hours": "full-time, 40 h/week"})
        self.assertNotIn("pro-rated to", line)
        self.assertIn("hourly", line)
        line = run.salary_line({"min_salary": "EUR 15.20 per hour", "hours": "38.5 h"})
        self.assertNotIn("pro-rated to", line)

    def test_hours_named_in_salary(self):
        line = run.salary_line({"min_salary": "EUR 3,200 gross/month for 40h", "hours": "full-time"})
        self.assertIn("1,600.00", line)
        line = run.salary_line({"min_salary": "EUR 2,800 gross/month (part-time 20h)", "hours": "full-time position"})
        self.assertNotIn("pro-rate", line)

    def test_part_time_30h_is_not_full_time(self):
        line = run.salary_line({"min_salary": "EUR 2,100 gross/month", "hours": "Part-time, 30 h/week"})
        self.assertNotIn("full-time", line)


if __name__ == "__main__":
    unittest.main()
