# Sales roles in the digest (04.11.2026)

Owner decision 04.11.2026: sales roles are now wanted (reverses the 05.10.2026 "no cold sales" rule). In scope: SDR/BDR,
junior/associate business development, account executive / account manager (junior/mid), partnerships, sales associate
at tech, SaaS, AI, fintech, crypto, media or ad-tech companies, working in English. Still out: retail/shop-floor sales,
field sales (Außendienst), call centers, door-to-door, insurance/financial-product agent sales, senior titles, high
German. German "Vertrieb" titles are NOT added: in our feeds they are insurance field sales, back-office sales support
or German-only roles (checked on the prod DB 04.11: UNIQA/DONAU/Generali/Wiener Städtische Vertrieb rows).

## Task 1 - prefilter.py
1. EXCLUDED: delete the alternatives `business development|bizdev` (line 32) and
   `sales (assistant|representative|development)|\bsdr\b|\bbdr\b|account executive` (line 34). Keep
   `außendienst|aussendienst` in EXCLUDED, and keep `verkauf|filial|einzelhandel`, call center, customer service.
   Update the comment above EXCLUDED with one line: sales roles moved out on 04.11.2026 (owner), field sales stays.
2. ROLE_GROUPS: add as the LAST group
   `"sales": (re.compile(r"\bsales\b|\bsdr\b|\bbdr\b|business\s+development|\bbizdev\b|account\s+(?:executive|manager)"
   r"|\bpartnerships?\b|\bpartner\s+(?:manager|sales)", re.I), True),`
   with a one-line comment: owner 04.11.2026; 'Vertrieb' titles not included (German field/insurance sales).
3. Nothing else changes (SENIOR, SOFT_EXCLUDED, STUDENT_FORMAT, GERMAN stay as they are).

Expected verdicts (write them as tests in a NEW file tests/test_prefilter_sales.py, same style as
tests/test_prefilter_wave3.py, helpers v() = prefilter.verdict({"title": t, "description": ""}) and
why() = prefilter.reason(...)):
- keep, reason "sales": "Sales Development Representative (SDR / BDR) - Fintech Payouts", "Business Development
  Representative", "Account Executive (m/f/d)", "Account Manager (w/m/d) Logistics", "Sales Manager - Native Russian
  Speaking (m/f/x)", "Partner Manager, Consulting Partnerships", "Inside Sales Specialist", "BDR", "Key Account Manager".
- keep, reason "target" (TARGET matches first): "Junior Sales Consultant", "Junior Account Executive".
- keep, reason "graduate": "Graduate Sales Programme" (GRADUATE comes before groups).
- senior: "Senior Account Executive", "Head of Sales", "Sales Director Europe", "Sales Expert (w/m/d)".
- excluded_field: "Sales Trainee im Außendienst (w/m/d) - Wien", "Verkäufer:in Filiale", "Call Center Sales Agent",
  "Sales Business Analyst" (soft exclusion, no AI), "Customer Service Sales Representative".
- not_target_role: "Vertriebsmitarbeiter:in Wien", "Vertriebsassistent (w|m|d)", "Gebietsleiter:in Vertrieb",
  "Presales Solution Designer Cyber Security (w/m/d)", "Salesforce Administrator".
- german_required: "Account Executive" with description "Fließendes Deutsch und sehr gutes Englisch".
- Turning the group off (patch ROLE_GROUPS["sales"] to enabled False) makes "Account Executive (m/f/d)" not_target_role.

Update tests/test_prefilter_wave3.py test_hard_exclusions_hold_with_ai: remove "AI Sales Representative" from that list
(it is no longer a hard exclusion) and add one assertion in the new file that "AI Sales Representative" is keep
(reason "ai_edge", because AI_EDGE is checked before groups).

## Task 2 - scoring/profile.md
First copy it to scoring/profile.v7.md (backup, unchanged). Then:
- In "## What fits", after the Growth line, add:
  `- Sales (added 04.11.2026): SDR/BDR, junior or associate business development, account executive or account manager (junior/mid), partnerships, sales associate - at tech, SaaS, AI, fintech, crypto, media or ad-tech companies, working in English. Asking for 0-3 years of sales is a fit; cold calling is fine. Commission-only pay: lower the score by about 10 and say so in why_ru.`
- In "## What does not fit", replace the line
  `- Engineering, design, sales/SDR/account executive, customer service, live chat, events/animation, office assistance.`
  with
  `- Engineering, design, customer service, live chat, events/animation, office assistance.`
  `- Retail or shop-floor sales, field sales (Außendienst), call-center or door-to-door sales, insurance or financial-product agent sales (Vertrieb von Versicherungen/Finanzprodukten).`
No other line changes.

## Task 3 - preview on a prod snapshot (no writes)
Run `python3 scripts/apply_wave3.py --db <scratch> --days 15`
(dry run, no --apply) and include in the report the count and the list (title | company | reason) of rows the new
rules would set to keep.

## Done when
- python3 -m py_compile prefilter.py; full suite `python3 -m unittest discover -s tests` passes (report the count).
- Report: files changed, test count before/after, the Task 3 list.
