# Stage 1 spec: 14-day window ("витрина")

Approved by Misha 08.10.2026. Product = main session. Read store.py, prepare.py, score.py, digest.py, bot.py, collect.py, enrich.py before changing anything.

## Why
Today only jobs first seen in the last 3 days reach the bot, so a quiet day = an empty digest, and the 728 jobs seen on 07.10 (67 rule-approved) were stored as a baseline without rule or score and will never be shown. Misha wants every relevant vacancy of the last 14 days available until he marks it, including older open vacancies from company feeds.

## Definitions
- WINDOW_DAYS = 14, one constant in store.py, used everywhere instead of BACKLOG_DAYS.
- A job is **in window** when: verdict is null AND closed = 0 AND (first_seen >= now - 14 days OR (source = 'company' AND last_seen is from the latest morning run)). So company jobs stay as long as the company feed still lists them.
- **Витрина** = in-window jobs that were already delivered as a card (sent_at not null, listed = 0) and still have no verdict.

## Changes

1. store.py
   - New columns: last_seen text (latest run that saw the job), vitrina_at text (last time the job was re-sent from the витрина).
   - DB path overridable for tests: DB = Path(os.environ.get("JOBDIGEST_DB") or default).
   - WINDOW_DAYS = 14 and a helper that returns the SQL condition + params for "in window", so prepare/digest/bot share one definition.

2. prepare.py
   - For every job in today's prefiltered.json: set last_seen = now (new and existing rows).
   - For existing rows whose rule is null: write rule (and data) from today's prefiltered.json - this backfills the 07.10 baseline.
   - Company closure: a company-source job not in today's company_jobs.json, while its company has at least one job in today's company_jobs.json, gets closed = 1. If the company has no jobs today (feed failed or empty), change nothing.
     Only for rows with verdict null. Closed jobs never re-enter the queue or the витрина.
   - Scoring queue = same rules as now, but window = in-window condition instead of BACKLOG_DAYS (unscored rule='keep' jobs + the existing title-only retry logic).
   - Print counts: seen, new, rule backfilled, closed by feed, queue size.

3. enrich.py: MAX_JOBS overridable by env ENRICH_MAX_JOBS (default stays 40). Nothing else.

4. score.py: no logic change expected; check it still works with a queue of ~70.

5. digest.py
   - pending(): replace BACKLOG_DAYS with the in-window condition. Cards and the rejected list work as now.
   - After all cards and the rejected list, if the витрина (excluding jobs delivered in this run) is not empty, send one message:
     "В витрине ещё N вакансий без отметки за 14 дней" with one inline button "Показать ещё 5" (callback_data "w:more").
     If there were no new jobs today, the header still says so and the витрина message is still sent.
   - Dry run prints it too.
   - Remove the now-unused BACKLOG_DAYS.

6. bot.py
   - Callback "w:more" and command /vitrina: take up to 5 витрина jobs ordered by vitrina_at (nulls first), then score desc; send each as a full card (digest.job_message with the same buttons; link = open_url or url via store.usable_url). After each accepted send: vitrina_at = now, msg = text. Then send "Осталось в витрине: N" with the "Показать ещё 5" button if N > 0, otherwise "Витрина разобрана".
   - answerCallbackQuery first (fast), then send.
   - A job marked from any of its messages leaves the витрина (verdict not null) - no extra code expected, check it.
   - /stats: add a line "В витрине без отметки: N".

7. collect.py
   - Remove ("business analyst", "Wien") from ADZUNA_QUERIES (Misha removed Business Analyst on 06.10).
   - Adzuna paging: if a page returns 50 results, fetch the next page, max 5 pages per query; keep 2.6 s between calls (25/min limit). Daily call `collect.py 2` stays; the product runs `collect.py 14` once for the backfill.

## Acceptance criteria (QA checks each)
1. On a copy of the server DB + today's prefiltered.json/company_jobs.json, prepare.py backfills rule for the 07.10 rows: queue contains the ~67 never-scored rule-approved jobs; nothing already scored or marked is re-queued.
2. A second prepare.py run the same day queues nothing new and changes no verdict/sent_at.
3. Company closure: removing one job of a company from company_jobs.json marks only that job closed; removing all jobs of a company marks none.
4. digest.py --dry-run after scoring: new cards = scored, unsent, >= 50, in window; jobs already sent are not re-sent; витрина line counts only sent-unmarked-open in-window jobs.
5. Jobs with first_seen older than 14 days (non-company) drop out of queue, digest and витрина; company jobs still in the feed stay.
6. bot "w:more" logic (test the selection/update function without Telegram): returns 5, marks vitrina_at, next call returns the next 5, marked jobs excluded.
7. All existing behaviour unchanged: delivered only on Telegram success, promote logic, closed list, reason buttons, notes.
8. py_compile passes on all files.
