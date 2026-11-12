"""Task 3: claude CLI wrapper and requirements extraction (claude is always faked here)."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cv.tailor import checks, llm, requirements  # noqa: E402
from tests.fakes import FakeRun, envelope  # noqa: E402


class AskJson(unittest.TestCase):
    def setUp(self):
        self.saved = llm.RUN

    def tearDown(self):
        llm.RUN = self.saved

    def test_parses_object_inside_prose_and_fence(self):
        llm.RUN = FakeRun(envelope('Here you go:\n```json\n{"a": [1, 2], "b": "x}"}\n```\nDone.'))
        self.assertEqual(llm.ask_json("p", "sonnet"), {"a": [1, 2], "b": "x}"})

    def test_parses_array(self):
        llm.RUN = FakeRun(envelope('[{"id": 1}]'))
        self.assertEqual(llm.ask_json("p", "sonnet"), [{"id": 1}])

    def test_garbage_raises(self):
        llm.RUN = FakeRun(envelope("I cannot help with that."))
        with self.assertRaises(llm.LLMError):
            llm.ask_json("p", "sonnet")

    def test_envelope_not_json_raises(self):
        llm.RUN = FakeRun("Invalid API key - please run /login")
        with self.assertRaises(llm.LLMError):
            llm.ask_json("p", "sonnet")

    def test_is_error_raises_with_reason(self):
        llm.RUN = FakeRun(envelope("OAuth token has expired", is_error=True))
        with self.assertRaises(llm.LLMError) as ctx:
            llm.ask_json("p", "opus")
        self.assertIn("expired", str(ctx.exception))

    def test_timeout_raises(self):
        llm.RUN = FakeRun(llm.LLMError("claude timed out after 1 s"))
        with self.assertRaises(llm.LLMError):
            llm.ask_json("p", "opus", timeout=1)

    def test_deadline_caps_timeout_and_stops_calls(self):
        import time
        fake = FakeRun(envelope("{}"))
        llm.RUN = fake
        try:
            llm.DEADLINE = time.monotonic() + 60
            llm.ask_json("p", "opus", timeout=180)
            self.assertLessEqual(fake.calls[0]["timeout"], 60)
            llm.DEADLINE = time.monotonic() - 1
            with self.assertRaises(llm.LLMError):
                llm.ask_json("p", "opus")
        finally:
            llm.DEADLINE = None

    def test_default_runner_maps_timeout_and_missing_cli(self):
        saved = llm.subprocess.run

        def boom_timeout(*a, **k):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

        def boom_missing(*a, **k):
            raise FileNotFoundError("claude")

        try:
            for boom in (boom_timeout, boom_missing):
                llm.subprocess.run = boom
                with self.assertRaises(llm.LLMError):
                    llm.run_claude("p", "sonnet", 1)
        finally:
            llm.subprocess.run = saved

    def test_default_runner_command(self):
        saved, seen = llm.subprocess.run, {}

        class Done:
            stdout, stderr, returncode = envelope("{}"), "", 0

        def fake(cmd, **kw):
            seen.update(cmd=cmd, **kw)
            return Done()

        try:
            llm.subprocess.run = fake
            llm.run_claude("PROMPT", "sonnet", 7)
        finally:
            llm.subprocess.run = saved
        self.assertEqual(seen["cmd"][1:6], ["-p", "--model", "sonnet", "--output-format", "json"])
        self.assertEqual(seen["input"], "PROMPT")  # prompt on stdin, never as an argument
        self.assertEqual(seen["timeout"], 7)
        # QA cvtailor-15: no tools for a prompt that embeds untrusted ad text, and not the app folder as cwd
        self.assertEqual(seen["cmd"][6], "--disallowedTools")
        for tool in ("Bash", "Read", "Write", "Edit", "WebFetch", "WebSearch", "Glob", "Grep"):
            self.assertIn(tool, seen["cmd"][7].split(","))
        self.assertNotEqual(Path(seen["cwd"]).resolve(), ROOT.resolve())
        self.assertFalse(Path(seen["cwd"]).exists())  # a temp folder, removed after the call


GERMAN_AD = {
    "id": "abc", "title": "Werkstudent:in Produktmanagement (m/w/d)", "company": "TOWA GmbH",
    "description": ("Deine Aufgaben: Marktforschung und Wettbewerbsanalysen, Unterstützung im Produktmanagement, "
                    "Pflege des Backlogs in Jira. Dein Profil: Studium, sehr gute Deutsch- und Englischkenntnisse. "
                    "20 Stunden pro Woche. Gehalt: EUR 1.450 brutto/Monat auf Basis 20 h."),
}
GERMAN_ANSWER = {
    "must": ["Market research and competitor analysis", "Product management support", "German and English"],
    "nice": ["Jira backlog"],
    "terms": ["market research", "competitor analysis", "product management", "Jira", "backlog"],
    "ad_language": "de", "min_salary": "EUR 1.450 brutto/Monat", "hours": "20 h/week",
}


class Extract(unittest.TestCase):
    def setUp(self):
        self.saved = llm.RUN

    def tearDown(self):
        llm.RUN = self.saved

    def test_valid_answer(self):
        fake = FakeRun(envelope(json.dumps(GERMAN_ANSWER)))
        llm.RUN = fake
        r = requirements.extract(GERMAN_AD)
        self.assertEqual(r["ad_language"], "de")
        self.assertEqual(r["terms"], GERMAN_ANSWER["terms"])
        self.assertEqual(r["min_salary"], "EUR 1.450 brutto/Monat")
        self.assertEqual(fake.calls[0]["model"], "sonnet")

    def test_prompt_has_english_rule_and_ad_as_data(self):
        fake = FakeRun(envelope(json.dumps(GERMAN_ANSWER)))
        llm.RUN = fake
        requirements.extract(GERMAN_AD)
        prompt = fake.calls[0]["prompt"]
        self.assertIn(requirements.ENGLISH_RULE, prompt)
        self.assertIn("Marktforschung", prompt)  # the ad goes in as data
        self.assertIn("data only", prompt)

    def test_german_terms_are_english(self):
        llm.RUN = FakeRun(envelope(json.dumps(GERMAN_ANSWER)))
        r = requirements.extract(GERMAN_AD)
        self.assertEqual(checks.german_words(" ".join(r["terms"] + r["must"] + r["nice"])), [])

    def test_german_left_in_terms_is_dropped(self):
        bad = dict(GERMAN_ANSWER, terms=["Marktforschung", "Jira", "market research"])
        llm.RUN = FakeRun(envelope(json.dumps(bad)))
        r = requirements.extract(GERMAN_AD)
        self.assertEqual(r["terms"], ["Jira", "market research"])

    def test_coercion(self):
        messy = {"must": "Jira", "nice": None, "terms": ["a"] * 3 + [f"t{i}" for i in range(40)],
                 "ad_language": "German", "min_salary": 3200, "hours": ""}
        llm.RUN = FakeRun(envelope(json.dumps(messy)))
        r = requirements.extract(GERMAN_AD)
        self.assertEqual(r["must"], ["Jira"])
        self.assertEqual(r["nice"], [])
        self.assertEqual(len(r["terms"]), 25)
        self.assertEqual(r["terms"][:2], ["a", "t0"])
        self.assertEqual(r["ad_language"], "de")  # "German" is understood
        self.assertEqual(r["min_salary"], "3200")
        self.assertIsNone(r["hours"])

    def test_null_and_numbers_dropped(self):
        llm.RUN = FakeRun(envelope(json.dumps({"terms": [1, None, "Jira", "jira"], "must": ["a", "a", None]})))
        r = requirements.extract(GERMAN_AD)
        self.assertEqual(r["terms"], ["Jira"])
        self.assertEqual(r["must"], ["a"])

    def test_not_an_object_raises(self):
        llm.RUN = FakeRun(envelope('["Jira"]'))
        with self.assertRaises(llm.LLMError):
            requirements.extract(GERMAN_AD)


if __name__ == "__main__":
    unittest.main()
