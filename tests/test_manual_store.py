"""Stage 8 (manual applications) in store.py: Misha adds an application he already sent. Every test runs on a temp DB
with the real schema; add_manual and remove_manual do not commit, so the tests commit or read through the same
connection, exactly like the bot and the seed script do."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import store  # noqa: E402

ACME = {"company": "Acme Labs", "title": "Brand & Marketing Intern",
             "url": "https://acme.example/careers/brand-marketing-intern"}
APPLIED = "2026-10-15T12:00:00"


def days_ago(n):
    return (datetime.now() - timedelta(days=n)).isoformat(timespec="seconds")


class Db(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(store, "DB", Path(self.dir.name) / "data" / "state.db")
        self.patch.start()
        self.con = store.connect()

    def tearDown(self):
        self.con.close()
        self.patch.stop()
        self.dir.cleanup()

    def add(self, jid, company, title, url="", first_seen=None, sent=False, verdict=None, source="company",
            status=None, applied_at=None, data=None, key=None, closed_by=None, dup_of=None):
        job = {"company": company, "title": title, "url": url}
        self.con.execute("insert into jobs (id, key, company, title, url, source, first_seen, sent_at, verdict,"
                         " status, applied_at, data, closed, closed_by, dup_of)"
                         " values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (jid, key or store.dedupe_key(job), company, title, url, source, first_seen or days_ago(2),
                          "2026-10-08T15:05:48" if sent else None, verdict, status, applied_at,
                          json.dumps(data or job, ensure_ascii=False), 1 if closed_by else 0, closed_by, dup_of))
        self.con.commit()

    def row(self, jid):
        return self.con.execute("select * from jobs where id=?", (jid,)).fetchone()

    def log(self, jid):
        return [tuple(r) for r in self.con.execute("select status, at from status_log where job_id=? order by rowid",
                                                   (jid,))]

    def count(self, where="1=1"):
        return self.con.execute(f"select count(*) from jobs where {where}").fetchone()[0]


class Create(Db):
    def test_create_makes_a_manual_row_that_counts_as_interested_and_sent(self):
        jid, outcome = store.add_manual(self.con, ACME, APPLIED, channel="email + CV", note="chat")
        self.con.commit()
        self.assertEqual(outcome, "created")
        r = self.row(jid)
        self.assertEqual((r["source"], r["rule"], r["verdict"], r["reasons"]), ("manual", "manual", "interested", "[]"))
        self.assertEqual((r["status"], r["status_at"], r["applied_at"]), ("applied", APPLIED, APPLIED))
        self.assertIsNotNone(r["sent_at"])
        self.assertEqual((r["closed"], r["closed_by"], r["score"], r["scored_at"]), (0, None, None, None))
        self.assertEqual(r["key"], store.dedupe_key(ACME))
        data = json.loads(r["data"])
        self.assertEqual((data["manual"], data["channel"], data["note"], data["url"]),
                         (True, "email + CV", "chat", ACME["url"]))
        self.assertEqual(self.log(jid), [("applied", APPLIED)])

    def test_ember_replied_logs_the_applied_day_and_the_later_status(self):
        job = {"company": "Ember Studio", "title": "Open application", "url": "https://www.ember.example/"}
        jid, _ = store.add_manual(self.con, job, "2026-10-15T12:00:00", status="replied", status_at="2026-10-16T12:00:00")
        self.con.commit()
        r = self.row(jid)
        self.assertEqual((r["status"], r["status_at"], r["applied_at"]), ("replied", "2026-10-16T12:00:00",
                                                                           "2026-10-15T12:00:00"))
        self.assertEqual(self.log(jid), [("applied", "2026-10-15T12:00:00"), ("replied", "2026-10-16T12:00:00")])

    def test_same_key_again_is_unchanged_and_adds_no_row(self):
        jid, _ = store.add_manual(self.con, ACME, APPLIED)
        self.con.commit()
        again, outcome = store.add_manual(self.con, ACME, APPLIED)
        self.assertEqual((again, outcome), (jid, "unchanged"))
        self.assertEqual(self.count("source='manual'"), 1)

    def test_day_strings_become_noon_for_a_past_day_and_now_for_today(self):
        self.assertEqual(store.manual_datetime("2026-10-15"), "2026-10-15T12:00:00")
        today = datetime.now().strftime("%Y-%m-%d")
        self.assertTrue(store.manual_datetime(today).startswith(today))
        self.assertNotEqual(store.manual_datetime(today), f"{today}T12:00:00")
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        with self.assertRaises(ValueError):
            store.manual_datetime(tomorrow)
        with self.assertRaises(ValueError):
            store.manual_datetime("15.10.2026")  # ten characters, not a day

    def test_unknown_status_is_refused(self):
        with self.assertRaises(ValueError):
            store.add_manual(self.con, ACME, APPLIED, status="hired")


class Attach(Db):
    def test_attach_turns_a_rejected_bot_card_into_an_application(self):
        self.add("card", "Acme Labs", "Brand & Marketing Intern", url=ACME["url"], sent=True, verdict="rejected")
        jid, outcome = store.add_manual(self.con, ACME, APPLIED, attach_to="card")
        self.con.commit()
        self.assertEqual((jid, outcome), ("card", "attached"))
        r = self.row("card")
        self.assertEqual((r["verdict"], r["reasons"], r["status"], r["applied_at"]),
                         ("interested", "[]", "applied", APPLIED))
        self.assertEqual(json.loads(r["data"])["manual_applied"], True)
        self.assertEqual(self.count("source='manual'"), 0)
        self.assertEqual(self.log("card"), [("applied", APPLIED)])

    def test_attach_to_a_row_with_a_status_is_unchanged_and_sets_no_flag(self):
        """Misha set the status himself on the bot card: nothing is written, so remove_manual cannot clear it."""
        self.add("card", "Acme Labs", "Brand & Marketing Intern", sent=True, verdict="interested",
                 status="interview", applied_at="2026-10-10T12:00:00")
        before = dict(self.row("card"))
        self.assertEqual(store.add_manual(self.con, ACME, "2026-10-05T12:00:00", status="applied",
                                          attach_to="card"), ("card", "unchanged"))
        self.con.commit()
        r = self.row("card")
        self.assertEqual((r["status"], r["applied_at"], r["verdict"]), ("interview", "2026-10-10T12:00:00", "interested"))
        self.assertNotIn("manual_applied", json.loads(r["data"]))
        self.assertEqual(r["verdict_at"], before["verdict_at"])
        self.assertEqual(self.log("card"), [])

    def test_remove_manual_refuses_a_status_misha_set_himself(self):
        self.add("card", "Acme Labs", "Brand & Marketing Intern", sent=True, verdict="interested", status="replied",
                 applied_at="2026-10-10T12:00:00")
        store.add_manual(self.con, ACME, APPLIED, attach_to="card")
        self.con.commit()
        self.assertFalse(store.remove_manual(self.con, "card"))
        self.con.commit()
        r = self.row("card")
        self.assertEqual((r["status"], r["applied_at"]), ("replied", "2026-10-10T12:00:00"))
        self.assertEqual(self.log("card"), [])

    def test_attach_keeps_an_earlier_applied_at_already_on_a_row_without_status(self):
        self.add("card", "Acme Labs", "Brand & Marketing Intern", sent=True, applied_at="2026-10-01T12:00:00")
        self.assertEqual(store.add_manual(self.con, ACME, APPLIED, attach_to="card"), ("card", "attached"))
        self.con.commit()
        self.assertEqual((self.row("card")["status"], self.row("card")["applied_at"]),
                         ("applied", "2026-10-01T12:00:00"))

    def test_attach_twice_is_unchanged_the_second_time(self):
        self.add("card", "Acme Labs", "Brand & Marketing Intern", sent=True)
        self.assertEqual(store.add_manual(self.con, ACME, APPLIED, attach_to="card"), ("card", "attached"))
        self.con.commit()
        self.assertEqual(store.add_manual(self.con, ACME, APPLIED, attach_to="card"), ("card", "unchanged"))

    def test_one_exact_candidate_is_attached_without_being_asked(self):
        self.add("card", "Acme Labs", "Brand & Marketing Intern", url="https://acme.example/x", sent=True,
                 verdict="interested")
        jid, outcome = store.add_manual(self.con, ACME, APPLIED)
        self.con.commit()
        self.assertEqual((jid, outcome), ("card", "attached"))
        self.assertEqual(self.count(), 1)

    def test_attach_to_a_missing_row_is_refused(self):
        with self.assertRaises(ValueError):
            store.add_manual(self.con, ACME, APPLIED, attach_to="nope")


class Removal(Db):
    def test_remove_closes_a_manual_row_for_good(self):
        jid, _ = store.add_manual(self.con, ACME, APPLIED)
        self.con.commit()
        self.assertTrue(store.remove_manual(self.con, jid))
        self.con.commit()
        r = self.row(jid)
        self.assertEqual((r["closed"], r["closed_by"], r["verdict"], r["status"], r["applied_at"], r["status_at"]),
                         (1, "manual_removed", None, None, None, None))
        self.assertEqual(self.log(jid)[-1][0], "removed")
        self.assertFalse(store.remove_manual(self.con, jid))

    def test_remove_on_an_attached_card_keeps_its_interested_mark(self):
        self.add("card", "Acme Labs", "Brand & Marketing Intern", sent=True, verdict="interested")
        store.add_manual(self.con, ACME, APPLIED, attach_to="card")
        self.con.commit()
        self.assertTrue(store.remove_manual(self.con, "card"))
        self.con.commit()
        r = self.row("card")
        self.assertEqual((r["verdict"], r["status"], r["applied_at"], r["status_at"]), ("interested", None, None, None))
        self.assertNotIn("manual_applied", json.loads(r["data"]))
        self.assertEqual(self.log("card")[-1][0], "reset")

    def test_revive_reopens_a_removed_manual_row(self):
        jid, _ = store.add_manual(self.con, ACME, APPLIED)
        self.con.commit()
        sent_at = self.row(jid)["sent_at"]
        store.remove_manual(self.con, jid)
        self.con.commit()
        again, outcome = store.add_manual(self.con, ACME, "2026-10-20T12:00:00", status="interview",
                                          status_at="2026-10-22T12:00:00")
        self.con.commit()
        self.assertEqual((again, outcome), (jid, "revived"))
        r = self.row(jid)
        self.assertEqual((r["closed"], r["closed_by"], r["verdict"], r["status"]), (0, None, "interested", "interview"))
        self.assertEqual(r["sent_at"], sent_at)
        self.assertEqual(self.count("source='manual'"), 1)


class Helpers(Db):
    def test_is_manual_for_manual_and_attached_rows_only(self):
        jid, _ = store.add_manual(self.con, ACME, APPLIED)
        self.add("card", "Woom", "Product Engineer (f/m/d)", sent=True, verdict="interested")
        store.add_manual(self.con, {"company": "Woom", "title": "Product Engineer (f/m/d)", "url": ""}, APPLIED,
                         attach_to="card")
        self.con.commit()
        self.assertTrue(store.is_manual(self.row(jid)))
        self.assertTrue(store.is_manual(self.row("card")))
        self.add("plain", "Other", "Analyst")
        self.assertFalse(store.is_manual(self.row("plain")))

    def test_not_manual_fragment_leaves_manual_rows_out_of_counts(self):
        self.assertEqual(store.NOT_MANUAL, "coalesce(source,'')!='manual'")
        store.add_manual(self.con, ACME, APPLIED)
        self.con.commit()
        self.assertEqual(self.count("verdict='interested' and " + store.NOT_MANUAL), 0)
        self.assertEqual(self.count("verdict='interested'"), 1)


class Duplicates(Db):
    def test_remember_does_not_overwrite_a_manual_row_on_the_same_link(self):
        """A.6: an alert with the same job number under another title must not rename the manual row."""
        job = {"company": "UNIDO", "title": "Intern (Climate Innovation)",
               "url": "https://careers.unido.org/job/Vienna-Intern-Climate/1370852755/"}
        jid, _ = store.add_manual(self.con, job, APPLIED)
        self.con.commit()
        alert = {"company": "UNIDO", "title": "Intern (A2D Facility)", "source": "company",
                 "url": "https://careers.unido.org/job/Vienna-Intern-A2D/1370852755/"}
        before = self.count()
        self.assertFalse(store.remember(self.con, alert))
        self.con.commit()
        self.assertEqual(alert["id"], jid)
        self.assertEqual(self.count(), before)  # the alert creates no row
        r = self.row(jid)
        self.assertEqual((r["title"], r["url"]), ("Intern (Climate Innovation)", job["url"]))

    def test_a_later_alert_with_the_same_key_creates_no_row(self):
        jid, _ = store.add_manual(self.con, ACME, APPLIED)
        self.con.commit()
        alert = {"company": "Acme Labs", "title": "Brand & Marketing Intern (m/w/d)", "source": "linkedin",
                 "url": "https://www.linkedin.com/jobs/view/4479863199/"}
        self.assertFalse(store.remember(self.con, alert))
        self.con.commit()
        self.assertEqual(alert["id"], jid)
        self.assertEqual(self.count(), 1)

    def test_an_unsent_scored_copy_is_closed_after_add_manual(self):
        """The copy has no verdict and was never sent; the manual row counts as sent, so it is the original."""
        self.add("copy", "CHECK24 Vergleichsportal Österreich", "Internship - Product Management (all genders)",
                 url="https://jobs.check24.de/de/jobs/produkt-projektmanagement/"
                     "ref669u-internship-product-management-all-genders?locations=vienna")
        job = {"company": "CHECK24", "title": "Internship - Product Management (all genders)",
               "url": "https://www.linkedin.com/jobs/view/4479863121/"}
        jid, outcome = store.add_manual(self.con, job, APPLIED)
        self.con.commit()
        self.assertEqual(outcome, "created")
        self.assertEqual(self.row("copy")["closed_by"], "duplicate")
        self.assertEqual(self.row("copy")["dup_of"], jid)

    def test_a_removed_manual_row_is_never_the_original(self):
        jid, _ = store.add_manual(self.con, ACME, APPLIED)
        self.con.commit()
        store.remove_manual(self.con, jid)
        self.con.commit()
        self.add("copy", "Acme Labs Labs", "Brand & Marketing Intern", url="https://x.at/copy")  # its own key
        self.assertIsNone(store.mark_if_duplicate(self.con, "copy"))
        self.assertEqual(self.row("copy")["closed"], 0)


class Queues(Db):
    def test_manual_rows_stay_out_of_the_scoring_window_and_the_showcase(self):
        """Acceptance: a manual row is never scored, never a card, never in the витрина (bot re-send list)."""
        jid, _ = store.add_manual(self.con, ACME, APPLIED)
        self.add("card", "Woom", "Product Engineer (f/m/d)", sent=True, verdict="interested")
        store.add_manual(self.con, {"company": "Woom", "title": "Product Engineer (f/m/d)", "url": ""}, APPLIED,
                         attach_to="card")
        self.con.commit()
        cond, params = store.in_window()
        in_queue = {r[0] for r in self.con.execute(f"select id from jobs where {cond}", params)}
        self.assertNotIn(jid, in_queue)
        self.assertNotIn("card", in_queue)
        self.assertNotIn(jid, {r["id"] for r in store.vitrina(self.con)})
        self.assertNotIn("card", {r["id"] for r in store.vitrina(self.con)})


class Candidates(Db):
    def test_exact_key_then_link_then_similar_and_no_removed_or_duplicate_rows(self):
        self.add("exact", "Acme Labs", "Brand & Marketing Intern", url="https://acme.example/jobs/1")
        self.add("link", "Acme", "Intern", url="https://careers.unido.org/job/A/1370852755/")
        self.add("similar", "Acme Labs GmbH", "Brand and Marketing Intern Vienna", url="https://x.at/y")
        removed, _ = store.add_manual(self.con, {"company": "Acme Labs", "title": "Brand Removed Intern",
                                                 "url": ""}, APPLIED)
        store.remove_manual(self.con, removed)
        self.add("orig", "Acme Labs", "Analyst", url="https://acme.example/jobs/9", sent=True)
        self.add("dup", "Acme Labs", "Brand Marketing Intern Copy", url="https://x.at/copy",
                 closed_by="duplicate", dup_of="orig")
        self.con.commit()
        hits = store.find_application_candidates(self.con, "Acme Labs", "Brand & Marketing Intern",
                                                 "https://careers.unido.org/job/B/1370852755/")
        ids = [r["id"] for r in hits]
        self.assertEqual(ids, ["exact", "link", "similar"])
        self.assertNotIn(removed, ids)
        self.assertNotIn("dup", ids)

    def test_a_duplicate_hit_counts_as_its_original(self):
        url = "https://careers.unido.org/job/A/1370852755/"
        self.add("orig", "Acme Labs", "Analyst", url=url, sent=True)
        self.add("dup", "Other Co", "Analyst copy", url=url, closed_by="duplicate", dup_of="orig")
        self.con.commit()
        self.assertEqual([r["id"] for r in store.exact_application_candidates(
            self.con, "Nobody", "Nothing", "https://careers.unido.org/job/B/1370852755/")], ["orig"])


class ForceNew(Db):
    """The bot's 'Новая' button: add_manual(..., force_new=True) and is_exact_candidate for the prompt."""

    def test_force_new_with_no_key_row_creates_even_when_a_link_candidate_exists(self):
        self.add("linked", "Acme", "Intern", url="https://careers.unido.org/job/A/1370852755/")
        self.con.commit()
        job = {"company": "UNIDO", "title": "Intern (A2D Facility)", "url": "https://careers.unido.org/job/B/1370852755/"}
        jid, outcome = store.add_manual(self.con, job, APPLIED, force_new=True)
        self.con.commit()
        self.assertEqual(outcome, "created")
        self.assertNotEqual(jid, "linked")
        self.assertEqual((self.row("linked")["verdict"], self.row("linked")["closed"]), (None, 0))

    def test_without_force_the_same_link_candidate_is_attached(self):
        """Contrast for the test above: the default still attaches to the one link candidate."""
        self.add("linked", "Acme", "Intern", url="https://careers.unido.org/job/A/1370852755/")
        self.con.commit()
        job = {"company": "UNIDO", "title": "Intern (A2D Facility)", "url": "https://careers.unido.org/job/B/1370852755/"}
        self.assertEqual(store.add_manual(self.con, job, APPLIED), ("linked", "attached"))

    def test_force_new_never_closes_the_different_job_Misha_just_named(self):
        """No mark_if_duplicate after a forced create: the unsent copy the owner called a different job stays open."""
        self.add("copy", "CHECK24 Vergleichsportal Österreich", "Internship - Product Management (all genders)",
                 url="https://jobs.check24.de/de/jobs/produkt-projektmanagement/"
                     "ref669u-internship-product-management-all-genders?locations=vienna")
        self.con.commit()
        job = {"company": "CHECK24", "title": "Internship - Product Management (all genders)",
               "url": "https://www.linkedin.com/jobs/view/4479863121/"}
        jid, outcome = store.add_manual(self.con, job, APPLIED, force_new=True)
        self.con.commit()
        self.assertEqual(outcome, "created")
        self.assertEqual((self.row("copy")["closed"], self.row("copy")["closed_by"]), (0, None))

    def test_force_new_with_an_exact_key_row_inserts_nothing_and_attaches(self):
        self.add("row", "Acme Labs", "Brand & Marketing Intern", sent=True)
        self.con.commit()
        jid, outcome = store.add_manual(self.con, ACME, APPLIED, force_new=True)
        self.con.commit()
        self.assertEqual((jid, outcome), ("row", "attached"))
        self.assertEqual(self.count(), 1)

    def test_force_new_with_an_active_manual_row_of_the_same_key_is_unchanged(self):
        jid, _ = store.add_manual(self.con, ACME, APPLIED)
        self.con.commit()
        self.assertEqual(store.add_manual(self.con, ACME, APPLIED, force_new=True), (jid, "unchanged"))
        self.assertEqual(self.count(), 1)

    def test_force_new_after_a_removal_is_a_new_row_not_a_revive(self):
        """A removed row no longer holds its key (S8-5), so the forced create inserts; the bot never offers it anyway."""
        jid, _ = store.add_manual(self.con, ACME, APPLIED)
        self.con.commit()
        store.remove_manual(self.con, jid)
        self.con.commit()
        new_id, outcome = store.add_manual(self.con, ACME, APPLIED, force_new=True)
        self.con.commit()
        self.assertEqual(outcome, "created")
        self.assertNotEqual(new_id, jid)
        self.assertEqual(self.row(jid)["closed_by"], "manual_removed")

    def test_force_new_and_attach_to_together_are_refused(self):
        self.add("row", "Acme Labs", "Analyst")
        with self.assertRaises(ValueError):
            store.add_manual(self.con, ACME, APPLIED, attach_to="row", force_new=True)

    def test_is_exact_candidate_is_true_only_for_the_exact_key_original(self):
        self.add("exact", "Acme Labs", "Brand & Marketing Intern", url="https://acme.example/jobs/1")
        self.add("link", "Acme", "Intern", url="https://careers.unido.org/job/A/1370852755/")
        self.add("similar", "Acme Labs GmbH", "Brand and Marketing Intern Vienna", url="https://x.at/y")
        self.add("orig", "Acme Labs", "Analyst", url="https://acme.example/jobs/9", sent=True)
        self.add("dup", "Other Co", "Analyst copy", url="https://acme.example/jobs/9", closed_by="duplicate",
                 dup_of="orig")
        self.con.commit()
        cands = {r["id"]: r for r in store.find_application_candidates(
            self.con, "Acme Labs", "Brand & Marketing Intern", "https://careers.unido.org/job/B/1370852755/")}
        self.assertTrue(store.is_exact_candidate(self.con, "Acme Labs", "Brand & Marketing Intern", cands["exact"]))
        self.assertFalse(store.is_exact_candidate(self.con, "Acme Labs", "Brand & Marketing Intern", cands["link"]))
        self.assertFalse(store.is_exact_candidate(self.con, "Acme Labs", "Brand & Marketing Intern", cands["similar"]))
        self.assertTrue(store.is_exact_candidate(self.con, "Acme Labs", "Analyst", self.row("dup")))


class Round3(Db):
    """S8-2 (verdict back on removal), S8-5 (a removed manual row no longer holds its key), S8-7 (caps)."""

    def test_remove_on_an_attached_card_restores_the_verdict_it_had(self):
        self.add("card", "Acme Labs", "Brand & Marketing Intern", sent=True)
        self.con.execute("update jobs set verdict='rejected', reasons='[\"bad fit\"]', verdict_at='2026-10-01T09:00:00'"
                         " where id='card'")
        self.con.commit()
        store.add_manual(self.con, ACME, APPLIED, attach_to="card")
        self.con.commit()
        self.assertEqual(self.row("card")["verdict"], "interested")
        self.assertEqual(json.loads(self.row("card")["data"])["pre_attach"],
                         {"verdict": "rejected", "reasons": '["bad fit"]', "verdict_at": "2026-10-01T09:00:00"})
        self.assertTrue(store.remove_manual(self.con, "card"))
        self.con.commit()
        r = self.row("card")
        self.assertEqual((r["verdict"], r["reasons"], r["verdict_at"], r["status"]),
                         ("rejected", '["bad fit"]', "2026-10-01T09:00:00", None))
        self.assertNotIn("pre_attach", json.loads(r["data"]))

    def test_removed_manual_row_does_not_absorb_a_later_alert_with_the_same_key(self):
        jid, _ = store.add_manual(self.con, ACME, APPLIED)
        self.con.commit()
        store.remove_manual(self.con, jid)
        self.con.commit()
        alert = {"company": "Acme Labs", "title": "Brand & Marketing Intern (m/w/d)", "source": "linkedin",
                 "url": "https://www.linkedin.com/jobs/view/4479863199/"}
        self.assertTrue(store.remember(self.con, alert))
        self.con.commit()
        self.assertNotEqual(alert["id"], jid)
        self.assertIsNone(self.row(alert["id"])["verdict"])  # the alert is a card again
        self.assertEqual(self.row(alert["id"])["closed"], 0)

    def test_removed_manual_row_does_not_absorb_a_later_alert_with_the_same_link(self):
        job = {"company": "UNIDO", "title": "Intern (Climate Innovation)",
               "url": "https://careers.unido.org/job/Vienna-Intern-Climate/1370852755/"}
        jid, _ = store.add_manual(self.con, job, APPLIED)
        self.con.commit()
        store.remove_manual(self.con, jid)
        self.con.commit()
        alert = {"company": "UNIDO", "title": "Intern (A2D Facility)", "source": "company",
                 "url": "https://careers.unido.org/job/Vienna-Intern-A2D/1370852755/"}
        self.assertTrue(store.remember(self.con, alert))
        self.con.commit()
        self.assertNotEqual(alert["id"], jid)

    def test_add_manual_again_after_a_removal_revives_it_and_restores_its_key(self):
        jid, _ = store.add_manual(self.con, ACME, APPLIED)
        self.con.commit()
        store.remove_manual(self.con, jid)
        self.con.commit()
        self.assertEqual(store.add_manual(self.con, ACME, APPLIED), (jid, "revived"))
        self.con.commit()
        r = self.row(jid)
        self.assertEqual((r["key"], r["closed"], r["verdict"]), (store.dedupe_key(ACME), 0, "interested"))
        self.assertNotIn("removed_key", json.loads(r["data"]))

    def test_add_manual_after_an_alert_took_the_key_attaches_to_the_alert_row(self):
        jid, _ = store.add_manual(self.con, ACME, APPLIED)
        self.con.commit()
        store.remove_manual(self.con, jid)
        self.con.commit()
        alert = {"company": "Acme Labs", "title": "Brand & Marketing Intern", "source": "linkedin",
                 "url": "https://www.linkedin.com/jobs/view/4479863199/"}
        store.remember(self.con, alert)
        self.con.commit()
        self.assertEqual(store.add_manual(self.con, ACME, APPLIED), (alert["id"], "attached"))
        self.con.commit()
        self.assertEqual(self.row(jid)["closed_by"], "manual_removed")

    def test_the_rekey_leaves_a_removed_row_on_its_moved_key(self):
        jid, _ = store.add_manual(self.con, ACME, APPLIED)
        self.con.commit()
        store.remove_manual(self.con, jid)
        self.con.commit()
        store.rekey(self.con)
        self.con.commit()
        self.assertNotEqual(self.row(jid)["key"], store.dedupe_key(ACME))

    def test_add_manual_caps_company_title_url_channel_and_note(self):
        job = {"company": "C" * 1000, "title": "T" * 1000, "url": "https://x.at/" + "u" * 3000}
        jid, _ = store.add_manual(self.con, job, APPLIED, channel="h" * 900, note="n" * 900)
        self.con.commit()
        r = self.row(jid)
        self.assertEqual((len(r["company"]), len(r["title"]), len(r["url"])), (200, 300, 2000))
        data = json.loads(r["data"])
        self.assertEqual((len(data["channel"]), len(data["note"]), len(data["url"])), (500, 500, 2000))

    def test_manual_row_for_key_finds_an_active_or_a_removed_row(self):
        jid, _ = store.add_manual(self.con, ACME, APPLIED)
        self.con.commit()
        self.assertEqual(store.manual_row_for_key(self.con, store.dedupe_key(ACME))["id"], jid)
        store.remove_manual(self.con, jid)
        self.con.commit()
        self.assertEqual(store.manual_row_for_key(self.con, store.dedupe_key(ACME))["id"], jid)
        self.assertIsNone(store.manual_row_for_key(self.con, "nobody|nothing"))


if __name__ == "__main__":
    unittest.main()
