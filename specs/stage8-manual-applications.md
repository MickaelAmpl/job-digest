# Этап 8: ручное добавление откликов - план v2 (26.10.2026, ночь, после аудита Sonnet)

Задача Миши (26.10, вечер): "мануальное добавление на те вакансии, на которые я уже откликнулся, чтобы они тоже шли в
статистику. Данные возьми из чата «Поиск AI-компаний в Вене». Подготовь план, проведи аудит и строй командой агентов.
Утром хочу увидеть результат."
Команда: Opus планирует и проверяет, Haiku пишет код, Sonnet проверил план (аудит v1 -> этот v2) и проверит код.
Деньги: $0 сверху. Выкладка вне окна 06:30-07:30.

## Что получится (для Миши)
1. В боте команда /add. Пишешь /add (или сразу "/add Компания / Должность"), бот просит строку
   «Компания / Должность» и, если есть, ссылку на вакансию в том же сообщении.
2. Страницу по ссылке бот в этой версии НЕ открывает (аудит: надёжно достать компанию и должность с любой страницы
   нельзя без риска и задержек кнопок) - ссылка просто сохраняется.
3. Бот показывает, что понял: «Должность · Компания · Отправил резюме: сегодня», кнопки
   [Сохранить] [Вчера] [Другая дата] [Изменить] [Отмена].
4. Если похожая вакансия уже есть в боте, он спросит: «Похоже, это уже есть: ...» [Это она] [Новая].
5. После «Сохранить» это обычная карточка отклика со статусами (Ответили -> Интервью -> Оффер / Отказ / Нет ответа)
   и кнопкой «Это не отправлял» (убирает ошибочный отклик целиком).
6. Отклики видны в /applied, /stats, /report и в итогах недели, с пометкой «вручную». Воронка «карточки -> отмечено ->
   интересно» считает только карточки бота; ручные отклики - отдельной строкой «Добавлено вручную».
7. Если та же вакансия придёт потом из LinkedIn или с сайта компании, второй карточки не будет (точно - при той же
   ссылке или том же названии компании и должности; иначе после оценки Claude она закроется как дубль).
8. Твои уже отправленные отклики загружены сразу - утром они в /applied.

## Уже отправленные отклики (загрузить сразу)
Таблица из 5 реальных откликов владельца убрана из публичной копии.

---

# Developer spec v2 (English)

## A. store.py (Dev E, after the dedupe work)
1. Schema (create in the existing migration path, idempotent): tables
   `manual_requests(asked_msg_id integer primary key, kind text, token text, at text)` (kind: new|date|edit) and
   `manual_drafts(token text primary key, chat text, msg_id integer, created_at text, json text)`.
2. `NOT_MANUAL = "coalesce(source,'')!='manual'"` - SQL fragment for every card/verdict count.
3. `find_application_candidates(con, company, title, url="") -> list[row]`: exact dedupe key; then find_same_link(url)
   when url is set; then rows (not closed_by duplicate/manual_removed) where same_employer(company, row.company) and
   similar_titles(title, row.title). Follow dup_of to the original when a hit is a duplicate. Reuse the helpers from
   the dedupe work (same_employer, posting_link...).
4. `add_manual(con, job, applied_at, status="applied", status_at=None, channel="", note="", attach_to=None)
   -> (job_id, outcome)` with outcome "created" | "attached" | "unchanged" | "revived". One transaction.
   - attach_to given (or exactly one exact-key/link candidate): on that row, if verdict != 'interested' set
     verdict='interested', reasons='[]', verdict_at=now(); if status IS NULL set status, status_at, applied_at and add
     status_log rows (applied at applied_at; plus the later status at status_at if not 'applied'); never change a
     non-NULL status; keep an earlier applied_at; set data.manual_applied=true; outcome "attached" or "unchanged".
   - a source='manual' row with closed_by='manual_removed' for the same key: reopen it ("revived").
   - else insert: source='manual', rule='manual', verdict='interested', reasons='[]', verdict_at=now(),
     status, status_at, applied_at, first_seen=now(), sent_at=now(), listed=0, closed=0, score NULL, scored_at NULL,
     key=dedupe_key, id=job_id(key) with the salt loop, data json incl. manual=true, channel, note, url;
     status_log rows as above; then call mark_if_duplicate(con, new_id) so an unsent copy is closed.
   Dates: full ISO datetimes. Today = now(); a past day = "YYYY-MM-DDT12:00:00". Never write via set_status (it stamps
   now()).
5. `remove_manual(con, job_id)`: source='manual' -> closed=1, closed_by='manual_removed', verdict/status/applied_at/
   status_at NULL, status_log 'removed'. For an attached row (data.manual_applied): status/applied_at/status_at NULL,
   data.manual_applied removed, status_log 'reset' - the original Интересно mark stays.
6. remember(): the same-link branch must not overwrite title/url of a source='manual' row.
7. `is_manual(row)` = source=='manual' or data.manual_applied.

## B. bot.py (Dev B, parallel to C, after A)
1. COMMANDS: add ("add", "добавить отклик вручную"). `/add <text>` inline works too.
2. Reply routing in handle_message: after asked_job, before the command chain; a /command still wins; CANCEL_WORDS
   cancel. A reply to a manual_requests.asked_msg_id is consumed (row deleted) and never saved as a note. Fallback: a
   message that is not a reply, while a manual_requests row of kind 'new' newer than 15 min exists, is routed to /add.
3. Parsing: URL = first http(s) token anywhere (stored, never fetched); the rest split into company / title on the first
   newline, " / " or " | ", else on " - "; both parts required (else ask again once, then give up with a hint).
   HTML-escape and cap() everything shown.
4. Confirmation message (store its message_id in the draft): "<b>Title</b> · Company · Отправил резюме: <date>" +
   [Сохранить] [Вчера] [Другая дата] [Изменить] [Отмена]; callback m:<save|yday|date|edit|cancel|this|new|gone>:<token>
   (token uuid4().hex[:10]). m: callbacks are handled BEFORE the generic job-id path in handle_callback.
   Вчера / Другая дата / Изменить only change the draft and re-render the same message. Другая дата: force_reply
   "ДД.ММ", current year, rolled back one year if in the future; a future date is refused.
   If find_application_candidates has hits: the confirmation shows the best hit and [Это она] (attach_to=hit)
   [Новая] (create) instead of [Сохранить]. Drafts expire after 24 h.
5. Save = store.add_manual in one transaction + delete the draft; a second press with no draft answers
   "Уже сохранено". The message becomes the collapsed application card (reuse collapsed_text/collapsed_markup), card
   ids stored like digest cards, so every status button works.
6. Manual cards: no "Адаптировать резюме" / "Написать письмо" (they would create CV/letter versions for an application
   already sent); add [Это не отправлял] (m:gone:<job_id>, asks once to confirm) -> store.remove_manual.
   Mark "(вручную)" in collapsed_text and in applied_list lines when is_manual(row).
7. stats_lines: "Добавлено вручную: N" line.

## C. report.py, digest.py, scripts/metrics.py (Dev C, parallel to B, after A)
- Apply store.NOT_MANUAL at report.py CARDS (:20), marked/interested (:68-69); digest.weekly_summary cards/marked/
  interested (:89-91); scripts/metrics.py db_rows (:88-89) so new/sent/cards/verdicts/closed_new ignore manual rows.
- report.SOURCE_LABELS: 'manual': 'вручную'; mark is_manual rows in report tables; "Добавлено вручную: N" in
  funnel_rows and in weekly_summary. "Отправил резюме" may exceed "Интересно" - expected.

## D. Seed (Dev E, after A): scripts/import_applications.py + research/applications_seed.json
- Fields: company, title, url, applied_at (YYYY-MM-DD), status (default applied), status_at, channel, note,
  existing_id (optional: attach to that row).
- Dry run by default: prints per row created/attached/unchanged and the candidates find_application_candidates sees.
  --apply writes. Idempotent: skips rows whose source='manual' row already exists (even if removed by Misha later) and
  non-manual rows whose status is set or whose status_log has a reset.
- The rows from the table (removed in this public copy).

## Acceptance (tests with a temp DB and a fake Telegram API; all existing tests green)
- A manual row never enters the scoring queue, a digest card, the витрина, the rejected list, feed closures, or
  metrics/funnel card counts; it does appear in /applied, /stats, /report, weekly summary with "(вручную)".
- A later alert with the same link or the same key creates no new row; an unsent scored copy is closed as duplicate.
- Attach never downgrades a status; reset/removal works; revive works; seed is idempotent.
- No network request is made by /add.

---

# Round 3: all QA minors of stage 7 round 2 and stage 8 (27.10.2026, Misha: "исправляй все мелкие находки обоих этапов")
Sources: docs/qa-stage7-wave2b-3.md (section "Round 2 ... New minors") and docs/qa-stage8-manual-applications.md.
Every fix gets a test that fails before it. All existing tests stay green. Code is LIVE; deploy outside 06:30-07:30.

## Dev E - store.py, tests/test_dedupe_wave2.py, tests/test_manual_store.py
S7-1 Employer rule false merges (VIG vs Donau Versicherung, "Austrian" vs "Austrian Power Grid", Siemens vs Siemens
     Energy, Mondi vs Mondi Gronau): the company rule (b) additionally needs evidence that it is one employer: the two
     rows share a registrable domain among their url/open_url (job boards excluded: linkedin, stepstone, adzuna,
     karriere, jobs.at, derstandard, indeed, glassdoor, xing) OR the extra words of the longer name are only
     descriptors (legal forms already stripped, plus: vergleichsportal, bikes, gruppe, group, holding, österreich,
     oesterreich, austria, international, digital, services, solutions, technologies, tech, software, labs, bank only
     if the shorter name is not a group brand). Report which of the 10 snapshot pairs still merge (expected: CHECK24 x3
     via domain, woom, others by evidence) and list the ones lost.
S7-2 Repost suppressed by an old row: the company rule and the link rule only apply when the original is still in
     the window (store.in_window logic) or its first_seen is within 30 days; otherwise the new row is a new posting.
S8-2 remove_manual on an ATTACHED row restores the verdict the row had before the attach: add_manual stores
     data.pre_attach = {verdict, reasons, verdict_at} when it changes a verdict; remove_manual puts it back.
S8-5 A removed manual row must not absorb later alerts forever: remove_manual moves the row's key to
     key + "#removed:" + id (unique) and stores data.removed_key; remember() and find_application_candidates no longer
     see it by key/link; add_manual's revive path finds it via data.removed_key (same company/title) and restores
     the original key if free (else stays a new row). Tests: alert after removal -> new row and a card is possible;
     /add again -> revived.
S8-7 add_manual caps company 200, title 300, url 2000, channel/note 500 (store CAPS style) before storing.

## Dev D - fetch_companies.py, tests/test_wave2_integration.py
S7-3 is_austrian residuals: (a) a big Austrian city (Wien, Vienna, Graz, Linz, Salzburg, Innsbruck) in a comma list
     whose other parts name foreign countries is Austrian ("Wien, Italien", "Wien, Poland", "Wien, England",
     "Wien, Slovakia, Czechia", "Innsbruck, Munich, CH" -> True) EXCEPT US namesakes: any US state name or code
     (Virginia/VA, Illinois/IL, Georgia/GA, Ohio/OH, Maryland/MD, Missouri/MO, West Virginia/WV, New Jersey/NJ,
     New York/NY, South Dakota/SD, Maine/ME, ...), "USA", "United States", "US" makes it foreign ("Vienna, Illinois",
     "Vienna, Georgia", "Vienna, VA" -> False). Keep every existing case. Report changed prod-DB rows (snapshot
     <scratch> read-only).

## Dev B - bot.py, tests/test_bot_manual.py
S8-1 Zombie card: resend_card (k:<id>), refresh_card, status/verdict callbacks refuse a row with
     closed_by='manual_removed' ("Этот отклик убран. Добавить снова: /add").
S8-3 Toasts: keep a short tombstone for finished drafts (saved / cancelled / expired) for 24 h, so a late press says
     "Уже сохранено" / "Черновик отменён" / "Черновик устарел, начни заново: /add" correctly.
S8-6 Routing: (a) a non-reply message that is exactly a CANCEL_WORD while a kind 'new' /add question is open cancels
     it; (b) a reply to an /add question that was already consumed answers "Этот вопрос уже закрыт, начни заново:
     /add" instead of saving a note (keep consumed asks marked, not deleted, for 24 h); (c) the non-reply fallback
     routes only messages with a URL, a newline, " / " or " | " (no longer " - "), within 10 minutes; (d) a date
     outside the current year is shown with its year in the confirmation ("31 декабря 2025").

## Dev C - report.py, digest.py (weekly_summary), tests/test_stats_manual.py
S8-4 "Добавлено вручную" counts every application added by hand: source='manual' (not removed) PLUS bot rows with
     data.manual_applied (json_extract(data,'$.manual_applied')) and a status. /stats and report show the same
     number as the "(вручную)" marks; weekly summary counts the same rows by applied_at in the week.

## Dev F - scripts/refresh_titles.py, scripts/rekey_stale.py, tests/test_refresh_rekey.py (new files only)
S7-4 refresh_titles.py --company <id> [--apply]: for company-feed rows whose URL matches a job in today's
     out/company_jobs.json (or a --feed file), set title to the feed's title (only when it differs; never for rows
     with a verdict... NO: titles are display only - update them for any row, but never touch key/id). Dry run by
     default; prints old -> new. Used once for unicredit-bank-austria (20 rows with code-style titles).
S7-5 rekey_stale.py [--apply]: rows whose stored key != store.dedupe_key(current company/title) get the new key when
     no other row has it; conflicts are listed, not changed; ids never change; rows with closed_by='manual_removed'
     or source='manual' are skipped. Dry run by default. Report the counts on the snapshot DB copy (copy it first).
