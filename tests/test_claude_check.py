"""Stage 11 (05.11.2026): scripts/claude_check.py - one tiny Claude call at the start of the morning. An auth failure
sends ONE Telegram line and sets a health flag; other failures only log; the exit code is always 0; run.sh runs the
check right after health.py start. No real claude and no Telegram: subprocess.run, alert.send and health.record are fakes."""
import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import claude_check as cc  # noqa: E402
import enrich  # noqa: E402


def done(stdout="", stderr="", rc=0):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def reply(result, is_error=False):
    return json.dumps({"type": "result", "is_error": is_error, "result": result})


class ClaudeCheckTest(unittest.TestCase):
    def setUp(self):
        self.run = mock.patch.object(cc.subprocess, "run").start()
        self.send = mock.patch.object(cc.alert, "send", return_value=True).start()
        self.record = mock.patch.object(cc.health, "record").start()
        self.addCleanup(mock.patch.stopall)

    def main_with(self, answer):
        if isinstance(answer, BaseException):
            self.run.side_effect = answer
        else:
            self.run.return_value = answer
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cc.main()
        return rc, out.getvalue()

    def test_clean_reply_is_ok_and_sends_nothing(self):
        rc, log = self.main_with(done(reply("OK")))
        self.assertEqual(rc, 0)
        self.send.assert_not_called()
        self.record.assert_called_once_with("claude", status="ok", detail="ok")
        self.assertIn("claude check: ok", log)

    def test_each_auth_marker_sends_one_line_with_the_login_steps(self):
        for marker in enrich.AUTH_MARKERS:  # the one marker list, owned by enrich.py
            with self.subTest(marker=marker):
                self.send.reset_mock()
                self.record.reset_mock()
                rc, _ = self.main_with(done(stderr=f"Error: {marker} happened", rc=1))
                self.assertEqual(rc, 0)
                self.send.assert_called_once()
                text = self.send.call_args.args[0]
                self.assertEqual(text, cc.MESSAGE)
                self.assertIn("ssh -t vps claude", text)
                self.assertIn("/login", text)
                self.record.assert_called_once_with("claude", status="auth", detail="auth")

    def test_auth_marker_inside_the_json_result_is_found(self):
        # The CLI may escape the slash: '\/login' is still '/login' once the reply is read as JSON.
        rc, _ = self.main_with(done(stdout='{"is_error": true, "result": "Run \\/login first"}', rc=1))
        self.assertEqual(rc, 0)
        self.send.assert_called_once()
        self.record.assert_called_once_with("claude", status="auth", detail="auth")

    def test_the_check_uses_enrich_auth_error(self):
        with mock.patch.object(enrich, "auth_error", return_value=True) as ae:
            rc, _ = self.main_with(done(stdout="plain words", rc=0))
        self.assertTrue(ae.called)
        self.send.assert_called_once()

    def test_fallback_when_enrich_does_not_import_is_logged_and_still_works(self):
        import builtins
        real_import = builtins.__import__

        def no_enrich(name, *args, **kwargs):
            if name == "enrich":
                raise ImportError("simulated")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=no_enrich):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.run.return_value = done(stderr="Please run /login", rc=1)
                rc = cc.main()
        self.assertEqual(rc, 0)
        self.send.assert_called_once()
        self.assertIn("built-in auth check used", out.getvalue())

    def test_timeout_is_other_logs_only(self):
        rc, log = self.main_with(subprocess.TimeoutExpired(cmd="claude", timeout=90))
        self.assertEqual(rc, 0)
        self.send.assert_not_called()
        self.record.assert_called_once_with("claude", status="other", detail="timeout")
        self.assertIn("claude check: other", log)

    def test_rate_limit_crash_and_missing_binary_are_other_and_send_nothing(self):
        cases = [done(reply("Rate limit reached, try again later", is_error=True), rc=1),
                 done(stderr="Segmentation fault", rc=139),
                 FileNotFoundError("claude")]
        for answer in cases:
            with self.subTest(answer=answer):
                self.send.reset_mock()
                rc, _ = self.main_with(answer)
                self.assertEqual(rc, 0)
                self.send.assert_not_called()
                self.assertEqual(self.record.call_args.kwargs["status"], "other")

    def test_exit_code_is_zero_even_when_the_check_itself_crashes(self):
        rc, log = self.main_with(RuntimeError("boom"))
        self.assertEqual(rc, 0)
        self.send.assert_not_called()

    def test_claude_runs_on_haiku_without_tools_in_a_scratch_folder(self):
        self.main_with(done(reply("OK")))
        args, kwargs = self.run.call_args
        cmd = args[0]
        self.assertEqual(cmd[:2], ["claude", "-p"])
        self.assertEqual(cmd[cmd.index("--model") + 1], "haiku")
        self.assertEqual(cmd[cmd.index("--disallowedTools") + 1], cc.TOOLS_OFF)
        self.assertEqual(kwargs["timeout"], 90)
        self.assertNotEqual(Path(kwargs["cwd"]).resolve(), ROOT)  # never the app folder with .env


class RunShClaudeCheckTest(unittest.TestCase):
    """The check is one step in run.sh, right after health.py start and before collect.py."""

    def test_run_sh_runs_the_check_after_health_start_and_before_collect(self):
        import subprocess as sp
        text = (ROOT / "run.sh").read_text(encoding="utf-8")
        self.assertEqual(sp.run(["bash", "-n", str(ROOT / "run.sh")]).returncode, 0)
        step = "step --timeout 120 python3 scripts/claude_check.py || true"
        self.assertEqual(text.count(step), 1)
        self.assertLess(text.index("python3 health.py start"), text.index(step))
        self.assertLess(text.index(step), text.index("step --timeout 900 python3 collect.py"))


if __name__ == "__main__":
    unittest.main()
