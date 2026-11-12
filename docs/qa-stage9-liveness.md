# QA: stage 9 weekly liveness check of Интересно (28.10.2026) - GO after one blocker fix; deployed 12:36

Spec specs/stage9-liveness.md. Built by Haiku (liveness.py + table + run.sh Sunday step; digest weekly block +
"Вернуть" message; bot o:<id> reopen), QA Sonnet. 1197 tests. Backup /opt/job-digest-backups/2026-10-28-123601.
First QA: NO-GO - digest.run() Sunday block reassigned `closed` -> TypeError / "83 of 84", exit 3, false alert on
every Sunday. Fixed (closed_msg) with Sunday run() tests. Also fixed before deploy: JSON-LD validThrough only for
postings matching the row's title; conditional closing UPDATE (skipped if the row changed); report.py table (a)
excludes closed rows. Re-check GO: Sunday dry run and faked-send run exit 0, delivered == total; no board/DENY
request even through redirects or robots; phrase list matched 1 of 1820 stored descriptions (an already-closed job).
Live run 28.10 12:38: checked 20, open 11, closed 0 (dead ones were closed by hand that morning), unknown 9
(Bitmovin x3 403; 6 board-only links).
Left for Misha: (2) a footer/archive line with a closing phrase on a live page would close it (Вернуть button
mitigates); (5) no reopen outside the Sunday message, and only the first 10 closed get buttons.
