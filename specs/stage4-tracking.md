# Stage 4 spec: application tracking in the bot + /report

Approved by Misha 08.10.2026 (variant 2: "бот + отчёт файлом"; status chain agreed). Product = main session. Read bot.py, digest.py, store.py first. Same council rules.

## Why
Misha wants to see what he marked interesting, where he already sent his CV and what happened next, and keep his own records - without setting up Notion or Sheets. Data stays in data/state.db (source of truth; the future CV-tailoring tool reads it).

## Status chain (agreed with Misha)
Интересно -> Отправил резюме -> Ответили -> Интервью -> Оффер / Отказ / Нет ответа

Stored separately from the verdict: verdict stays the button meaning (interested / not_interested / cant_fit / bad_terms); the application status is a new column.

## Changes

1. store.py: new columns `status` text (applied / replied / interview / offer / rejected / no_answer, null = not applied yet), `status_at` text, `applied_at` text; new table `status_log(job_id, status, at)` - every change appended (history for the report).

2. bot.py - card buttons
   - When a card is collapsed as "Отмечено: Интересно", its buttons become: [Открыть объявление] [Отправил резюме] [Изменить].
   - "Отправил резюме" -> status applied, applied_at now; the collapsed line shows "Отправил резюме · <date>" and buttons [Открыть] [Ответили] [Отказ] [Нет ответа] [Изменить].
   - "Ответили" -> buttons [Интервью] [Отказ] [Изменить статус]; "Интервью" -> [Оффер] [Отказ] [Изменить статус]. Final states (offer / rejected / no_answer) show only [Открыть] [Изменить статус].
   - "Изменить статус" shows all 6 statuses + "Сбросить" (back to no status). "Изменить" (existing) keeps restoring the full card and verdict buttons.
   - Callback data short: `s:<status>:<id>` (fits Telegram's 64-byte limit with 10-char ids).
   - Every press: update status, status_at, append status_log, edit the message, answerCallbackQuery first.
   - A status can be set only on jobs with verdict interested (if verdict changes away from interested, keep the status history but hide status buttons).

3. bot.py - commands (also add them to the bot menu via setMyCommands on start)
   - /interesting - "Интересно, резюме ещё не отправлено": list (title · company · score, linked to open_url/url), oldest first, max 30 lines, then "ещё N".
   - /applied - jobs with a status, grouped by status, each line with date sent and days since; max 40 lines.
   - /stats - funnel: прислано карточек -> Интересно -> Отправил -> Ответили -> Интервью -> Оффер; plus Отказ, Нет ответа; plus verdict counts and top reasons for Не подхожу / Не подходит; витрина count (existing line).
   - /report - sends an HTML file (sendDocument, filename report-YYYY-MM-DD.html) - see 5.

4. Weekly summary: digest.py on Sunday (Europe/Vienna) appends one extra message after the витрина line: last 7 days - cards received, marked, interesting, CVs sent, replies; and "Отправил резюме больше 14 дней назад без ответа: N - отметь «Нет ответа» или «Ответили»" with /applied hint. No new cron line.

5. report.py (new, stdlib only) -> self-contained HTML (inline CSS, no external scripts/fonts; must open offline on phone and Mac, light and dark via prefers-color-scheme):
   - Header: date, totals.
   - Funnel as horizontal bars (pure CSS widths).
   - Tables with sortable columns (small inline JS, no libraries) and a text filter box: (a) Интересно, не отправлено; (b) Отправлено - status, date sent, days since, last change; (c) All marked jobs - verdict, reasons, score, source, company, date.
   - Reasons breakdown for Не подхожу / Не подходит (counts) - "чего чаще всего не хватает".
   - Sources: per source - cards sent, interesting, applied.
   - Companies that replied (status replied or later).
   - Links open the posting (open_url, else url). Escape all text (html.escape).
   - Russian UI text, plain hyphens only (no long dashes).
   - bot /report calls report.py's build function and sends the file; also runnable as `python3 report.py out/report.html` for tests.

## Acceptance (QA)
1. Status flow via fake api: Интересно -> Отправил -> Ответили -> Интервью -> Оффер; each step edits the message with the right buttons, writes status/status_at/applied_at and a status_log row.
2. "Изменить статус" and "Сбросить" work; "Изменить" still restores the full card; non-interested jobs never get status buttons.
3. /interesting, /applied, /stats return correct counts on a DB copy with synthetic statuses; Telegram 4096-char limit respected.
4. report.py on a DB copy produces valid HTML (parse with html.parser), no external URLs except posting links, all user text escaped (test with a title containing <script>), sorting/filter JS present, works with 0 marked jobs.
5. Sunday summary appears only on Sunday (inject the date), counts correct.
6. Existing behaviour unchanged: verdict buttons, reasons, витрина, delivery, digest dry-run identical on a non-Sunday except nothing.
