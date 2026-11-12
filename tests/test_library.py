"""Library mode (09.10.2026): validation of cv/library.json and every selection rule, without Claude."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cv.tailor import checks, library  # noqa: E402

BASE = json.loads((ROOT / "cv" / "source" / "content" / "en.json").read_text(encoding="utf-8"))
LIB = json.loads(library.LIBRARY.read_text(encoding="utf-8"))
BASE_IDS = [["pm1", "pm3", "pm4", "pm5", "pm6"], ["fi1", "fi2", "fi3"], ["ra1", "ra2", "ra3", "ra4"], ["fo1", "fo2", "fo3"]]


def pick(roles=None, summary="A_product", swaps=None):
    return {"summary": summary, "roles": copy.deepcopy(roles or BASE_IDS), "swaps": swaps or []}


def with_role(i, ids):
    roles = copy.deepcopy(BASE_IDS)
    roles[i] = ids
    return roles


def texts(sel):
    return [[b["text"] for b in r] for r in sel["roles"]]


class Validation(unittest.TestCase):
    def broken(self, edit):
        lib = copy.deepcopy(LIB)
        edit(lib)
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "library.json"
            path.write_text(json.dumps(lib), encoding="utf-8")
            with self.assertRaises(library.LibraryError) as ctx:
                library.load(BASE, path)
        return str(ctx.exception)

    def test_real_library_matches_base(self):
        lib = library.load(BASE)
        self.assertEqual(library.base_ids(BASE, lib), BASE_IDS)
        self.assertEqual(library.base_summary_id(BASE, lib), "A_product")

    def test_base_bullet_missing_fails(self):
        self.assertIn("base bullet not in the library", self.broken(lambda l: l["roles"][0]["bullets"].pop("pm4")))

    def test_base_summary_must_be_summary_a(self):
        def edit(l):
            l["summaries"]["A_product"] = l["summaries"]["A_product"].replace("Codex.", "Codex daily.")
        self.assertIn("base summary is not summary A", self.broken(edit))

    def test_blocklisted_text_fails(self):
        self.assertIn("never-claim SQL", self.broken(lambda l: l["roles"][0]["bullets"].update(pm9="Wrote SQL reports")))

    def test_new_number_fails(self):
        msg = self.broken(lambda l: l["summaries"].update(D_x="Grew a channel to 500,000 users"))
        self.assertIn("new number 500,000", msg)

    def test_glyph_and_german_fail(self):
        msg = self.broken(lambda l: l["roles"][3]["bullets"].update(fo9="Launched \U0001F680", fo8="Marktforschung done"))
        self.assertIn("glyph", msg)
        self.assertIn("German Marktforschung", msg)

    def test_synonym_phrase_checked(self):
        self.assertIn("never-claim Scrum", self.broken(lambda l: l["synonyms"].append(["Kanban", "Scrum"])))

    def test_role_order_and_rules_checked(self):
        self.assertIn("is not the base role", self.broken(lambda l: l["roles"][0].update(role="Product Owner")))
        self.assertIn("must_keep id pm99", self.broken(lambda l: l["roles"][0].update(must_keep=["pm99"])))
        self.assertIn("base selection breaks a rule",
                      self.broken(lambda l: l["roles"][2].update(exclusive=[["ra1", "ra2"]])))

    def test_pin_last_checked_at_load(self):
        self.assertIn("pin_last id 'fo9' not in the library", self.broken(lambda l: l["roles"][3].update(pin_last="fo9")))
        self.assertIn("pin_last fo1 is not last in the base CV", self.broken(lambda l: l["roles"][3].update(pin_last="fo1")))

    def test_malformed_structure_is_library_error(self):
        """QA 09.10: bullets as a list raised AttributeError instead of LibraryError."""
        edits = {"bullets list": lambda l: l["roles"][0].update(bullets=["a", "b"]),
                 "role not object": lambda l: l["roles"].__setitem__(1, "Founder"),
                 "must_keep int": lambda l: l["roles"][0].update(must_keep=3),
                 "exclusive str": lambda l: l["roles"][2].update(exclusive="ra3,ra5"),
                 "exclusive int pair": lambda l: l["roles"][2].update(exclusive=[[1, 2]]),
                 "summaries list": lambda l: l.update(summaries=["x"]),
                 "synonyms dict": lambda l: l.update(synonyms={"a": "b"}),
                 "roles dict": lambda l: l.update(roles={"0": {}})}
        for name, edit in edits.items():
            self.assertTrue(self.broken(edit), name)

    def test_required_keys_and_typos(self):
        self.assertIn("synonyms key missing", self.broken(lambda l: l.pop("synonyms")))
        self.assertIn("role 0: must_keep key missing", self.broken(lambda l: l["roles"][0].pop("must_keep")))
        self.assertIn("unknown key 'must_kep'", self.broken(lambda l: l["roles"][0].update(must_kep=["pm1"])))
        self.assertIn("unknown key 'synonym'", self.broken(lambda l: l.update(synonym=[])))

    def test_overlapping_synonym_groups_fail(self):
        msg = self.broken(lambda l: l["synonyms"].append(["specs", "requirements docs"]))
        self.assertIn('synonym "specs" is in two groups', msg)

    def test_unreadable_library_fails(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "library.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(library.LibraryError):
                library.load(BASE, path)


class Choose(unittest.TestCase):
    def choose(self, answer):
        return library.choose(BASE, LIB, answer)

    def assert_role_base(self, sel, reverted, i, why):
        self.assertEqual(texts(sel)[i], BASE["experience"][i]["bullets"])
        entry = next(r for r in reverted if r["field"] == f"experience[{i}]")
        self.assertIn(why, entry["why"])

    def test_valid_selection_applied(self):
        sel, reverted = self.choose(pick(with_role(0, ["pm3", "pm2", "pm7"]), summary="B_research"))
        self.assertEqual(reverted, [])
        self.assertEqual(sel["summary"]["text"], LIB["summaries"]["B_research"])
        self.assertEqual(texts(sel)[0], [LIB["roles"][0]["bullets"][k] for k in ("pm3", "pm2", "pm7")])

    def test_unknown_id_and_id_of_other_role(self):
        for ids in (["pm3", "pm42"], ["pm3", "ra1"]):
            sel, reverted = self.choose(pick(with_role(0, ids)))
            self.assert_role_base(sel, reverted, 0, "unknown id for this role")

    def test_count_limits(self):
        sel, reverted = self.choose(pick(with_role(1, ["fi1", "fi2", "fi3", "fi1"])))
        self.assert_role_base(sel, reverted, 1, "allowed 1-3")
        sel, reverted = self.choose(pick(with_role(3, [])))
        self.assert_role_base(sel, reverted, 3, "0 bullets")

    def test_duplicate_id(self):
        sel, reverted = self.choose(pick(with_role(1, ["fi1", "fi1"])))
        self.assert_role_base(sel, reverted, 1, "repeats")

    def test_must_keep(self):
        sel, reverted = self.choose(pick(with_role(3, ["fo1", "fo2"])))
        self.assert_role_base(sel, reverted, 3, "must keep fo3")
        sel, reverted = self.choose(pick(with_role(0, ["pm1", "pm2"])))
        self.assert_role_base(sel, reverted, 0, "must keep pm3")

    def test_exclusive_pairs(self):
        for ids in (["ra1", "ra3", "ra5"], ["ra1", "ra6", "ra3"]):
            sel, reverted = self.choose(pick(with_role(2, ids)))
            self.assert_role_base(sel, reverted, 2, "exclude each other")
        sel, reverted = self.choose(pick(with_role(1, ["fi3", "fi4"])))
        self.assert_role_base(sel, reverted, 1, "exclude each other")
        sel, reverted = self.choose(pick(with_role(2, ["ra1", "ra5", "ra6"])))  # ra5 + ra6 is allowed
        self.assertEqual(reverted, [])

    def test_bad_role_does_not_touch_other_roles(self):
        sel, reverted = self.choose(pick(with_role(2, ["ra9"]), summary="C_ai"))
        self.assertEqual(sel["summary"]["id"], "C_ai")
        self.assertEqual([r["field"] for r in reverted], ["experience[2]"])

    def test_unknown_summary_id_falls_back_to_a(self):
        for sid in ("D_sales", None, 3):
            sel, reverted = self.choose(pick(summary=sid))
            self.assertEqual(sel["summary"]["text"], BASE["summary"])
            self.assertEqual(reverted[0]["field"], "summary")

    def test_pin_last_moved_to_the_end(self):
        self.assertEqual(LIB["roles"][3]["pin_last"], "fo3")
        sel, reverted = self.choose(pick(with_role(3, ["fo3", "fo1", "fo2"])))
        self.assertEqual(reverted, [])
        self.assertEqual([b["id"] for b in sel["roles"][3]], ["fo1", "fo2", "fo3"])
        sel, _ = self.choose(pick(with_role(3, ["fo1", "fo3"])))
        self.assertEqual([b["id"] for b in sel["roles"][3]], ["fo1", "fo3"])
        sel, _ = self.choose(pick(with_role(0, ["pm3", "pm1"])))  # a role without pin_last keeps the model's order
        self.assertEqual([b["id"] for b in sel["roles"][0]], ["pm3", "pm1"])

    def test_missing_roles_logged(self):
        sel, reverted = self.choose({"summary": "A_product", "roles": [["pm3", "pm1"]]})
        self.assertEqual(texts(sel)[0], [LIB["roles"][0]["bullets"][k] for k in ("pm3", "pm1")])
        self.assertEqual(texts(sel)[1:], [e["bullets"] for e in BASE["experience"][1:]])
        self.assertEqual([r["field"] for r in reverted], ["experience[1]", "experience[2]", "experience[3]"])


class Swaps(unittest.TestCase):
    def choose(self, swaps, roles=None, summary="A_product"):
        return library.choose(BASE, LIB, pick(roles, summary, swaps))

    def test_valid_swap(self):
        sel, reverted = self.choose([{"bullet": "pm6", "from": "the owners", "to": "stakeholders"}])
        self.assertEqual(reverted, [])
        self.assertEqual(texts(sel)[0][4], "Worked directly with stakeholders and with marketing on the go-to-market strategy")
        self.assertEqual(sel["swaps"], [{"bullet": "pm6", "from": "the owners", "to": "stakeholders"}])

    def test_summary_swap(self):
        sel, reverted = self.choose([{"bullet": "summary", "from": "specs", "to": "product requirements"}])
        self.assertEqual(reverted, [])
        self.assertIn("roadmap, product requirements, tickets", sel["summary"]["text"])

    def test_cross_group_swap_reverts_role(self):
        sel, reverted = self.choose([{"bullet": "pm6", "from": "owners", "to": "KPIs"}])
        self.assertEqual(texts(sel)[0], BASE["experience"][0]["bullets"])
        self.assertIn("not a listed synonym pair", reverted[0]["why"])
        self.assertEqual(sel["swaps"], [])

    def test_free_text_swap_reverts_role(self):
        sel, reverted = self.choose([{"bullet": "pm6", "from": "owners", "to": "the C-suite"}])
        self.assertEqual(texts(sel)[0], BASE["experience"][0]["bullets"])

    def test_phrase_must_occur_case_sensitive(self):
        for src in ("Owners", "KPIs"):
            other = "stakeholders" if src == "Owners" else "metrics"
            sel, reverted = self.choose([{"bullet": "pm6", "from": src, "to": other}])
            self.assertEqual(texts(sel)[0], BASE["experience"][0]["bullets"], src)
            self.assertEqual(reverted[0]["field"], "experience[0]")

    def test_first_occurrence_only_and_whole_words(self):
        groups = LIB["synonyms"]
        self.assertEqual(library._swap("metrics and metrics", {"from": "metrics", "to": "KPIs"}, groups),
                         ("KPIs and metrics", ""))
        self.assertTrue(library._swap("biometrics", {"from": "metrics", "to": "KPIs"}, groups)[1])
        self.assertTrue(library._swap("Jira tickets", {"from": "tickets", "to": "Jira tickets"}, groups)[1])

    def test_max_two_swaps_per_bullet(self):
        two = [{"bullet": "pm4", "from": "product docs", "to": "specs"}, {"bullet": "pm4", "from": "tickets", "to": "Jira tickets"}]
        sel, reverted = self.choose(two)
        self.assertEqual(reverted, [])
        self.assertEqual(texts(sel)[0][2], "Wrote the specs, roadmap and Jira tickets for developers and the designer; prepared wireframes")
        three = two + [{"bullet": "pm4", "from": "wireframes", "to": "wireframes in Figma"}]
        sel, reverted = self.choose(three)
        self.assertEqual(texts(sel)[0], BASE["experience"][0]["bullets"])
        self.assertIn("max 2", reverted[0]["why"])

    def test_swap_on_unselected_bullet_drops_only_that_swap(self):
        sel, reverted = self.choose([{"bullet": "pm7", "from": "Jira", "to": "x"},
                                     {"bullet": "pm4", "from": "tickets", "to": "Jira tickets"}],
                                    roles=with_role(0, ["pm3", "pm4", "pm1"]))
        self.assertEqual([b["id"] for b in sel["roles"][0]], ["pm3", "pm4", "pm1"])   # the selection stays
        self.assertIn("Jira tickets", texts(sel)[0][1])                               # the other swap too
        self.assertEqual([(r["field"], "not selected" in r["why"]) for r in reverted], [("swaps", True)])
        self.assertEqual(sel["swaps"], [{"bullet": "pm4", "from": "tickets", "to": "Jira tickets"}])

    def test_wrong_typed_ids_and_swap_targets_revert_only_that_unit(self):
        """QA 09.10: ints, lists, dicts, NaN crashed the whole run with TypeError."""
        for bad in ([1, 2, 3], [["pm1"], "pm3"], [{"id": "pm1"}], [float("nan")], 5, {"a": 1}, None):
            sel, reverted = self.choose([], roles=with_role(1, bad) if bad is not None else with_role(1, ["fi1", "fi1"]))
            self.assertEqual(texts(sel)[1], BASE["experience"][1]["bullets"], bad)
            self.assertEqual([r["field"] for r in reverted], ["experience[1]"], bad)
        for target in (["pm6"], {"id": "pm6"}, 3, float("nan")):
            sel, reverted = self.choose([{"bullet": target, "from": "the owners", "to": "stakeholders"},
                                         {"bullet": "pm4", "from": "tickets", "to": "Jira tickets"}])
            self.assertEqual([r["field"] for r in reverted], ["swaps"], target)
            self.assertIn("Jira tickets", texts(sel)[0][2])
        sel, reverted = self.choose([], summary=["A_product"])
        self.assertEqual(sel["summary"]["text"], BASE["summary"])

    def test_bad_summary_swap_reverts_summary_only(self):
        sel, reverted = self.choose([{"bullet": "summary", "from": "Claude", "to": "ChatGPT"},
                                     {"bullet": "pm6", "from": "the owners", "to": "stakeholders"}], summary="C_ai")
        self.assertEqual(sel["summary"]["text"], BASE["summary"])
        self.assertIn("stakeholders", texts(sel)[0][4])
        self.assertEqual([r["field"] for r in reverted], ["summary"])

    def test_unknown_target_ignored_and_logged(self):
        sel, reverted = self.choose([{"bullet": "zz1", "from": "owners", "to": "stakeholders"}, "junk"])
        self.assertEqual(texts(sel), [e["bullets"] for e in BASE["experience"]])
        self.assertEqual([r["field"] for r in reverted], ["swaps", "swaps"])

    def test_swaps_go_with_a_failed_role(self):
        sel, reverted = self.choose([{"bullet": "pm6", "from": "owners", "to": "stakeholders"}],
                                    roles=with_role(0, ["pm6", "pm6"]))
        self.assertEqual(texts(sel)[0], BASE["experience"][0]["bullets"])
        self.assertEqual(sel["swaps"], [])


class Assemble(unittest.TestCase):
    def test_only_library_text_and_locked_fields(self):
        sel, _ = library.choose(BASE, LIB, pick(with_role(2, ["ra5", "ra1", "ra7"]), summary="B_research",
                                                swaps=[{"bullet": "fi2", "from": "market and competitor analysis",
                                                        "to": "market and competitive research"}]))
        content = library.assemble(BASE, sel)
        for key in checks.LOCKED_TOP:
            self.assertEqual(content[key], BASE[key])
        for e, b in zip(content["experience"], BASE["experience"]):
            self.assertEqual({k: e[k] for k in checks.LOCKED_ROLE if k in b}, {k: b[k] for k in checks.LOCKED_ROLE if k in b})
        self.assertEqual(content["experience"][1]["bullets"][1],
                         "Ran market and competitive research to inform positioning and feature decisions")
        lib_texts = {t for r in LIB["roles"] for t in r["bullets"].values()}
        self.assertTrue(set(content["experience"][2]["bullets"]) <= lib_texts)

    def test_changes_from_selection(self):
        sel, _ = library.choose(BASE, LIB, pick(with_role(0, ["pm3", "pm1", "pm2", "pm4", "pm5"]), summary="B_research"))
        content = library.assemble(BASE, sel)
        ch = library.changes(BASE, LIB, sel, content)
        reasons = [c["reason"] for c in ch]
        self.assertIn("summary variant B_research", reasons)
        self.assertIn("library bullet pm2", reasons)
        self.assertIn("dropped pm6 for this ad", reasons)
        self.assertIn("bullets reordered", reasons)


# The summary C of 08.10 (4 printed lines); Misha shortened C on 09.10, so length tests use this one.
LONG_SUMMARY = ("Product manager with 3+ years in Web3 and fintech. Joined a crypto market data platform at day one and "
                "defined it from research and positioning to launch: roadmap, specs, tickets and GA4 product metrics; "
                "120,000+ users in year one. Founded and exited a Telegram media network (150,000+ subscribers). "
                "Works daily with Claude, Codex and Cursor for research, documentation and prototyping.")
LONG_LIB = copy.deepcopy(LIB)
LONG_LIB["summaries"]["D_long"] = LONG_SUMMARY


class SkillsWhitelist(unittest.TestCase):
    def write(self, edit):
        raw = json.loads(checks.SKILLS_ALLOWED.read_text(encoding="utf-8"))
        edit(raw)
        t = tempfile.mkdtemp()
        path = Path(t) / "skills_allowed.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path

    def test_real_whitelist_loads(self):
        allowed = library.load_skills(BASE)
        self.assertIn("jira (kanban)", allowed["tools"])

    def test_bad_whitelists_fail(self):
        cases = {"never-claim": lambda r: r["Tools"].append("Tableau"),
                 "base item \"Figma\" not listed": lambda r: r["Tools"].remove("Figma"),
                 'base group "AI" missing': lambda r: r.pop("AI"),
                 "must be": lambda r: r.update(Tools="Jira")}
        for want, edit in cases.items():
            with self.assertRaises(library.LibraryError) as ctx:
                library.load_skills(BASE, self.write(edit))
            self.assertIn(want, str(ctx.exception))

    def test_allowed_items(self):
        allowed = library.load_skills(BASE)
        kept, dropped = checks.allowed_items("Tools", "jira, Figma, Led, figma, Kanban", allowed)
        self.assertEqual(kept, ["Jira", "Figma"])
        self.assertEqual([x for x, _ in dropped], ["Led", "figma", "Kanban"])
        kept, dropped = checks.allowed_items("AI", ", ".join(["Claude", "Codex", "Cursor", "Perplexity", "Gemini",
                                                                   "Midjourney", "Stitch", "AI-assisted prototyping",
                                                                   "AI-assisted research", "Prompt engineering"] * 2), allowed)
        self.assertEqual(len(kept), 9)  # product 09.10: 'Prompt engineering' left the whitelist (QA cvtailor-12)
        self.assertIn("Prompt engineering", [x for x, _ in dropped])
        tools = json.loads(checks.SKILLS_ALLOWED.read_text(encoding="utf-8"))["Tools"]
        kept, dropped = checks.allowed_items("Tools", ", ".join(tools), allowed)
        self.assertEqual(len(kept), checks.MAX_SKILL_ITEMS)  # the per-line cap still holds
        kept, _ = checks.allowed_items("Tools", "Jira, " * 50000, allowed)
        self.assertEqual(kept, ["Jira"])


class LengthGuard(unittest.TestCase):
    def result(self, answer, lib=LONG_LIB):
        sel, rev = library.choose(BASE, lib, answer)
        r = {"content": library.assemble(BASE, sel), "selection": sel, "changes": [], "reverted": rev}
        return library.refresh(BASE, lib, r)

    def test_every_summary_variant_fits_the_base_lines(self):
        known = checks._known(BASE)
        base_lines = checks._field_lines("summary", BASE["summary"], known)
        for sid, text in LIB["summaries"].items():
            self.assertLessEqual(checks._field_lines("summary", text, known), base_lines, sid)
        self.assertGreater(checks._field_lines("summary", LONG_SUMMARY, known), base_lines)  # the test fixture is long

    def test_short_summary_c_kept_on_a_full_page(self):
        r = library.length_guard(BASE, LIB, self.result(pick(summary="C_ai"), LIB))
        self.assertEqual(r["content"]["summary"], LIB["summaries"]["C_ai"])
        self.assertEqual(r["reverted"], [])

    def test_long_summary_reverted_when_page_full(self):
        r = library.length_guard(BASE, LONG_LIB, self.result(pick(summary="D_long")))  # 4 printed lines, base has 3
        self.assertEqual(r["content"]["summary"], BASE["summary"])
        self.assertIn("summary", [x["field"] for x in r["reverted"]])

    def test_long_summary_kept_when_a_bullet_is_dropped(self):
        r = library.length_guard(BASE, LONG_LIB, self.result(pick(with_role(0, ["pm1", "pm3", "pm4", "pm6"]), summary="D_long")))
        self.assertEqual(r["content"]["summary"], LONG_SUMMARY)
        self.assertEqual(r["reverted"], [])

    def with_skills(self, items_by_group):
        r = self.result(pick(), LIB)
        r["content"]["skills"] = [dict(s, items=items_by_group.get(s["group"], s["items"])) for s in BASE["skills"]]
        return library.refresh(BASE, LIB, r)

    def test_long_skill_line_loses_its_last_items(self):
        rbi = "Excel & Google Sheets, Word, PowerPoint, Jira (Kanban), Confluence, Google Analytics 4 (GA4), Notion"
        self.assertEqual(len("Tools: " + rbi), 107)  # the pilot's RBI line, budget 103
        r = library.length_guard(BASE, LIB, self.with_skills({"Tools": rbi}))
        self.assertEqual(r["content"]["skills"][2]["items"],
                         "Excel & Google Sheets, Word, PowerPoint, Jira (Kanban), Confluence, Google Analytics 4 (GA4)")
        self.assertEqual([(x["field"], x["text"]) for x in r["reverted"]], [("skills[2]", "Notion")])
        self.assertIn("107 > 103", r["reverted"][0]["why"])
        self.assertIn("skills[2]", [c["field"] for c in r["changes"]])
        self.assertLessEqual(len("Tools: " + r["content"]["skills"][2]["items"]), 103)

    def test_several_items_dropped_in_order(self):
        items = "Notion, Figma, Canva, Excel & Google Sheets, Google Analytics 4 (GA4), Jira (Kanban), Confluence, Word"
        r = library.length_guard(BASE, LIB, self.with_skills({"Tools": items}))
        kept = r["content"]["skills"][2]["items"]
        self.assertTrue(items.startswith(kept))
        self.assertLessEqual(len("Tools: " + kept), 103)
        self.assertEqual([x["text"] for x in r["reverted"]], [x.strip() for x in items[len(kept) + 1:].split(",")])

    def test_single_item_too_long_reverts_line(self):
        r = library.length_guard(BASE, LIB, self.with_skills({"AI": "x" * 70}))
        self.assertEqual(r["content"]["skills"][3], BASE["skills"][3])
        self.assertEqual([x["field"] for x in r["reverted"]], ["skills[3]"])

    def test_fitting_skill_line_untouched(self):
        r = library.length_guard(BASE, LIB, self.with_skills({"AI": "Claude, Codex"}))
        self.assertEqual(r["content"]["skills"][3]["items"], "Claude, Codex")
        self.assertEqual(r["reverted"], [])

    def test_fitting_selection_untouched(self):
        r = library.length_guard(BASE, LIB, self.result(pick(with_role(0, ["pm3", "pm2", "pm1", "pm4", "pm6"]))))
        self.assertEqual(r["reverted"], [])
        self.assertLessEqual(checks.total_lines(r["content"], BASE), checks.budgets(BASE)["lines"])


if __name__ == "__main__":
    unittest.main()
