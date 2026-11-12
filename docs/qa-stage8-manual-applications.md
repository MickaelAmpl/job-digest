# QA: stage 8 manual applications (26-27.10.2026 night) - verdict GO, 0 blockers, 0 majors

Plan audit (Sonnet) -> spec v2 (specs/stage8-manual-applications.md) -> build (Haiku x3) -> QA (Sonnet). 1076 tests.
Evidence (scratchpad qa8/): seed on a copy of the evening prod DB: dry run 4 create + 1 attach (Driftwood 9c7bd2de10);
--apply 2707 -> 2711 rows, only 9c7bd2de10 changed among old rows; second --apply 5 unchanged. /applied 5 lines
"(вручную)", Ember Studio under Ответили; /stats Отправил резюме 5, Ответили 1, Добавлено вручную 4, cards/Интересно
unchanged. No leaks: queue, digest.pending, vitrina, metrics.db_rows identical before/after; alerts sharing a manual
key/link absorbed; feed closures 0 manual rows. No network in /add (sockets patched to raise). m: callbacks incl.
stale/double/exact vs non-exact candidate/removal/revive OK. Normal cards unchanged (CV/letter buttons, statuses).

## Minor findings (for Misha, not fixed)
1. Zombie card: after "Это не отправлял", a stale k:<id> button from an older /applied list resends the removed row
   as a bare card with Интересно buttons; pressing them brings it back.
2. "Это она" on a "Не интересно" card turns it Интересно; "Это не отправлял" later leaves it Интересно.
3. "Уже сохранено" toast also for a cancelled/expired draft deleted by another press.
4. Counters: /stats "Добавлено вручную: 4" (source manual) while 5 lines carry "(вручную)" (Driftwood attached);
   weekly summary counts by applied_at in the week.
5. A removed manual row absorbs later alerts with the same key/link (vacancy never shown as a card again).
6. Routing edges: plain non-reply "отмена" while an /add question is open is saved as a note (the bot's prompt says
   "или «отмена»"; force_reply usually makes it a reply, which cancels correctly); a second reply to a consumed ask
   becomes a note; a wish with " - " or a link sent within 15 min becomes a draft; "31.12" typed in October becomes
   2025-12-31 and the confirmation shows no year.
7. company/title stored uncapped in jobs (shown capped).

---

# Round 3 (27.10.2026): all 7 stage-8 minors fixed, QA GO, deployed 13:51
(backup /opt/job-digest-backups/2026-10-27-135148; 1129 tests). S8-1 removed rows refused on every old button;
S8-2 pre-attach verdict restored; S8-3 24 h tombstones with exact toasts; S8-4 "Добавлено вручную" = manual + attached
(5 in /stats = 5 marks); S8-5 removed row's key moved (key#removed:<id>), later alerts become new rows, /add revives;
S8-6 cancel word, closed questions, fallback 10 min with URL/newline/" / "/" | ", year shown on other-year dates;
S8-7 caps. Server checks: SQLite 3.45.1 (json_extract/json_valid ok); 0 rows removed under the old code.
New minors (not fixed, cosmetic): stale "Это не отправлял"/"Нет, оставить" on a removed row edits the message to
"Отмечено: ?" (no data change); saved card and /applied show other-year dates without the year; a Save press after
the 24 h tombstone is pruned says "Уже сохранено" even for a cancelled draft.
