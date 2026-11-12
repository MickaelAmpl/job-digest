# Target role titles: research (16 Oct 2026)

Read-only research. No code changed.

Caveats
- Source: prod-state.db copy, 1,856 rows, first_seen 7 to 16 Oct (about 8.4 days). The "15 days" window is the whole DB. Weekly = count x 0.83.
- Descriptions are empty for almost all rows, so the German check could not run. Counts are upper bounds.

## 1. What he liked and rejected

| Verdict | Rows | Content |
|---|---|---|
| interested | 22 (21 real + 1 LinkedIn footer) | 11 student formats (intern, Praktikant, student job, Werkstudent) |
| not_interested | 3 (2 real + 1 footer) | Henkel full-time internship; e-dialog 25 h+ with German |
| cant_fit | 7 | reasons: german 6, skill 5, exp 3 |
| bad_terms | 3 | Bitpanda securities PM; Holzkern Marketing & Growth (full-time); Birner automotive PM |

Clusters in the 21 real "interested" rows: product / PO / generalist 7; market research / intelligence 4; revenue / marketing ops, CRM, growth 5; bank and insurance student jobs 3; other 2.

Reading: rejections are mostly language (German C1/C2 in 6 of 7 cant_fit) and domain (finance, insurance, physical product). Student formats need current enrollment, which he lacks (facts.md), so scoring caps them at 55.

Data issue: "See all jobs on LinkedIn" footer rows are stored as jobs.

## 2. Dropped titles that look like a fit

| Title | Company | Rule | Why it may fit / blocker |
|---|---|---|---|
| (Junior) Business Operations Manager Austria | Scalable Capital | excluded_field | Fintech, junior, Vienna, full-time, open. Blocker: "fließend Deutsch (C2)". [opened] |
| Program Coordinator (m/w/d) | Sportradar | not_target_role | Vienna, full-time, open. Close to his roadmap and ticket work. Requirements not seen. |
| Project Associate | UNIDO | not_target_role | Vienna, international organisation. Not verified. |
| AI Governance Team Member | Vienna Insurance Group | not_target_role | Vienna, full-time. Insurance; German likely (not verified). |
| AI & Beyond Incubator (Ongoing Intake) | Bitmovin | not_target_role | Venture fit. Page has no role details. [opened] |
| Digital Transformation Specialist | EY Austria | not_target_role | Vienna, digital consulting. Experience not verified. |
| Werkstudent Customer Success & Business Development Start-Up | mjuks GmbH | excluded_field | Start-up, Vienna, student format. Title only. |
| Junior Marketing Manager - Content & Growth (geringfügig) | Brunwig | excluded_field | Junior, marginal employment. Content rejected by Misha on 05.10: keep excluded. |

## 3. Proposed title patterns

Counts = DB rows that would pass (not senior, not excluded, currently not_target_role). Weekly = open rows x 0.83.

**A. AI / product / revenue operations, AI governance, business enablement**
- EN: AI Operations, Product Operations, Revenue Operations, AI Governance. DE: AI Governance, Business Enablement, Produktbetrieb.
- Fact: daily AI tools user; ops and roadmap work at Kestrel and the Web3 platform.
- Risk: VIG-type roles probably German; RevOps often needs Salesforce.
- Volume: 4 rows, 2 open, about 2 per week. **MAYBE.**

**B. CRM / lifecycle / retention, junior, no tool must-have**
- EN: CRM Specialist, Lifecycle Specialist, Retention Specialist. DE: CRM-Spezialist.
- Fact: interest in Bitmovin RevOps and kununu CRM.
- Risk: Vienna karriere.at CRM list (opened) shows "Mindestens 3-5 Jahre Erfahrung im CRM/Lifecycle Marketing" (Holzkern) and "Erfahrung mit Salesforce Marketing Cloud" (tipp3). He has neither tool. https://www.karriere.at/jobs/crm/wien
- Volume: 3 rows, 3 open, about 2.5 per week. **MAYBE.**

**C. Programme / project coordinator, project associate (narrow)**
- EN: Program Coordinator, Project Associate. DE: Programmkoordination, Projektassistenz.
- Fact: PM work with roadmaps, specs and tickets (facts.md); profile names international organisations as fit.
- Risk: "Project Administrator" and "Projektkraft" are admin-like; "Projektleiter" is engineering.
- Volume: two titles only, about 2 per week. **MAYBE.**

**D. Venture / incubator / accelerator / EIR**
- EN: Incubator, Accelerator, Venture Associate, Entrepreneur in Residence. DE: Inkubator, Accelerator-Programm.
- Fact: Kestrel sat in the Seedline Capital accelerator; investor outreach (facts.md).
- Risk: low volume; the open match has no details.
- Volume: 1 open, about 1 per week. **YES.**

**E. Market research / strategy / insight associate (not deals)**
- EN: Strategy Associate, Research Associate, Insights Associate. DE: Marktforschung, Strategie-Assistenz.
- Fact: research analyst at Northwind, 13 documents (facts.md); market analysis at Kestrel.
- Risk: deals and M&A domain (KPMG "Associate Deals & Strategy", closed).
- Volume: 2 rows, 1 open, about 1 per week. **YES.**

**F. Junior digital or transformation consulting (no SAP, no finance)**
- EN: Digital Consultant (junior / associate), Transformation Consultant only if digital.
- Fact: profile allows digital consulting; strategy consulting is a no.
- Risk: big-four juniors often ask for a degree and domain experience.
- Volume: 2 open, about 2 per week. **MAYBE**, check each description.

**G. Marketing associate / executive (not manager, not content)**
- EN: Marketing Associate, Marketing Executive.
- Fact: go-to-market work with the marketer (facts.md).
- Risk: marketing roles want campaign experience.
- Volume: few. **MAYBE** for associate and executive; **NO** for manager.

**H. Partner / alliance managers (non-sales)**: 1 open (Storyblok, US remote). **NO.**

**I. Crypto / Web3 / fintech titles**: 2 would pass, both legal. **NO** new pattern.

## 4. Stay excluded
- Senior, Lead, Head, Principal, Expert: profile hard reject.
- Business Analyst, BI, data analyst: SQL, Power BI, Tableau are must-haves he lacks.
- Finance, audit, tax, fraud, risk, legal: no domain.
- Engineering, AI Engineer, developer: not his field.
- Social media, community, content: rejected by Misha on 05.10.
- Sales, SDR, account executive, business development, customer service: profile excludes them.
- Business operations: excluded on 06.10. The one real case needs C2 German, so the rule stays.

## 5. Top 10 recommendations (priority order)
1. Add venture / incubator / accelerator / EIR (pattern D).
2. Add strategy / research / insight associate, excluding deals (pattern E).
3. Add Program Coordinator and Project Associate only (pattern C).
4. Add AI governance and business-enablement titles, plus AI and product operations (pattern A).
5. Add junior CRM / lifecycle titles with no tool must-have (pattern B).
6. Add junior digital consulting, excluding SAP and finance (pattern F).
7. Add Marketing Associate and Marketing Executive only (pattern G).
8. Fix the German regex: "fließend Deutsch (C2)" is not caught.
9. Keep "business operations" excluded for now.
10. Stop LinkedIn footer rows from being stored as jobs.

Expected extra per week before the German check: A about 2, B about 2.5, C about 2, D about 1, E about 1, F about 2, G a few. Total roughly 10 to 12, of which realistically 3 to 5 would be junior and non-German.

## Sources opened
- https://www.karriere.at/jobs/crm/wien
- https://jobs.smartrecruiters.com/scalablegmbh/744000153498329
- https://bitmovin.com/careers/8704987002?gh_jid=8704987002
- https://www.jobs.at/j/werkstudent/wien (CHECK24 PM Werkstudent, Teilzeit/geringfügig, 1010 Wien, from 2,373 EUR/month; summary only)
- https://www.stepstone.de/jobs/produktmanagement/in-wien (0 Vienna product-management matches within 30 km at fetch; 20 outside)

Not verified: UNIDO, EY, Sportradar, VIG, Holzkern, mjuks, Worldline (DB rows only). dreamworkhq.com returned HTTP 410.
