"""Task 4 (library mode, 09.10.2026): the selection call (LLM always faked). The verifier is gone:
summary and bullets are approved library texts by construction."""
import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cv.tailor import library, llm, tailor  # noqa: E402
from tests.fakes import FakeRun, envelope  # noqa: E402

BASE = json.loads((ROOT / "cv" / "source" / "content" / "en.json").read_text(encoding="utf-8"))
FACTS = (ROOT / "cv" / "facts.md").read_text(encoding="utf-8")
LIB = library.load(BASE)
REQS = {"must": ["Jira"], "nice": [], "terms": ["Jira", "backlog"], "ad_language": "en",
        "min_salary": None, "hours": None}
BASE_IDS = [["pm1", "pm3", "pm4", "pm5", "pm6"], ["fi1", "fi2", "fi3"], ["ra1", "ra2", "ra3", "ra4"], ["fo1", "fo2", "fo3"]]


def answer(**over):
    a = {"summary": "A_product", "roles": copy.deepcopy(BASE_IDS), "swaps": [], "skills": copy.deepcopy(BASE["skills"]),
         "gaps": [], "terms": []}
    a.update(over)
    return envelope(json.dumps(a))


class Tailor(unittest.TestCase):
    def setUp(self):
        self.saved = llm.RUN

    def tearDown(self):
        llm.RUN = self.saved

    def test_sonnet_call_with_library_rules_facts_and_requirements(self):
        fake = FakeRun(answer(gaps=[{"requirement": "Scrum", "why": "Kanban only"}]))
        llm.RUN = fake
        r = tailor.run(BASE, FACTS, REQS, LIB)
        self.assertEqual(fake.calls[0]["model"], "sonnet")
        self.assertLessEqual(fake.calls[0]["timeout"], 180)
        prompt = fake.calls[0]["prompt"]
        self.assertIn("CHOOSING from his approved wording library", prompt)
        self.assertIn('"pm7"', prompt)                              # library ids go in
        self.assertIn('must keep ["ra1"]', prompt)                  # rules per role go in
        self.assertIn("ra3 + ra5", prompt)
        self.assertIn("Product Manager, Web3 company", prompt)     # fact bank goes in
        self.assertIn('"backlog"', prompt)                          # requirements go in
        self.assertEqual(r["content"], BASE)                        # base choices -> base CV
        self.assertEqual(r["changes"], [])
        self.assertEqual(r["gaps"], [{"requirement": "Scrum", "why": "Kanban only"}])

    def test_prompt_has_skill_budgets_and_pin_last(self):
        fake = FakeRun(answer(gaps=[{"requirement": "Scrum master certification", "why": "none", "ru": "Сертификат Scrum Master"},
                                    {"requirement": "SQL", "why": "not used", "ru": 5}]))
        llm.RUN = fake
        r = tailor.run(BASE, FACTS, REQS, LIB)
        prompt = fake.calls[0]["prompt"]
        for group, limit in (("Product", 104), ("Go-to-market & Web3", 101), ("Tools", 103), ("AI", 62)):
            self.assertIn(f'"{group}: ..." at most {limit} characters', prompt)
        self.assertIn("loses items from its end", prompt)
        self.assertIn("fo3 always goes last", prompt)
        self.assertEqual(r["gaps"][0]["ru"], "Сертификат Scrum Master")
        self.assertNotIn("ru", r["gaps"][1])  # not a string: no Russian wording, the bot shows the English one

    def test_choices_assembled_from_library(self):
        llm.RUN = FakeRun(answer(summary="B_research", roles=[["pm3", "pm2", "pm1"], ["fi2", "fi1"], ["ra1", "ra5", "ra7"],
                                                              ["fo1", "fo3"]],
                                 swaps=[{"bullet": "pm2", "from": "specs", "to": "product requirements"}]))
        r = tailor.run(BASE, FACTS, REQS, LIB)
        exp = r["content"]["experience"]
        self.assertEqual(r["content"]["summary"], LIB["summaries"]["B_research"])
        self.assertEqual(exp[0]["bullets"][1], "Turned market research and positioning into the roadmap and product requirements "
                                               "for a crypto market data platform")
        self.assertEqual(exp[2]["bullets"], [LIB["roles"][2]["bullets"][k] for k in ("ra1", "ra5", "ra7")])
        self.assertEqual(r["reverted"], [])
        self.assertIn("library bullet pm2 (swap: specs -> product requirements)", [c["reason"] for c in r["changes"]])

    def test_free_text_content_is_ignored(self):
        c = copy.deepcopy(BASE)
        c["experience"][0]["role"] = "Head of Product"
        c["headline"] = "Senior PM"
        c["experience"][0]["bullets"][0] = "Led a team of 50 engineers"
        c["summary"] = "Built a 12-company LatAm crypto ranking"
        llm.RUN = FakeRun(answer(content=c, experience=c["experience"]))
        r = tailor.run(BASE, FACTS, REQS, LIB)
        self.assertEqual(r["content"], BASE)

    def test_answer_without_choices_is_a_format_error(self):
        for bad in (json.dumps({"content": BASE}), json.dumps(["A_product"])):
            llm.RUN = FakeRun(envelope(bad))
            with self.assertRaises(llm.LLMError):
                tailor.run(BASE, FACTS, REQS, LIB)

    def test_invalid_role_logged_never_free_text(self):
        llm.RUN = FakeRun(answer(roles=[["pm3", "Defined everything"], ["fi1"], ["ra1"], ["fo1", "fo3"]]))
        r = tailor.run(BASE, FACTS, REQS, LIB)
        self.assertEqual(r["content"]["experience"][0]["bullets"], BASE["experience"][0]["bullets"])
        self.assertEqual(r["reverted"][0]["field"], "experience[0]")
        self.assertEqual(r["content"]["experience"][1]["bullets"], [LIB["roles"][1]["bullets"]["fi1"]])

    def test_skill_groups_locked(self):
        skills = [{"group": "Tooling", "items": "Jira, Notion"}, {"group": "AI", "items": "Claude, Codex, Cursor"}]
        llm.RUN = FakeRun(answer(skills=skills))
        r = tailor.run(BASE, FACTS, REQS, LIB)
        groups = [s["group"] for s in r["content"]["skills"]]
        self.assertEqual(sorted(groups), sorted(s["group"] for s in BASE["skills"]))
        self.assertEqual({s["group"]: s["items"] for s in r["content"]["skills"]}["AI"], "Claude, Codex, Cursor")
        llm.RUN = FakeRun(answer(skills="Jira, SQL"))
        r = tailor.run(BASE, FACTS, REQS, LIB)
        self.assertEqual(r["content"]["skills"], BASE["skills"])
        self.assertIn("skills", r["violations"])

    def test_claimable_marks(self):
        terms = [{"term": "Jira", "claimable": True}, {"term": "backlog", "claimable": "yes"}]
        llm.RUN = FakeRun(answer(terms=terms))
        r = tailor.run(BASE, FACTS, dict(REQS, terms=["Jira", "backlog", "SQL"]), LIB)
        self.assertEqual(r["claimable"], {"Jira": True, "backlog": False, "SQL": False})

    def test_shorten_sends_previous_choices_section_and_lines(self):
        llm.RUN = FakeRun(answer(roles=[["pm3", "pm2", "pm1", "pm4", "pm5"], *BASE_IDS[1:]]))
        first = tailor.run(BASE, FACTS, REQS, LIB)
        fake = FakeRun(answer())
        llm.RUN = fake
        tailor.shorten(BASE, FACTS, REQS, LIB, first, "experience[0]", 3)
        prompt = fake.calls[0]["prompt"]
        self.assertIn("experience[0]", prompt)
        self.assertIn("3 lines", prompt)
        self.assertIn('"pm2"', prompt.split("## Your previous choices")[1])
        self.assertEqual(fake.calls[0]["model"], "sonnet")


if __name__ == "__main__":
    unittest.main()
