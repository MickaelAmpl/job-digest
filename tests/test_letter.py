"""Stage 6: the cover-letter engine with a fake claude, a temp DB, a temp cv dir and a small fixture library
(cv/letter_library.json is not used here, so these tests do not depend on its content)."""
import copy
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
from cv.tailor import letter, library, llm, run  # noqa: E402
from tests.fakes import FakeRun, envelope  # noqa: E402

BASE = json.loads((ROOT / "cv" / "source" / "content" / "en.json").read_text(encoding="utf-8"))
COMPANY = "Acme Data"
TITLE = "Junior Product Owner (m/w/d) - Agile IT Delivery"
AD = ("Acme Data builds a market data platform for crypto traders. We are looking for a Junior Product Owner "
      "(m/w/d) to manage the backlog in Jira, write product specs and run market research. English required, "
      "German a plus. Vienna, hybrid working.")
AD_DE = ("Wir suchen eine Produktmanagerin für unser Team in Wien. Du erstellst Anforderungen, pflegst das Backlog in "
         "Jira und führst Marktforschung durch. Deutschkenntnisse auf B2-Niveau sind erforderlich. Start nach Vereinbarung.")
AD_DE_EN = AD_DE + " Englischkenntnisse sind ein Plus."
REQS = {"must": ["backlog management", "Jira"], "nice": ["German"], "terms": ["Jira", "backlog", "market research"],
        "ad_language": "en", "min_salary": None, "hours": None}

LONG = ("Across every release I kept the same habit: write the expected result before the work starts, then compare "
        "the result with it and say plainly where it differs. ")

LIB = {
    "note": "fixture",
    "hook_templates": ["The {title} role at {company} is close to the work I do: research, product specs and launches."],
    "identity": {
        "id_product": ("I work as a product manager for fintech and crypto products, from the first research question "
                       "to the launch and the metrics after it."),
        "id_research": ("My background is market research and product analysis for blockchain and fintech companies, "
                        "followed by product work inside a live team."),
    },
    "proofs": {
        "pr_research": {"label_ru": "рынок и позиционирование", "tags": ["market research", "positioning", "competitor analysis"],
                        "text": ("Before the team wrote any code for a crypto market data platform, I ran the market research "
                                 "and set the positioning: who the product was for, which competitors it would be measured against "
                                 "and what it would not do. The research went into the roadmap, so the first release answered a "
                                 "question that users already had. Every later decision was checked against it.")},
        "pr_docs": {"label_ru": "спеки и тикеты", "tags": ["product specs", "roadmap", "tickets", "wireframes", "Jira"],
                    "text": ("I write the product documents that developers and designers work from: the specs, the roadmap and "
                             "the tickets in Jira, with wireframes and references for the designer. Every ticket states the user "
                             "problem and the check that proves it is solved, so the work can be picked up without a meeting. When "
                             "the scope changes, I update the document the same day.")},
        "pr_metrics": {"label_ru": "метрики и решения", "tags": ["metrics", "KPIs", "GA4", "analytics", "monetization"],
                       "text": ("I choose the product metrics before a release and read them after it. On a data platform that "
                                "meant GA4 engagement and monetization numbers, which I used to decide what to build next and what "
                                "to drop. I also kept a short weekly note of what the numbers changed, so the team could act on "
                                "them without another meeting.")},
        "pr_owners": {"label_ru": "выход на рынок", "tags": ["stakeholders", "go-to-market", "marketing"],
                      "text": ("I work directly with the founders and the marketer on go-to-market: who the first users are, which "
                               "channel brings them, and what the product must show on the first screen. The decisions are written "
                               "down in the roadmap, so the next conversation starts from them. I bring the research into those "
                               "talks and bring the answers back to the team.")},
        "pr_research_pub": {"label_ru": "white papers", "tags": ["white paper", "token model", "research"],
                            "text": ("At a blockchain research company I wrote white papers for several projects and designed a "
                                     "token model with supply, allocation and utility, which the technical leads reviewed before "
                                     "anything was published. The work gave me the habit of writing for readers who check every claim, "
                                     "which is the same discipline a product spec needs.")},
        "pr_fintech": {"label_ru": "путь пользователя", "tags": ["fintech", "user journey", "wireframes", "UX", "investors"],
                       "text": ("At a neobank start-up I mapped the full card application journey and prepared the wireframes for "
                                "the designer, and I ran competitor analysis to shape the feature list. I also wrote to venture "
                                "funds as part of the fundraising, which showed me how a product story sounds to people who decide "
                                "on money.")},
        "pr_ai": {"label_ru": "ИИ-инструменты", "tags": ["AI tools", "Claude", "Codex", "prototyping"],
                  "text": ("I use AI tools every day for product work: drafting specs, prototyping screens and checking research "
                           "with Claude and Codex. I name the AI-assisted parts as such, and I check every fact myself before it "
                           "goes into a document, a ticket or a presentation for the team.")},
    },
    "exclusive": [["pr_research", "pr_research_pub"]],
    "closings": {
        "cl_1": "I can talk through any of these examples on a short call, and I can share the documents behind them with you.",
        "cl_2": "Happy to send the research and specs behind any of these examples before we speak.",
    },
    "sign_off": "Kind regards,",
}

HOOK = "Acme Data is a crypto market data company, and this role covers product research and specs for its platform."
PROOFS = ["pr_research", "pr_docs", "pr_metrics"]
WRITER = {"identity": "id_product", "proofs": PROOFS, "closing": "cl_1", "hook": HOOK,
          "evidence": [{"requirement": "Jira", "proof": "pr_docs"}, {"requirement": "market research", "proof": "pr_research"}],
          "swaps": []}
VERIFY_OK = {"ok": True, "problems": []}


def ans(obj):
    return envelope(json.dumps(obj))


def copy_lib(**changes):
    lib = copy.deepcopy(LIB)
    for key, value in changes.items():
        lib[key] = value
    return lib


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.lib_path = t / "letter_library.json"
        self.write_lib(LIB)
        self.saved = (store.DB, llm.RUN, os.environ.get("JOBDIGEST_CV_DIR"), letter.LIBRARY, library.LIBRARY)
        store.DB = t / "state.db"
        os.environ["JOBDIGEST_CV_DIR"] = str(t / "cv")
        letter.LIBRARY = self.lib_path
        con = store.connect()
        for jid, company, title, desc in (
                ("job1", COMPANY, TITLE, AD),
                ("job2", "Tito & Friends", "Product Intern", ""),
                ("job3", COMPANY, "Student Job - Junior Product Owner (m/w/d)", AD),
                ("job4", "Berliner Team GmbH", "Produktmanagerin (m/w/d)", AD_DE),
                ("job5", "Berliner Team GmbH", "Produktmanagerin (m/w/d)", AD_DE_EN),
                ("job6", "e.Hive", "Student Job (f/m/x) - e.Hive", AD)):
            con.execute("insert into jobs (id, key, company, title, data) values (?,?,?,?,?)",
                        (jid, jid, company, title, json.dumps({"description": desc})))
        con.commit()
        con.close()

    def tearDown(self):
        store.DB, llm.RUN, cv, letter.LIBRARY, library.LIBRARY = self.saved
        if cv is None:
            os.environ.pop("JOBDIGEST_CV_DIR", None)
        else:
            os.environ["JOBDIGEST_CV_DIR"] = cv
        llm.DEADLINE = None
        self.tmp.cleanup()

    def write_lib(self, lib, raw=None):
        self.lib_path.write_text(raw if raw is not None else json.dumps(lib, ensure_ascii=False), encoding="utf-8")

    def saved_requirements(self, job="job1", reqs=REQS, n=2):
        d = Path(os.environ["JOBDIGEST_CV_DIR"]) / job / str(n)
        d.mkdir(parents=True, exist_ok=True)
        (d / "requirements.json").write_text(json.dumps(reqs), encoding="utf-8")

    def write(self, *answers, job="job1"):
        fake = FakeRun(*answers)
        llm.RUN = fake
        result = letter.write_letter(job)
        return result, fake

    def body_of(self, result):
        return result["text"].split("\n\n", 1)[1].rsplit("\n\nKind regards,", 1)[0]


class LibraryRules(Env):
    def test_fixture_loads(self):
        self.assertEqual(letter.load_library(BASE)["sign_off"], "Kind regards,")

    def test_injected_cliche_rejected(self):
        lib = copy_lib()
        lib["proofs"]["pr_docs"]["text"] = "I am passionate about specs. " + LIB["proofs"]["pr_docs"]["text"]
        self.write_lib(lib)
        with self.assertRaises(library.LibraryError) as cm:
            letter.load_library(BASE)
        self.assertEqual(cm.exception.file, "letter_library.json")
        self.assertIn("letter_library.json", str(cm.exception))
        self.assertIn("pr_docs", str(cm.exception))
        self.assertIn("cliche", str(cm.exception))

    def test_never_claim_term_rejected(self):
        lib = copy_lib()
        lib["proofs"]["pr_docs"]["text"] = LIB["proofs"]["pr_docs"]["text"] + " I also set up Trello for the team."
        self.write_lib(lib)
        with self.assertRaisesRegex(library.LibraryError, "never"):
            letter.load_library(BASE)

    def test_new_number_rejected(self):
        lib = copy_lib()
        lib["proofs"]["pr_docs"]["text"] = LIB["proofs"]["pr_docs"]["text"] + " That cut the backlog by 40%."
        self.write_lib(lib)
        with self.assertRaisesRegex(library.LibraryError, "number"):
            letter.load_library(BASE)

    def test_german_word_rejected(self):
        lib = copy_lib()
        lib["closings"]["cl_2"] = "Happy to send the research with Erfahrung in the field."
        self.write_lib(lib)
        with self.assertRaisesRegex(library.LibraryError, "german"):
            letter.load_library(BASE)

    def test_cv_copy_sentence_rejected(self):
        lib = copy_lib()
        lib["proofs"]["pr_research"]["text"] = ("Defined the product's business logic from day one: research, "
                                                "positioning, then development with the CTO")
        self.write_lib(lib)
        with self.assertRaisesRegex(library.LibraryError, "copies a CV line"):
            letter.load_library(BASE)

    def test_letter_never_term_rejected(self):
        lib = copy_lib()
        lib["proofs"]["pr_ai"]["text"] = LIB["proofs"]["pr_ai"]["text"] + " I am available from May."
        self.write_lib(lib)
        with self.assertRaisesRegex(library.LibraryError, "letter_never"):
            letter.load_library(BASE)

    def test_closing_would_rejected(self):
        lib = copy_lib()
        lib["closings"]["cl_2"] = "I would be happy to send the research behind these examples."
        self.write_lib(lib)
        with self.assertRaisesRegex(library.LibraryError, "would"):
            letter.load_library(BASE)

    def test_duplicate_id_rejected(self):
        raw = json.dumps(LIB, ensure_ascii=False)
        raw = raw[:-1] + ', "closings": {"cl_1": "x"}}'
        self.write_lib(None, raw=raw)
        with self.assertRaisesRegex(library.LibraryError, "duplicate id closings"):
            letter.load_library(BASE)

    def test_too_few_proofs_rejected(self):
        lib = copy_lib(proofs={k: v for k, v in list(LIB["proofs"].items())[:5]})
        self.write_lib(lib)
        with self.assertRaisesRegex(library.LibraryError, "at least 6"):
            letter.load_library(BASE)

    def test_template_placeholder_rejected(self):
        lib = copy_lib(hook_templates=["Hello {company}, the {title} role {salary}."])
        self.write_lib(lib)
        with self.assertRaisesRegex(library.LibraryError, "hook_templates"):
            letter.load_library(BASE)


class AiTells(Env):
    """Misha 10.10.2026: no sign of AI writing in the hook, the library or the body."""

    def problems(self, hook, ad=AD, title="Junior Product Owner - Agile IT Delivery"):
        return letter.hook_problems(hook, COMPANY, letter.checks.cv_text(BASE) + " " + " ".join(letter.run.load_rules()[1]),
                                    letter.checks.cv_text(BASE), ad, *letter.checks.load_never_claim(), title=title)

    def test_clean_hook_passes(self):
        self.assertEqual(letter.ai_tells(HOOK), [])
        self.assertEqual(self.problems(HOOK), [])

    def test_tell_labels(self):
        cases = {
            "vibrant": "Acme Data is a vibrant crypto data company, and I have taken a product of that kind to launch.",
            "not only ... but": "Acme Data is not only a data company but also a research team, and I have built products.",
            "it's not X, it's Y": "It's not a tool, it's a team for traders, and I have built products for Acme Data.",
            "isn't just/only": "Acme Data isn't just a data company, and I have built products of that kind for it.",
            "-ing tail": "Acme Data builds crypto data tools for traders, showing where the market moves, and I have built them.",
            "taught me": "Acme Data works on crypto data, and research taught me how traders read a market.",
            "rather than": "Acme Data sells crypto data rather than charts, and I have built a product of that kind.",
            "potentially": "Acme Data could potentially help traders, and I have built a product of that kind.",
            "transition opener": "Acme Data builds crypto data tools. However, I have built a product of that kind.",
            "curly quote": "Acme Data’s crypto data tools help traders, and I have built a product of that kind.",
            "em or en dash": "Acme Data builds crypto data tools — and I have built a product of that kind.",
            "emoji": "Acme Data builds crypto data tools 📈 and I have built a product of that kind.",
            "markdown bold": "**Acme Data** builds crypto data tools, and I have built a product of that kind.",
            "markdown list": "Acme Data builds crypto data tools.\n- and I have built a product of that kind.",
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.assertIn(label, letter.ai_tells(text))
                if label != "markdown list":  # a line break in the hook is already a reason of its own
                    self.assertIn(f"звучит как ИИ: {label}", self.problems(text))

    def test_curly_apostrophe_word_rule_still_matches(self):
        self.assertIn("in today's", letter.ai_tells("In today\u2019s market, Acme Data builds tools."))

    def test_five_word_copy_from_ad_is_template(self):
        hook = "Acme Data builds a market data platform for crypto traders, and I have built a product of that kind."
        self.assertTrue(any(p.startswith("копирует фразу из объявления:") for p in self.problems(hook)))

    def test_title_and_company_words_may_repeat_the_ad(self):
        ad = "Acme Data hires a Product Owner for Agile IT Delivery work across Vienna and Berlin teams."
        hook = "Acme Data needs Product Owner for Agile IT Delivery, and I have built products of that kind."
        own_title = "Product Owner for Agile IT Delivery"
        self.assertEqual([p for p in self.problems(hook, ad=ad, title=own_title) if p.startswith("копирует")], [])
        self.assertTrue([p for p in self.problems(hook, ad=ad, title="x") if p.startswith("копирует")])

    def test_library_ai_tell_rejected_with_id_and_label(self):
        lib = copy_lib()
        lib["proofs"]["pr_docs"]["text"] = LIB["proofs"]["pr_docs"]["text"] + " The team is vibrant."
        self.write_lib(lib)
        with self.assertRaisesRegex(library.LibraryError, "proof pr_docs: .*AI tell: vibrant"):
            letter.load_library(BASE)

    def test_real_library_loads(self):
        real = ROOT / "cv" / "letter_library.json"  # not letter.LIBRARY: setUp points that at the fixture
        self.assertTrue(real.is_file())
        lib = letter.load_library(BASE, path=real)
        self.assertGreaterEqual(len(lib["proofs"]), 6)

    def test_writer_prompt_has_the_rules_and_banned_words(self):
        self.assertIn("Write like a person, not like an AI", letter.WRITER_PROMPT)
        self.assertIn("<<BANNED>>", letter.WRITER_PROMPT)
        self.assertIn("delve", letter.BANNED_WORDS)

    def test_blank_title_and_company_before_letter_never(self):
        self.assertTrue(letter._never_hits("Student Job at Student Co"))
        self.assertEqual(letter._never_hits("Student Job at Student Co", ("Student Job", "Student Co")), [])


class RepeatAndTitle(Env):
    def problems(self, hook, repeats=(), ad=AD, title="Junior Product Owner - Agile IT Delivery"):
        return letter.hook_problems(hook, COMPANY, letter.checks.cv_text(BASE) + " " + " ".join(letter.run.load_rules()[1]),
                                    letter.checks.cv_text(BASE), ad, *letter.checks.load_never_claim(), title=title,
                                    repeats=repeats)

    def test_hook_repeating_cv_number_is_template(self):
        hook = "Acme Data runs a crypto platform that passed 120,000 users, and this role covers its specs."
        self.assertTrue(any(p.startswith("крючок повторяет опыт") for p in self.problems(hook)))

    def test_number_from_the_ad_is_allowed(self):
        ad = AD + " The platform has 120,000 users."
        hook = "Acme Data runs a crypto platform with 120,000 users, and this role covers its specs."
        self.assertEqual([p for p in self.problems(hook, ad=ad) if p.startswith("крючок")], [])

    def test_five_word_phrase_from_identity_or_proof_is_template(self):
        proof = LIB["proofs"]["pr_research"]["text"]
        hook = "Acme Data is a crypto data company, and the role needs someone who ran the market research and set."
        self.assertTrue(any(p.startswith("крючок повторяет опыт") for p in self.problems(hook, repeats=[proof])))
        self.assertEqual([p for p in self.problems(hook, repeats=["Unrelated words here for one line only."])
                          if p.startswith("крючок")], [])

    def test_hook_flow_repeating_proof_is_template_without_verifier(self):
        self.saved_requirements()
        proof = LIB["proofs"]["pr_research"]["text"]
        hook = "Acme Data is a crypto data company, and the role needs someone who ran the market research and set."
        result, fake = self.write(ans(dict(WRITER, hook=hook)), ans({"hook": hook}))
        self.assertEqual(result["hook_source"], "template")
        self.assertTrue(result["hook_note"].startswith("в крючке: крючок повторяет опыт"), result["hook_note"])
        self.assertEqual(len(fake.calls), 2)
        self.assertIn(proof, result["text"])

    def test_clean_title_hours_and_location(self):
        cases = {
            "Werkstudent Digital Analytics (20-25h/week)": "Werkstudent Digital Analytics",
            "Junior Product Owner (20 h)": "Junior Product Owner",
            "Sales Assistant (Teilzeit 20 Std.)": "Sales Assistant (Teilzeit)",
            "Product Owner (m/w/d) - 25h/week - Wien": "Product Owner - Wien",
            "Working Student (m/w/d)  20-30 Stunden/Woche": "Working Student",
            "Product Owner (m/w/d) | 25h": "Product Owner",
            "Praktikum Digital Analytics (m/w/d)| Ab September, Wien": "Praktikum Digital Analytics",
            "Student Job (f/m/x) - e.Hive": "Student Job - e.Hive",
            "Part-time Analyst (part-time)": "Part-time Analyst (part-time)",
            "Teilzeit Product Manager": "Teilzeit Product Manager",
        }
        for raw, want in cases.items():
            with self.subTest(raw):
                self.assertEqual(letter.clean_title(raw), want)

    def test_notes_are_short_russian_and_english_goes_to_result_json(self):
        self.saved_requirements()
        result, _ = self.write(ans(WRITER), ans({"ok": False, "problems": ["company claim not in the ad"]}))
        for note in result["notes"] + [result["hook_note"]]:
            self.assertRegex(note, "[а-яА-Я]", note)
        self.assertEqual(result["verify_problems"], ["company claim not in the ad"])
        result, _ = self.write(llm.LLMError("claude timed out after 180 s"))
        for note in result["notes"]:
            self.assertRegex(note, "[а-яА-Я]", note)
        self.assertTrue(result["debug"])


class QaFixes(Env):
    """Sonnet QA 10.10: invisible characters, hook layout, salutation, title, whole-letter scan, sheet, length, layout."""

    def hook_probs(self, hook, ad=AD, title="Junior Product Owner - Agile IT Delivery", repeats=()):
        return letter.hook_problems(hook, COMPANY, letter.checks.cv_text(BASE) + " " + " ".join(letter.run.load_rules()[1]),
                                    letter.checks.cv_text(BASE), ad, *letter.checks.load_never_claim(), title=title,
                                    repeats=repeats)

    def add_job(self, jid, company, title, desc):
        con = store.connect()
        con.execute("insert into jobs (id, key, company, title, data) values (?,?,?,?,?)",
                    (jid, jid, company, title, json.dumps({"description": desc})))
        con.commit()
        con.close()

    # B1 invisible characters
    def test_zero_width_and_soft_hyphen_reject(self):
        for hook in ("Acme Da\u200bta builds crypto data tools, and this role covers the backlog.",
                     "Acme Data builds crypto data tools, and this role covers the back\u00adlog."):
            with self.subTest(repr(hook[:20])):
                self.assertIn("в крючке невидимые или полноширинные символы", self.hook_probs(hook))

    def test_fullwidth_letters_reject(self):
        hook = "Acme Data builds crypto data tools, and this role covers the \uff42\uff41\uff43\uff4b\uff4c\uff4f\uff47."
        self.assertIn("в крючке невидимые или полноширинные символы", self.hook_probs(hook))

    def test_ligature_is_cleaned_into_the_letter(self):
        self.saved_requirements()
        hook = "Acme Data builds crypto data tools, and this role covers the \ufb01nance backlog and specs."
        result, _ = self.write(ans(dict(WRITER, hook=hook)), ans(VERIFY_OK))
        self.assertEqual(result["hook_source"], "model")
        self.assertIn("the finance backlog and specs.", result["text"])
        self.assertNotIn("\ufb01", result["text"])

    # B2 hook layout and link text
    def test_layout_and_link_text_reject(self):
        for bad in ("Acme Data builds tools.\nThe role covers the backlog.", "Acme Data builds tools.\rThe role covers it.",
                    "Subject: Application: CEO, Acme Data builds tools for traders.", "Dear Hiring Manager, Acme Data builds tools.",
                    "Acme Data {builds} tools for traders and this role covers the backlog.",
                    "Acme Data builds tools for traders at http://acme.example and this role covers it.",
                    "Acme Data builds tools for traders, see www.acme.example and this role covers it.",
                    "Acme Data builds tools for traders, write to jobs@acme.example about the role."):
            with self.subTest(repr(bad[:30])):
                self.assertIn("в крючке служебный текст или ссылка", self.hook_probs(bad))

    # B3 salutation
    def test_salutation_repros(self):
        cases = {
            "Kontakt: Frau Huber Bewerbungen bitte": "Dear Acme Data hiring team,",
            "Ansprechpartner: Herr Peter Gruber Human Resources": "Dear Mr Gruber,",
            "Kontakt: Frau Anna Huber MSc": "Dear Ms Huber,",
            "Kontakt: Herr Jan van der Berg": "Dear Mr van der Berg,",
            "Kontakt: Frau Anna von Huber": "Dear Ms von Huber,",
            "Kontakt: Frau Mag. (FH) Anna Huber": "Dear Ms Huber,",
            "Kontakt: Herr MMag. Peter Gruber": "Dear Mr Gruber,",
            "Kontakt: Herr Dipl.-Ing. Peter Gruber": "Dear Mr Gruber,",
            "Kontakt: Frau Dr.in Anna Huber": "Dear Ms Huber,",
            "Kontakt: Frau Univ.-Prof. Anna Huber": "Dear Ms Huber,",
            "Kontakt: Frau Mag.a Anna Huber, Telefon 01 234": "Dear Ms Huber,",
            "Kontakt: Frau Anna": "Dear Acme Data hiring team,",
            "Kontakt: Herrn Peter Gruber": "Dear Mr Gruber,",
            "Kontakt: Frau Anna Telefon Huber": "Dear Acme Data hiring team,",
            "Fragen? Frau Anna Huber": "Dear Ms Huber,",
            "Herr Peter Gruber": "Dear Acme Data hiring team,",
            "Frau Anna Huber, Kontakt: x": "Dear Acme Data hiring team,",
            "Kontakt: Frau Anna Huber": "Dear Ms Huber,",
        }
        for ad, want in cases.items():
            with self.subTest(ad):
                self.assertEqual(letter.salutation(ad, COMPANY), want)

    def test_salutation_anchor_must_come_before_and_within_200(self):
        self.assertEqual(letter.salutation("Frau Anna Huber. Kontakt: Stefan", COMPANY), "Dear Acme Data hiring team,")
        far = "Kontakt: " + "x" * 250 + " Frau Anna Huber"
        self.assertEqual(letter.salutation(far, COMPANY), "Dear Acme Data hiring team,")

    def test_team_company_salutation(self):
        self.assertEqual(letter.salutation("nothing", "Berliner Team GmbH"), "Dear Berliner Team,")
        self.assertEqual(letter.salutation("nothing", "Tito & Friends"), "Dear Tito & Friends hiring team,")

    # M4 title: money, dates, studies, first segment kept
    def test_title_drops_money_dates_and_studies(self):
        cases = {
            "Product Owner (m/w/d) ab EUR 3.200 brutto/Monat": "Product Owner",
            "Product Manager, ab 01.11.2026": "Product Manager",
            "Praktikum Digital Analytics (ab 1.11.)": "Praktikum Digital Analytics",
            "Werkstudent (Studium Wirtschaft) Analyst": "Werkstudent Analyst",
            "Product Owner - Master's programme": "Product Owner",
            "Product Owner - Vollzeit, ab sofort": "Product Owner - Vollzeit",
            "Intern - Start im März": "Intern",
            "Product Owner (m/w/d) - Gehalt 3000 gross": "Product Owner",
            "Master Data Analyst": "Master Data Analyst",
        }
        for raw, want in cases.items():
            with self.subTest(raw):
                self.assertEqual(letter.clean_title(raw), want)

    # M5 letter-never extended, hook-only list, digits, sheet
    def test_extended_letter_never_terms(self):
        for phrase in ("We pay well", "wages are fine", "good compensation", "15 euros", "a work permission",
                       "right to work here", "while you study", "at a university", "a degree", "my BSc", "my thesis",
                       "masters level", "residency status"):
            with self.subTest(phrase):
                self.assertTrue(letter._never_hits(phrase))

    def test_hook_only_terms(self):
        for word in ("part-time", "full-time", "as soon as possible", "can start", "available", "availability"):
            with self.subTest(word):
                probs = self.hook_probs(f"Acme Data builds crypto data tools, and I am {word} for the role.")
                self.assertTrue(any(p.startswith("в крючке запрещённое слово") for p in probs), probs)

    def test_hook_digits_only_a_year_from_the_ad(self):
        hook = "Acme Data builds crypto data tools in 2026, and this role covers the backlog."
        self.assertEqual([p for p in self.hook_probs(hook, ad=AD + " Started in 2026.") if p.startswith("в крючке цифры")], [])
        self.assertTrue([p for p in self.hook_probs(hook) if p.startswith("в крючке цифры")])
        self.assertTrue([p for p in self.hook_probs("Acme Data builds tools for 2 traders, and this role covers it.")
                         if p.startswith("в крючке цифры")])

    def test_answer_sheet_stays_out_of_prompts(self):
        self.assertNotIn("Knockout answer sheet", letter._rules(BASE)[4])
        self.assertIn("Knockout answer sheet", letter._rules(BASE)[1])  # the checks still see the full facts
        self.saved_requirements()
        result, fake = self.write(ans(WRITER), ans(VERIFY_OK))
        for call in fake.calls:
            self.assertNotIn("Knockout answer sheet", call["prompt"])

    # M7 layout: one paragraph per proof
    def test_each_proof_is_its_own_paragraph(self):
        self.saved_requirements()
        result, _ = self.write(ans(WRITER), ans(VERIFY_OK))
        paras = result["text"].split("\n\n")
        for pid in PROOFS:
            self.assertIn(LIB["proofs"][pid]["text"], paras)
        self.assertTrue(paras[2].startswith(HOOK))
        self.assertIn(LIB["closings"]["cl_1"], paras)

    # M4 final whole-letter scan
    def test_surname_that_is_a_letter_never_word_gets_the_team_salutation(self):
        self.add_job("jsal", COMPANY, "Junior Product Owner", AD + " Kontakt: Herr Peter Salary, Vienna.")
        self.saved_requirements("jsal")
        result, _ = self.write(ans(WRITER), ans(VERIFY_OK), job="jsal")
        self.assertEqual(result["status"], "ok")
        self.assertIn("Dear Acme Data hiring team,", result["text"])
        self.assertNotIn("Salary", result["text"])

    def test_still_a_term_after_team_salutation_is_blocked(self):
        from unittest import mock
        self.saved_requirements()
        real = letter._never_hits
        with mock.patch.object(letter, "_never_hits", lambda text, blank=(): ["Visa"] if "Dear" in text else real(text, blank)):
            result, _ = self.write(ans(WRITER), ans(VERIFY_OK))
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["blocked_term"], "Visa")
        self.assertEqual(result["reason"], "в письме осталось запрещённое: Visa")
        con = store.connect()
        self.assertEqual(con.execute("select count(*) from letters where job_id='job1'").fetchone()[0], 0)
        con.close()

    def test_title_and_company_are_not_scanned(self):
        self.add_job("jstud", "Master Data GmbH", "Student Job (f/m/x) - Master Data", AD)
        self.saved_requirements("jstud")
        hook = "Master Data GmbH builds crypto data tools, and this role covers the backlog and specs."
        result, _ = self.write(ans(dict(WRITER, hook=hook)), ans(VERIFY_OK), job="jstud")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["subject"], "Application: Student Job")  # the ' - Master Data' segment is dropped
        self.assertEqual(result["hook_source"], "model")

    # M6 length: floor 180, cap 380 kept
    def test_length_limits(self):
        self.assertEqual(letter.BODY_WORDS, (180, 380))

    # 10 more AI tells, hook rules
    def test_more_ai_tells(self):
        for text, label in (("We show a commitment to open data.", "commitment to"),
                            ("The team is deeply rooted in crypto.", "deeply rooted"),
                            ("Acme Data is cited in industry reports.", "industry reports"),
                            ("Experts agree that data matters.", "experts agree"),
                            ("It is my passion for data.", "my passion"),
                            ("I would love to join.", "i would love"),
                            ("Acme Data is not a tool, but a team.", "not a X, but Y")):
            with self.subTest(label):
                self.assertIn(label, letter.ai_tells(text))

    def test_exclamation_and_company_repeats_in_hook(self):
        self.assertIn("в крючке восклицательный знак",
                      self.hook_probs("Acme Data builds crypto data tools, and this role covers the backlog!"))
        self.assertIn("название компании в крючке больше двух раз",
                      self.hook_probs("Acme Data builds tools for Acme Data traders and Acme Data users, and this role "
                                      "covers the backlog."))

    # 11 no "I"-paragraph note
    def test_no_i_paragraph_note(self):
        self.saved_requirements()
        result, _ = self.write(ans(WRITER), ans(VERIFY_OK))
        self.assertFalse(any("«I»" in n for n in result["notes"]))

    # 12 german fallback when the requirements failed
    def test_german_fallback_when_extraction_fails(self):
        ad = ("Wir suchen eine Produktmanagerin für unser Team in Wien. Du erstellst die Anforderungen und pflegst das "
              "Backlog in Jira und führst Marktforschung durch, und wir freuen uns, dass du der Sache folgst. Sie sind "
              "herzlich willkommen, die Bewerbung läuft über uns.")
        self.add_job("jde1", "Berliner Team GmbH", "Produktmanagerin (m/w/d)", ad)
        result, _ = self.write(llm.LLMError("no"), ans(WRITER), ans(VERIFY_OK), job="jde1")
        self.assertTrue(result["german_warning"])
        self.add_job("jde2", "Berliner Team GmbH", "Produktmanagerin (m/w/d)", ad + " English is a plus.")
        result, _ = self.write(llm.LLMError("no"), ans(WRITER), ans(VERIFY_OK), job="jde2")
        self.assertFalse(result["german_warning"])

    # 13 letters/<n>/requirements.json is reused and saved
    def test_letter_requirements_reused_after_cv_versions(self):
        got = dict(REQS, must=["Jira"])
        first, fake1 = self.write(ans(got), ans(WRITER), ans(VERIFY_OK))
        self.assertEqual(len(fake1.calls), 3)
        second, fake2 = self.write(ans(WRITER), ans(VERIFY_OK))
        self.assertEqual(len(fake2.calls), 2)
        self.assertTrue((Path(first["version"]) / "requirements.json").is_file())

    # 14 verifier gets the ad at most MAX_AD_CHARS
    def test_verifier_ad_is_capped(self):
        long_ad = AD + "x" * 9000 + " ENDMARK"
        self.add_job("jlong", COMPANY, "Junior Product Owner", long_ad)
        self.saved_requirements("jlong")
        result, fake = self.write(ans(WRITER), ans(VERIFY_OK), job="jlong")
        self.assertEqual(len(fake.calls), 2)
        for call in fake.calls:
            self.assertNotIn("ENDMARK", call["prompt"])

    # M9 gender markers and tidy-up
    def test_gender_markers_and_tidy(self):
        cases = {
            "Product Manager (m|w|d)": "Product Manager",
            "Product Manager [m/w/d]": "Product Manager",
            "Product Manager (m/w/divers)": "Product Manager",
            "Product Manager (m/w/d/*)": "Product Manager",
            "Product Manager m/w/d/x": "Product Manager",
            "Product Manager (m/f/nb)": "Product Manager",
            "Product Manager (all genders welcome)": "Product Manager",
            "Product Manager mwd": "Product Manager",
            "Product Manager m / w / d": "Product Manager",
            "Projektmanager*in (m/w/d)": "Projektmanager*in",
            "Kundenbetreuer:in (m/w/d)": "Kundenbetreuer:in",
            "Product Manager (m/w/d) , Wien": "Product Manager, Wien",
            "Product Manager ()": "Product Manager",
            "Product   Manager": "Product Manager",
            "Product Owner \u2013 Wien": "Product Owner - Wien",
        }
        for raw, want in cases.items():
            with self.subTest(raw):
                self.assertEqual(letter.clean_title(raw), want)


class HookRetry(Env):
    """Pilot 10.10: one hook retry with the reasons in English; at most four model calls; the deadline stops it."""
    COPY = "Acme Data builds a market data platform for crypto traders, and I have built a product of that kind."
    GOOD = "Acme Data is a crypto market data company, and this role covers product research and specs for its platform."

    def test_retry_success_gives_model_hook(self):
        self.saved_requirements()
        result, fake = self.write(ans(dict(WRITER, hook=self.COPY)), ans({"hook": self.GOOD}), ans(VERIFY_OK))
        self.assertEqual(result["hook_source"], "model")
        self.assertEqual(result["hook_attempts"], 2)
        self.assertIn(self.GOOD, result["text"])
        self.assertEqual(len(fake.calls), 3)
        self.assertIn("copies a 5-word phrase from the ad", fake.calls[1]["prompt"])  # the reason, in English

    def test_passing_first_hook_is_one_attempt(self):
        self.saved_requirements()
        result, fake = self.write(ans(WRITER), ans(VERIFY_OK))
        self.assertEqual(result["hook_attempts"], 1)
        self.assertEqual(len(fake.calls), 2)

    def test_retry_failing_gives_template(self):
        self.saved_requirements()
        result, fake = self.write(ans(dict(WRITER, hook=self.COPY)), ans({"hook": self.COPY}))
        self.assertEqual(result["hook_source"], "template")
        self.assertEqual(result["hook_attempts"], 2)
        self.assertTrue(result["hook_note"].startswith("в крючке: "), result["hook_note"])
        self.assertEqual(len(fake.calls), 2)

    def test_total_calls_at_most_four(self):
        result, fake = self.write(ans(REQS), ans(dict(WRITER, hook=self.COPY)), ans({"hook": self.GOOD}), ans(VERIFY_OK))
        self.assertEqual(result["hook_source"], "model")
        self.assertEqual(len(fake.calls), 4)  # extraction, writer, retry, verifier

    def test_retry_prompt_is_short_and_has_rules(self):
        self.saved_requirements()
        _, fake = self.write(ans(dict(WRITER, hook=self.COPY)), ans({"hook": self.GOOD}), ans(VERIFY_OK))
        retry = fake.calls[1]["prompt"]
        self.assertLess(len(retry), len(fake.calls[0]["prompt"]))
        self.assertIn('Return ONLY a JSON object, no other text: {"hook": "<text>"}', retry)
        self.assertIn("Do not reuse any 5 consecutive words from the ad", retry)

    def test_deadline_stops_the_retry(self):
        class PastDeadline(FakeRun):
            def __call__(self, prompt, model, timeout):
                out = super().__call__(prompt, model, timeout)
                llm.DEADLINE = time.monotonic() - 1  # the whole-run deadline passes after the writer answers
                return out

        self.saved_requirements()
        fake = PastDeadline(ans(dict(WRITER, hook=self.COPY)), ans({"hook": self.GOOD}))
        llm.RUN = fake
        result = letter.write_letter("job1")
        self.assertEqual(len(fake.calls), 1)  # the retry never reached claude
        self.assertEqual(result["hook_source"], "template")
        self.assertEqual(result["hook_attempts"], 2)
        self.assertIsNone(llm.DEADLINE)

    def test_never_claim_terms_in_writer_prompt_and_scrum_rejected(self):
        self.saved_requirements()
        _, fake = self.write(ans(WRITER), ans(VERIFY_OK))
        self.assertIn("Scrum", fake.calls[0]["prompt"])
        self.assertIn("Do not reuse any 5 consecutive words from the ad", fake.calls[0]["prompt"])
        probs = letter.hook_problems("Acme Data runs Scrum sprints for traders, and this role covers the backlog.",
                                     COMPANY, letter.checks.cv_text(BASE), letter.checks.cv_text(BASE), AD,
                                     *letter.checks.load_never_claim(), title="Junior Product Owner")
        self.assertTrue(any("Scrum" in p for p in probs), probs)

    def test_alchemy_pay_passes_and_lowercase_pay_is_caught(self):
        self.assertEqual(letter._never_hits("a competitor, SwiftRamp, and Stripe"), [])
        self.assertTrue(letter._never_hits("my pay expectation is open"))


class PlatformVariants(Env):
    """Pilot 10.10: the platform is introduced once; later context proofs use text_after (real library ids)."""
    HOOK = "Acme Data is a crypto market data company, and this role covers product research and specs for its platform."

    def setUp(self):
        super().setUp()
        self.real = json.loads((ROOT / "cv" / "letter_library.json").read_text(encoding="utf-8"))
        letter.LIBRARY = ROOT / "cv" / "letter_library.json"  # Env pointed it at the fixture

    def run_with(self, identity, proofs):
        self.saved_requirements()
        writer = dict(WRITER, identity=identity, proofs=proofs, closing="cl_walkthrough", hook=self.HOOK)
        result, fake = self.write(ans(writer), ans(VERIFY_OK))
        return result, fake

    def test_identity_introduces_platform_so_both_use_text_after(self):
        result, _ = self.run_with("id_product", ["pr_specs_delivery", "pr_metrics", "pr_market_research"])
        self.assertEqual(result["status"], "ok", result)
        text = result["text"]
        for pid in ("pr_specs_delivery", "pr_metrics"):
            self.assertIn(self.real["proofs"][pid]["text_after"], text)
            self.assertNotIn(self.real["proofs"][pid]["text"], text)

    def test_first_context_proof_uses_full_text_then_text_after(self):
        result, _ = self.run_with("id_research", ["pr_metrics", "pr_gtm_stakeholders", "pr_market_research"])
        text = result["text"]
        self.assertIn(self.real["proofs"]["pr_metrics"]["text"], text)
        self.assertNotIn(self.real["proofs"]["pr_metrics"]["text_after"], text)
        self.assertIn(self.real["proofs"]["pr_gtm_stakeholders"]["text_after"], text)
        self.assertNotIn(self.real["proofs"]["pr_gtm_stakeholders"]["text"], text)

    def test_text_after_is_checked_as_library_text(self):
        lib = json.loads(json.dumps(self.real))
        lib["proofs"]["pr_specs_delivery"]["text_after"] = "It was vibrant to work on the specs."
        path = Path(self.tmp.name) / "bad.json"
        path.write_text(json.dumps(lib), encoding="utf-8")
        with self.assertRaisesRegex(library.LibraryError, "proof pr_specs_delivery text_after: AI tell: vibrant"):
            letter.load_library(BASE, path=path)

    def test_text_after_without_context_is_rejected(self):
        lib = json.loads(json.dumps(self.real))
        del lib["proofs"]["pr_specs_delivery"]["context"]
        path = Path(self.tmp.name) / "bad2.json"
        path.write_text(json.dumps(lib), encoding="utf-8")
        with self.assertRaisesRegex(library.LibraryError, "text_after without context"):
            letter.load_library(BASE, path=path)

    def test_real_library_with_variants_loads(self):
        lib = letter.load_library(BASE, path=ROOT / "cv" / "letter_library.json")
        self.assertIn("text_after", lib["proofs"]["pr_specs_delivery"])

    def test_copy_check_uses_the_variant_used(self):
        after = self.real["proofs"]["pr_specs_delivery"]["text_after"]
        words = " ".join(after.split()[:6])
        hook = f"Acme Data is a crypto data company where someone can {words.lower()} for its platform."
        probs = letter.hook_problems(hook, COMPANY, letter.checks.cv_text(BASE), letter.checks.cv_text(BASE), AD,
                                     *letter.checks.load_never_claim(), title="Junior Product Owner",
                                     repeats=[after])
        self.assertTrue(any(p.startswith("крючок повторяет опыт") for p in probs), probs)


class QaRound2(Env):
    """Sonnet QA round 2: names, money in titles, hook bans, retry fence, blocked path."""

    def hook_probs(self, hook, ad=AD, title="Junior Product Owner"):
        return letter.hook_problems(hook, COMPANY, letter.checks.cv_text(BASE), letter.checks.cv_text(BASE), ad,
                                    *letter.checks.load_never_claim(), title=title)

    def add_job(self, jid, company, title, desc):
        con = store.connect()
        con.execute("insert into jobs (id, key, company, title, data) values (?,?,?,?,?)",
                    (jid, jid, company, title, json.dumps({"description": desc})))
        con.commit()
        con.close()

    def test_names_keep_first_and_surname_only(self):
        cases = {
            "Herr Peter Gruber Geschäftsführer": "Dear Mr Gruber,",
            "Herr Peter Gruber Wien": "Dear Mr Gruber,",
            "Frau Anna Huber Senior Recruiter": "Dear Ms Huber,",
            "Mr Jan van der Berg Head of Product": "Dear Mr van der Berg,",
            "Frau Anna Maria Huber": "Dear Ms Huber,",
            "Frau Anna Maria von Huber": "Dear Ms von Huber,",
        }
        for name, want in cases.items():
            with self.subTest(name):
                self.assertEqual(letter.salutation("Kontakt: " + name, COMPANY), want)

    def test_money_and_dates_in_the_title_are_cut(self):
        cases = {
            "Product Manager € 3.000": "Product Manager",
            "Product Manager 3.000 Euro Mindestgehalt": "Product Manager",
            "Product Manager mit Gehalt ab 3000": "Product Manager",
            "Product Manager 40.000 p.a.": "Product Manager",
            "Product Manager 45k": "Product Manager",
            "Product Manager ab sofort": "Product Manager",
            "Product Manager - ab sofort - Wien": "Product Manager - Wien",
            "Product Manager 01.11.2026": "Product Manager",
            "Product Manager (m/w/d) 3,000 brutto": "Product Manager",
            "3.000 Euro Mindestgehalt": "",
        }
        for raw, want in cases.items():
            with self.subTest(raw):
                self.assertEqual(letter.clean_title(raw), want)

    def test_empty_title_uses_fixed_hook_and_plain_subject(self):
        self.add_job("jempty", COMPANY, "3.000 Euro Mindestgehalt", AD)
        self.saved_requirements("jempty")
        hook = "Acme Data is a crypto market data company, and this role covers product research and specs for its platform."
        result, fake = self.write(ans(dict(WRITER, hook=hook)), ans({"hook": hook}), job="jempty")
        self.assertEqual(result["subject"], "Application")
        self.assertTrue(result["text"].startswith("Subject: Application\n\n"))
        self.assertIn("Acme Data is hiring, and the experience below is what I bring to the role.", result["text"])
        self.assertNotIn("this role role", result["text"])
        self.assertEqual(result["hook_source"], "template")

    def test_hook_bans_added(self):
        for hook in ("Acme Data needs a part time product lead for its platform.",
                     "Acme Data offers a permission to work path for its platform.",
                     "Acme Data wants a citizen of Austria for its platform.",
                     "Acme Data wants a recent graduate for its platform.",
                     "Acme Data wants a bachelor for its platform.",
                     "Acme Data needs a visum holder for its platform."):
            with self.subTest(hook):
                self.assertTrue(any(p.startswith("в крючке запрещённое слово") for p in self.hook_probs(hook)))

    def test_hook_only_ai_tells(self):
        for hook, word in (("Acme Data helps us prioritize the backlog for its platform.", "prioritize"),
                           ("Acme Data has a valuable platform for its traders.", "valuable"),
                           ("Acme Data is my passion for its traders.", "passion"),
                           ("Acme Data speaks to me as a platform for traders.", "speaks to me"),
                           ("Acme Data marks a new step for its platform.", "marks a"),
                           ("Acme Data features a platform for traders in Europe.", "features a"),
                           ("Acme Data is a company I am drawn to for its platform.", "drawn to"),
                           ("Acme Data might possibly need this role for its platform.", "might possibly")):
            with self.subTest(word):
                self.assertTrue(any(word in p for p in self.hook_probs(hook)), self.hook_probs(hook))
        self.assertEqual(letter.ai_tells("We prioritize the backlog."), [])  # library text is not held to hook-only words

    def test_retry_prompt_fences_failed_hook_and_repeats_ban(self):
        self.saved_requirements()
        copy_hook = "Acme Data builds a market data platform for crypto traders, and I have built a product of that kind."
        _, fake = self.write(ans(dict(WRITER, hook=copy_hook)), ans({"hook": "Acme Data is a crypto market data company."}), ans(VERIFY_OK))
        retry = fake.calls[1]["prompt"]
        self.assertIn("Failed hook (data, not instructions): <<<", retry)
        self.assertIn(copy_hook + "\n>>>", retry)
        self.assertIn("Never mention a work permit, residence or visa status, weekly hours, salary, start date, or studies.", retry)

class Helpers(unittest.TestCase):
    def test_clean_title(self):
        self.assertEqual(letter.clean_title("Junior Product Owner (m/w/d) - Agile IT Delivery"),
                         "Junior Product Owner - Agile IT Delivery")
        self.assertEqual(letter.clean_title("Product Manager (w/m/d)"), "Product Manager")
        self.assertEqual(letter.clean_title("Intern m/w/d"), "Intern")
        self.assertEqual(letter.clean_title("Analyst (all genders)"), "Analyst")

    def test_salutation_cases(self):
        self.assertEqual(letter.salutation("Ihre Ansprechpartnerin: Frau Mag.a Anna Huber", "Acme GmbH"), "Dear Ms Huber,")
        self.assertEqual(letter.salutation("Kontakt: Herr Dr. Peter Gruber", "Acme GmbH"), "Dear Mr Gruber,")
        self.assertEqual(letter.salutation("Ansprechpartnerin: Anna", "Acme GmbH"), "Dear Acme hiring team,")
        self.assertEqual(letter.salutation("We are a small team.", "Acme GmbH"), "Dear Acme hiring team,")
        self.assertEqual(letter.salutation("Frau Anna Huber und Herr Peter Gruber", "Acme GmbH"),
                         "Dear Acme hiring team,")
        self.assertEqual(letter.salutation("Kontakt: Herr Peter Gruber, Frau Anna Huber", "Acme GmbH"), "Dear Mr Gruber,")

    def test_signature_from_en_json(self):
        expected = "Kind regards,\nAlex Morgan\n" + " · ".join(c["text"] for c in BASE["contacts"])
        self.assertEqual(letter.signature(BASE, "Kind regards,"), expected)

    def test_model_hook_passes_rules(self):
        probs = letter.hook_problems(HOOK, COMPANY, letter.checks.cv_text(BASE), letter.checks.cv_text(BASE), AD,
                                     *letter.checks.load_never_claim())
        self.assertEqual(probs, [])


class Engine(Env):
    def test_model_hook_letter_and_row(self):
        self.saved_requirements()
        result, fake = self.write(ans(WRITER), ans(VERIFY_OK))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["hook_source"], "model")
        self.assertEqual(result["hook_note"], "")
        self.assertIn(HOOK, result["text"])
        self.assertTrue(result["text"].startswith("Subject: Application: Junior Product Owner - Agile IT Delivery\n\nDear"))
        self.assertNotIn("(m/w/d)", result["text"])
        self.assertIn(COMPANY, result["text"])
        self.assertEqual(result["subject"], "Application: Junior Product Owner - Agile IT Delivery")
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.calls[0]["model"], "sonnet")
        self.assertEqual(fake.calls[1]["model"], "sonnet")
        self.assertTrue(result["text"].endswith(letter.signature(BASE, "Kind regards,")))
        self.assertTrue(180 <= result["words"] <= 380, result["words"])
        self.assertEqual({e["proof"] for e in result["evidence"]} & set(PROOFS), {"pr_docs", "pr_research"})
        self.assertFalse(result["german_warning"])
        self.assertIsNone(llm.DEADLINE)
        con = store.connect()
        row = con.execute("select * from letters where id=?", (result["letter_id"],)).fetchone()
        con.close()
        self.assertEqual((row["job_id"], row["status"], row["hook_source"]), ("job1", "ok", "model"))
        self.assertEqual(Path(row["path"]).read_text(encoding="utf-8"), result["text"])
        self.assertTrue((Path(row["path"]).parent / "result.json").is_file())

    def test_hook_problem_falls_back_to_template(self):
        cases = {
            "invented number": "Acme Data grew to 5,000 users, and I took a product of that kind from research to launch.",
            "cliche": "Acme Data is a dynamic team, and I have built crypto data products from research to launch.",
            "German hook": "Acme Data baut eine Plattform für Krypto-Daten, und ich habe Erfahrung im Produktmanagement.",
            "missing company": "A crypto market data platform needs a product owner who can write specs, and I have written them.",
            "permit mention": "Acme Data builds crypto data tools, and my work permit lets me start work now.",
            "salary mention": "Acme Data builds crypto data tools, my salary expectation is flexible, and I write specs.",
            "starts with I am": "I am a product manager, and Acme Data builds crypto data tools I can help with.",
        }
        self.saved_requirements()
        for name, hook in cases.items():
            with self.subTest(name):
                result, fake = self.write(ans(dict(WRITER, hook=hook)), ans({"hook": hook}))
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["hook_source"], "template")
                self.assertEqual(result["hook_attempts"], 2)
                self.assertTrue(result["hook_note"].startswith("в крючке"), result["hook_note"])
                self.assertIn("The Junior Product Owner - Agile IT Delivery role at Acme Data is close to the work I do", result["text"])
                self.assertEqual(len(fake.calls), 2)  # writer, one hook retry; no verifier after a failed rule

    def test_verifier_not_ok_gives_template(self):
        self.saved_requirements()
        result, fake = self.write(ans(WRITER), ans({"ok": False, "problems": ["company claim not in the ad"]}))
        self.assertEqual(result["hook_source"], "template")
        self.assertEqual(result["hook_note"], "проверка не подтвердила крючок")
        self.assertEqual(result["verify_problems"], ["company claim not in the ad"])
        self.assertNotIn("company claim not in the ad", " ".join(result["notes"]))
        self.assertEqual(len(fake.calls), 2)

    def test_verifier_error_gives_template(self):
        self.saved_requirements()
        result, _ = self.write(ans(WRITER), llm.LLMError("claude timed out after 120 s"))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["hook_source"], "template")
        self.assertEqual(result["hook_note"], "проверка не ответила")

    def test_writer_error_still_gives_a_letter(self):
        self.saved_requirements()
        result, fake = self.write(llm.LLMError("claude timed out after 180 s"))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["hook_source"], "template")
        self.assertIn("Claude не ответил, письмо собрано по шаблону", result["notes"])
        self.assertIn(COMPANY, result["text"])
        self.assertIn("The Junior Product Owner - Agile IT Delivery role at Acme Data", result["text"])
        self.assertTrue(180 <= result["words"] <= 380, result["words"])
        self.assertEqual(len(fake.calls), 1)

    def test_unreadable_writer_answer_still_gives_a_letter(self):
        self.saved_requirements()
        result, _ = self.write(ans(["not", "an", "object"]))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["hook_source"], "template")
        self.assertIn(COMPANY, result["text"])

    def test_unknown_duplicate_and_exclusive_proofs(self):
        self.saved_requirements()
        picks = ["nope", "pr_research", "pr_research", "pr_research_pub", "pr_docs"]
        result, _ = self.write(ans(dict(WRITER, proofs=picks)), ans(VERIFY_OK))
        self.assertEqual(result["status"], "ok")
        text = result["text"]
        self.assertIn(LIB["proofs"]["pr_research"]["text"], text)
        self.assertIn(LIB["proofs"]["pr_docs"]["text"], text)
        self.assertNotIn(LIB["proofs"]["pr_research_pub"]["text"], text)
        proofs_used = {e["proof"] for e in result["evidence"]}
        self.assertNotIn("nope", proofs_used)
        self.assertNotIn("pr_research_pub", proofs_used)
        self.assertEqual(len(set(e["proof"] for e in result["evidence"])), len(result["evidence"]))

    def test_no_letter_never_term_in_any_letter(self):
        self.saved_requirements("job1")
        self.saved_requirements("job3")
        for job, title in (("job1", "Junior Product Owner - Agile IT Delivery"),
                           ("job3", "Student Job - Junior Product Owner")):
            with self.subTest(job):
                result, _ = self.write(ans(WRITER), ans(VERIFY_OK), job=job)
                blank = (title, COMPANY)
                self.assertEqual(letter._never_hits(result["text"], blank), [])

    def test_student_title_kept_in_subject_without_note(self):
        self.saved_requirements("job6")
        hook = "e.Hive is a crypto data company, and I have taken a product of that kind from research to launch."
        result, _ = self.write(ans(dict(WRITER, hook=hook)), ans(VERIFY_OK), job="job6")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["subject"], "Application: Student Job - e.Hive")
        self.assertEqual(result["hook_source"], "model")
        self.assertEqual(result["notes"], [])
        self.assertEqual(letter._never_hits(result["text"], ("Student Job - e.Hive", "e.Hive")), [])

    def test_long_proofs_drop_the_third(self):
        long_lib = copy_lib()
        for k in ("pr_research", "pr_docs", "pr_metrics"):
            long_lib["proofs"][k]["text"] = (LIB["proofs"][k]["text"] + " ") + LONG * 2
        self.write_lib(long_lib)
        self.saved_requirements()
        result, _ = self.write(ans(WRITER), ans(VERIFY_OK))
        self.assertEqual(result["status"], "ok")
        self.assertLessEqual(result["words"], 380)
        self.assertNotIn(long_lib["proofs"]["pr_metrics"]["text"], result["text"])
        self.assertIn(long_lib["proofs"]["pr_docs"]["text"], result["text"])

    def test_german_ad_warning(self):
        self.saved_requirements("job4", dict(REQS, ad_language="de"), n=1)
        self.saved_requirements("job5", dict(REQS, ad_language="de"), n=1)
        result, _ = self.write(ans(WRITER), ans(VERIFY_OK), job="job4")
        self.assertTrue(result["german_warning"])
        result, _ = self.write(ans(WRITER), ans(VERIFY_OK), job="job5")
        self.assertFalse(result["german_warning"])

    def test_requirements_reused_without_a_call(self):
        self.saved_requirements()
        result, fake = self.write(ans(WRITER), ans(VERIFY_OK))
        self.assertEqual(len(fake.calls), 2)
        self.assertFalse((Path(result["version"]) / "requirements.json").exists())

    def test_requirements_extracted_when_absent(self):
        got = dict(REQS, must=["Jira"], min_salary=None, hours=None)
        result, fake = self.write(ans(got), ans(WRITER), ans(VERIFY_OK))
        self.assertEqual(len(fake.calls), 3)
        self.assertIn("You extract the requirements", fake.calls[0]["prompt"])
        saved = json.loads((Path(result["version"]) / "requirements.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, got)

    def test_need_text_without_description(self):
        result, fake = self.write(job="job2")
        self.assertEqual(result["status"], "need_text")
        self.assertEqual(result["reason"], "нет текста вакансии")
        self.assertEqual(fake.calls, [])

    def test_pasted_text_is_used(self):
        pasted = Path(os.environ["JOBDIGEST_CV_DIR"]) / "job2" / "pasted.txt"
        pasted.parent.mkdir(parents=True, exist_ok=True)
        pasted.write_text(AD, encoding="utf-8")
        self.saved_requirements("job2")
        result, _ = self.write(ans(WRITER), ans(VERIFY_OK), job="job2")
        self.assertEqual(result["status"], "ok")

    def test_unknown_job_raises_lookup(self):
        llm.RUN = FakeRun()
        with self.assertRaises(LookupError):
            letter.write_letter("nojob")


if __name__ == "__main__":
    unittest.main()
