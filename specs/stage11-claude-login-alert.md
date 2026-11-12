# Stage 11: alert when Claude's login on the server expires (05.11.2026, Misha "Да, сделай")

Incident 05.11: the Claude CLI OAuth session on vps expired ("Failed to authenticate: OAuth session expired and could
not be refreshed"). score.py scored 0 of 12 (fallback 55 cards "Без оценки Claude"), enrich.py failed 8 searches, cards
had no links and said "Описание ещё не искал (не хватило времени утром)" - wrong reason. Misha only found out from odd
cards. Re-login is a manual step only Misha can do (ssh -t vps claude, then /login).
Council: Haiku developers, Sonnet QA, product deploys outside 06:30-07:30. Tests fail before the fix, suite green.

## Dev D - scripts/claude_check.py (new), run.sh (one step), tests/test_claude_check.py (new)
- claude_check.py: runs `claude -p "Reply with OK" --model haiku --output-format json` with a 90 s timeout (same
  subprocess style and environment as score.py's ask_claude - reuse its tool-disabling flags if any). Classifies:
  ok | auth (stdout/stderr/result mentions any of: "Failed to authenticate", "OAuth", "/login", "not logged in",
  "Invalid API key", "authentication") | other (timeout, rate limit, crash). Prints one log line. Exit 0 always.
  On auth: sends ONE Telegram line via scripts/alert.py (call it as a subprocess, or import its send function - it
  imports nothing from the app):
  "Claude на сервере разлогинился: сегодня бот не сможет оценить вакансии и найти описания. Войди заново:
  открой терминал и выполни ssh -t vps claude, затем /login (ссылку для входа скопирую тебе в чат, если не
  копируется)." Also records health flag (health.record("claude", status="auth")) if health.py offers record;
  otherwise skip that part and say so.
  On other: only the log line (no Telegram - rate limits recover by themselves; score.py retries next morning).
- run.sh collect: run `step --timeout 120 python3 scripts/claude_check.py || true` right after `health.py start`
  (before collect.py), so the alert arrives at ~06:31, long before the 07:30 digest. Nothing else in run.sh changes.
- Tests: ok / auth (each marker) / timeout / other error with a fake subprocess; alert sent only for auth; exit code 0.

## Dev C - enrich.py, digest.py (card line), tests/test_enrich_auth_reason.py (new)
- enrich.py: when a Claude call fails with an auth error (same markers), mark the job data
  data.enrich_error = "auth" (keep "failed (not counted as tries)" as today) and clear it on the next successful
  search. Other failures leave it unset.
- digest.py job_message: for a title-only job whose data.enrich_error == "auth", the line is
  "<i>Описание не искал: Claude на сервере разлогинился - поищу после повторного входа.</i>" instead of
  "Описание ещё не искал (не хватило времени утром)...". All other cases unchanged.
- Tests for both.
