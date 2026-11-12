"""Student formats (owner, 30.10.2026: 'убери все вакансии, где я должен учиться в университете; оставляем только
graduate, part-time, full-time'). Misha is not enrolled, so a job that needs a student is dropped by title, and a
posting that needs current enrolment is capped at 30 by score.bounds(). No network, no model."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import prefilter  # noqa: E402
import score  # noqa: E402


def v(title, description=""):
    return prefilter.verdict({"title": title, "description": description})


STUDENT_TITLES = [
    "Werkstudent:in Produktmanagement",
    "Working Student (f/m/d) - Finance",
    "Student Job (f/m/x) - Data & Project Management (25 h/week)",
    "Studentenjob (w/m/x) - Client Lifecycle Management",
    "Studentische:r Mitarbeiter:in People Transformation",
    "Student Vertragsmanagement (w/m/d)",
    "Pflichtpraktikum Küche",
    "Studienbegleitendes Praktikum im Market Management",
    "FH-/Uni-Praktikum Corporate Communications",
    "Uni-Praktikum Product Marketing",
    "FH-Praktikum Product",
    "Praktikum für Studierende Marketing",
    "Studentjob (f/m/x) - Agile Delivery Client Coverage (20h/week)",  # one word, found in the prod copy
    "Student Worker",  # found in the prod copy: a student job by its own name
    "FH-Pflichtpraktikant im Group Controlling (w/m/x)",  # found in the prod copy
    "Work& Study für Studierende: KI & Data Analytics",  # found in the prod copy
]

KEPT_TITLES = [
    "Internship - Product Management (all genders)",
    "Sportradar's International Graduate Programme 2027/2028 (m/f/d)",
    "Praktikum Strategy & Execution | EY-Parthenon (w/m/d)",
    "Junior Product Owner",
    "Trainee Marketing",
    "Unilever Praktikum Product Marketing",  # 'uni' inside a word is not a university
    # a job board's domain in the title is not a student job; a trainee programme stays
    "Management Trainee Programm / Junior Sales Manager (m/w/d) Voll- und Teilzeit - Studentjob.at",
    # 'mitten im Studium' needs enrolment: the scoring caps it (profile.md line 8), the title rules do not drop it
    "Praktikant:in (m/w/d) Time / 38,5 h Experience / mitten im Studium Start / ab März 2027",
]


class StudentFormats(unittest.TestCase):
    def test_student_formats_are_dropped_as_excluded_field(self):
        for t in STUDENT_TITLES:
            with self.subTest(t):
                self.assertEqual(v(t), "excluded_field")
                self.assertIsNone(prefilter.reason({"title": t}))

    def test_target_roles_without_a_student_format_are_kept(self):
        for t in KEPT_TITLES:
            with self.subTest(t):
                self.assertEqual(v(t), "keep")

    def test_student_recruiting_is_not_a_student_job(self):
        """'Student Recruiting' is a recruiting role about students, not a job for a student."""
        for t in ["Student Recruiting Manager", "Student Recruiting Manager (m/w/d)"]:
            with self.subTest(t):
                self.assertNotEqual(v(t), "excluded_field")

    def test_bare_student_and_werkstudent_are_no_longer_target(self):
        """Werkstudent, working student and bare student left TARGET; the drop is the student rule's job."""
        self.assertNotIn("student", prefilter.TARGET.pattern.split("|"))
        self.assertNotIn("werkstudent", prefilter.TARGET.pattern)
        self.assertNotIn("working student", prefilter.TARGET.pattern)

    def test_student_format_is_hard_even_with_ai_in_the_title(self):
        """An AI word lifts a soft exclusion, never a student format."""
        self.assertEqual(v("AI Werkstudent (m/w/d)"), "excluded_field")
        self.assertEqual(v("Student Job AI Automation (m/w/d)"), "excluded_field")


class ScoreCaps(unittest.TestCase):
    """score.bounds(): 'статус студента' caps at 30 (was 55); the German-ad-without-description cap stays 55."""

    def test_enrolment_required_is_capped_at_30(self):
        s = {"score": 80, "why_ru": "Нужен статус студента: Werkstudent.", "german": "none"}
        out = score.bounds({"id": "x", "description": "Werkstudent 20 h, Deutsch B1."}, s)
        self.assertEqual(out["score"], 30)
        self.assertIn("нужен статус студента", out["why_ru"].lower())

    def test_german_ad_without_description_still_capped_at_55(self):
        s = {"score": 80, "why_ru": "Немецкое объявление без описания.", "german": "ad_in_german"}
        out = score.bounds({"id": "x", "description": ""}, s)
        self.assertEqual(out["score"], 55)

    def test_both_caps_take_the_lower(self):
        s = {"score": 80, "why_ru": "Нужен статус студента.", "german": "ad_in_german"}
        out = score.bounds({"id": "x", "description": ""}, s)
        self.assertEqual(out["score"], 30)

    def test_other_jobs_keep_their_score(self):
        s = {"score": 80, "why_ru": "Хорошая роль для Product Manager.", "german": "none"}
        out = score.bounds({"id": "x", "description": "English posting."}, s)
        self.assertEqual(out["score"], 80)


if __name__ == "__main__":
    unittest.main()
