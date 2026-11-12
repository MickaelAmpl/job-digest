# Stage 7 wave 2b + 3: QA fixes (26.10.2026, approved by Misha: "на все вопросы ответ да")

Source: docs/qa-stage7-wave2b-3.md (read it first). Same rules as specs/stage7-wave2b-wave3-tasks.md ("Rules for every
task"). Five developers in parallel, strict file ownership below. Every fix gets a test that fails before the fix.
Run `python3 -m unittest discover -s tests -t .` before reporting (882 OK now; other devs add tests in parallel).
Live network only to re-verify your own companies, same politeness rules.

## Dev A - prefilter.py, tests/test_prefilter_wave3.py
- SENIOR also drops "vice president" and German group leads: gruppenleit (Gruppenleiter, Gruppenleiterin, Gruppenleitung).
- Hard EXCLUDED also: elektriker, automatisierungstechnik, ingenieur (covers "Automatisierungsingenieur").
- Tests: "Vice President AI", "Gruppenleiter KI", "Gruppenleitung Application & Automation Services" -> senior;
  "Elektriker Gebäudeautomation", "Mitarbeiter Automatisierungstechnik", "Robotik- und Automatisierungsingenieur" ->
  excluded_field; "Marketing Operations Manager (AI & Automation)" and "AI Associate" still keep.
- Misha approved keeping techniker / data scientist / open-application / talent-pool exclusions and ALL role groups on.

## Dev B1 - ats_api.py, tests/test_ats_api.py, tests/fixtures/ats_api/
- QA B1 (blocker): Workday `total` is only valid on offset 0; later pages send 0. Keep the first page's total; stop when
  offset >= total or a page is empty; capped=True whenever fewer than total were read (page limit, deadline, empty page
  before total). Test: mock board of 120 (total only on page 1) -> either all 120 within the page limit or capped=True;
  live Accenture board (43) must read 43 or be capped. Raise the page limit so a board of up to 200 Austrian-filtered
  jobs is read in full.
- QA minor 2: a wall-clock deadline per company (60 s) across all requests of one adapter call; on deadline stop and
  return capped=True (never an uncapped partial list). Per-request timeout = min(40, remaining).
- QA minor 8: your POST helper must not follow redirects (raise on 3xx).

## Dev B2 - ats_pages.py, tests/test_ats_pages.py, tests/fixtures/ats_pages/
- QA minor 2: wall-clock deadline per company (60 s) across all requests incl. robots reads; on deadline stop paging and
  return capped=True; per-request timeout = min(40, remaining). Test with a fake slow fetch.
- Check your paging the same way QA found for Workday: is any "total" only valid on the first page? Is an empty page
  before the stated total treated as complete? Fix and test if so.

## Dev C - site_jobs.py, tests/test_site_jobs.py, tests/fixtures/site_jobs/, companies.json (only dev allowed to edit it)
- QA M1 (Oberbank): titles and places belong to the neighbouring card. Take the title from the link's own
  title="Jobdetails zu ..." attribute (title_clean strips the prefix); drop title_pattern/location_after for Oberbank
  unless the place can be read from the SAME card; else default_location only if all Oberbank jobs are Austrian (they
  are an Austrian bank; check the page). Verify live: each URL's slug matches its title.
- QA M2 (A1): an empty page before the reached total is NOT complete: capped=True. Complete only when the total was
  reached (or the source has no total and the last page was shorter than the page size - document which).
- QA minor 1: wild, statistik-austria, bearingpoint-austria: replace static complete:true by a count check - a regex
  on the list page for the stated number ("15 offene Stellen", "18 Stellen verfügbar", "See 1 Job openings");
  complete only when found == stated, else capped. If the count is missing on the page: capped.
- QA minor 2: enforce the per-company time limit during reads too (per-request timeout = min(default, remaining)).
- QA minor 7: Microsoft answered 429 twice: on 429 wait (Retry-After if given, max 20 s, else 10 s) and retry once.
- QA minor 9: DenizBank titles carry card text ("... Vollzeit Wien, Zentrale Einzelheiten"): clean to the job title.
- QA minor 7: companies.json: set "full_feed": false for journi (Kula is always capped) - and for any other company
  whose feed is capped by design every day, so the digest does not repeat it every morning. Keep companies.json
  formatting: json.dumps(..., ensure_ascii=False, indent=1) + "\n".

## Dev D - fetch_companies.py, health.py, tests/test_wave2_integration.py
- QA minor 2: global deadline in fetch_companies.main(): stop starting new companies after FETCH_DEADLINE = 900 s from
  start; each company not started is recorded in `failed` as "<name>: не успел (лимит времени)" (so prepare.py never
  treats it as an empty answer and the digest shows it), then the files are written as now. Test with a fake clock.
- QA minor 8: get() must not follow a redirect to a fetch_posting.DENY host or a non-public address: use a redirect
  handler that checks each hop with fetch_posting's host guards (find the right helper: DENY via
  enrich.registrable_domain, _public_host or similar). Test with a fake redirect.
- QA minor 6 (is_austrian):
  - A location that names a foreign country (Germany, Deutschland, DE as its own token, Switzerland, Schweiz, CH,
    Czech, Slovakia, Hungary, Italy, USA, Virginia, United States, UK...) and does not name Austria/Österreich is NOT
    Austrian, even if a town matches ("Horn, Germany", "Wolfsberg, Germany", "Vienna, Virginia" -> False;
    "Wien, Austria" and "Horn, Niederösterreich" -> True).
  - TOWN_BADEN: also not "Baden-Baden", "Baden, Germany", "Baden, Switzerland", "Baden (AG)".
  - Add towns: Brunn am Gebirge, Premstätten, Schladming, Parndorf, Seiersberg, Gratkorn, Mondsee, Ternitz,
    Hennersdorf. "Rum" (Tirol) only as "Rum bei Innsbruck" or "Rum, Tirol" (plain "rum" is a word).
  - Run the new is_austrian over all company jobs in the prod DB copy
    (<scratch>
    read-only, field `location` in jobs.data) and report every row whose result changed.
- QA minor 7 (health.py): the "Пропущены битые записи" note only for a company with at least 2 bad records AND at least
  10% of its feed; single stray records stay silent. Test.

# Round 2: QA re-check minors N1-N6 (26.10.2026 ~21:00, Misha: "исправляй все шесть, начни с UniCredit")
Source: docs/qa-stage7-wave2b-3.md, section "Re-check after the fixes round". Same rules and ownership as above.

## Dev D - fetch_companies.py, tests/test_wave2_integration.py
1. FIRST - N4 UniCredit (avature RSS, companies.json id unicredit-bank-austria, feed
   https://careers.unicredit.eu/en_GB/jobsuche/SearchJobs/feed/?search=UCBA). The feed returns exactly 20 items.
   (a) Prove whether it is a slice: compare with the number of UCBA jobs on the careers site search
   (https://careers.unicredit.eu/en_GB/jobsuche/SearchJobs/?search=UCBA or similar; robots.txt first).
   (b) If it is a slice, find a public way to read all (Avature lists often take paging params such as jobOffset /
   jobRecordsPerPage / listFilterMode - verify, do not trust) and read every page within the 60 s per-company deadline;
   any RSS that returns exactly a known page size and cannot be paged further is capped=True (generalise the
   successfactors rule in rss() to avature).
   (c) Report the 5 UniCredit jobs closed by the feed rule (product gives the titles below): for each, is it still on
   the careers site today? (Product will reopen the false ones.)
       AT - EXT - WP - Praktikum - UCBA - Corporates - Markets - DE
       AT - EXT - WP - Praktikum - UCBA - Corporates - Commercial S...
       AT - EXT - WP - Praktikum - UCBA - Corporates - EXIN - DE - ...
       AT - EXT - UCBA - Retail Network - BestStart - Spittal an de...
       AT - EXT - UCBA - Retail Network - BestStart - St. Pölten
2. N1: the foreign-country rule must work per location segment: split on ';', '/', '|', ' oder ', ' or ', newline; a
   location is Austrian if ANY segment is Austrian by the existing rules (a segment that names only a foreign country
   or a town plus a foreign country is foreign). "Vienna, Munich, Germany" -> True (Vienna has no country of its own:
   when a comma list ends with a foreign country, only the towns that are not Austrian belong to it - keep it simple:
   a segment containing an Austrian town AND a foreign country, with no Austria mention, is True only for the
   ambiguous names Wien/Vienna/Graz/Linz/Salzburg/Innsbruck when the country is Germany/DE - document the rule you
   choose). Must stay: "Horn, Germany" False, "Wolfsberg, Germany" False, "Vienna, Virginia" False, "Vienna, VA"
   False, "Bolzano, South Tyrol, Italy" False. Must become True: "Wien; Berlin, DE", "Wien oder München
   (Deutschland)", "Graz / Zürich, Schweiz". Report every prod-DB company row whose result changes (as last time).
3. N6: SmartRecruiters paging gets the same 60 s per-company deadline (stop -> capped=True), per-request timeout
   min(40, remaining).

## Dev C - site_jobs.py, tests/test_site_jobs.py, tests/fixtures/site_jobs/, companies.json
1. N2: _from_json must keep the FIRST page's total (a later page's 0 or missing total must not end the read as
   complete). Test with the QA mock (total only on page 1).
2. N5: Oberbank place must come from the card itself however long it is: bound the search to the card (e.g. from the
   previous card's end to this card's link) instead of a fixed 3000-char window; if no place is found in the card,
   the job is kept with default_location only if Oberbank's board is Austria-only (it is: check), else counted as bad.
   Test with a synthetic card of 6000 chars.

## Dev A - prefilter.py, tests/test_prefilter_wave3.py
1. N3: 'ingenieur' must not drop Wirtschaftsingenieur* titles ("Praktikum Wirtschaftsingenieurwesen",
   "Wirtschaftsingenieur Product", "Werkstudent Ingenieurwesen Produktmanagement" must reach the normal checks).
   "Robotik- und Automatisierungsingenieur" stays excluded.
