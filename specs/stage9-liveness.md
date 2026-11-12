# Этап 9: еженедельная проверка живости вакансий в «Интересно» (28.10.2026)

Миша 28.10: «Да, сделай» (на предложение: еженедельная проверка вакансий из «Интересно» + бот узнаёт фразы о
закрытых вакансиях). Повод: 28.10 вручную нашли 6 мёртвых вакансий в «Интересно» (Sportradar «Sorry, this job has
expired», UNIQA «Diese Stelle wurde leider bereits besetzt», TechTalk, TOWA 404, CHECK24 «nicht mehr verfügbar»),
бот считал их живыми. Команда: Opus планирует и проверяет, Haiku пишет, Sonnet QA. $0 сверху.

## Что увидит Миша
- Каждое воскресенье утром бот проверяет страницы вакансий из «Интересно» (отметка есть, резюме ещё не отправлено).
- Закрытые убирает из /interesting. В воскресных итогах: «Проверил «Интересно»: N, закрылись K, не смог проверить M»,
  ниже списки со ссылками. Отдельным сообщением закрытые с кнопкой «Вернуть» (если бот ошибся).
- Вакансию, которую Миша вернул, бот больше сам не закрывает.
- LinkedIn, jobs.at, StepStone, karriere.at, Adzuna, derStandard бот не открывает - такие вакансии в «проверь сам».

# Developer spec (English)

## Dev E - liveness.py (new), store.py (table), run.sh (one step), tests/test_liveness.py (new)
- store.py SCHEMA: `liveness_checks(id integer primary key autoincrement, job_id text not null, checked_at text not null,
  result text not null /* open|closed|unknown|skipped */, reason text, url text)`; index on (checked_at).
- liveness.py `run(con, today=None, fetch=None) -> dict` (fetch injectable for tests) and `main()`:
  - Rows: verdict='interested' AND status IS NULL AND coalesce(closed,0)=0.
  - URL: open_url if usable, else url. A URL whose registrable domain is in fetch_posting.DENY or enrich.JOB_BOARDS
    minus fetch_posting.ATS (job boards; ATS hosts are fine) -> result 'unknown', reason 'доска объявлений - проверь
    сам'. No URL -> 'unknown', 'нет ссылки'. Never request a DENY host, not even through a redirect.
  - Rows Misha reopened (data.liveness.reopened_at set) -> 'skipped' (never auto-closed again).
  - Fetch with fetch_posting's guards (public address check on every hop, DENY on every hop, robots.txt, size cap,
    per-host gap) - reuse fetch_posting internals; the allow rule here is "public, not DENY, not a job board" because
    these URLs were stored by enrich or by product (not the stricter enrich.trusted_url). 20 s per page, at most 4
    workers, whole run <= 600 s (rows not reached -> 'unknown', 'не успел').
  - Closed (only these): HTTP 404 or 410 for the stored URL (after redirects); JSON-LD validThrough before today;
    a phrase in the page text from LIVENESS_CLOSED = fetch_posting.CLOSED plus: "this job has expired",
    "sorry, this job has expired", "stelle wurde (leider )?(bereits )?(besetzt|vergeben)", "nicht mehr (verfügbar|aktiv|
    ausgeschrieben)", "es konnte leider nichts gefunden werden", "job nicht gefunden", "this position (is|has been)
    (closed|filled)", "no longer (open|active)". Everything else (200 without a phrase) -> 'open'. HTTP 401/403/429/5xx,
    timeouts, refusals -> 'unknown' with a short Russian reason ('сайт не пустил (403)', 'не открылся').
  - A closed row: closed=1, closed_by='liveness', promote=0, data.liveness={at, reason, url}. One commit per row.
  - Every row checked gets a liveness_checks row. main() prints a summary line "liveness: checked N, open A, closed K,
    unknown U, skipped S" and exits 0 even if some pages fail.
- run.sh collect: on Sundays only (`[ "$(date +%u)" = 7 ]`), after score.py and before `health.py finish`:
  `step --timeout 660 python3 liveness.py || true`. Nothing else in run.sh changes. Update tests/test_run.py if it
  pins the step list.
- Tests (no network, fake fetch): each closed rule; phrase match from the real snippets above; 403 and timeout ->
  unknown; board and empty URL -> unknown without a fetch call; reopened row skipped; a DENY host in a redirect chain
  is refused; closed rows disappear from bot.interesting_list; time budget; liveness_checks rows written.

## Dev C - digest.py (weekly summary part), tests/test_weekly_liveness.py (new)
- weekly_summary (Sunday): if liveness_checks has rows from the last 24 h, add a block:
  "Проверил «Интересно»: N. Закрылись: K. Не смог проверить: U." then up to 10 lines "закрылась: <a>Title</a> · Company
  (reason)" and up to 10 lines "проверь сам: <a>Title</a> · Company (reason)". HTML-escaped, capped like the rest.
- After the weekly summary message, if K > 0: one more message "Закрылись из «Интересно»" with one button per closed job
  (max 10): text "Вернуть: <Company · Title>" capped to fit Telegram, callback_data "o:<job_id>". Sent once per Sunday
  together with the summary (same weekly_sent guard). Dry run prints it.
- Tests: summary block numbers and lines; no block without checks; buttons and callback data <= 64 bytes; sent once.

## Dev B - bot.py (callback), tests/test_bot_liveness.py (new)
- Callback "o:<job_id>": if the row has closed_by='liveness' -> closed=0, closed_by=NULL, data.liveness.reopened_at=now
  (keep the rest of data.liveness), commit, answer "Вернул в «Интересно»"; edit the button message to drop that button
  (or mark it "[вернул]"). Any other row -> answer "Эту вакансию закрыл не я - не трогаю". Handled before the generic
  job-id path like m: callbacks.
- Tests: reopen works and the row is back in interesting_list; second press harmless; non-liveness row refused.
