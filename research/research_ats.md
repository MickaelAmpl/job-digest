# ATS mechanics for a Vienna entry-level PM/PO/Analyst CV - research (8 Oct 2026)

Labels: **[P]** = primary (vendor docs, law, original study). **[S]** = secondary (news, blogs, CV sellers). **NV** = not verified.

## 1. What happens to a CV after upload

1. **The file is stored as an attachment, always.** Parsing is a separate step. If parsing fails, the recruiter still has the original file. Greenhouse: on a failed parse "the resume has only been attached to the candidate" [P] https://support.greenhouse.io/hc/en-us/articles/200989175-Unsuccessful-resume-parse
2. **Text extraction**: PDF/DOCX are converted to text. Image files are not parsed. Lever: it "cannot parse information from image files (like JPG or PNG)". Quick check: if you can't highlight the text, it won't parse [P] https://help.lever.co/hc/en-us/articles/20087345054749
3. **Segmentation and field extraction**: the parser finds sections (Work history, Education, Skills), then pulls out name, contacts, titles, employers, dates and degrees. Textkernel documents which findings matter: no sections, no WORK HISTORY, nonstandard headers, columns rearranged, dates split across lines, no email or phone [P] https://developer.textkernel.com/tx-platform/v10/resume-parser/overview/parser-output
4. **Candidate profile**: the parsed fields fill the profile, and the full CV text is indexed for search. Personio shows parsed fields (current employer, title, years of experience) as list columns and filters [P] https://support.personio.de/hc/en-us/articles/360010193018
5. **Deduplication**: ATS usually merge records by email/phone. This was not checked per vendor (NV). Practical point: use the same email for every application.

**Which parser each ATS uses**

| ATS | Parser | Evidence |
|---|---|---|
| Personio | Textkernel (subcontractor, opt-in) | [P] support.personio.de/hc/en-us/articles/360010193018 |
| SmartRecruiters | Textkernel (old announcement) | [S] https://www.onrec.com/news/news-archive/smartrecruiters-chooses-textkernel%E2%80%99s-resume-parsing-software |
| Greenhouse | HireAbility, per the subprocessor list of 8 Mar 2024. Absent from the Aug 2026 excerpt, so the current vendor is NV | [P] https://www.greenhouse.com/uk/subprocessors-in-use-march-8-2024 |
| Recruitee, onlyfy, softgarden | Textkernel, per a German test portal | [S] trusted.de/beste-cv-parsing-software |
| Teamtailor | Own "Co-pilot resume parser" add-on, engine not named | [P] https://support.teamtailor.com/en/articles/16127956-co-pilot-resume-parser |
| SAP SuccessFactors | Built-in parser with known limits (KBA 2081576, login required). Textkernel has been an SAP partner since 2013 (matching); core parser engine NV | [P] https://userapps.support.sap.com/sap/support/knowledge/en/2081576 |
| Workday, Lever, Ashby, Workable, join, d.vinci, Umantis/Abacus | Not public (NV). Claims that "Lever uses Textkernel/Sovren" come from CV-seller blogs only | - |

Sovren belongs to Textkernel (acquired 2021), and Textkernel belongs to Bullhorn [P] https://www.textkernel.com/learn-support/blog/textkernel-acquires-sovren-to-become-the-global-leader-in-ai-powered-recruitment-technology/. Because of this, the Textkernel quality codes are the best single public proxy for "what parsers dislike".

## 2. Auto-reject vs auto-rank

**Rule of thumb, with evidence:** automatic *rejection* comes almost always from **knockout questions in the application form** (work permit, German level, location, salary). It does not come from scoring the CV text. AI on the CV text *sorts and labels*, and a human rejects.

| ATS | Knockout auto-reject | AI on the CV |
|---|---|---|
| Personio | **Yes.** "If the answers do not meet your criteria, the application is rejected" [P] https://support.personio.de/hc/en-us/articles/28017305502493-Set-up-automated-screening | "AI Talent Screening Agent" shows pass/fail indicators against role criteria. It runs on Anthropic and is switched on by an admin [P] support.personio.de/hc/en-us/articles/38477031176861 |
| Recruitee | **Yes**, knockout questions with an automatic disqualify reason and a delayed rejection email [P] https://support.recruitee.com/articles/6108676 | NV |
| Workable | **Yes**, a Yes/No question with "No" means disqualified. The candidate is not told [P] https://help.workable.com/hc/en-us/articles/115012238688 | "Workable Agent" screens against an ideal-candidate profile [P] https://help.workable.com/hc/en-us/articles/40661870891415. Score-threshold auto-disqualify is NV |
| Ashby | Auto-reject rules on application-form answers, e.g. location [P] https://ashbyhq.notion.site/Auto-Reject-Applications-49a396c635594085889fd601ed66539c | AI review checks the CV against recruiter-defined criteria and gives Meets / Doesn't meet / Undecided per criterion. Ashby: "never ranks or gives numerical ratings", a human decides, and recruiters can *filter* by criteria [P] https://ashbyhq.com/ai |
| Greenhouse | Rule-based auto-reject exists on higher tiers [S]. The Greenhouse doc was NV | **Talent Matching**: Strong / Good / Partial / Limited match plus matched and missing skills, built from **resume data only** against recruiter "calibration". "No mechanism that automatically… rejects" [P] https://support.greenhouse.io/hc/en-us/articles/41131616864283-Talent-Matching-Data-Processing-FAQ |
| Workday (HiredScore) | Configurable questionnaires (NV in detail) | **A/B/C/D grade** visible to recruiters, from basic and preferred qualifications in the job description. No resume means no grade. Workday tells recruiters top grades are "most efficient" to screen, but C/D may still fit [P] https://doc.workday.com/hiredscore/en-us/workday-hiredscore/recruiter-productivity-/reference--candidate-grades.html ; Fit & Gap view and per-qualification assessment, Aug 2026 [P] https://doc.workday.com/hiredscore/en-us/workday-hiredscore/hiredscore-release-notes/2026/august/wednesday--5th-august.html |
| SmartRecruiters | "Rejection screening questions" exist [P] https://learning.sap.com/courses/smartrecruiters-for-sap-successfactors-academy/creating-rejection-screening-questions | **Winston Match**: 4-star score with explanation; the list is sorted by score; sub-scores for skills, experience and education (Mar 2026). OECD: "does not make automated decisions" [P] https://learning.sap.com/courses/smartrecruiters-for-sap-successfactors-academy/introducing-winston-match ; https://www.oecd.org/careers/dataprotectionpolicy.htm |
| SAP SuccessFactors | Prescreening questions (NV detail) | AI skills matching: extracts skills from the CV against requisition skills plus "related skills". Joule Premium add-on [P] https://www.sap.com/products/hcm/successfactors-recruiting-applicant-screening.html |
| Teamtailor | "Smart Move" moves candidates by answer. Auto-reject NV | Co-pilot: 5-bullet CV summary and gap timeline; skills found in the CV are added as competencies [P] https://support.teamtailor.com/en/articles/8403177-generate-resume-summary |
| join.com | NV | Match score with per-dimension explanation: keyword/skill overlap plus embedding similarity [P, glossary] https://join.com/en/glossary/ai-candidate-matching |
| LinkedIn job posts | "Auto-rate applicants who don't meet requirements" via screening questions, with optional automatic rejection emails [P] https://business.linkedin.com/talent-solutions/cx/jobs-101/manage-applicants | Top fit / Maybe / Not a fit from must-have and preferred qualifications |
| d.vinci, Umantis/Abacus, onlyfy | NV. Umantis lists a "matching score" [S] | - |

**Implication:** the biggest auto-reject risk is a **form answer** (work authorisation, German level, salary, start date), not the CV layout. Next comes **missing evidence of each must-have qualification** in the CV text, because Greenhouse, HiredScore, Ashby and SAP all score requirement-by-requirement against the job description.

**EU AI Act (Austria):** Annex III point 4(a) makes AI "to analyse and filter job applications, and to evaluate candidates" **high-risk** [P] https://artificialintelligenceact.eu/annex/3/. Deployers must keep human oversight and inform affected people (Art. 26), and people get a right to explanation (Art. 86) [P] https://eur-lex.europa.eu/eli/reg/2024/1689/oj. The Digital Omnibus moves the standalone Annex III obligations from 2 Aug 2026 to **2 Dec 2027**. It was adopted by Parliament and Council in Jun 2026; the Official Journal publication date is NV [S, law firm] https://www.gibsondunn.com/eu-ai-act-omnibus-agreement. GDPR Art. 22 already restricts solely-automated decisions with significant effects [P] https://eur-lex.europa.eu/eli/reg/2016/679/oj. This is why vendors in the EU design their tools as "assistive, human decides" (Greenhouse and Ashby wording above). Knockout rules on explicit form answers are the main fully automatic step left.

## 3. How recruiters search

- **Greenhouse**: full-text search over resumes, notes and scorecards. Supports AND/OR/NOT, "exact phrase", parentheses and a `*` wildcard (`Java*`). **No mention of stemming or automatic synonyms**: in the doc's OR example the user types the synonyms [P] https://support.greenhouse.io/hc/en-us/articles/202360199-Search-candidates-using-Boolean-queries ; https://support.greenhouse.io/hc/en-us/articles/115004600186-Search-resumes-for-keywords
- **Lever**: field prefixes such as `title:` search the **parsed work-history titles** [P] https://help.lever.co/hc/en-us/articles/20087317030685. The claim of word stemming comes from Jobscan only [S].
- **Textkernel Search** (used by SAP partners, Bullhorn and job boards): semantic. It expands a query with synonyms and related terms, normalises job titles into classes, groups and codes, and supports Must / Should / Nice-to-have weighting [P, archived] https://kb.bullhorn.com/bh4sf/Content/BH4SF/Archived/textkernelCandidateAndJobSearchUserGuide.htm
- **Personio**: filters on parsed fields (current title, employer, years of experience) [P].

**Conclusion:** search can be literal (Greenhouse Boolean) or semantic (Textkernel, LLM matchers). A CV that uses the **job ad's exact term at least once**, plus the common variant, works in both. A missing literal term fails in a literal search. A parsed *title* field is searched separately, so the title line matters.

## 4. Parsing pitfalls - evidence

| Pitfall | Evidence |
|---|---|
| Columns | Greenhouse lists "columned layout" as a cause of failed parses [P]. Textkernel code 433: "HUGE MISTAKE… columns rather than simple top-to-bottom" [P]. Textkernel blog: ≥15% of CVs use columns, and before their fix only 62% were rendered correctly. Keyword search survives a bad rendering, but structured extraction "compounds" errors [P] https://textkernel.com/learn-support/blog/improving-extraction-from-column-resumes |
| Tables, headers/footers, text boxes | Greenhouse: "complex resumes with tables, headers, and footers"; name or contact "in the header, footer, or text box" [P]. Textkernel 311: contact info not at the top [P] |
| Graphics, photos, word art, image-only CV | Greenhouse, Lever, Workable [P] https://jobseekers.workable.com/hc/en-us/articles/360036231313 |
| Letter-spaced text ("S K I L L S") | Greenhouse: "spaces between the letters" [P] |
| Section headings | Textkernel 151: "clear, unambiguous, commonly-used header on a separate line". Also 325 (section without header), 412-417 (no or nonstandard sections) [P]. Greenhouse: "without clear sections" [P] |
| Dates | Textkernel 418 (date range split over lines), 419/224/225 (jobs without dates). Workday "Present" problems [S] |
| Job titles, companies | Greenhouse: "incomplete job titles"; company names without "Inc., Co., LTD, LLC" [P]. Textkernel 221/222 [P] |
| PDF vs DOCX | Textkernel lists "Document is a PDF" as a *Major Issue* (300) [P]. Greenhouse, Lever and Workable accept both. Textkernel converts 70+ formats [P] https://developers.textkernel.com/Parser/master. Verdict: a text-based PDF is accepted everywhere, and DOCX is marginally safer for older parsers. Use DOCX when the form offers it or recommends it |
| Scanned PDF | Not parsed (no text layer) [P, Lever] |
| File size | Greenhouse: no parse above **2.5 MB** (uploads up to 100 MB) [P]. Workable 5 MB [P] |
| File types | Greenhouse .doc/.docx/.pdf/.rtf/.txt [P] https://support.greenhouse.io/hc/en-us/articles/360052218132 ; Recruitee PDF/Word/ODT [P] https://support.recruitee.com/en/articles/1109254-upload-and-parse-cvs |
| Hyperlinks | Greenhouse does not load externally linked *images* [P]. SAP KBA "embedded links… parsing rejected" for a SmartRecruiters PDF; title only, content NV [P] https://userapps.support.sap.com/sap/support/knowledge/en/3768717. Plain-text URLs are safe |
| Fonts: Type3, ligatures | No vendor doc found (NV). Technical risk: Type3 or bitmap fonts and missing ToUnicode maps make text extract as garbage, and "fi/fl" ligatures can extract as one glyph. Testable by round-tripping the text with pdftotext |
| File name | No evidence it affects parsing (NV). It matters only to the human reader |
| Multiple emails/phones, references, DOB, marital status | Textkernel 132/133/111/123/124 [P] |
| Separate skills section | Textkernel 112 flags it as a "suggested improvement". Sovren-heritage parsers compute skill experience from skills mentioned *inside* jobs [P, code list]. So skills belong in bullets as well as in the list |

## 5. Keywords: legitimate vs harmful

- **Exact plus variant**: Greenhouse Boolean has no documented synonym handling [P], while Textkernel expands synonyms [P]. Write "Google Analytics 4 (GA4)" once, then either form. This is not vendor-specified; it is a logical consequence of the two search models.
- **Evidence per requirement**: Greenhouse shows "matched and missing skills" [P]. HiredScore grades each basic and preferred qualification and shows met / not met [P]. Ashby gives a Meets / Doesn't meet verdict per criterion with citations [P]. Each must-have from the ad should map to a visible line in the CV.
- **Density**: no vendor documents a density score (NV). Talent Matching works from extracted skills and requirements, not frequency. Repetition beyond natural use gains nothing that can be documented.
- **Hidden white text or prompt injection**: Greenhouse reported about 1% of ~300M CVs in H1 2025 with white-text messages [S] https://www.bostonglobe.com/2025/10/07/business/ai-resumes-jobs-recruiters-applications. A USENIX Security 2026 / hireEZ study found ~1% with hidden injections, and its detector (visual vs extracted text) runs in hireEZ production [S] https://pratt.duke.edu/news/thwarting-prompt-injection/. Greenhouse lists "fake or poorly disguised data" as a parse-failure cause [P]. Extracted text is what recruiters see in snippets, so hidden text becomes visible. **Never.**
- **Stuffing detection**: no vendor documents a stuffing penalty (NV). The risk is human: the snippet view (Greenhouse shows matching snippets) exposes keyword lists.

## 6. The "75% rejected by ATS" claim

- Traced to **Preptel** (2012), a CV-optimisation vendor that closed in 2013. No published method exists [S] https://blog.hiringthing.com/applicant-tracking-system-myths ; https://www.f1jobs.io/resources/blog/ats-resume-service-75-percent-rejection-myth. Original material NV.
- The serious source is **HBS/Accenture "Hidden Workers" (2021)**: 88% of employers say qualified high-skill candidates are screened out because they "did not exactly match" criteria. This is about rigid *criteria filters* such as degree, gaps and knockouts, not formatting [P] https://nahr.shrm.org/sites/default/files/hiddenworkers09032021.pdf (copy of HBS PDF; summary https://www.library.hbs.edu/working-knowledge/how-to-tap-the-talent-automated-hr-platforms-miss)
- **Verdict:** 75% is not credible as a parsing rejection rate. The real risks are (a) knockout rules, (b) requirement-matching ranks a CV low, (c) a recruiter only reads the top of a sorted list.

## 7. LinkedIn Easy Apply, karriere.at, StepStone

- **LinkedIn Easy Apply**: the poster receives the application, the uploaded CV and answers, and "relevant skills from your profile may be included" [P] https://www.linkedin.com/help/linkedin/answer/a512353. The poster sees Top fit / Maybe / Not a fit buckets based on screening questions and qualifications, and can auto-reject [P] https://business.linkedin.com/talent-solutions/cx/jobs-101/manage-applicants ; https://www.linkedin.com/help/linkedin/answer/a519651. Applications posted by an agency may be shared with other agency clients [P]. Both the profile and the CV must be consistent.
- **karriere.at "smart bewerben"**: the application "including all attachments" goes directly to the employer. If you are logged in, the **automatically generated karriere.at CV** is attached unless you replace it with your own file [P] https://www.karriere.at/lp/was-ist-smart-bewerben. **Replace it with your own CV.** "Jetzt bewerben" redirects to the employer's ATS. No recruiter-facing match score is documented (NV).
- **StepStone**: the data is either stored on StepStone, where the employer can also access your profile, or forwarded to the employer's ATS, as the employer chooses [P] https://www.stepstone.at/e-recruiting/nutzungsbedingungen/. "Connect" redirects to the ATS form, where you upload the CV [P] https://www.stepstone.de/e-recruiting/en/connect-en. A recruiter-side score is NV.

## 8. CV length (~3 years of experience)

- **Austria**: "all the essentials on a maximum of two pages"; tabular format, photo common [P, Austrian Business Agency] https://www.workinaustria.com/en/employees/career-navigator-online/career-navigator/application. WU ZBP and Uni Wien Uniport page counts are NV (no public page found).
- **International tech**: Google recruiters advise **one page** for early career [S] https://www.teach.cs.toronto.edu/ambassadors/blog/2025-events/google_resume_workshop
- **Studies**: ResumeGo (a CV seller, 2018): recruiters chose 2-page CVs 2.3x overall and 1.4x at entry level [S] https://www.resumego.net/research/one-or-two-page-resumes/. Ladders eye-tracking: 7.4 s initial screen, focused on titles, companies, dates and education, with penalties for multi-column and cluttered layouts [S] https://www.hrdive.com/news/eye-tracking-study-shows-recruiters-look-at-resumes-for-7-seconds/541582/
- **Does the ATS care?** No length penalty is documented. Textkernel only truncates very long documents (408) [P]. Length is a human factor.
- **Recommendation**: 1 page for international or tech employers, up to 2 for Austrian/DACH corporates. Content in the top third: title, summary and current role, because of the 7 s scan.

## 9. Checklist for the CV generator (testable)

1. Output a **text-based PDF** (and DOCX on request); text must be extractable (pdftotext is non-empty and matches the source). [Lever, Greenhouse]
2. **Single column**; no tables, text boxes, or frames for layout. [Greenhouse; TK 433]
3. **Nothing in the page header or footer**: name and contacts in the body, first lines. [Greenhouse; TK 311]
4. **No images, icons, photo, charts or skill bars** in the ATS version. A photo goes only in a separate Austrian "human" version, if at all. [Greenhouse, Workable]
5. **File size < 2 MB.** [Greenhouse 2.5 MB]
6. **Standard section headings**, each on its own line: Summary, Experience / Work Experience, Education, Skills, Languages, Certifications. [TK 151/325/412-417]
7. **No letter-spaced text** (no "E X P E R I E N C E"); use CSS letter-spacing only if extraction stays a single word. [Greenhouse]
8. Each job has a **title, employer and start-end date on one line**, in one consistent format ("MM/YYYY – MM/YYYY" or "Mon YYYY – Present"). [TK 221/222/224/225/418]
9. **Full job titles**, no abbreviations ("Product Owner", not "PO"). [Greenhouse]
10. **Employer with its legal suffix where real** (GmbH, AG, Ltd). [Greenhouse]
11. **Exactly one email and one phone**, plus LinkedIn as a plain-text URL. [TK 132/133]
12. **No DOB, marital status or references section.** [TK 111/123/124; EU anti-discrimination practice]
13. **Each must-have from the job ad is covered by at least one concrete CV line**; the generator reports any requirement without coverage. [Greenhouse matched/missing; HiredScore met/not met; Ashby per-criterion]
14. **The job ad's exact term appears at least once**; acronym plus full form on first use ("Google Analytics 4 (GA4)"). [Greenhouse Boolean literal; Textkernel synonyms]
15. **Skills appear inside job bullets as well as in the Skills list.** [TK 112]
16. **Job title line/headline mirrors the target role** where truthful (Lever `title:` search, Textkernel title normalisation).
17. **No hidden or white text, no tiny fonts, no off-page text**; the generator diffs rendered text against extracted text. [Greenhouse ~1%; hireEZ detector]
18. **No keyword-dump block**; no term appears more than ~3 times unless natural (NV heuristic, human snippet review).
19. **Standard fonts with ToUnicode mappings; no Type3 fonts**; verify ligatures extract as "fi" (pdffonts + pdftotext check). [technical, NV in vendor docs]
20. **Length: 1 page (international/tech) or up to 2 (DACH)**; key facts in the top third. [workinaustria; Ladders]
21. **Language**: English CV for English ads; German level stated by CEFR (e.g. "German B2"). [workinaustria]
22. **The knockout-answer sheet is prepared separately** (work permit status, German level, salary in EUR gross/year, notice period, location) and kept consistent with the CV. This is the main real auto-reject path. [Personio, Recruitee, Workable, LinkedIn]
23. **On karriere.at, replace the auto-generated profile CV** with the tailored file; keep the LinkedIn profile consistent with the CV. [karriere.at, LinkedIn]
24. **File name** `Firstname-Lastname-CV-Role.pdf` (human convenience; no ATS effect documented).
