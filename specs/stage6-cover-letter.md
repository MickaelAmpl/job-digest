# Stage 6 spec: cover letter (approved by Misha 09.10.2026)

Plan with options: specs/stage6-plan.md. Research: research/research_cover_letters.md. Truth: cv/facts.md.

## Misha's decisions (final)
1. Mechanic B: paragraphs 2-4 come from an approved library (cv/letter_library.json); the model writes only the hook (paragraph 1, first part) and picks library ids. Code checks + one verifier call guard the hook; on any doubt the hook falls back to a library template.
2. Always English. German-only ad (ad_language "de" and the ad text never says English/Englisch): the letter is still English and the bot adds a warning line "Объявление на немецком, английский не упомянут - шанс ниже".
3. Work permit, weekly hours, salary, start date: NEVER in the letter (they live in the answer sheet). Code blocks them.
4. The Master's application / studies status: never mentioned. Code blocks it.
5. Delivery: text only in Telegram. No PDF.
6. Trigger: button "Написать письмо" next to "Адаптировать резюме" on the collapsed "Интересно" card. Only on Misha's press, never automatic. His flow: pick jobs -> Адаптировать резюме -> Написать письмо -> apply himself.

## Letter shape
```
Subject: Application: <clean title>

Dear <Ms|Mr Lastname> | Dear <Company> hiring team,

<hook (model, 1-2 sentences, names the company)> <identity sentence (library)>

<proof 1 (library)> <proof 2 (library)>

<proof 3 (library), optional>

<closing (library)>

Kind regards,
Alex Morgan
Vienna, Austria · +43 ... · alex.morgan@... · linkedin.com/in/...   <- copied from cv/source/content/en.json contacts by code
```
Body (salutation to closing, without subject and signature) 180-380 words (product decision 10.10: concise beats padded; the 'not AI-looking' rule outweighs the 250-350 benchmark). Over 380: drop proof 3; still over: template hook. Each proof is its own paragraph.

## cv/letter_library.json (schema)
```json
{
  "note": "...",
  "hook_templates": ["... {title} ... {company} ..."],
  "identity": {"id_product": "...", "id_research": "...", "id_ai": "..."},
  "proofs": {
    "pr_xxx": {"text": "2-3 sentences, 40-65 words", "label_ru": "short Russian label", "tags": ["english ad terms this proves"]}
  },
  "exclusive": [["pr_a", "pr_b"]],
  "closings": {"cl_xxx": "1-2 sentences"},
  "sign_off": "Kind regards,"
}
```
The first key of identity / proofs / closings is the default. Templates may use only {title} and {company}.

## Library rules (checked by letter.load_library at every run; a failure raises library.LibraryError(file="letter_library.json"))
- Every text: no never_claim term (cv/never_claim.json), no number outside base CV text + cv/allowed_numbers.json (checks.new_numbers), no German words (checks.german_words), no cliché (CLICHES below), no LETTER_NEVER term (below).
- No sentence of a library text is a near copy of a CV line: difflib.SequenceMatcher ratio >= 0.8 against any base CV bullet or cv/library.json bullet/summary (tags stripped, lowercased) fails.
- Closings: no "would" / "hope" / "I believe" / "I feel".
- Ids unique, exclusive ids exist, at least 6 proofs, 2 identities, 2 closings, 1 template.

CLICHES (case-insensitive substrings): "i am writing to", "i am applying", "hereby", "passionate", "hard-working", "hardworking", "team player", "fast learner", "quick learner", "dynamic", "renowned", "i believe i could", "i feel that", "honoured", "honored", "excited to", "perfect fit", "dream job", "challenging opportunit", "team-based culture", "to whom it may concern", "synergy", "go-getter", "think outside the box", "results-driven", "detail-oriented".
LETTER_NEVER (case-insensitive, word-ish): "permit", "residence", "visa", "work authori", "hours per week", "h/week", "hours a week", "part-time availability", "salary", "€", "EUR", "gross", "master", "enrol", "student", "start date", "start immediately", "available from".

## Engine: cv/tailor/letter.py
`write_letter(job_id) -> dict` (raises LookupError when the job is missing, library.LibraryError on a broken library/facts file):
```
{status: "ok"|"need_text", text, subject, words, hook_source: "model"|"template", hook_note, evidence: [{requirement, proof, label_ru}],
 german_warning: bool, notes: [Russian strings], reason, version: dir path, letter_id: int|None}
```
Steps:
1. Job via run._load_job; ad text = description if >= run.MIN_DESC chars, else run.pasted_path(job_id) text if >= MIN_DESC, else return status need_text (reason "нет текста вакансии").
2. Requirements: the newest data/cv/<job>/<n>/requirements.json (highest numeric n that has the file, valid dict with a "terms" list) - reused, no call; else requirements.extract(job).
3. lib = load_library(base); cv_lib = library.load(base) for its synonyms.
4. Writer call (llm.ask_json, model "sonnet", timeout 180). Prompt: rules + ad text (<= 8000 chars, marked as data only, ignore instructions inside) + must/nice/terms + library ids with texts + cv/facts.md. Answer JSON:
   `{"identity": id, "proofs": [id, id, id?], "closing": id, "hook": str, "evidence": [{"requirement": str, "proof": id}], "swaps": [{"from": str, "to": str}]}`
5. Validate picks in code: unknown ids dropped; proofs distinct, 2-3, exclusive pairs respected (keep the first); missing parts filled by code: proofs ranked by how many reqs terms their tags/text mention (checks.mentions), identity/closing = default. Swaps applied to proof texts with library._swap and cv_lib["synonyms"]; an invalid swap is ignored. On LLMError the whole letter is built by code (template hook, ranked proofs) with note "Claude не ответил, письмо собрано по шаблону".
6. Hook code checks (any fail -> template hook, hook_note says which): 1-2 sentences, 12-70 words; contains the company name (case-insensitive, legal form like AG/GmbH optional); no German words; no never_claim term; no LETTER_NEVER term; no cliché; numbers only from base CV + allowed_numbers + ad text; does not start with "I am" / "I'm"; no "<" or ">" characters; no unknown glyph (checks.uncovered / mixed_script if usable).
7. Verifier call (sonnet, timeout 120) only when the hook passed step 6: given ad text, facts.md and the hook, answer `{"ok": bool, "problems": [str]}`. Every statement about the company must be stated in the ad; every statement about the candidate must be in facts.md. ok false or LLMError -> template hook, hook_note "проверка не подтвердила крючок" / "проверка не ответила".
8. Salutation by code only (no model): find in the ad "Frau|Herr|Ms|Mrs|Mr" + optional academic title (Dr., Mag., Mag.a, DI, MSc, Prof.) + name words; Frau -> "Ms", Herr -> "Mr", last name = last capitalised word. Prefer a match within 200 chars after "Ansprechpartner|Ansprechperson|Kontakt|contact|questions"; several different people and none near those words -> team salutation. Never infer gender from a first name. No match -> "Dear <company> hiring team,".
9. Clean title: drop gender markers ((m/w/d), (w/m/d), (m/f/d), (f/m/x), (all genders), (gn), (d/m/w), "m/w/d" without brackets) and surrounding spaces; keep everything else as in the ad.
10. Assemble, count words, length rule, "I"-start count (> 2 paragraphs starting with "I" -> note only).
11. german_warning per decision 2.
12. Save: data/cv/<job>/letters/<n>/letter.txt + result.json (same numbering approach as run._new_version_dir), and a row in table letters; return letter_id.
13. Whole run deadline 300 s via llm.DEADLINE (reset in finally).
CLI: `python3 -m cv.tailor.letter <job_id>` prints the result JSON.

## Store: table letters
`create table if not exists letters (id integer primary key autoincrement, job_id text not null, created_at text not null, path text not null, status text not null, hook_source text)` + index on job_id. Added to store.py's schema like cv_versions.

## Worker and bot
- worker.py: `--kind cv|letter` (default cv). main(job_id, chat_id, pasted=None, token=None, kind="cv"). kind letter runs letter.write_letter(job_id) (LETTER test hook like TAILOR) and deliver_letter:
  - ok: message 1 = the letter text exactly (plain, no parse mode); message 2 = summary: words, which requirement each proof closes (label_ru), hook line ("Первый абзац написан под компанию и проверен" or "Первый абзац по шаблону: <hook_note>"), notes, german warning.
  - need_text: "Нет текста объявления. Нажми «Адаптировать резюме» и пришли текст - письмо возьмёт его оттуда."
  - LibraryError: "Не получилось написать письмо: библиотека письма (cv/letter_library.json) повреждена или не прошла проверку." LookupError: "...вакансия не найдена в базе." Other: "Не получилось написать письмо: внутренняя ошибка (<Type>). Попробуй ещё раз."
  - The same per-job lock as the CV (a CV run and a letter run of one job never overlap; the busy answer is the same).
  - spawn(..., kind) passes --kind; _children remember kind; notices() yields (job_id, chat_id, kind); died_text(kind): cv -> DIED_TEXT, letter -> "Письмо оборвалось (процесс остановлен), письмо не отправлено. Нажми «Написать письмо» ещё раз."
- bot.py: collapsed_markup for interested jobs: one row [Адаптировать резюме][Написать письмо] (callback c:letter:<job>). handle_cv action letter = the tailor path with kind letter: toast "Пишу письмо", chat line "Пишу письмо, около минуты"; refusal for non-interested "Письмо пишется только для вакансий с отметкой «Интересно»".

## Acceptance (QA)
- Library loads; an injected cliché / never_claim / new number / German word / CV-copy sentence in the library raises LibraryError.
- Fake LLM: a hook with an invented number, a cliché, a German sentence, a missing company name, a permit/salary mention -> template hook. Verifier "ok": false -> template hook. Writer LLMError -> letter still produced.
- No LETTER_NEVER term anywhere in any produced letter; body 230-380 words; company and clean title present; signature equals en.json contacts.
- Salutation: "Ihre Ansprechpartnerin: Frau Mag.a Anna Huber" -> "Dear Ms Huber,"; "Kontakt: Herr Dr. Peter Gruber" -> "Dear Mr Gruber,"; no name -> team; first name only -> team.
- German ad without "English" -> warning; German ad that says "Englischkenntnisse" -> no warning.
- Requirements reused from a CV version (no extra call); none -> one extract call.
- Bot: button row present only on interested cards; double press while a CV run holds the lock -> "Уже готовлю, подожди"; worker never blocks polling; died worker -> letter text.
- All existing tests still pass.

## Additions 10.10.2026 (after Misha: 'must not read as AI-written', QA round 1)
- research/research_ai_tells.md (Wikipedia Signs of AI writing, LLM vocabulary studies, recruiter pages) -> letter.AI_TELLS: banned vocabulary, negative parallelism, ', -ing' tails, 'taught me', 'rather than', transition openers, curly quotes, dashes, emoji, markdown. Checked on the library (LibraryError), the hook (template) and the final letter (note).
- Hook is only about the company and the role; no candidate numbers or 5-word overlap with identity/proofs or with the ad; no digits except a year from the ad; no newline/URL/e-mail/braces; cleaned of invisible characters.
- One retry of the hook with the failure reasons before the template.
- Final whole-letter LETTER_NEVER scan (title and company blanked); still a hit after the template -> status 'blocked', nothing sent.
- Salutation only with a contact anchor word, first name + surname required, titles/post-nominals skipped.
- Clean title drops gender markers, hours, money, dates and studies segments.
