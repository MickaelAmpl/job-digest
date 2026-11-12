# CV Tailoring Implementation Plan (stage 5)

> **For agentic workers:** executed by the job-digest council: developer writes code per task, QA tests per task, product (main session) runs server steps and deploys. Steps use checkbox (`- [ ]`) syntax. Not a git repo: no commits; back up changed files into backups/<date>-<task>/ before editing.

**Goal:** a button under an "Интересно" card returns, in 1-3 minutes, a one-page PDF CV tailored to that vacancy, a change list, keyword coverage before/after, the gaps, and a knockout answer sheet - without a single fact that is not in the fact bank.

**Architecture:** a small package `cv/tailor/` on the server: requirements (Claude sonnet) -> tailoring (Claude opus) -> verifier (Claude opus) -> code checks -> build.py -> PDF checks -> saved version. The bot spawns it as a detached worker per press; the worker sends results to Telegram itself.

**Tech Stack:** Python 3.12 stdlib only; `claude -p` CLI (Misha's subscription, prompt via stdin); headless Chromium + local PT Serif font for PDF; poppler-utils (pdftotext, pdfinfo, pdffonts) for checks; SQLite (data/state.db).

**Spec:** specs/stage5-cv-tailoring.md. Also read: research/research_ats.md section 9, cv/facts.md, cv/source/content/en.json, cv/source/build.py.

## Global Constraints
- Facts only from cv/facts.md and cv/source/content/en.json; English only (also for German ads).
- Locked fields copied from base by code: name, contacts, headline, experience[].role/org/dates/location, education, certifications, languages, extra, labels, order.
- Blocklist (cv/never_claim.json, case-insensitive, word boundary): ChatGPT, Bybit, SQL, Power BI, Tableau, Python, R (as a skill), SAP, Miro, Trello, Asana, ClickUp, Linear, Amplitude, Mixpanel, Hotjar, Google Tag Manager, Looker Studio, A/B test, user interviews, DefiLlama, Dune, Etherscan, Nansen, Webflow, Zapier, Make.com, n8n, Midjourney, Scrum.
- Every number token in the tailored text exists in base or facts text.
- One A4 page. File < 2 MB. File name `Alex-Morgan-CV-<Company>-<Role>.pdf` (ASCII, hyphens, max 80 chars).
- No term from the ad more than 3 times in the CV. No hidden text.
- Plain hyphens only in all Russian bot text; no long dashes.
- Tests: `python3 -m unittest discover -s tests -v` (stdlib unittest; claude calls always faked in unit tests).

## Review Focus
1. Ad with no description at all (LinkedIn title-only) -> the bot asks for pasted text instead of tailoring from the title. Test in Task 7.
2. Claude returns broken JSON, times out, or the login expired -> the worker sends the base CV with a clear reason, never silence. Test in Task 5.
3. A tailored version overflows to 2 pages even after one shortening retry -> fall back to base wording for the longest section, still 1 page. Test in Task 5.
4. Two presses on the same card within a minute -> one worker at a time per job (lock file), second press answers "уже готовлю". Test in Task 7.
5. Ad in German with German skill names ("Marktforschung") -> CV contains the English term, no German words (check: no word from a German stop-list in the PDF text). Test in Task 3.

---

### Task 1: Server prerequisites and offline font (product + developer)

**Files:**
- Modify: `cv/source/build.py` (local @font-face instead of Google Fonts link; CHROME path from env `CHROME_BIN`, default Mac path)
- Create: `cv/source/fonts/` (PT Serif regular, bold, italic, bold-italic TTF, OFL licence file)
- Test: `tests/test_build.py`

**Interfaces:**
- Produces: `build.build(content_path: str, pdf_path: str) -> Path` (unchanged signature), works on Mac and on the server without network.

- [ ] **Step 1 (product):** on vps install `chromium` (snap or Chrome .deb), `poppler-utils`; record versions in the handoff. Misha approved 08.10.2026.
- [ ] **Step 2: test** `test_build_one_page_offline`: build en.json with network disabled (env `CV_OFFLINE=1` makes build fail if the HTML references http(s) fonts) -> PDF exists, `/Count 1` in the PDF bytes, size < 2 MB.
- [ ] **Step 3:** run, expect FAIL (Google Fonts link present).
- [ ] **Step 4:** implement local @font-face (file:// URLs to cv/source/fonts/*.ttf) and `CHROME_BIN`.
- [ ] **Step 5:** run on Mac and (product) on the server: PASS; visual diff vs the approved PDF - same line breaks (compare `pdftotext -layout` output on the server with the approved text).

### Task 2: Code checks (pure functions)

**Files:**
- Create: `cv/tailor/checks.py`, `cv/never_claim.json`
- Test: `tests/test_checks.py`

**Interfaces:**
- Produces:
  - `enforce_locked(base: dict, new: dict) -> tuple[dict, list[str]]` - returns new with locked fields copied from base + list of violated field paths.
  - `blocklist_hits(text: str, terms: list[str]) -> list[str]`
  - `new_numbers(allowed_text: str, text: str) -> list[str]` (numbers incl. "120,000+", "5", "2022").
  - `coverage(terms: list[str], text: str) -> float` (exact phrase, case-insensitive, 0..1; empty terms -> 1.0).
  - `overused(terms: list[str], text: str, limit: int = 3) -> list[str]`
  - `cv_text(content: dict) -> str` (all visible text in render order, tags stripped).

- [ ] **Step 1: tests** with exact cases: role title changed -> restored + "experience[0].role" in violations; "ChatGPT" in a skill -> hit; "Rapidly grew to 500,000 users" vs base -> ["500,000"]; coverage(["Product Owner","Jira"], "Jira and Confluence") == 0.5; "Kanban" x4 -> overused.
- [ ] **Step 2:** run, FAIL. **Step 3:** implement. **Step 4:** run, PASS.

### Task 3: Claude wrapper + requirements extraction

**Files:**
- Create: `cv/tailor/llm.py`, `cv/tailor/requirements.py`
- Test: `tests/test_requirements.py`

**Interfaces:**
- Produces:
  - `llm.ask_json(prompt: str, model: str, timeout: int = 300) -> dict|list` - `claude -p --model <m> --output-format json`, prompt on stdin, extracts the JSON object/array, raises `LLMError` on is_error / bad JSON / timeout. Injectable runner for tests (`llm.RUN = fake`).
  - `requirements.extract(job: dict) -> dict` with keys: `must` (list[str]), `nice` (list[str]), `terms` (list[str], exact English key terms, max 25), `ad_language` ("en"|"de"|"other"), `min_salary` (str|None, as written in the ad), `hours` (str|None). German ad terms are translated to English in `terms`.

- [ ] **Step 1: tests** with a faked runner: valid answer parsed; garbage -> LLMError; German fixture ad (Marktforschung, Produktmanagement) -> fake answer used, and a contract test asserting the prompt text contains the English-only instruction.
- [ ] **Step 2-4:** FAIL -> implement -> PASS.

### Task 4: Tailoring + verifier

**Files:**
- Create: `cv/tailor/tailor.py`, `cv/tailor/verify.py`, `cv/tailor/prompts/tailor.md`, `cv/tailor/prompts/verify.md`
- Test: `tests/test_tailor.py`

**Interfaces:**
- Consumes: `llm.ask_json`, `checks.*`, requirements dict.
- Produces:
  - `tailor.run(base: dict, facts: str, reqs: dict) -> dict` = `{"content": dict, "changes": [{"field","before","after","reason","fact"}], "gaps": [{"requirement","why"}], "violations": [str]}`; content already passed through `enforce_locked`.
  - `verify.run(base: dict, facts: str, result: dict) -> dict` - same shape; every changed sentence the verifier marks unsupported is reverted to base wording and moved to `result["reverted"]`.
- Prompt rules (tailor.md): only summary, bullets (choose/reorder/rephrase within the same role), skills order/pick from facts; use the ad's exact term once when a fact supports it; acronym + full form on first use; no new facts/numbers/tools; English only; keep bullet count per role within base count ±1; output JSON only.

- [ ] **Step 1: tests** (faked LLM): a fake tailoring that renames a role -> role restored, violation logged; a fake that adds "SQL" -> after `verify.run` with a fake verifier flagging it, the sentence equals the base sentence and is in `reverted`; >2 violations -> `tailor.run` raises `TailorRejected`.
- [ ] **Step 2-4:** FAIL -> implement -> PASS.

### Task 5: Orchestrator, PDF checks, storage

**Files:**
- Create: `cv/tailor/run.py`, `cv/tailor/pdfcheck.py`
- Modify: `store.py` (table `cv_versions(id integer primary key, job_id text, created_at text, pdf_path text, content text, changes text, gaps text, reverted text, coverage_before real, coverage_after real, status text)`)
- Test: `tests/test_run.py`

**Interfaces:**
- Consumes: Tasks 1-4.
- Produces:
  - `pdfcheck.check(pdf: Path) -> dict` = pages, size, text, issues (Type3 fonts via pdffonts, ligature chars U+FB00-FB06, hyphen-split words: a token in cv_text with "-" missing from the extracted text) ; raises `ToolMissing` when poppler is absent.
  - `run.tailor_job(job_id: str, pasted_text: str|None = None) -> dict` = `{"status": "ok"|"fallback"|"need_text", "pdf": str, "coverage": [before, after], "changes": [...], "gaps": [...], "reverted": [...], "answers": str, "reason": str}`; writes files to `data/cv/<job_id>/<n>/` and a cv_versions row.
  - CLI: `python3 -m cv.tailor.run <job_id> [--text path]` prints that dict as JSON.
- Flow rules: no description and no pasted text -> `need_text`; one shortening retry if pages > 1 (tell the model which section to cut by ~N lines), then base wording for the longest role; any LLMError/TailorRejected -> `fallback` = base CV PDF + reason; knockout answers from facts.md with `min_salary` filled (pro-rated note if ad is full-time).

- [ ] **Step 1: tests** (faked LLM + fake build producing 1-page/2-page PDFs): ok path writes a row; 2-page then 1-page after retry -> ok; always 2 pages -> base wording, 1 page; LLM timeout -> fallback with reason; no description -> need_text; coverage_after >= coverage_before on the ok fixture; file name for company "Raiffeisen Bank International (RBI)" / role "Student Job - Agile IT Delivery" is ASCII, <= 80 chars, starts with "Alex-Morgan-CV-".
- [ ] **Step 2-4:** FAIL -> implement -> PASS.

### Task 6: Pilot on 5 real jobs (product) - STOP for Misha

- [ ] Pick 5 "Интересно" jobs from state.db: GoStudent Technical Product Generalist, RBI Agile IT Delivery, UNIQA Market Intelligence, one German-language ad (TOWA or REWE), one title-only (Tito & Friends) with pasted ad text.
- [ ] Run `python3 -m cv.tailor.run <id>` on the server for each; send Misha the 5 PDFs + per job: coverage before -> after, changes, gaps, reverted.
- [ ] Collect Misha's corrections; turn each into a prompt rule or a blocklist entry; rerun the affected jobs. Do not start Task 7 until Misha approves.

### Task 7: Bot button, worker, pasted-text flow

**Files:**
- Modify: `bot.py` (button `c:tailor:<id>` on cards with verdict interested, next to status buttons; `/cv <id>` not needed)
- Create: `cv/tailor/worker.py`
- Modify: `store.py` (table `cv_requests(job_id text primary key, asked_msg_id integer, at text)`)
- Test: `tests/test_bot_tailor.py`

**Interfaces:**
- Consumes: `run.tailor_job`, bot `api()`.
- Produces: `worker.main(job_id: str, chat_id: str, pasted: str|None)`; bot spawns `python3 -m cv.tailor.worker <job_id>` detached (`start_new_session=True`), lock file `data/cv/<job_id>/.lock` (stale after 10 min).
- Messages (Russian): press -> "Готовлю резюме, 1-3 минуты"; need_text -> "Описания вакансии нет. Пришли текст объявления ответом на это сообщение" (store asked_msg_id; a reply to it starts the worker with the text); ok -> sendDocument + summary "Совпадение: X% -> Y%", up to 8 changes, gaps, buttons [Шпаргалка для анкеты] [Отправил резюме]; fallback -> base PDF + "Не получилось адаптировать: <reason>. Прислал базовое резюме."; double press while locked -> "Уже готовлю, подожди".

- [ ] **Step 1: tests** with fake api and fake tailor_job: button only on interested cards; press spawns once, second press answered "Уже готовлю"; need_text then reply -> worker called with the text; fallback message text; callback_data <= 64 bytes.
- [ ] **Step 2-4:** FAIL -> implement -> PASS.

### Task 8: QA, deploy, handoff (product + QA)
- [ ] QA runs the spec acceptance list on the server copy (/tmp app copy pattern) incl. one real end-to-end press in a test chat flow with fake send.
- [ ] Product: backup, rsync (keep run.sh +x), restart bot, one real press by Misha, check data/cv/ and cv_versions.
- [ ] Handoff note in cv/README.md: how to update facts.md / blocklist, rebuild base CV, where versions live, what to do if Chromium or claude login breaks. Memory updated.
