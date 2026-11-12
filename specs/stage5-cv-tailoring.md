# Stage 5 spec: CV tailoring + knockout sheet (cover letter moved to stage 6)

Draft 08.10.2026 for Misha's approval. Product = main session. Council: developer (opus), QA (sonnet), junior.
Inputs already agreed: research/research_ats.md (24-rule checklist), research/research_cover_letters.md, research/research_nda_verification.md, cv/facts.md (fact bank, source of truth), cv/source/content/en.json (approved base CV, 08.10.2026), cv/source/build.py.

## Goal
From a vacancy Misha marked "Интересно", produce in 1-3 minutes: a one-page PDF CV tailored to that vacancy (same layout, same facts, wording and keyword choice adapted), a list of what changed, keyword coverage before/after, the requirements he does not meet, an answer sheet for knockout questions, and on request a cover letter. Misha applies himself; nothing is ever sent to an employer by the tool.

## Hard rules (enforced in code where possible, not only in the prompt)
1. Facts only from cv/facts.md and the base CV. No new employers, titles, dates, numbers, tools, skills or results. The "never claim" list in facts.md (tools not used, ChatGPT, Bybit, SQL/Power BI/Python, ...) is a blocklist checked in code.
2. Locked fields: name, contacts, headline, every role title, organisation, dates, location, education, certifications, languages. They are always copied from the base by code after the model answers (the model cannot change them); any attempt is logged as a violation, and more than 2 violations in one version fail it (fallback = base CV).
3. Every number in the tailored text must already exist in the base CV or facts.md (code check).
4. English only, also for German-language ads (Misha 08.10.2026): German requirement terms are mapped to their English equivalents, no German words in the CV.
5. One page (code check via page count); if longer, one shortening retry, then fall back to the base CV for that section.
6. ATS checks from research_ats.md section 9 that apply to a generated file: text-based PDF, single column, extracted text order = source order, no ligature characters, no Type3 fonts, standard headings, hyphenated words not split, file < 2 MB, file name Alex-Morgan-CV-<Company>-<Role>.pdf.
7. The ad's exact term appears at least once when a fact supports it; acronym with full form on first use. No term more than 3 times. No hidden text, no keyword block.

## Pipeline (one job id)
1. Load the job from data/state.db: title, company, description (company feed / enrich), open_url. No description -> the bot asks Misha to paste the ad text as a reply; continue when he does.
2. Requirements (Claude, sonnet): JSON of must-haves, nice-to-haves, exact key terms, ad language, stated minimum salary and hours if any.
3. Tailoring (Claude, opus): input = base en.json + facts.md + requirements. Output = the same JSON schema plus a change log (field, before, after, reason, fact used) and a gaps list (requirement, why not claimable). Allowed: rewrite the summary, choose/reorder/rephrase bullets inside each role, swap in the ad's terms where a fact supports them, reorder skills and pick skills from facts.md.
4. Verifier (Claude, opus, separate call): checks every changed sentence against facts.md; any unsupported claim -> that sentence reverts to the base wording and is listed as "откатил".
5. Code checks (rules 1-7), build PDF with build.py, extract text, page count, coverage = share of the ad's key terms found (exact phrase, case-insensitive) in base vs tailored text.
6. Save a version: table cv_versions(job_id, created_at, pdf path, content json, change log, gaps, coverage_before, coverage_after, letter) + files in data/cv/<job_id>/. Which CV went to which company stays traceable (links to the status "Отправил резюме").

## Cover letter - MOVED TO STAGE 6 (Misha 08.10.2026: first finish CV tailoring)
Kept here as the agreed direction for stage 6.
- Rules from research_cover_letters.md: 4 paragraphs, 250-350 words (cap 380), exact role and company, 2-3 requirements each tied to a fact with an action and a result, no CV sentence repeated verbatim, cliché blacklist, salary and hours only if the ad asks, one work-permit sentence only if relevant, English.
- One company-specific fact only if it comes from a page we fetched (company site / original posting) with its URL; otherwise no company fact rather than an invented one.
- Same verifier pass as the CV. Delivered as text in the chat (to paste into forms) and as a PDF in the CV's style.

## Knockout answer sheet
From facts.md: work permit (20 h/week availability), start date ASAP, German B1 (ÖSD 2020), English C1 (BSc in English), salary: empty if optional, otherwise the ad's stated minimum pro-rated to his hours. Shown as a short message with the per-job salary figure filled in.

## Bot
- On a card with verdict "Интересно": new button "Адаптировать резюме".
- Press -> immediate reply "Готовлю резюме, 1-3 минуты" -> a detached worker process (the bot's polling loop never blocks) -> sends the PDF + one message: coverage before -> after, up to 8 changes, gaps, buttons [Шпаргалка для анкеты] [Отправил резюме].
- Re-press gives a new version (old ones kept). Failures send a short reason and the base CV as fallback.

## Server
- Install a headless Chromium (or Chrome .deb), the PT Serif font locally (no web font fetch at render time) and poppler-utils (pdftotext, pdfinfo, pdffonts) for the checks. Misha approved installing Chromium 08.10.2026; installed packages are listed in the handoff.
- Same claude CLI on Misha's subscription; no new keys, no spend.

## Phases (stop after each for Misha)
- 5a. Engine on the Mac: requirements, tailoring, verifier, code checks, build. Run on 5 of Misha's real "Интересно" jobs (incl. one German-language ad and one title-only job with pasted text). Misha reviews the 5 PDFs + change logs.
- 5c. Server install, bot button, worker, storage. QA, deploy, handoff note.

## Acceptance (QA, per phase)
- Locked fields identical; blocklist terms absent; no new numbers; one page; extraction order and hyphen check pass; coverage after >= before; verifier reverts an injected false claim in a test; German ad produces no German words; title-only job asks for text; worker never blocks the bot; versions saved and linked to the job.
