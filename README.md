# Job Digest

A Telegram bot that does my morning job search in Austria. Every day at 07:30 it sends me the new openings that fit my profile, each with a score and a short "why", and buttons that teach it what I want. On request it builds a one-page CV for a specific ad, from wording I have already approved.

I am a product person, not a software developer. I wrote the specs, made every product decision and tested the result; the code was written and reviewed by AI agents in Claude Code. The `specs/` and `docs/` folders are the real build trail: what was asked, what was decided, and what QA found at each stage.

> This is the public copy. The real bot reads my own fact bank, CV and job database, which stay private. Here the candidate is fictional (Alex Morgan), and personal data, applications and server details are removed.

## The problem

Job ads in Vienna are spread over LinkedIn, karriere.at, StepStone, DER STANDARD, a university job board and hundreds of company career pages. Reading all of it every morning takes an hour, most of it is noise (senior roles, fluent German, the wrong field), and the good ones are easy to miss. Then every application needs a CV tuned to the ad, and that is where people start inventing things.

## What it does

1. **Collects** new ads every morning at 06:30: job-alert emails (read-only IMAP), the Adzuna API and the public career feeds of about 200 Austrian companies (Greenhouse, Lever, Workable, Personio, SmartRecruiters and others).
2. **Filters by rules** first: Austria only, entry level, no "fluent German" requirement, target roles. Free and instant.
3. **Reads the full ad** when the alert had only a title.
4. **Scores** each remaining ad with Claude against a written profile (`scoring/profile.md`): a score from 0 to 100 and a one-line reason.
5. **Sends the digest** at 07:30 in Telegram: one card per job, best first, with buttons: *Interesting*, *Applied*, *Not for me* (with a reason). The choices feed back into the next day's scoring.
6. **Builds a tailored CV** on a button press: a one-page PDF for that ad, plus a sheet of answers for the application form (start date, languages, salary line from the ad). A cover letter on request.
7. **Tracks applications**: `/applied`, `/report` with the funnel, a weekly check that "Interesting" jobs are still open, manual entries for applications sent outside the bot.
8. **Watches itself**: a failed source or step is shown in the next digest's header; if Claude's login on the server expires, an alert goes out.

> [SCREENSHOT: morning digest card in Telegram - score, reason, buttons]
> [SCREENSHOT: tailored CV PDF sent by the bot]

## How it keeps the CV honest

The model never writes CV sentences. It can only **choose**:

- **Approved wording library** (`cv/library.json`): every bullet and summary is text I approved. The model picks bullet ids and their order; code rebuilds the CV from the library and rejects anything else.
- **Fact bank** (`cv/facts.md`): the only source of truth, with dated corrections ("the CTO led development, not me").
- **Never-claim list** (`cv/never_claim.json`): tools and methods I have never used. Checked in code, including spellings like "chat-gpt" or "Power-BI".
- **Numbers check** (`cv/allowed_numbers.json`): a number that is not in the base CV or the fact bank is refused.
- **Gaps are reported, not hidden**: requirements the facts cannot support come back to me as a list in Telegram, and never reach the CV.

Text inside a job ad is treated as data, never as instructions (QA tested this with hostile ads).

## How it was built

Planned on 5 October 2026; the first version (collect, filter, score, daily digest with buttons) ran on 10 October. Since then it has grown in stages, each with a written spec, my approval, a build, an independent QA pass and a deploy script with backup and rollback.

| Stage | What | Spec | QA |
|---|---|---|---|
| 1 | 14-day window of open jobs, fixes after the first week | `specs/stage1-vitrina.md`, `specs/stage1-1-and-3.md` | `docs/qa-full-2026-10-09.md`, `docs/qa-round2-2026-10-09.md` |
| 4 | Application tracking in the bot and `/report` | `specs/stage4-tracking.md` | |
| 5 | Tailored CV and form answer sheet | `specs/stage5-cv-tailoring.md`, `specs/stage5-plan.md` | |
| 6 | Cover letter from a paragraph library | `specs/stage6-cover-letter.md`, `specs/stage6-plan.md` | `docs/qa-stage6-2026-10-10.md`, `docs/letters-pilot-2026-10-10.md` |
| 7 | Source and scoring repair after an audit | `specs/stage7-plan.md`, `specs/stage7-wave2b-wave3-tasks.md` | `docs/audit-2026-10-16.md`, `docs/qa-stage7-*.md` |
| 8 | Manual applications | `specs/stage8-manual-applications.md` | `docs/qa-stage8-manual-applications.md` |
| 9 | Weekly "is it still open" check | `specs/stage9-liveness.md` | `docs/qa-stage9-liveness.md` |
| 10-12 | Fixes, Claude login alert, company feeds to about 200, sales roles | `specs/stage10-*.md`, `specs/stage11-*.md`, `specs/stage12-*.md`, `specs/sales-roles-2026-11-04.md` | `docs/qa-sales-2026-11-04.md` |

Some specs are in Russian, my working language with the agents; the developer specs are in English.

**Who did what.** I set the goal, wrote and approved every spec, decided every trade-off and checked the results in Telegram. A main Claude Code session acted as the engineering lead: it split work into tasks with clear file ownership and handed them to developer agents (`.claude/agents/`), and a separate QA agent re-tested every change before deploy. Research on ATS systems and cover letters (`research/`) was done the same way, with sources.

## Stack

- Python 3, standard library only on the server
- SQLite (`store.py`)
- Telegram Bot API (long polling, systemd service)
- Claude, called through the Claude Code CLI on the server (`claude -p`), for scoring, CV choice and letters
- Headless Chrome for the CV PDF (`cv/source/build.py`)
- A small Linux server: cron at 06:30 and 07:30, a daily database backup after the digest
- `scripts/deploy.sh`: tests on the server first, backup, deploy, checks, printed rollback command

## Run it

```bash
cp .env.example .env            # Telegram bot token and chat id, Gmail app password, Adzuna key
python3 -m pytest -q            # the whole suite runs offline with fakes
./run.sh collect                # collect, filter and score
./run.sh send                   # send the digest
python3 bot.py                  # button listener
```

`scoring/profile.md` and `cv/` describe the candidate; replace them with your own.

## Status

In daily use since October 2026.
