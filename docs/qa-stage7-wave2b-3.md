# QA: stage 7 wave 2 part 2 (company adapters) + wave 3 (filter), 26.10.2026

QA: Sonnet. Verdict: FAIL for deploy as is - 1 blocker, 2 major, 9 minor. Tests: 882 OK (3 skipped) on 3.10 and 3.12.
QA scripts and logs: session scratchpad qa/ (wd_run.py, wd_mock.py, sim_close.py, a1_empty_page.py, regress.py, integ_run.py).
Status: reported to Misha 26.10.2026 evening; nothing fixed yet.

## Blocker
B1. Workday under-reads boards over 40 jobs and reports capped=False -> prepare.py closes live jobs. ats_api.py:134,137,144.
Workday sends `total` only on offset 0, later pages send total 0; code overwrites total, stops after page 2 and computes
capped = 40 < 0 = False. Live Accenture: (0,43,20),(20,0,20) -> 39 jobs, capped False; board has 43. Mock board of 120
-> 40 jobs, uncapped. sim_close.py: day 2 closes 3 still-open jobs. No test covers total=0 on later pages.

## Major
M1. Oberbank: 15 of 16 titles and locations belong to the neighbouring job (companies.json title_pattern +
location_after, site_jobs.py:218-224). Card text comes before the "Jobdetails anzeigen" link; config reads text after it.
E.g. ...junior-financial-risk-manager-17806 stored as "Data Analyst - Risikomanager:in". Anchors carry
title="Jobdetails zu <title>" which title_clean already strips. Capped, so no closures, but wrong cards and title flips.
M2. A1: an empty page mid-paging counts as complete (site_jobs.py:286 `if not items or reached: return ..., True`).
Mock page 3 empty with totalItems 54 -> 12 of 54, capped False -> 42 jobs would close.

## Minor
1. Static `complete: true` without a count check: wild, statistik-austria, bearingpoint-austria (pages print their count).
2. Time: ats_api/ats_pages have no wall-clock limit (40 s per request; Workday worst 240 s, ats_pages 5 x (40+30) s);
   site_jobs checks 30 s only before a request (~60 s real); a1 10 requests. fetch_companies writes only at the end and
   run.sh kills at 1200 s. Normal run: 94 s for 51 companies. Suggest a global deadline (~900 s) then write.
3. wave3_preview has no "Уйдёт" section (removed on product request). Hand count over 2,202 rows: 0 stored keeps lost
   by title; 40 would be german_required from descriptions (never re-judged); 34 gain, 9 in the 15-day window.
4. Hard exclusions techniker, data scientist, initiativ, talent pool are not in spec/plan: "Junior Data Scientist",
   "Werkstudent Data Scientist", "Praktikum Produkttechniker" now excluded. 0 stored rows affected. Needs OK.
5. AI_EDGE noise / SENIOR gaps: keep for "Vice President AI", "Gruppenleiter KI", "Gruppenleitung Application &
   Automation Services", "Elektriker Gebäudeautomation", "Mitarbeiter Automatisierungstechnik",
   "Robotik- und Automatisierungsingenieur".
6. is_austrian: "Vienna, Virginia" True (pre-existing). New false positives "Horn, Germany", "Wolfsberg, Germany";
   "Baden-Baden", "Baden, Germany/Switzerland". Misses: Brunn am Gebirge, Premstätten, Schladming, Parndorf,
   Seiersberg, Gratkorn, Mondsee, Ternitz, Hennersdorf, Rum.
7. Health noise: Journi (Kula) always capped without full_feed:false; permanent bad_records notes (Accenture, DIMOCO,
   SMATRICS, Statistik, BearingPoint); Microsoft 429.
8. fetch_companies.get() (used by ats_api, ats_pages) follows redirects without DENY/SSRF check; ats_api checks no
   robots (pre-existing pattern; current endpoints clean).
9. DenizBank titles carry card text; JOIN with more than 5 jobs reads only 5 (capped).

## Checked OK
apply_wave3.py (dry run unchanged sha; --apply 9 rows, rule only, no demotion, idempotent); SSRF/DENY refusals for 14
hostile URLs; DNS of live run: 46 hosts, none on DENY; robots allow all endpoints; Workday/Coveo POSTs anonymous and
bounded; two live runs 13 min apart identical (565 Austrian jobs); DB copy integration: day 1 489 new, 0 closed,
queue 70; rerun 0 new 0 closed; filter ReDoS < 1 ms; group switches work.

## Not tested
Telegram rendering of new jobs, deploy.sh, server env, day-over-day stability of uncapped feeds, Microsoft, JOIN > 5,
Phenom paging > 100, Oracle with Austrian jobs, counts vs websites beyond 6 companies, Crawl-delay and terms of use.

---

# Re-check after the fixes round (26.10.2026 ~20:25) - verdict GO

Fixes: specs/stage7-wave2b-3-qa-fixes.md (Misha: "на все вопросы ответ да"). 930 tests OK on 3.10 and 3.12.
B1 fixed (live Accenture 43 read, uncapped; mocks 40/41/120/200 full, 201 capped; sim_close2 closes 0). M1 fixed
(Oberbank 16/16 slugs match, place from same card). M2 fixed (empty mid page -> capped). m1 fixed (count_pattern),
m2 fixed (60 s per company, 900 s global; not-started companies stay open), m5 fixed, m6 mostly ("Vienna, VA" still
True), m7 partly (Microsoft 429 in the 20:24 run), m8 fixed, m9 fixed. m3 (preview "Уйдёт") not assigned, not done.
Integration on a fresh prod copy with the 20:24 full fetch: 499 new rows, 4 closed (SumUp Bolzano - correct; DeepL and
foodora - gone from board; UniCredit Spittal - see N4), 1 reopened, queue 72.

## New findings (all minor, for Misha)
N1. Foreign-country rule drops multi-location strings where the Austrian place has no country: "Vienna, Munich,
    Germany", "Wien; Berlin, DE", "Graz / Zürich, Schweiz" -> False (were True). Today: 0 jobs lost. Fix: check per
    ';' or '/' segment.
N2. site_jobs._from_json re-reads total on every page (latent Workday trap); A1 unaffected (54 on every page).
N3. 'ingenieur' also drops "Wirtschaftsingenieur..." titles (0 stored rows). Fix: lookbehind for 'wirtschafts'.
N4. Pre-existing: UniCredit Avature RSS returns exactly 20 = RSS_CAP but rss() marks only successfactors as capped;
    if it is a slice, its closures are false. Unverified.
N5. Oberbank location window 3000 chars (longest card today 2030); longer card -> job silently dropped (capped).
N6. SmartRecruiters can still take ~400 s on a mass stall; started at 900 s it can pass the 1200 s kill.

Deployed 26.10.2026 20:30 (backup /opt/job-digest-backups/2026-10-26-203022).

---

# Round 2 (N1-N6 + UniCredit + cross-source duplicates), QA verdict GO, deployed 26.10.2026 22:23
(backup /opt/job-digest-backups/2026-10-26-222259; 980 tests). All N1-N6 fixed; UniCredit 25/25 uncapped, 20 known
+ 5 new on a DB copy; 5 evening duplicates marked by hand (backup dupfix-2026-10-26.db); mark_if_duplicate over the
whole 2,707-row snapshot: 10 pairs, all true duplicates; 3.8 ms mean per call.
New minors for Misha (not fixed): (1) employer rule can merge group entities with identical titles (VIG vs Donau,
"Austrian" vs "Austrian Power Grid", Siemens vs Siemens Energy) - 0 cases in data, shown in "Закрыты и дубли";
(2) a repost is suppressed by an old sent row regardless of age (suggest: employer rule only within ~21 days);
(3) is_austrian residuals: "Wien, Italien/Poland" False, "Vienna, Illinois/Georgia" True - 0 prod rows;
(4) 20 known UniCredit rows keep code-style titles; (5) pre-existing 28 stale keys (missed merges only).
Ops note: a stale remote `while pgrep -f 'run.sh collect'` loop (matching itself) blocked deploy.sh's guard; killed.
Never start remote wait loops whose pattern matches their own command line.

---

# Round 3 (27.10.2026): stage-7 round-2 minors S7-1..S7-5 fixed, QA GO, deployed 13:51
S7-1 employer rule needs evidence (shared company host or descriptor-only extra words incl. versicherung/insurance):
VIG/Donau, Austrian/APG, Siemens/Siemens Energy, Mondi/Mondi Gronau stay apart; 7 of 10 snapshot pairs still merge
(CHECK24 x3, woom, Wüstenrot, NAVAX, Wiener Städtische); lost: Donau (by design), Statistik, AVILOO (true duplicates,
may show twice). S7-2 30-day/window gate on the company-name rule only; QA found it first also gated the link rule
(an applied job older than 30 days could come back as a new card) - fixed before deploy: link rule never age-gated,
judged rows always recent. S7-3 is_austrian: big city + foreign country = Austrian; US names anywhere, US codes only
as last token of a comma part; MA/ME/IN/OR/... not codes ("Wien, MA 48" Austrian). S7-4/S7-5 on the server:
refresh_titles UniCredit 19 changed (0 open rows with "AT - ..." titles left), rekey_stale 37 rekeyed, 10 conflicts
left (existing duplicate pairs from different sources); second runs no-op; integrity ok.
