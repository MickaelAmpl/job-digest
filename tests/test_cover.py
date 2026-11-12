"""Term index, deterministic fill and the counts message (09.10.2026, Bitmovin case: 'AI tools' sat in pm7,
the model did not pick it, the bot said 0% -> 0%)."""
import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cv.tailor import checks, cover, library, llm, run, worker  # noqa: E402
from tests.fakes import FakeRun, envelope  # noqa: E402
from tests.test_run import BASE, BASE_IDS, Env  # noqa: E402

LIB = library.load(BASE)
ALLOWED = checks.load_skills_allowed()
BITMOVIN_TERMS = ["HubSpot", "Salesforce", "CRM", "Revenue Operations", "Marketing automation", "Data analysis",
                  "AI tools", "Lead management", "Pipeline reporting", "Attribution", "Looker", "SQL", "B2B SaaS",
                  "Lifecycle marketing", "Campaign operations", "Forecasting", "Data hygiene", "Zapier",
                  "Process automation", "German"]
BITMOVIN_REQS = {"must": ["CRM", "Data analysis"], "nice": ["German"], "terms": BITMOVIN_TERMS, "ad_language": "en",
                 "min_salary": None, "hours": None}


def answer(roles=None, claimable=(), terms=BITMOVIN_TERMS, skills=None, summary="A_product", swaps=None):
    return {"summary": summary, "roles": copy.deepcopy(roles or BASE_IDS), "swaps": swaps or [],
            "skills": skills or copy.deepcopy(BASE["skills"]), "gaps": [{"requirement": "HubSpot", "why": "never used",
                                                                         "ru": "HubSpot"}],
            "terms": [{"term": t, "claimable": t in claimable} for t in terms]}


def selected(base, lib, ans, claimable):
    """A result as tailor._parse builds it, from a selection answer."""
    sel, reverted = library.choose(base, lib, ans)
    result = {"content": library.assemble(base, sel, ans["skills"]), "selection": sel, "changes": [], "gaps": [],
              "violations": [], "reverted": reverted, "claimable": {t: True for t in claimable}}
    return library.refresh(base, lib, result)


def ids(result, i):
    return [b["id"] for b in result["selection"]["roles"][i]]


class Bitmovin(Env):
    def test_pm7_added_ai_tools_covered_data_analysis_no_wording(self):
        fake = FakeRun(envelope(json.dumps(BITMOVIN_REQS)),
                       envelope(json.dumps(answer(claimable=("Data analysis", "AI tools")))))
        llm.RUN = fake
        r = run.tailor_job("job1")
        self.assertEqual(r["status"], "ok", r["reason"])
        prompt = fake.calls[1]["prompt"]
        self.assertIn('- "AI tools": summary C_ai, pm7', prompt)
        self.assertIn('- "Data analysis": nothing in the library or the skills list', prompt)
        self.assertIn("## Cover the claimable terms", prompt)
        content = self.content(r)
        pm7 = LIB["roles"][0]["bullets"]["pm7"]
        self.assertIn(pm7, content["experience"][0]["bullets"])
        self.assertNotIn(LIB["roles"][0]["bullets"]["pm6"], content["experience"][0]["bullets"])  # lowest priority
        self.assertIn(LIB["roles"][0]["bullets"]["pm3"], content["experience"][0]["bullets"])     # must_keep stays
        added = [c for c in r["changes"] if c["reason"] == "library bullet pm7"]
        self.assertEqual(added[0]["note"], "добавлено под требование «AI tools»")
        self.assertEqual(r["counts"], {"terms": 20, "claimable": 2, "before": 0, "after": 1})
        self.assertEqual(r["claimable_no_wording"], ["Data analysis"])
        self.assertEqual(r["coverage"], [0.0, 0.5])
        saved = json.loads((Path(r["pdf"]).parent / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["claimable_no_wording"], ["Data analysis"])
        self.assertEqual(checks.total_lines(content, BASE), checks.budgets(BASE)["lines"])
        text = worker.summary_text(r, worker.role_names(BASE))
        self.assertTrue(text.startswith(
            "Требований в вакансии: 20. Ты закрываешь фактами: 2 (Data analysis, AI tools). "
            "В резюме было 0 из 2, стало 1 из 2.\nВакансия в основном вне твоего опыта.\n"
            "Нет готовой формулировки для: Data analysis (можно добавить в библиотеку)"), text)
        self.assertIn("- PM: добавлен пункт «Worked in Kanban", text)
        self.assertIn("- добавлено под требование «AI tools»", text)
        self.assertNotIn("%", text)
        self.assertNotIn("—", text)


class FillRules(unittest.TestCase):
    def fill(self, roles=None, claimable=(), skills=None, terms=None):
        res = selected(BASE, LIB, answer(roles=roles, skills=skills, terms=list(claimable)), claimable)
        return res, cover.fill(BASE, LIB, res, terms or list(claimable), ALLOWED)

    def test_must_keep_and_pin_last_never_replaced(self):
        roles = copy.deepcopy(BASE_IDS)
        roles[3] = ["fo1", "fo3"]                      # both must_keep, fo3 pin_last; fo2 has 'advertising'
        before, after = self.fill(roles, ["advertising"])
        self.assertEqual(ids(after, 3), ["fo1", "fo3"])
        self.assertFalse(checks.mentions("advertising", checks.cv_text(after["content"])))
        self.assertNotIn("filled", after)

    def test_exclusive(self):
        roles = copy.deepcopy(BASE_IDS)
        roles[1] = ["fi3", "fi1", "fi2"]                           # fi4 excludes fi3: only fi3 may make room
        _, after = self.fill(roles, ["Sent cold outreach"])  # only fi4 carries it ("Seedline Capital" is fixed text already)
        self.assertEqual(ids(after, 1), ["fi4", "fi1", "fi2"])
        self.assertEqual(library._role_problem(LIB["roles"][1], 3, ids(after, 1)), "")

    def test_removal_never_uncovers_a_claimable_term(self):
        _, after = self.fill(claimable=["AI tools"])               # pm6 is the lowest priority
        self.assertEqual(ids(after, 0), ["pm1", "pm3", "pm4", "pm5", "pm7"])
        _, after = self.fill(claimable=["AI tools", "owners"])     # pm6 carries 'owners': pm5 goes instead
        self.assertEqual(ids(after, 0), ["pm1", "pm3", "pm4", "pm7", "pm6"])
        self.assertEqual(len(ids(after, 0)), len(BASE_IDS[0]))
        self.assertTrue(checks.mentions("owners", checks.cv_text(after["content"])))

    def test_pin_last_stays_last_after_fill(self):
        lib = copy.deepcopy(LIB)
        lib["roles"][3]["must_keep"] = []                          # only pin_last guards fo3 here
        lib["roles"][3]["bullets"]["fo4"] = "Sold advertising packages to crypto projects"
        roles = copy.deepcopy(BASE_IDS)
        roles[3] = ["fo1", "fo3"]
        res = selected(BASE, lib, answer(roles=roles, terms=["advertising packages"]), ["advertising packages"])
        after = cover.fill(BASE, lib, res, ["advertising packages"], ALLOWED)
        self.assertEqual(ids(after, 3), ["fo4", "fo3"])            # fo1 replaced, fo3 never the victim, still last

    def test_length_budget_refuses_a_longer_bullet(self):
        roles = copy.deepcopy(BASE_IDS)
        roles[2] = ["ra1", "ra2", "ra4", "ra7"]                     # the page is full (23 of 23 lines)
        res = selected(BASE, LIB, answer(roles=roles, terms=["country briefs"]), ["country briefs"])
        self.assertEqual(checks.total_lines(res["content"], BASE), checks.budgets(BASE)["lines"])
        after = cover.fill(BASE, LIB, res, ["country briefs"], ALLOWED)  # ra5 prints 2 lines in every slot: refused
        self.assertEqual(ids(after, 2), ["ra1", "ra2", "ra4", "ra7"])
        self.assertNotIn("filled", after)

    def test_skill_item_within_line_budget(self):
        _, after = self.fill(claimable=["Midjourney"])
        line = after["content"]["skills"][3]
        self.assertTrue(line["items"].endswith("Midjourney"), line)
        self.assertLessEqual(len(f'{line["group"]}: {line["items"]}'), checks.budgets(BASE)["skills"]["AI"])
        kept, dropped = checks.allowed_items("AI", line["items"], ALLOWED)
        self.assertEqual(dropped, [])
        note = [c.get("note") for c in after["changes"] if c["field"] == "skills[3]"]
        self.assertEqual(note, ["добавлено под требование «Midjourney»"])

    def test_swap_on_a_selected_bullet(self):
        _, after = self.fill(claimable=["Jira tickets"])
        self.assertIn("Jira tickets", after["content"]["experience"][0]["bullets"][2])
        self.assertEqual(after["selection"]["swaps"], [{"bullet": "pm4", "from": "tickets", "to": "Jira tickets"}])
        self.assertEqual(ids(after, 0), BASE_IDS[0])

    def test_never_over_three_mentions(self):
        skills = copy.deepcopy(BASE["skills"])
        skills[2]["items"] = "Jira, Jira (Kanban), Confluence"     # Jira x2 + pm7 = 3; the pm4 swap would be the 4th
        roles = copy.deepcopy(BASE_IDS)
        roles[0] = ["pm1", "pm3", "pm4", "pm5", "pm7"]
        terms = ["Jira tickets", "Jira"]
        res = selected(BASE, LIB, answer(roles=roles, skills=skills, terms=terms), ["Jira tickets"])
        self.assertEqual(checks._count("Jira", checks.cv_text(res["content"])), 3)
        after = cover.fill(BASE, LIB, res, terms, ALLOWED)
        self.assertEqual(checks.overused(terms, checks.cv_text(after["content"])), [])
        self.assertEqual(after["selection"]["swaps"], [])

    def test_reverted_fill_loses_its_note(self):
        _, after = self.fill(claimable=["AI tools"])
        self.assertTrue(any(c.get("note") for c in after["changes"]))
        back = library.revert(BASE, LIB, after, "experience[0]", "test")
        self.assertFalse(any(c.get("note") for c in back["changes"]))

    def test_no_wording_and_fixed_text(self):
        ix = cover.term_index(BASE, LIB, ["Data analysis", "Research Analyst", "Cursor", "pm"])
        self.assertEqual(ix["Data analysis"], [])
        self.assertEqual(ix["Research Analyst"][0], {"kind": "fixed"})
        self.assertEqual(ix["Cursor"], [{"kind": "skill", "group": "AI", "item": "Cursor"}])
        self.assertEqual(cover.no_wording(ix, ["Data analysis", "Research Analyst", "Cursor"]), ["Data analysis"])


class Message(unittest.TestCase):
    def text(self, **kw):
        r = {"changes": [], "gaps": [], "reason": "", "claimable_no_wording": []}
        r.update(kw)
        return worker.summary_text(r, worker.role_names(BASE))

    def test_k_zero(self):
        t = self.text(counts={"terms": 12, "claimable": 0, "before": 0, "after": 0}, claimable=[])
        self.assertTrue(t.startswith("Требований в вакансии: 12. По фактам ты не закрываешь требования этой вакансии - "
                                     "резюме оставлено базовым по смыслу.\nВакансия в основном вне твоего опыта."), t)
        self.assertNotIn("Ты закрываешь фактами", t)

    def test_small_k_few_terms_no_outside_line(self):
        t = self.text(counts={"terms": 4, "claimable": 1, "before": 0, "after": 1}, claimable=["Jira"])
        self.assertTrue(t.startswith("Требований в вакансии: 4. Ты закрываешь фактами: 1 (Jira). "
                                     "В резюме было 0 из 1, стало 1 из 1."), t)
        self.assertNotIn("вне твоего опыта", t)
        self.assertNotIn("Нет готовой формулировки", t)

    def test_normal_lists_five_terms(self):
        terms = ["Jira", "backlog", "market research", "Figma", "roadmap", "GA4", "KPIs"]
        t = self.text(counts={"terms": 15, "claimable": 7, "before": 3, "after": 6}, claimable=terms,
                      claimable_no_wording=["backlog"])
        first = t.splitlines()[0]
        self.assertEqual(first, "Требований в вакансии: 15. Ты закрываешь фактами: 7 (Jira, backlog, market research, "
                                "Figma, roadmap и ещё 2). В резюме было 3 из 7, стало 6 из 7.")
        self.assertNotIn("вне твоего опыта", t)
        self.assertIn("Нет готовой формулировки для: backlog (можно добавить в библиотеку)", t)


if __name__ == "__main__":
    unittest.main()
