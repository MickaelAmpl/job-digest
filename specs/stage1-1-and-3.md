# Spec: stage 1.1 fixes + stage 3 (54 new companies)

Approved by Misha 08.10.2026 ("по рекомендации" on QA items A-D, window 15 days). Product = main session. Same rules as stage 1: test on scratch copies, never deploy, never touch .env.

## Part 1 - stage 1.1 fixes

1. **Window = 15 days.** WINDOW_DAYS = 15 in store.py, so a job first seen exactly 14 days ago (any time of day) is still in. Update texts that say "за 14 дней" to match the constant (digest витрина line, bot replies), do not hard-code the number in text.

2. **A. Failed feed must not hide a company's jobs.** Today company jobs stay in window only if last_seen = latest run. Change: record per company the last run where its feed returned >= 1 job (e.g. meta key or a small table company_feed(company_id, last_ok)). A company job stays in window while its company's feed is either listing it today, or did not answer / returned 0 jobs today (then keep the previous state). It leaves the window only when the company answered today without this job (that is when it gets closed) or by the normal non-company 15-day rule if it never was a company-feed job.

3. **B. Reopen.** A job with closed = 1 and verdict null that appears again in today's company feed (same key or url) gets closed = 0 and is in window again. Do not reopen jobs closed by enrich ("оригинал пишет, что закрыта") - only those closed by the feed rule. Store why it was closed (e.g. closed_by = 'feed' | 'enrich') so the two are distinguishable; existing closed rows: closed by feed if source='company' and data has no enrich "closed" flag, else enrich.

4. **D (Misha's Г). Same link = same job.** In store.remember(): before inserting a new row, if a row with the same normalized url (strip query string and trailing slash; ignore empty urls and generic urls like a careers home page - only treat as same when the path has more than one segment) already exists, reuse that row's id (return False) instead of inserting. Example from 08.10: "Product Owner*in Businessplattformen" from Wiener Stadtwerke and Wiener Netze GmbH, same url https://jobs.wienerstadtwerke.at/de/jobs/28185. Existing duplicate rows: leave them (no deletes).

5. **E (Misha's Д). Broken score.** A job whose score_json has no integer score must not be counted in the витрина numbers (digest line, bot "Осталось", /stats) - use one shared filter so counts equal what can be shown.

## Part 2 - stage 3: 54 new companies

Input: <scratch> (54 approved by Misha; fields slug, name, niche, niche_key, top, careers_url, ats, feed_url, feed_ok, ...).

1. Append them to companies.json in the existing record shape (id = slug, name, niche = niche_key, top, careers_url, ats, feed_url, feed_ok). Keep the 140 existing records untouched. Total must be 194.
2. For companies with ats in greenhouse / ashby / smartrecruiters / personio / recruitee / join (adapters exist in fetch_companies.py): make sure feed_url is in the form the adapter expects; run fetch_companies.py for just the new ones (add a way to limit, e.g. env FETCH_ONLY=slug1,slug2 or a --only flag) and confirm each returns Austrian jobs or explain why not.
3. HEROLD uses Teamtailor (RSS). Add a small Teamtailor adapter only if it is a few lines in the style of the existing RSS adapter; otherwise leave it with feed_ok false and say so.
4. The other ~31 "custom"/workday/successfactors/umantis/onlyfy companies: no new adapters now. They stay in companies.json with feed_ok false (they are still useful for the scoring niche and for future adapters). List them in the report with their ats.
5. Network calls to the companies' public job feeds are allowed (they are public APIs/feeds). Be polite: one request per company, timeouts.

## Acceptance (QA)
1. A non-company job first seen 14 days 23 hours ago is in window; 15 days 1 hour - out.
2. Company feed missing today (company absent from company_jobs.json) -> its open jobs stay in window and in the витрина; nothing closed.
3. Feed answers without a job -> that job closed (closed_by feed); next day the job reappears -> reopened, back in window; enrich-closed job never reopened.
4. A second job with the same normalized url from another company name -> no new row; first row's id reused.
5. Broken-score job not counted in витрина numbers.
6. companies.json has 194 records, the 140 old ones byte-identical in content; fetch_companies.py runs end to end on all companies without crashing.
7. All stage 1 checks still pass (rerun the stage 1 test scripts in scratchpad/stage1test/scripts, adjusting for 15 days).
