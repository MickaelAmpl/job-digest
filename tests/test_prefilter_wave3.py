"""Wave 3 title rules (stage 7, 26.10.2026): AI edge, soft exclusions, graduate programmes, role groups,
German phrases, and the preview script. No network; the preview runs on a temp DB, never on data/."""
import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import prefilter  # noqa: E402
from scripts import apply_wave3, wave3_preview  # noqa: E402

RULES = {"keep", "senior", "excluded_field", "not_target_role", "german_required"}


def v(title, description=""):
    return prefilter.verdict({"title": title, "description": description})


def why(title):
    return prefilter.reason({"title": title})


# Titles that pass only through a role group, one list per group.
GROUP_TITLES = {
    "venture": ["Venture Associate", "Entrepreneur in Residence", "Inkubator Associate", "Accelerator Associate",
                "EIR Associate"],
    "strategy_research": ["Strategy Associate", "Strategie-Assistenz", "Associate, Strategy (m/w/d)",
                          "Research Associate", "Insights Manager"],
    "program_coordination": ["Program Coordinator", "Programme Coordinator (m/w/d)", "Programmkoordinator",
                             "Project Associate"],
    "operations_ai": ["Revenue Operations Manager", "RevOps Manager", "Business Enablement Manager",
                      "Marketing Operations Coordinator"],
    "crm_lifecycle": ["CRM Specialist", "Customer Lifecycle Manager", "Retention Manager"],
    "digital_consulting": ["Digital Consultant", "Technology Consultant", "Consultant (Digital)"],
    "marketing_associate": ["Marketing Associate", "Marketing Executive (m/w/d)"],
    "customer_success": ["Customer Success Manager", "Client Success Associate", "Kundenerfolg Managerin"],
    "sales": ["Business Development Representative", "Account Executive (m/f/d)", "Key Account Manager", "BDR"],
}

REQUIRED_GERMAN = ["fließend Deutsch", "fliessend Deutsch", "Deutsch fließend", "Deutsch (C2)", "Deutsch: C1",
                   "Deutschkenntnisse (C1)", "Deutsch auf C1-Niveau", "Deutsch - Muttersprache"]
SOFTENED_GERMAN = ["fließend Deutsch von Vorteil", "Deutsch (C2) von Vorteil", "Deutschkenntnisse (C1) wünschenswert",
                   "Fluent German is not required", "Sehr gute Deutschkenntnisse von Vorteil",
                   "Deutsch fließend von Vorteil"]


class AIEdge(unittest.TestCase):
    def test_ai_titles_pass_target(self):
        """Task A 1: AI_EDGE passes the TARGET check."""
        for t in ["AI Agents & Business Automation", "Digital Transformation Specialist",
                  "Marketing Operations Manager (AI & Automation)", "KI-Agent Manager",
                  "Künstliche Intelligenz Trainer", "Digitale Transformation Manager",
                  "Agentic Workflow Specialist"]:
            with self.subTest(t):
                self.assertEqual(v(t), "keep")
                self.assertEqual(why(t), "ai_edge")

    def test_soft_exclusions_lifted_by_ai_only(self):
        """Task A 1: AI_EDGE overrides business analyst, business operations, project coordinator,
        founder's associate and data analyst."""
        for t in ["AI Business Analyst", "Data Analyst (KI)", "AI Project Coordinator", "AI Founder's Associate",
                  "Business Operations (Automation)"]:
            with self.subTest(t):
                self.assertEqual(v(t), "keep")
                self.assertEqual(why(t), "ai_edge")

    def test_soft_exclusions_stay_without_ai(self):
        for t in ["Business Analyst", "Data Analyst", "Business Operations Manager", "Project Coordinator",
                  "Founder's Associate"]:
            with self.subTest(t):
                self.assertEqual(v(t), "excluded_field")
                self.assertIsNone(why(t))

    def test_hard_exclusions_hold_with_ai(self):
        """Task A 1: the rest of EXCLUDED still drops even with AI in the title."""
        for t in ["AI Engineer", "AI Customer Support", "Automation Engineer", "AI Developer",
                  "AI Financial Analyst", "CRM Developer", "Graduate Engineer"]:
            with self.subTest(t):
                self.assertEqual(v(t), "excluded_field")

    def test_senior_still_first(self):
        self.assertEqual(v("Senior AI Product Manager"), "senior")
        self.assertEqual(v("Head of Digital Transformation"), "senior")

    def test_ki_needs_word_boundary(self):
        """'ki' inside 'Kinderbetreuer' is not AI."""
        self.assertEqual(v("Kinderbetreuer"), "not_target_role")
        self.assertEqual(v("Office Manager"), "not_target_role")

    def test_plain_agent_is_not_ai_edge(self):
        """26.10.2026: 'agent' counts only as AI agent. Customer and backoffice agents are not AI work."""
        for t in ["French Speaking Customer Experience Agent", "Backoffice Agent (mensch*)", "Insurance Agent"]:
            with self.subTest(t):
                self.assertEqual(v(t), "not_target_role")
                self.assertIsNone(why(t))

    def test_digitalisierung_alone_is_not_ai_edge(self):
        """26.10.2026: the word 'Digitalisierung' alone is not AI work (scanning clerk, IT specialist)."""
        for t in ["Mitarbeiter:in Digitalisierung/Scanning (w/m/x) - befristet für 1 Jahr",
                  "IT-Spezialist – Digitalisierung & eHealth"]:
            with self.subTest(t):
                self.assertEqual(v(t), "not_target_role")

    def test_techniker_and_data_scientist_are_hard(self):
        """26.10.2026: hard exclusions, so AI or automation in the title does not lift them."""
        for t in ["Automatisierungstechniker:in (m/w/d)", "Data Scientist mit KI-Fokus und Kundenkontakt",
                  "Data Scientist"]:
            with self.subTest(t):
                self.assertEqual(v(t), "excluded_field")

    def test_reason_of_ai_titles(self):
        self.assertEqual(why("AI Agents & Business Automation"), "ai_edge")
        self.assertEqual(why("Junior Product Analyst"), "target")


class Graduate(unittest.TestCase):
    def test_graduate_programmes_pass(self):
        """Task A 2."""
        for t in ["Graduate Programme 2027", "International Graduate"]:
            with self.subTest(t):
                self.assertEqual(v(t), "keep")
                self.assertEqual(why(t), "graduate")

    def test_graduate_engineer_still_excluded(self):
        self.assertEqual(v("Graduate Engineer"), "excluded_field")


class RoleGroups(unittest.TestCase):
    def test_all_groups_on_and_named(self):
        """Task A 3: every group is a named entry with an enabled flag, all True for now."""
        self.assertEqual(list(prefilter.ROLE_GROUPS), list(GROUP_TITLES))
        self.assertTrue(all(enabled for _, enabled in prefilter.ROLE_GROUPS.values()))

    def test_each_group_passes_its_titles(self):
        for name, titles in GROUP_TITLES.items():
            for t in titles:
                with self.subTest(group=name, title=t):
                    self.assertEqual(v(t), "keep")
                    self.assertEqual(why(t), name)

    def test_switching_a_group_off_drops_its_titles(self):
        """Misha switches groups one by one: a disabled group must not pass anything."""
        for name, titles in GROUP_TITLES.items():
            pattern, _ = prefilter.ROLE_GROUPS[name]
            with mock.patch.dict(prefilter.ROLE_GROUPS, {name: (pattern, False)}):
                for t in titles:
                    with self.subTest(group=name, title=t):
                        self.assertNotEqual(why(t), name)
                        self.assertEqual(v(t) == "keep", why(t) is not None)

    def test_switched_off_group_only_title_is_not_target_role(self):
        pattern, _ = prefilter.ROLE_GROUPS["operations_ai"]
        with mock.patch.dict(prefilter.ROLE_GROUPS, {"operations_ai": (pattern, False)}):
            self.assertEqual(v("Revenue Operations Manager"), "not_target_role")

    def test_strategy_veto(self):
        """Task A 3: strategy_research is not used when the title has deal(s), m&a, transaction or valuation."""
        for t in ["Strategy Associate (M&A)", "Strategy Associate, Deals", "Transaction Strategy Associate",
                  "Valuation Research Associate"]:
            with self.subTest(t):
                self.assertEqual(v(t), "not_target_role")

    def test_project_coordinator_stays_excluded(self):
        """Task A 3: 'project associate' is in, 'project coordinator' stays out (Misha 05.10)."""
        self.assertEqual(v("Project Coordinator"), "excluded_field")
        self.assertEqual(v("Project Associate"), "keep")

    def test_plain_business_operations_stays_excluded(self):
        self.assertEqual(v("Business Operations Manager"), "excluded_field")
        self.assertEqual(v("Business Enablement Manager"), "keep")

    def test_adventure_is_not_venture(self):
        self.assertEqual(v("Adventure Guide"), "not_target_role")

    def test_digital_consulting_both_orders(self):
        self.assertEqual(why("Consultant (Digital)"), "digital_consulting")
        self.assertEqual(v("Digital Designer"), "not_target_role")

    def test_reason_credit_order(self):
        """A group gets only the jobs no earlier rule keeps."""
        self.assertEqual(why("Marketing Operations Coordinator"), "operations_ai")
        self.assertEqual(why("Digital Transformation Consultant"), "ai_edge")
        self.assertEqual(why("Junior Product Analyst"), "target")
        self.assertEqual(why("Graduate Programme 2027"), "graduate")


class German(unittest.TestCase):
    def test_required_phrases_drop(self):
        """Task A 4: each new phrase drops the job as german_required."""
        for phrase in REQUIRED_GERMAN:
            with self.subTest(phrase=phrase):
                self.assertEqual(v("Junior Product Analyst", f"Wir erwarten: {phrase}."), "german_required")

    def test_softened_phrases_keep(self):
        """Task A 4: SOFTENED still wins."""
        for phrase in SOFTENED_GERMAN:
            with self.subTest(phrase=phrase):
                self.assertEqual(v("Junior Product Analyst", phrase), "keep")

    def test_plain_mentions_are_not_required(self):
        self.assertEqual(v("Junior Product Analyst", "Deutsch, Englisch C1 und Grundkenntnisse"), "keep")
        self.assertEqual(v("Junior Product Analyst", "Deutschland (Wien)"), "keep")

    def test_german_phrase_returns_the_matched_text(self):
        self.assertEqual(prefilter.german_phrase("Wir erwarten Deutsch (C2)."), "Deutsch (C2)")
        self.assertEqual(prefilter.german_phrase("Fluent German is not required"), "")


class Verdicts(unittest.TestCase):
    def test_only_the_five_verdicts_come_out(self):
        titles = ["AI Engineer", "Senior AI Product Manager", "Office Manager", "AI Agents", "Business Analyst"]
        self.assertTrue({v(t) for t in titles} <= RULES)


class QAFixes(unittest.TestCase):
    """QA round 26.10.2026 (specs/stage7-wave2b-3-qa-fixes.md, Dev A; docs/qa-stage7-wave2b-3.md minor 5)."""

    def test_vice_president_and_group_leads_are_senior(self):
        for t in ["Vice President AI", "Gruppenleiter KI", "Gruppenleiterin Marketing",
                  "Gruppenleitung Application & Automation Services"]:
            with self.subTest(t):
                self.assertEqual(v(t), "senior")

    def test_electrician_automation_technology_and_engineers_are_hard(self):
        for t in ["Elektriker Gebäudeautomation", "Mitarbeiter Automatisierungstechnik",
                  "Robotik- und Automatisierungsingenieur"]:
            with self.subTest(t):
                self.assertEqual(v(t), "excluded_field")

    def test_wirtschaftsingenieur_reaches_the_normal_checks(self):
        """Round 2, N3: 'ingenieur' must not drop Wirtschaftsingenieur* and Ingenieurwesen titles."""
        for t in ["Praktikum Wirtschaftsingenieurwesen", "Wirtschaftsingenieur Product",
                  "Wirtschafts-Ingenieur Product"]:
            with self.subTest(t):
                self.assertEqual(v(t), "keep")
        # 'Werkstudent Ingenieurwesen ...' is a Werkstudent: dropped by the student-format rule (owner, 30.10.2026)
        self.assertEqual(v("Werkstudent Ingenieurwesen Produktmanagement"), "excluded_field")

    def test_automatisierungsingenieur_stays_excluded(self):
        """Round 2, N3: the engineering titles of the QA round stay out."""
        for t in ["Robotik- und Automatisierungsingenieur", "Automatisierungsingenieur", "Projektingenieur Product"]:
            with self.subTest(t):
                self.assertEqual(v(t), "excluded_field")

    def test_ai_titles_still_keep(self):
        for t in ["Marketing Operations Manager (AI & Automation)", "AI Associate"]:
            with self.subTest(t):
                self.assertEqual(v(t), "keep")

    def test_open_applications_are_excluded(self):
        """Product's exclusion of 26.10.2026: open applications are not vacancies."""
        for t in ["Initiativbewerbung", "Open Application - Product", "Talent Pool Product Management"]:
            with self.subTest(t):
                self.assertEqual(v(t), "excluded_field")


class Preview(unittest.TestCase):
    def make_db(self, folder):
        path = Path(folder) / "state.db"
        con = sqlite3.connect(path)
        con.execute("create table jobs (id text primary key, title text, company text, source text,"
                    " first_seen text, rule text, data text, verdict text, closed integer, scored_at text)")
        rows = [
            ("a1", "AI Agents & Business Automation", "Acme", "company", "2026-10-25T09:00:00", "not_target_role",
             {"title": "AI Agents & Business Automation", "description": ""}),
            ("a2", "Graduate Programme 2027", "Beta", "linkedin", "2026-10-20T09:00:00", "not_target_role",
             {"title": "Graduate Programme 2027", "description": ""}),
            ("a3", "Business Analyst", "Gamma", "company", "2026-10-24T09:00:00", "excluded_field",
             {"title": "Business Analyst", "description": ""}),
            ("a4", "Junior Product Analyst", "Delta", "company", "2026-10-26T06:00:00", "keep",
             {"title": "Junior Product Analyst", "description": "Wir erwarten Deutsch (C2)."}),
            ("a5", "Old Product Analyst", "Old", "company", "2026-09-01T09:00:00", "not_target_role",
             {"title": "Old Product Analyst", "description": ""}),
        ]
        con.executemany("insert into jobs (id, title, company, source, first_seen, rule, data)"
                        " values (?,?,?,?,?,?,?)",
                        [(i, t, c, s, f, r, json.dumps(d, ensure_ascii=False)) for i, t, c, s, f, r, d in rows])
        con.commit()
        con.close()
        return path

    def test_preview_lists_added_and_german_line(self):
        with tempfile.TemporaryDirectory() as d:
            db = self.make_db(d)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                wave3_preview.main(["--db", str(db), "--days", "15"])
            out = buf.getvalue()
        self.assertIn("## Добавится: 2", out)
        self.assertIn("| AI Agents & Business Automation | Acme | company |", out)
        self.assertIn("### graduate (1)", out)
        self.assertIn("Строк, где в описании теперь видно требование немецкого: 1 "
                      "(на работу бота не влияет: правило применяется только к новым вакансиям)", out)
        self.assertNotIn("Уйдёт", out)  # the bot never re-judges a kept row: no drop table
        self.assertNotIn("Old Product Analyst", out)  # outside the 15-day window

    def test_preview_is_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            db = self.make_db(d)
            con = wave3_preview.connect(db)
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("insert into jobs (id) values ('x')")

    def test_out_writes_md_and_json(self):
        with tempfile.TemporaryDirectory() as d:
            db = self.make_db(d)
            out_base = Path(d) / "wave3"
            with contextlib.redirect_stdout(io.StringIO()):
                wave3_preview.main(["--db", str(db), "--out", str(out_base)])
            self.assertIn("## Добавится", (Path(d) / "wave3.md").read_text(encoding="utf-8"))
            payload = json.loads((Path(d) / "wave3.json").read_text(encoding="utf-8"))
            self.assertEqual(sorted(payload["added"]), ["ai_edge", "graduate"])
            self.assertEqual([j["title"] for j in payload["german_seen_after_keep"]], ["Junior Product Analyst"])
            self.assertEqual(payload["german_seen_after_keep"][0]["phrase"], "Deutsch (C2)")
            self.assertNotIn("dropped", payload)


class Apply(unittest.TestCase):
    """apply_wave3.py on a temp DB: dry run writes nothing; --apply sets rule='keep' on the qualifying rows only."""

    def make_db(self, folder):
        path = Path(folder) / "state.db"
        con = sqlite3.connect(path)
        con.execute("create table jobs (id text primary key, title text, company text, source text,"
                    " first_seen text, rule text, data text, verdict text, closed integer, scored_at text)")
        def job(title, description=""):
            return json.dumps({"title": title, "description": description}, ensure_ascii=False)
        # id, title, company, first_seen, stored rule, verdict, closed
        rows = [
            ("q1", "Revenue Operations Manager", "Acme", "2026-10-24T09:00:00", "not_target_role", None, 0),
            ("q2", "AI Agents & Business Automation", "Beta", "2026-10-23T09:00:00", "not_target_role", None, None),
            ("g1", "Customer Success Manager", "Gamma", "2026-10-22T09:00:00", "not_target_role", None, 0),
            ("j1", "Graduate Programme 2027", "Delta", "2026-10-21T09:00:00", "not_target_role", "interested", 0),
            ("c1", "Graduate Programme 2028", "Eps", "2026-10-20T09:00:00", "not_target_role", None, 1),
            ("k1", "Junior Product Analyst", "Zeta", "2026-10-19T09:00:00", "keep", None, 0),
            ("k2", "Business Analyst", "Eta", "2026-10-18T09:00:00", "keep", None, 0),
            ("o1", "Revenue Operations Manager", "Theta", "2026-09-01T09:00:00", "not_target_role", None, 0),
            ("n1", "Junior Product Analyst", "Iota", "2026-10-26T06:00:00", "keep", None, 0),
        ]
        con.executemany("insert into jobs (id, title, company, source, first_seen, rule, data, verdict, closed,"
                        " scored_at) values (?,?,?,'company',?,?,?,?,?,NULL)",
                        [(i, t, c, f, r, job(t), v, cl) for i, t, c, f, r, v, cl in rows])
        con.commit()
        con.close()
        return path

    def rules(self, db):
        con = sqlite3.connect(db)
        out = dict(con.execute("select id, rule from jobs"))
        scored = con.execute("select count(*) from jobs where scored_at is not null").fetchone()[0]
        con.close()
        return out, scored

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            db = self.make_db(d)
            before, _ = self.rules(db)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                changes = apply_wave3.main(["--db", str(db)])
            self.assertEqual(self.rules(db)[0], before)
            self.assertIn("dry run", buf.getvalue())
            self.assertEqual({c["id"] for c in changes}, {"q1", "q2", "g1"})

    def test_apply_changes_only_qualifying_rows(self):
        with tempfile.TemporaryDirectory() as d:
            db = self.make_db(d)
            before, _ = self.rules(db)
            with contextlib.redirect_stdout(io.StringIO()):
                apply_wave3.main(["--db", str(db), "--apply"])
            after, scored = self.rules(db)
        self.assertEqual({i for i in before if before[i] != after[i]}, {"q1", "q2", "g1"})
        self.assertTrue(all(after[i] == "keep" for i in ("q1", "q2", "g1")))
        self.assertEqual(scored, 0)  # scored_at stays NULL: prepare.py queues the job next morning

    def test_judged_closed_and_kept_rows_untouched(self):
        """Misha's verdict, a closed row, an old window row and a kept row that now fails stay as they are."""
        with tempfile.TemporaryDirectory() as d:
            db = self.make_db(d)
            with contextlib.redirect_stdout(io.StringIO()):
                apply_wave3.main(["--db", str(db), "--apply"])
            after, _ = self.rules(db)
        self.assertEqual(after["j1"], "not_target_role")  # judged by Misha
        self.assertEqual(after["c1"], "not_target_role")  # closed
        self.assertEqual(after["o1"], "not_target_role")  # outside the window
        self.assertEqual(after["k2"], "keep")  # no demotion of a kept row
        self.assertEqual(after["k1"], "keep")

    def test_disabled_group_not_applied(self):
        """A group switched off later is not applied, even when it would keep the row."""
        pattern, _ = prefilter.ROLE_GROUPS["customer_success"]
        with tempfile.TemporaryDirectory() as d:
            db = self.make_db(d)
            with mock.patch.dict(prefilter.ROLE_GROUPS, {"customer_success": (pattern, False)}):
                with contextlib.redirect_stdout(io.StringIO()):
                    apply_wave3.main(["--db", str(db), "--apply"])
            after, _ = self.rules(db)
        self.assertEqual(after["g1"], "not_target_role")
        self.assertEqual(after["q1"], "keep")

    def test_apply_on_missing_file_creates_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "missing.db"
            with self.assertRaises(sqlite3.OperationalError):
                apply_wave3.main(["--db", str(db), "--apply"])
            self.assertFalse(db.exists())


if __name__ == "__main__":
    unittest.main()
