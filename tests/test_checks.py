"""Task 2: code checks that do not trust the model (locked fields, blocklist, numbers, coverage)."""
import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cv.tailor import checks  # noqa: E402

BASE = json.loads((ROOT / "cv" / "source" / "content" / "en.json").read_text(encoding="utf-8"))
FACTS = (ROOT / "cv" / "facts.md").read_text(encoding="utf-8")


class Locked(unittest.TestCase):
    def test_role_title_restored(self):
        new = copy.deepcopy(BASE)
        new["experience"][0]["role"] = "Senior Product Owner"
        out, violations = checks.enforce_locked(BASE, new)
        self.assertEqual(out["experience"][0]["role"], "Product Manager")
        self.assertIn("experience[0].role", violations)

    def test_unchanged_has_no_violations(self):
        out, violations = checks.enforce_locked(BASE, copy.deepcopy(BASE))
        self.assertEqual(violations, [])
        self.assertEqual(out, BASE)

    def test_missing_locked_fields_copied_silently(self):
        new = {"summary": "x", "experience": [{"bullets": e["bullets"]} for e in BASE["experience"]],
               "skills": BASE["skills"]}
        out, violations = checks.enforce_locked(BASE, new)
        self.assertEqual(violations, [])
        for key in ("name", "contacts", "headline", "education", "certifications", "languages", "extra",
                    "labels", "order"):
            self.assertEqual(out[key], BASE[key], key)
        self.assertEqual(out["experience"][2]["org"], "Northwind Labs")
        self.assertEqual(out["summary"], "x")

    def test_dropped_role_restored_and_logged(self):
        new = copy.deepcopy(BASE)
        new["experience"].pop()
        new["headline"] = "Product Owner"
        out, violations = checks.enforce_locked(BASE, new)
        self.assertEqual(len(out["experience"]), 4)
        self.assertEqual(out["experience"][3], BASE["experience"][3])
        self.assertEqual(out["headline"], "Product Manager")
        self.assertIn("experience[3]", violations)
        self.assertIn("headline", violations)

    def test_bullet_count_outside_range(self):
        new = copy.deepcopy(BASE)
        new["experience"][1]["bullets"] = ["a"] * 6  # base has 3
        out, violations = checks.enforce_locked(BASE, new)
        self.assertEqual(out["experience"][1]["bullets"], BASE["experience"][1]["bullets"])
        self.assertIn("experience[1].bullets", violations)


class Blocklist(unittest.TestCase):
    def setUp(self):
        self.terms, self.cs = checks.load_never_claim()

    def test_chatgpt_hit(self):
        self.assertEqual(checks.blocklist_hits("Claude, ChatGPT, Codex", self.terms, self.cs), ["ChatGPT"])

    def test_case_insensitive_and_plural(self):
        hits = checks.blocklist_hits("Ran a/b tests and user interviews; wrote sql", self.terms, self.cs)
        self.assertEqual(sorted(hits), ["A/B test", "AB test", "SQL", "user interview"])

    def test_no_false_positive_on_words(self):
        text = "linear growth, R&D budget, Sapient, Pythonic"
        self.assertEqual(checks.blocklist_hits(text, self.terms, self.cs), [])
        self.assertEqual(checks.blocklist_hits("Python, R, Excel", self.terms, self.cs), ["Python", "R"])

    def test_base_cv_is_clean(self):
        self.assertEqual(checks.blocklist_hits(checks.cv_text(BASE), self.terms, self.cs), [])


class Numbers(unittest.TestCase):
    def test_new_number_found(self):
        allowed = checks.cv_text(BASE) + "\n" + FACTS
        self.assertEqual(checks.new_numbers(allowed, "Rapidly grew to 500,000 users"), ["500,000"])

    def test_existing_numbers_pass(self):
        allowed = checks.cv_text(BASE)
        text = "Reached 120,000+ users; 150,000+ subscribers; team of 5; exited in 2022; GA4"
        self.assertEqual(checks.new_numbers(allowed, text), [])

    def test_dropping_plus_ok_adding_plus_not(self):
        allowed = checks.cv_text(BASE)
        self.assertEqual(checks.new_numbers(allowed, "120,000 users"), [])
        self.assertEqual(checks.new_numbers(allowed, "a team of 5+"), ["5+"])

    def test_number_words(self):
        allowed = checks.cv_text(BASE)
        # a count/growth word needs the same word in the base or facts, not just the digit
        self.assertEqual(checks.new_numbers(allowed, "wrote three white papers"), ["three"])
        self.assertEqual(checks.new_numbers(allowed, "led a team of twelve"), ["twelve"])
        self.assertEqual(checks.new_numbers(allowed, "from day one"), [])
        for s, hit in (("doubled retention", "doubled"), ("dozens of projects", "dozens"),
                       ("a tenfold rise", "tenfold"), ("half a million users", "half a million"),
                       ("thousands of users", "thousands")):
            self.assertIn(hit, checks.new_numbers(allowed, s), s)
        self.assertEqual(checks.new_numbers(allowed, "scaffold someone alone"), [])

    def test_suffix_tokens(self):
        allowed = checks.cv_text(BASE)
        for s, tok in (("up 40%", "40%"), ("3x the plan", "3x"), ("ranked #1", "#1"), ("1st place", "1st"),
                       ("120k+ users", "120k+"), ("1.5M users", "1.5M"), ("150K", "150K")):
            self.assertEqual(checks.new_numbers(allowed, s), [tok], s)
        self.assertEqual(checks.new_numbers(allowed, "120000+ users, 150 000 subscribers, GA4, B1"), [])

    def test_curated_facts_numbers(self):
        allowed = checks.cv_text(BASE) + " " + " ".join(checks.load_allowed_numbers())
        self.assertEqual(checks.new_numbers(allowed, "a 12-company ranking, sizing 2022-2030"), [])
        self.assertEqual(checks.new_numbers(allowed, "since 08.10.2026"), ["08.10"])


class Sanitize(unittest.TestCase):
    def setUp(self):
        self.terms, self.cs = checks.load_never_claim()

    def test_markup(self):
        self.assertTrue(checks.markup_ok("Reached <b>120,000+ users</b> in <i>year</i> one"))
        for bad in ("docs <SQL>", "docs <led a team of 50 engineers>", "<b>docs</b><b>", "a < b", "x >",
                    "<b><i>x</b></i>", "<script>alert(1)</script>", "<img src=x onerror=alert(2)>"):
            self.assertFalse(checks.markup_ok(bad), bad)
        self.assertFalse(checks.markup_ok("<b>Jira</b>", rich=False))

    def test_checks_see_printed_text(self):
        self.assertEqual(checks.blocklist_hits("Wrote docs <SQL>", self.terms, self.cs), ["SQL"])
        self.assertEqual(checks.new_numbers(checks.cv_text(BASE), "docs <led a team of 50 engineers>"), ["50"])
        self.assertIn("<SQL>", checks.strip_tags("<b>Wrote</b> docs <SQL>"))

    def test_bad_markup_fields(self):
        new = copy.deepcopy(BASE)
        new["experience"][0]["bullets"][2] += " <won award>"
        new["skills"][0]["items"] += ", <b>Jira</b>"
        self.assertEqual([p for p, _ in checks.bad_markup(new)], ["experience[0].bullets[2]", "skills[0]"])

    def test_variants_and_normalisation(self):
        for s, hit in (("chat-gpt", "ChatGPT"), ("Chat GPT", "ChatGPT"), ("Chat\u200bGPT", "ChatGPT"),
                       ("\uff23\uff48\uff41\uff54\uff27\uff30\uff34", "ChatGPT"), ("S.Q.L queries", "SQL"),
                       ("Power-BI", "Power BI"), ("PowerBI", "Power BI"), ("PostgreSQL", "PostgreSQL"),
                       ("MySQL", "MySQL"), ("NoSQL", "NoSQL"), ("split testing", "split test"),
                       ("customer interviews", "customer interview"), ("a user interview", "user interview"),
                       ("python3", "Python"), ("Tools: Linear, Jira", "Linear"), ("tickets in Linear", "Linear")):
            self.assertIn(hit, checks.blocklist_hits(s, self.terms, self.cs), s)
        for s in ("SQLite", "Scrumptious", "R&D", "Linear growth of users", "linear growth", "Pythonic"):
            self.assertEqual(checks.blocklist_hits(s, self.terms, self.cs), [], s)


class Structure(unittest.TestCase):
    def test_foreign_bullet(self):
        new = copy.deepcopy(BASE)
        new["experience"][1]["bullets"][2] = BASE["experience"][2]["bullets"][1]
        self.assertEqual([p for p, _ in checks.foreign_bullets(BASE, new)], ["experience[1].bullets[2]"])
        self.assertEqual(checks.foreign_bullets(BASE, copy.deepcopy(BASE)), [])

    def test_duplicate_dropped(self):
        new = copy.deepcopy(BASE)
        new["experience"][0]["bullets"][3] = new["experience"][0]["bullets"][2]
        r = checks.dedupe_bullets({"content": new, "reverted": []})
        self.assertEqual(len(r["content"]["experience"][0]["bullets"]), 4)
        self.assertEqual(r["reverted"][0]["why"], "duplicate bullet dropped")

    def test_budgets(self):
        b = checks.budgets(BASE)
        self.assertEqual(b["bullet"], 119)
        self.assertEqual(b["bullets"], [5, 3, 4, 3])
        self.assertEqual(b["summary"], int(len(BASE["summary"]) * 1.05))  # rule: +5% on the base summary
        self.assertEqual(b["lines"], 23)  # summary 3 + bullets 16 + skills 4, as in the approved PDF

    def test_length_guard(self):
        new = copy.deepcopy(BASE)
        new["experience"][1]["bullets"].append("Ran market and competitor analysis for the card product")  # 4 > 3
        new["experience"][2]["bullets"][0] = "Wrote white papers " * 8                                    # > 119
        new["summary"] = BASE["summary"] + " Also a long extra sentence about many things."                # > summary budget
        new["skills"][3]["items"] += ", Notion AI, Figma AI, Canva AI, many more assistants"             # > 62
        out = checks.length_guard(BASE, {"content": new, "reverted": [], "changes": []})
        c = out["content"]
        self.assertEqual(c["experience"][1]["bullets"], BASE["experience"][1]["bullets"])
        self.assertEqual(c["summary"], BASE["summary"])
        self.assertEqual(c["skills"], BASE["skills"])
        # no base bullet is similar enough to the padded line, so it is dropped rather than guessed
        self.assertEqual(c["experience"][2]["bullets"], BASE["experience"][2]["bullets"][1:])
        self.assertEqual(sum(w["why"].startswith("length") for w in out["reverted"]), 4)

    def test_length_guard_printed_lines(self):
        new = copy.deepcopy(BASE)
        for e in new["experience"]:  # the suffix pushes the longer bullets onto a second line
            e["bullets"] = [b + " with the team in Jira" for b in e["bullets"]]
        self.assertGreater(checks.total_lines(new, BASE), checks.budgets(BASE)["lines"])
        out = checks.length_guard(BASE, {"content": new, "reverted": [], "changes": []})
        self.assertLessEqual(checks.total_lines(out["content"], BASE), checks.budgets(BASE)["lines"])
        self.assertTrue(all("lines" in r["why"] or "chars" in r["why"] for r in out["reverted"]))
        kept = sum(b.endswith(" with the team in Jira") for e in out["content"]["experience"] for b in e["bullets"])
        self.assertGreater(kept, 3)  # only the lines that wrap go back

    def test_measure_matches_approved_layout(self):
        from cv.tailor import measure
        self.assertEqual([measure.lines(x) for e in BASE["experience"] for x in e["bullets"]],
                         [1, 1, 1, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1])
        self.assertEqual(measure.lines(BASE["summary"], "summary"), 3)


class QARound2(unittest.TestCase):
    def setUp(self):
        self.terms, self.cs = checks.load_never_claim()

    def test_skill_groups_locked_by_name(self):
        new = copy.deepcopy(BASE)
        new["skills"] = [{"group": "AI", "items": "Claude, Cursor"},
                         {"group": "Tooling", "items": "Jira, Notion"},       # renamed 'Tools'
                         {"group": "product", "items": "Roadmapping"},       # case differs: same group
                         {"group": "Go-to-market & Web3", "items": ""}]      # empty: base line comes back
        out, violations = checks.enforce_locked(BASE, new)
        self.assertEqual(sorted(s["group"] for s in out["skills"]), sorted(s["group"] for s in BASE["skills"]))
        by = {s["group"]: s["items"] for s in out["skills"]}
        self.assertEqual(by["AI"], "Claude, Cursor")
        self.assertEqual(by["Product"], "Roadmapping")
        self.assertEqual(by["Tools"], BASE["skills"][2]["items"])
        self.assertEqual(by["Go-to-market & Web3"], BASE["skills"][1]["items"])
        self.assertEqual(violations, [])

    def test_skill_revert_never_drops_a_group(self):
        new = copy.deepcopy(BASE)
        new["skills"][3]["items"] = "Claude, ChatGPT"
        out, _ = checks.revert_field(BASE, new, "skills[3]")
        self.assertEqual(out["skills"], BASE["skills"])

    def test_glyphs_outside_font(self):
        self.assertEqual(checks.uncovered(checks.cv_text(BASE)), [])
        self.assertEqual(checks.uncovered("about \u2248 90, EUR \u20ac, \u00b1 5"), [])  # in the font: measured normally
        for text in ("Launched the app \U0001F680", "Roadmap \u2192 release", "Done \u2713",
                     "Rated \u2605", "Market \u5e02\u573a"):
            self.assertTrue(checks.uncovered(text), text)
        new = copy.deepcopy(BASE)
        new["experience"][3]["bullets"][2] = "<b>Exited the business in 2022</b> \U0001F680"
        self.assertEqual([p for p, _ in checks.bad_glyphs(new)], ["experience[3].bullets[2]"])

    def test_mixed_script(self):
        self.assertEqual(checks.mixed_script("Pyth\u043en and \u0421QL"), ["Pyth\u043en", "\u0421QL"])
        self.assertEqual(checks.mixed_script(checks.cv_text(BASE) + " \u041f\u0440\u0438\u0432\u0435\u0442"), [])

    def test_new_formatting_stripped(self):
        new = copy.deepcopy(BASE)
        for e in new["experience"]:
            e["bullets"] = ["<b>" + checks.strip_tags(b) + "</b>" for b in e["bullets"]]
        new["summary"] = "<i>" + BASE["summary"] + "</i>"
        out = checks.strip_new_formatting(BASE, new)
        self.assertNotIn("<", out["summary"])
        self.assertEqual(out["experience"][0]["bullets"][0], BASE["experience"][0]["bullets"][0])
        self.assertEqual(out["experience"][3]["bullets"][2], "Exited the business in 2022")  # not bold in base since 09.10
        self.assertEqual(out["experience"][0]["bullets"][1], checks.strip_tags(BASE["experience"][0]["bullets"][1]))
        self.assertEqual(checks.changed_fields(BASE, out), [])
        moved = copy.deepcopy(BASE)
        moved["summary"] = "Reached <b>120,000+ users</b> in year one."  # a base span may be used elsewhere
        self.assertIn("<b>120,000+ users</b>", checks.strip_new_formatting(BASE, moved)["summary"])

    def test_case_only_edit_is_a_change(self):
        new = copy.deepcopy(BASE)
        new["experience"][0]["bullets"][2] = BASE["experience"][0]["bullets"][2].upper()
        self.assertEqual([p for p, _ in checks.changed_fields(BASE, new)], ["experience[0].bullets[2]"])

    def test_empty_bullets_dropped(self):
        new = copy.deepcopy(BASE)
        new["experience"][0]["bullets"][1] = "<b></b>"
        new["experience"][0]["bullets"][2] = "  "
        out, violations = checks.enforce_locked(BASE, new)
        self.assertEqual(out["experience"][0]["bullets"], BASE["experience"][0]["bullets"])  # 3 left: outside +-1
        self.assertEqual(violations, ["experience[0].bullets"])
        new["experience"][0]["bullets"][2] = BASE["experience"][0]["bullets"][2]
        out, violations = checks.enforce_locked(BASE, new)
        self.assertEqual(len(out["experience"][0]["bullets"]), 4)
        self.assertEqual(violations, [])

    def test_surrogate_scrubbed_and_dashes_plain(self):
        self.assertEqual(checks.clean("Jira\ud800 tickets"), "Jira tickets")
        self.assertEqual(checks.clean("research \u2014 launch \u2013 growth"), "research - launch - growth")

    def test_blocklist_round2(self):
        for s, hit in (("statistics in R.", "R"), ("Excel, R, Jira", "R"), ("GPT-4 prompts", "GPT-4"),
                       ("built on GPT-4o", "GPT-4o"), ("OpenAI API calls", "OpenAI API"), ("LangChain agents", "LangChain")):
            self.assertIn(hit, checks.blocklist_hits(s, self.terms, self.cs), s)
        for s in ("R&D", "R.E.M.", "GPT-4o mini"):
            self.assertNotIn("R", checks.blocklist_hits(s, self.terms, self.cs), s)


class Coverage(unittest.TestCase):
    def test_half(self):
        self.assertEqual(checks.coverage(["Product Owner", "Jira"], "Jira and Confluence"), 0.5)

    def test_empty_terms(self):
        self.assertEqual(checks.coverage([], "anything"), 1.0)

    def test_whole_phrase_only(self):
        self.assertEqual(checks.coverage(["Jira"], "Jirafication"), 0.0)

    def test_plurals_and_hyphens(self):
        self.assertEqual(checks.coverage(["Product Owner", "market research", "KPIs"],
                                         "Product Owners; market-research; KPI tracking"), 1.0)

    def test_overused(self):
        text = "Kanban one. Kanban two. kanban three. Kanban four. Jira once."
        self.assertEqual(checks.overused(["Kanban", "Jira"], text), ["Kanban"])
        self.assertEqual(checks.overused(["Kanban"], "Kanban " * 3), [])


class Text(unittest.TestCase):
    def test_cv_text_order_and_tags(self):
        text = checks.cv_text(BASE)
        self.assertNotIn("<b>", text)
        self.assertIn("120,000+ users", text)
        order = [text.index(h) for h in ("Alex Morgan", "Summary", "Experience", "Education", "Skills",
                                          "Languages", "Certifications", "Extracurricular Activities")]
        self.assertEqual(order, sorted(order))

    def test_german_words(self):
        self.assertEqual(checks.german_words(checks.cv_text(BASE)), [])
        self.assertEqual(checks.german_words("Marktforschung und Produktmanagement"),
                         ["Marktforschung", "und", "Produktmanagement"])
        self.assertEqual(checks.german_words("Anforderungsmanagement tickets, Planung"),
                         ["Anforderungsmanagement", "Planung"])
        self.assertEqual(checks.german_words("young among stakeholders, positioning, mentions, Kanban"), [])


class Fields(unittest.TestCase):
    def test_changed_fields(self):
        new = copy.deepcopy(BASE)
        new["experience"][0]["bullets"][1] = "Reached 120,000+ users in year one"
        new["experience"][0]["bullets"].reverse()  # reorder alone is not a change
        changed = checks.changed_fields(BASE, new)
        self.assertEqual([t for _, t in changed], ["Reached 120,000+ users in year one"])

    def test_revert_bullet_to_base(self):
        new = copy.deepcopy(BASE)
        before = BASE["experience"][0]["bullets"][3]
        new["experience"][0]["bullets"][3] = before + " and SQL dashboards"
        out, restored = checks.revert_field(BASE, new, "experience[0].bullets[3]")
        self.assertEqual(out["experience"][0]["bullets"][3], before)
        self.assertEqual(restored, before)

    def test_revert_new_bullet_drops_it(self):
        new = copy.deepcopy(BASE)
        new["experience"][1]["bullets"].append("Built SQL dashboards for the CEO weekly review")
        out, restored = checks.revert_field(BASE, new, "experience[1].bullets[3]")
        self.assertEqual(out["experience"][1]["bullets"], BASE["experience"][1]["bullets"])
        self.assertIsNone(restored)

    def test_revert_summary_and_skill(self):
        new = copy.deepcopy(BASE)
        new["summary"] = "Python expert."
        new["skills"][2]["items"] = "Python, Jira"
        out, _ = checks.revert_field(BASE, new, "summary")
        out, _ = checks.revert_field(BASE, out, "skills[2].items")
        self.assertEqual(out["summary"], BASE["summary"])
        self.assertEqual(out["skills"][2]["items"], BASE["skills"][2]["items"])


if __name__ == "__main__":
    unittest.main()
