"""Stage 6: the 'Написать письмо' button, its press, and the letter worker's messages. Fake Telegram api, fake spawn
and a fake letter engine (worker.LETTER): no Claude, no network. cv/tailor/letter.py is not imported here."""
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot  # noqa: E402
import store  # noqa: E402
from cv.tailor import library, worker  # noqa: E402
from tests.test_bot_tailor import AD, CHAT, Env  # noqa: E402

LETTER_TEXT = ("Dear Ms Huber,\n\nRBI is hiring a Junior Product Owner for Agile IT Delivery. I run backlogs in Jira "
               "and write specs that teams can build from.\n\nIn my last role I ran market research for a product "
               "team and turned the results into a roadmap.\n\nKind regards,\nAlex Morgan")


def letter_result(**kw):
    """A write_letter result as letter.py returns it (status ok unless overridden)."""
    out = {"status": "ok", "text": LETTER_TEXT, "subject": "Application: Student Job - Agile IT Delivery",
           "words": 260, "hook_source": "model", "hook_note": "",
           "evidence": [{"requirement": "Jira", "proof": "pr_jira", "label_ru": "Jira и бэклог"},
                        {"requirement": "market research", "proof": "pr_research", "label_ru": "Исследование рынка"}],
           "german_warning": False, "notes": [], "reason": "", "version": "", "letter_id": 1}
    out.update(kw)
    return out


class Letter(Env):
    def setUp(self):
        super().setUp()
        self.saved_letter = worker.LETTER
        worker.LETTER = None
        worker._children.clear()
        worker._notices.clear()

    def tearDown(self):
        worker.LETTER = self.saved_letter
        worker._children.clear()
        worker._notices.clear()
        super().tearDown()

    def toast(self):
        return self.api.of("answerCallbackQuery")[-1]["text"]

    def run_letter(self, result):
        """worker.main(kind='letter') with a fake engine; the lock must be free again afterwards."""
        calls = []

        def engine(job_id):
            calls.append(job_id)
            if isinstance(result, BaseException):
                raise result
            return result
        worker.LETTER = engine
        token = worker.acquire("j1")
        worker.main("j1", CHAT, None, token, kind="letter")
        self.assertFalse(worker.lock_path("j1").exists())  # released in every case
        return calls


class Button(Letter):
    def test_row_with_both_buttons_on_interested_card(self):
        markup = bot.collapsed_markup(self.row("j1"), "j1")
        row = [{"text": "Адаптировать резюме", "callback_data": "c:tailor:j1"},
               {"text": "Написать письмо", "callback_data": "c:letter:j1"}]
        self.assertIn(row, markup)
        self.assertEqual(sum("c:tailor:j1" in json.dumps(r, ensure_ascii=False) for r in markup), 1)

    def test_button_on_every_status_of_interested_card(self):
        for status in (None,) + store.STATUSES:
            self.con.execute("update jobs set status=? where id='j1'", (status,))
            self.assertIn("c:letter:j1", json.dumps(bot.collapsed_markup(self.row("j1"), "j1")), status)

    def test_no_letter_button_on_other_verdicts(self):
        for jid in ("j2", "j3"):
            self.assertNotIn("c:letter", json.dumps(bot.collapsed_markup(self.row(jid), jid)))

    def test_callback_data_fits_64_bytes(self):
        jid = store.job_id("some company#some very long title of a job ad that goes on and on")
        markup = bot.collapsed_markup({"verdict": "interested", "status": None, "open_url": None, "url": None}, jid)
        datas = [b["callback_data"] for r in markup for b in r if "callback_data" in b]
        letter = [d for d in datas if d.startswith("c:letter:")]
        self.assertEqual(len(letter), 1)
        self.assertTrue(all(len(d.encode()) <= 64 for d in datas), datas)


class Press(Letter):
    def test_press_spawns_letter_worker(self):
        self.press("c:letter:j1")
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.spawned[0][:2], ("j1", CHAT))
        self.assertEqual(self.spawned[0][4], "letter")
        self.assertEqual(self.toast(), "Пишу письмо")
        self.assertEqual(self.api.texts()[-1], "Пишу письмо, около минуты")

    def test_refusals_on_other_verdicts_and_unknown_job(self):
        self.press("c:letter:j2")
        self.assertEqual(self.toast(), "Письмо пишется только для вакансий с отметкой «Интересно»")
        self.assertEqual(self.api.texts()[-1], "Письмо пишется только для вакансий с отметкой «Интересно»")
        self.press("c:letter:nope")
        self.assertEqual(self.toast(), "Эта вакансия не найдена в базе")
        self.assertEqual(self.spawned, [])
        self.assertFalse(worker.lock_path("j2").exists())

    def test_busy_while_cv_run_holds_the_lock(self):
        token = worker.acquire("j1")  # a CV run of the same job
        self.press("c:letter:j1")
        self.assertEqual(self.toast(), "Уже готовлю, подожди")
        self.assertEqual(self.spawned, [])
        worker.release("j1", token)
        self.press("c:letter:j1")
        self.assertEqual(len(self.spawned), 1)

    def test_cv_press_busy_while_letter_run_holds_the_lock(self):
        self.press("c:letter:j1")
        self.press("c:tailor:j1")
        self.assertEqual(self.toast(), "Уже готовлю, подожди")
        self.assertEqual(len(self.spawned), 1)

    def test_double_letter_press_second_is_busy(self):
        self.press("c:letter:j1")
        self.press("c:letter:j1")
        self.assertEqual(self.toast(), "Уже готовлю, подожди")
        self.assertEqual(self.api.texts()[-1], "Уже готовлю, подожди")
        self.assertEqual(len(self.spawned), 1)

    def test_spawn_failure_frees_lock_and_says_so(self):
        def boom(*a, **kw):
            raise OSError("no python")
        worker.spawn = boom
        self.press("c:letter:j1")
        self.assertEqual(self.api.texts()[-1], "Не получилось запустить письмо (OSError). Попробуй ещё раз.")
        self.assertFalse(worker.lock_path("j1").exists())

    def test_real_spawn_adds_kind_only_for_letter(self):
        seen = []

        class Popen:
            def __init__(self, args, **kw):
                seen.append(args)

            def poll(self):
                return None
        real_spawn = self.saved[2]  # the spawn before Env replaced it with a fake
        saved_popen, worker.subprocess.Popen = worker.subprocess.Popen, Popen
        try:
            real_spawn("j1", CHAT, "tok-letter", None, kind="letter")
            real_spawn("j1", CHAT, "tok-cv", None)
        finally:
            worker.subprocess.Popen = saved_popen
        self.assertEqual(seen[0][-2:], ["--kind", "letter"])
        self.assertNotIn("--kind", seen[1])
        self.assertEqual([c["kind"] for c in worker._children], ["letter", "cv"])


class Worker(Letter):
    def test_ok_sends_letter_then_summary(self):
        calls = self.run_letter(letter_result())
        self.assertEqual(calls, ["j1"])
        self.assertEqual(len(self.api.of("sendMessage")), 2)
        self.assertEqual(self.api.texts()[0], LETTER_TEXT)
        self.assertNotIn("parse_mode", self.api.of("sendMessage")[0])  # plain text, no formatting
        summary = self.api.texts()[1]
        self.assertIn("Письмо: 260 слов.", summary)
        self.assertIn("Какие требования закрывает:", summary)
        self.assertIn("- Jira -> Jira и бэклог", summary)
        self.assertIn("- market research -> Исследование рынка", summary)
        self.assertIn("Первый абзац написан под компанию и проверен.", summary)
        self.assertNotIn("Заметки:", summary)
        self.assertNotIn("Внимание", summary)

    def test_letter_runs_not_the_cv_engine(self):
        cv_calls = []
        worker.TAILOR = lambda job_id, text=None: cv_calls.append(job_id)
        self.run_letter(letter_result())
        self.assertEqual(cv_calls, [])

    def test_template_hook_line(self):
        self.run_letter(letter_result(hook_source="template", hook_note="проверка не подтвердила крючок."))
        self.assertIn("Первый абзац по шаблону: проверка не подтвердила крючок.", self.api.texts()[1])

    def test_notes_listed(self):
        self.run_letter(letter_result(notes=["Claude не ответил, письмо собрано по шаблону"]))
        summary = self.api.texts()[1]
        self.assertIn("Заметки:\n- Claude не ответил, письмо собрано по шаблону", summary)

    def test_german_warning_line(self):
        self.run_letter(letter_result(german_warning=True))
        self.assertIn("Внимание: объявление на немецком, английский не упомянут - шанс ниже.", self.api.texts()[1])
        self.api.calls.clear()
        self.run_letter(letter_result(german_warning=False))
        self.assertNotIn("немецком", self.api.texts()[-1])

    def test_evidence_at_most_four_and_empty_skipped(self):
        ev = [{"requirement": f"req {n}", "proof": "p", "label_ru": f"label {n}"} for n in range(6)]
        ev.insert(0, {"requirement": "no label", "proof": "p", "label_ru": ""})
        self.run_letter(letter_result(evidence=ev))
        summary = self.api.texts()[1]
        self.assertIn("- req 0 -> label 0", summary)
        self.assertIn("- req 3 -> label 3", summary)
        self.assertNotIn("req 4", summary)
        self.assertNotIn("no label", summary)

    def test_dashes_replaced_in_letter_and_summary(self):
        self.run_letter(letter_result(text="Hello — world – done", notes=["a — b"]))
        self.assertEqual(self.api.texts()[0], "Hello - world - done")
        self.assertIn("- a - b", self.api.texts()[1])
        for text in self.api.texts():
            self.assertNotIn("—", text)
            self.assertNotIn("–", text)

    def test_long_letter_is_cut(self):
        self.run_letter(letter_result(text="x" * 5000))
        self.assertLessEqual(len(self.api.texts()[0]), worker.MAX_TEXT)
        self.assertTrue(self.api.texts()[0].endswith("..."))

    def test_need_text_message(self):
        self.run_letter({"status": "need_text", "reason": "нет текста вакансии"})
        self.assertEqual(self.api.texts(), [worker.NEED_TEXT_LETTER])
        self.assertEqual(self.api.texts()[-1], "Нет текста объявления. Нажми «Адаптировать резюме» и пришли текст - "
                                               "письмо возьмёт его оттуда.")
        self.assertEqual(self.api.of("sendDocument"), [])

    def test_library_error_message(self):
        self.run_letter(library.LibraryError("letter_library.json: bad"))
        self.assertEqual(self.api.texts()[-1], "Не получилось написать письмо: библиотека письма "
                                               "(cv/letter_library.json) повреждена или не прошла проверку.")

    def test_lookup_error_message(self):
        self.run_letter(LookupError("j1"))
        self.assertEqual(self.api.texts()[-1], "Не получилось написать письмо: вакансия не найдена в базе.")

    def test_generic_error_message(self):
        self.run_letter(ValueError("boom"))
        self.assertEqual(self.api.texts()[-1],
                         "Не получилось написать письмо: внутренняя ошибка (ValueError). Попробуй ещё раз.")

    def test_unexpected_status_is_an_internal_error(self):
        self.run_letter({"status": "fallback", "reason": "x"})
        self.assertEqual(self.api.texts()[-1],
                         "Не получилось написать письмо: внутренняя ошибка (RuntimeError). Попробуй ещё раз.")

    def test_blocked_sends_one_message_and_no_letter(self):
        self.run_letter(letter_result(status="blocked", text="", blocked_term="salary",
                                      reason="в письме осталось запрещённое: salary"))
        self.assertEqual(self.api.texts()[-1],
                         "Не получилось написать письмо: в тексте осталось запрещённое (salary). Письмо не отправлено.")
        self.assertFalse(any(LETTER_TEXT in t for t in self.api.texts()))

    def test_blocked_term_is_capped_at_60_characters(self):
        term = "x" * 100
        self.run_letter(letter_result(status="blocked", text="", blocked_term=term, reason="x"))
        msg = self.api.texts()[-1]
        self.assertIn("(" + "x" * 60 + ")", msg)
        self.assertNotIn("x" * 61, msg)

    def test_summary_names_default_hook_when_no_note(self):
        self.run_letter(letter_result(hook_source="template", hook_note=""))
        self.assertIn("Первый абзац по шаблону.", self.api.texts()[1])


class Died(Letter):
    def dead(self, kind, code=1):
        token = worker.acquire("j1")

        class Proc:
            def poll(self):
                return code
        worker._children.append({"proc": Proc(), "job_id": "j1", "token": token, "chat_id": CHAT, "kind": kind})

    def test_died_text_per_kind(self):
        self.assertEqual(worker.died_text("letter"),
                         "Письмо оборвалось (процесс остановлен), письмо не отправлено. Нажми «Написать письмо» ещё раз.")
        self.assertEqual(worker.died_text("cv"), worker.DIED_TEXT)

    def test_notices_carry_kind(self):
        self.dead("letter")
        self.assertEqual(worker.notices(), [("j1", CHAT, "letter")])
        self.assertEqual(worker.notices(), [])
        self.assertFalse(worker.lock_path("j1").exists())

    def test_bot_loop_sends_letter_text_for_a_dead_letter_worker(self):
        self.dead("letter")
        bot.report_dead_workers({})
        self.assertEqual(self.api.texts(), [worker.LETTER_DIED_TEXT])

    def test_bot_loop_sends_cv_text_for_a_dead_cv_worker(self):
        self.dead("cv")
        bot.report_dead_workers({})
        self.assertEqual(self.api.texts(), [worker.DIED_TEXT])

    def test_clean_exit_is_not_reported(self):
        self.dead("letter", code=0)
        bot.report_dead_workers({})
        self.assertEqual(self.api.texts(), [])
        self.assertFalse(worker.lock_path("j1").exists())


class Cli(Letter):
    def parse(self, argv):
        seen = []
        saved_main, saved_mask = worker.main, bot.mask_streams
        worker.main = lambda *a, **k: seen.append(a) or 0
        bot.mask_streams = lambda: None
        try:
            worker.cli(argv)
        finally:
            worker.main, bot.mask_streams = saved_main, saved_mask
        return seen[-1]

    def test_kind_letter_is_passed_to_main(self):
        self.assertEqual(self.parse(["j1", "4242", "--lock", "tok", "--kind", "letter"]),
                         ("j1", "4242", None, "tok", "letter"))

    def test_kind_defaults_to_cv(self):
        self.assertEqual(self.parse(["j1", "4242", "--lock", "tok"])[4], "cv")

    def test_unknown_kind_is_refused(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.parse(["j1", "4242", "--kind", "video"])


if __name__ == "__main__":
    unittest.main()
