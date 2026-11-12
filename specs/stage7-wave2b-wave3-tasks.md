# Stage 7: wave 2 part 2 (company feeds) + wave 3 (filter) - developer tasks (26.10.2026)

Approved by Misha 26.10.2026 ("да делай, работай"). Plan: specs/stage7-plan.md, steps 4.2 and 5.
Council: product (main session) orchestrates and integrates; four developer tasks run in parallel; Sonnet QA after.
Nothing is deployed until Misha reviews the wave 3 preview list and the adapter report.

## Rules for every task
- Python 3.12 stdlib only. Match the surrounding code (short docstrings that say WHY).
- Edit ONLY the files your task owns (listed per task). Never edit companies.json, fetch_companies.py,
  prefilter.py (unless owned), store.py, run.sh, .env, data/. Never deploy, never ssh, never send Telegram.
- Never request these hosts, not even once: karriere.at, jobs.at, linkedin.com, stepstone.at/.de, indeed,
  glassdoor, willhaben.at, xing.com (their terms ban robots). fetch_posting.DENY is the full list.
- Network probing while you build is allowed for the companies you own: read robots.txt of each host first and
  respect it; at least 1 s between requests to one host; identify honestly with fetch_companies.UA.
- Tests: no network. Save trimmed real responses as fixtures under tests/fixtures/<your module>/ and test the
  parsing against them. Run `python3 -m unittest discover -s tests -t .` from the project root before reporting:
  all tests must pass (currently 774, run time ~30 s). Other tasks add tests in parallel; only your own
  files are your responsibility, but report any failure you see elsewhere.
- Report under 400 words: files, what works, per-company table (for adapter tasks), how tested, open risks.

## Integration contract (adapter tasks B1, B2, C)
- Your module does `from fetch_companies import Feed, day, flat, get, job, records, text` (and may use
  fetch_companies.UA). fetch_companies will load your module inside main() and merge your `ADAPTERS` dict:
  `ADAPTERS = {"<ats name>": function}`. Each function takes the company dict `c` from companies.json and
  returns a `Feed` (list of job dicts built with `job(c, title, location, url, desc, posted, employment)`).
- A company entry you configure gets: `ats` = your adapter name, `feed_url` = the URL your function starts from,
  optional extra keys your function reads (document each key in the function docstring), and
  `default_location` (e.g. "Wien") ONLY when the source never states a location and the company is known to
  hire only in Austria. Without a location the job is dropped by is_austrian().
- Feed.capped = True whenever you are not sure you read the WHOLE board (paging not finished, a cap, a page
  limit, a listing that may hide jobs). A capped feed never closes jobs; an uncapped feed that misses a job
  closes it as "removed by the company" - so a wrong uncapped feed is worse than a capped one.
- A failure (HTTP error, changed layout) must raise, not return an empty Feed: an empty uncapped feed would
  close every job of that company. If a page parses but finds 0 jobs where the page clearly has jobs, raise.
- Time: at most 30 s and at most 6 requests per company per morning run. Descriptions are optional: enrich.py
  fetches the posting page later for jobs that pass the filter. Prefer title + url + location from the listing.
- Deliver `research/wave2_feeds_<task>.json`: one object per company you own, with
  {id, status: "connected"|"unreadable", ats, feed_url, extra keys, default_location, capped (true/false),
  jobs_total, jobs_austria, checked_against: "<careers page URL>: N jobs visible on <date>", reason (why
  unreadable, or notes)}. product merges these into companies.json; you do not edit it.
- Your input: research/wave2_keep.json (77 companies Misha kept; field `task` says which task owns each one).

## Task A - wave 3 filter (owns prefilter.py, tests/test_prefilter_wave3.py, scripts/wave3_preview.py)
1. AI edge exception. New regex AI_EDGE on the TITLE: `\b(ai|ki|genai)\b`, "artificial intelligence",
   "künstliche intelligenz", automation, automatisierung, `\bagents?\b`, agentic, "digital transformation",
   "digitale transformation", digitalisierung. A title matching AI_EDGE:
   - passes the TARGET check (so "AI Agents & Business Automation", "Digital Transformation Specialist",
     "Marketing Operations Manager (AI & Automation)" are kept);
   - overrides ONLY these soft exclusions: business analyst, business operations, project coordinator /
     projektkoordinat, founder's associate, data analyst. Split EXCLUDED into SOFT_EXCLUDED (those) and the rest;
     the rest still drops even with AI in the title ("AI Engineer", "AI Customer Support" stay dropped);
   - SENIOR still drops first (unchanged order: senior, excluded, target, german).
2. Graduate programmes: "graduate" passes TARGET ("Graduate Programme 2027", "International Graduate").
3. New role groups, each a named entry in an ordered dict ROLE_GROUPS = {name: (regex, enabled)} checked as an
   addition to TARGET. All enabled=True for now; product flips them after Misha decides. Names and patterns:
   - venture: incubator, inkubator, accelerator, venture, entrepreneur in residence, `\beir\b`
   - strategy_research: strategy/strategie + (associate|analyst|assistenz), "research associate",
     insights? (associate|analyst|manager); NOT when the title has deal(s), m&a, transaction, valuation
   - program_coordination: program(me)? coordinator, programmkoordinat, "project associate" (project coordinator
     stays excluded - Misha said no on 05.10)
   - operations_ai: (ai|product|revenue|marketing|growth) operations, revops, "ai governance",
     "business enablement" (plain "business operations" stays excluded unless AI_EDGE)
   - crm_lifecycle: `\bcrm\b`, lifecycle, retention
   - digital_consulting: (digital|technology|transformation|digitalisierung\w*) + (consultant|berater)
   - marketing_associate: marketing (associate|executive|operations)
   - customer_success: customer success, client success, kundenerfolg (customer service/support stay excluded)
   A helper `reason(job)` returns which rule made a job pass: "target", "ai_edge", "graduate" or the group name;
   verdict() return values do not change (still "keep", "senior", ... - prepare.py and metrics depend on them).
4. German fix: GERMAN must also catch "fließend Deutsch", "fliessend Deutsch", "Deutsch fließend",
   "Deutsch (C2)", "Deutsch: C1", "Deutschkenntnisse (C1)", "Deutsch auf C1-Niveau", "Deutsch - Muttersprache".
   SOFTENED still wins ("fließend Deutsch von Vorteil" is not required).
5. scripts/wave3_preview.py --db <path> [--days 15]: opens the DB read-only (sqlite3 "file:...?mode=ro"),
   takes jobs with first_seen in the last N days counted back from the newest first_seen in the DB, rebuilds the
   job from `data` (title, description), recomputes verdict with the NEW rules and prints Markdown:
   (a) "Добавится": rows whose stored rule != 'keep' and new verdict == 'keep', grouped by reason, with
   title, company, source, first_seen; (b) "Уйдёт": stored rule == 'keep' and new verdict != 'keep' (expected:
   German fix only), with the new verdict. Also writes the same as JSON next to --out (default stdout only).
   Test DB for you: <scratch> (read-only copy of production; never write to it).
6. Tests in tests/test_prefilter_wave3.py: every example title above (pass and stay-dropped), each group, each
   German phrase (required and softened), and that all existing tests still pass.

## Task B1 - JSON-API systems (owns ats_api.py, tests/test_ats_api.py, tests/fixtures/ats_api/, research/wave2_feeds_B1.json)
Companies: `task == "B1"` in research/wave2_keep.json (27): JOIN, Workday, eRecruiter, Personio, SuccessFactors,
Teamtailor, BambooHR, Factorial, HiBob, Kula, GoHire.
- Personio, SuccessFactors, Teamtailor already have adapters in fetch_companies.py ("personio" = XML
  https://<tenant>.jobs.personio.de/xml, "successfactors" full sitemal feed, "teamtailor" RSS): for these only
  find the right feed_url and verify it; no new code.
- Write adapters for the others. Hints (verify, do not trust): Workday tenants answer
  POST https://<tenant>.wd<N>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs with JSON
  {"limit":20,"offset":0,"searchText":"","appliedFacets":{}} - page with offset, total in the reply; use the
  location facet for Austria if the board is global. JOIN: the public company page embeds the job list (look for
  a JSON blob / __NEXT_DATA__ or an API the page calls). eRecruiter: public job list JSON or XML per tenant.
  BambooHR: https://<tenant>.bamboohr.com/careers/list (JSON). Factorial, HiBob, Kula, GoHire: find the public
  endpoint the careers page itself calls.
- `get()` in fetch_companies only does GET; for Workday POST write a small helper in your module with the same
  UA, a 40 s timeout and the same size cap.

## Task B2 - page-based systems (owns ats_pages.py, tests/test_ats_pages.py, tests/fixtures/ats_pages/, research/wave2_feeds_B2.json)
Companies: `task == "B2"` (17): rexx, Phenom, softgarden, onlyfy (prescreen), jobbase, umantis, Prospective,
Oracle (Recruiting Cloud), TalentLink, Hurma.
- First look for a feed or JSON the system offers (rexx and softgarden often have XML/RSS or JSON; Oracle
  Recruiting Cloud has a public REST "recruitingCEJobRequisitions" endpoint; Phenom pages embed a JSON
  "phApp.ddo" blob or call a widgets API). Only parse HTML when no structured source exists.
- One adapter per system, configured per company with feed_url and documented extra keys.

## Task C - companies' own sites (owns site_jobs.py, tests/test_site_jobs.py, tests/fixtures/site_jobs/, research/wave2_feeds_C.json)
Companies: `task == "C"` (33): own career page (24), script-loaded or not found (5), closed to robots (1),
boards only (3).
- One generic adapter "site": fetch the company's job-list page, then in this order: (1) schema.org JobPosting /
  ItemList in JSON-LD on the list page; (2) a JSON source the page itself loads (look at the page source for an
  API URL or embedded JSON); (3) links to job pages matched by a per-company `link_pattern` regex (key in
  companies.json), title = link text. Use fetch_posting's page fetching with its guards (robots, private-IP and
  DENY checks, size and time limits) - find and reuse its internal fetch function rather than writing a new HTTP
  client; if that is impossible without editing fetch_posting.py, say so in the report and use urllib with an
  explicit robots check via fetch_posting._robots_ok.
- If a company's jobs are only reachable through a job board (karriere.at, LinkedIn...) or the page needs a
  browser to show jobs, mark it "unreadable" with the reason. Do not use a headless browser.
- Big employers with global boards (Accenture, Microsoft, Mastercard...): keep only Austrian locations;
  if the site hides locations, do not set default_location - mark unreadable instead.
