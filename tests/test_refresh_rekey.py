"""scripts/refresh_titles.py (S7-4) and scripts/rekey_stale.py (S7-5): the one-off title refresh and stale-key pass.
Every test runs on a temp DB with the real schema; the scripts are called through main() with --db pointing at it."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import store  # noqa: E402
from scripts import refresh_titles as rt  # noqa: E402
from scripts import rekey_stale as rk  # noqa: E402


class Db(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Path(self.dir.name) / "data" / "state.db"
        self.patch = mock.patch.object(store, "DB", self.db)
        self.patch.start()
        self.con = store.connect()

    def tearDown(self):
        self.con.close()
        self.patch.stop()
        self.dir.cleanup()

    def add(self, jid, company, title, url="", key=None, source="company", company_id=None, closed_by=None):
        job = {"company": company, "title": title, "url": url}
        self.con.execute("insert into jobs (id, key, company, title, url, source, company_id, first_seen, closed_by,"
                         " data) values (?,?,?,?,?,?,?,?,?,?)",
                         (jid, key or store.dedupe_key(job), company, title, url, source, company_id,
                          "2026-10-20T10:00:00", closed_by, json.dumps(job, ensure_ascii=False)))
        self.con.commit()

    def snapshot(self):
        """Every row as a tuple, ordered by id: compared before and after a run."""
        return [tuple(r) for r in self.con.execute("select * from jobs order by id").fetchall()]

    def row(self, jid):
        return self.con.execute("select * from jobs where id=?", (jid,)).fetchone()

    def run_quiet(self, module, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            result = module.main(list(argv))
        return result, out.getvalue()


class Refresh(Db):
    def setUp(self):
        super().setUp()
        self.feed = Path(self.dir.name) / "company_jobs.json"
        self.feed.write_text(json.dumps([
            {"company_id": "uc", "url": "https://u.example/1",
             "title": "Praktikant:in (w/m/d) im Bereich Trade Finance"},
            {"company_id": "uc", "url": "https://u.example/2", "title": "Kundenberater:in (w/m/d) - Wien"},
            {"company_id": "other", "url": "https://o.example/1", "title": "Other feed title"},
        ]), encoding="utf-8")
        self.add("a", "UniCredit Bank Austria", "AT-EXT-WP-Praktikum-UCBA-Trade-Finance-DE-2027",
                 url="https://u.example/1", company_id="uc", key="unicredit|old key")
        self.add("b", "UniCredit Bank Austria", "Kundenberater:in (w/m/d) - Wien",
                 url="https://u.example/2", company_id="uc")
        self.add("c", "UniCredit Bank Austria", "Old title, link gone",
                 url="https://u.example/9", company_id="uc")
        self.add("d", "Other", "Other old title", url="https://o.example/1", company_id="other")
        self.add("m", "UniCredit Bank Austria", "Misha's own title", url="https://u.example/1", company_id="uc",
                 source="manual")

    def run_refresh(self, *argv):
        return self.run_quiet(rt, "--company", "uc", "--feed", str(self.feed), "--db", str(self.db), *argv)

    def test_only_the_title_of_matching_company_rows_changes(self):
        before = {t[0]: t for t in self.snapshot()}  # t[0] is the id column
        changes, text = self.run_refresh("--apply")
        self.assertEqual([c["id"] for c in changes], ["a"])
        after = {t[0]: t for t in self.snapshot()}
        self.assertEqual(set(after), set(before))  # ids never change
        names = self.row("a").keys()
        for jid in before:
            if jid == "a":
                changed = {n for n, x, y in zip(names, before[jid], after[jid]) if x != y}
                self.assertEqual(changed, {"title"})
                self.assertEqual(self.row("a")["title"], "Praktikant:in (w/m/d) im Bereich Trade Finance")
                # the key stays, even though the title moved
                self.assertEqual(self.row("a")["key"], "unicredit|old key")
            else:  # matched-but-same, not in the feed, other company, manual: all untouched
                self.assertEqual(after[jid], before[jid], jid)
        self.assertIn("matched 2, changed 1, unchanged 1", text)

    def test_dry_run_writes_nothing(self):
        before = self.snapshot()
        changes, text = self.run_refresh()
        self.assertEqual([c["id"] for c in changes], ["a"])
        self.assertEqual(self.snapshot(), before)
        self.assertIn("nothing written", text)
        self.assertIn("  a  AT-EXT-WP-Praktikum-UCBA-Trade-Finance-DE-2027"
                      " -> Praktikant:in (w/m/d) im Bereich Trade Finance", text)

    def test_second_apply_changes_nothing(self):
        self.run_refresh("--apply")
        after_first = self.snapshot()
        changes, text = self.run_refresh("--apply")
        self.assertEqual(changes, [])
        self.assertEqual(self.snapshot(), after_first)
        self.assertIn("matched 2, changed 0, unchanged 2", text)


class Rekey(Db):
    def run_rekey(self, *argv):
        """(moves, conflicts, text) of one run."""
        (moves, conflicts), text = self.run_quiet(rk, "--db", str(self.db), *argv)
        return moves, conflicts, text

    def test_stale_key_moves_to_the_current_rule_and_the_id_stays(self):
        self.add("a", "Acme", "Senior Data Engineer (m/w/d)", key="old|key")
        before = self.snapshot()
        moves, conflicts, text = self.run_rekey("--apply")
        self.assertEqual([r["id"] for r, _ in moves], ["a"])
        self.assertEqual(self.row("a")["key"], "acme|senior data engineer")
        self.assertEqual(self.row("a")["title"], "Senior Data Engineer (m/w/d)")
        self.assertEqual(len(self.snapshot()), len(before))
        self.assertIn("stale 1, rekeyed 1, conflicts 0, skipped 0 (written: 1)", text)

    def test_conflict_is_listed_and_not_changed(self):
        self.add("a", "Acme", "Data Engineer", key="old|a")
        self.add("b", "Acme", "Data Engineer (m/w/d)", key="acme|data engineer")  # already holds the new key
        moves, conflicts, text = self.run_rekey("--apply")
        self.assertEqual(moves, [])
        self.assertEqual([r["id"] for r, _, _ in conflicts], ["a"])
        self.assertEqual(self.row("a")["key"], "old|a")
        self.assertIn("conflicts 1", text)
        self.assertIn("held by: b Acme | Data Engineer (m/w/d)", text)

    def test_manual_and_removed_rows_are_skipped(self):
        self.add("m", "Acme", "Senior Analyst", key="old|m", source="manual")
        self.add("r", "Beta", "Product Manager", key="old|r", closed_by="manual_removed")
        self.add("ok", "Gamma", "Designer", key="old|ok")
        moves, conflicts, text = self.run_rekey("--apply")
        self.assertEqual([r["id"] for r, _ in moves], ["ok"])
        self.assertEqual(self.row("m")["key"], "old|m")
        self.assertEqual(self.row("r")["key"], "old|r")
        self.assertIn("stale 3, rekeyed 1, conflicts 0, skipped 2", text)

    def test_second_apply_changes_nothing(self):
        self.add("a", "Acme", "Senior Data Engineer (m/w/d)", key="old|a")
        self.add("b", "Acme", "Data Engineer", key="old|b")
        self.add("c", "Acme", "Data Engineer (m/w/d)", key="acme|data engineer")  # b's new key: a conflict for b
        self.run_rekey("--apply")
        after_first = self.snapshot()
        moves, conflicts, text = self.run_rekey("--apply")
        self.assertEqual(moves, [])
        self.assertEqual(self.snapshot(), after_first)
        self.assertEqual([r["id"] for r, _, _ in conflicts], ["b"])
        self.assertIn("rekeyed 0, conflicts 1", text)

    def test_a_chain_of_moves_resolves_in_one_run(self):
        self.add("b", "Acme", "Y Role", key="acme|x role")  # holds the key a wants; b itself is stale
        self.add("a", "Acme", "X Role", key="old|a")
        moves, conflicts, text = self.run_rekey("--apply")
        self.assertEqual(sorted(r["id"] for r, _ in moves), ["a", "b"])
        self.assertEqual(self.row("a")["key"], "acme|x role")
        self.assertEqual(self.row("b")["key"], "acme|y role")
        self.assertIn("conflicts 0", text)

    def test_dry_run_writes_nothing(self):
        self.add("a", "Acme", "Senior Data Engineer (m/w/d)", key="old|a")
        before = self.snapshot()
        moves, conflicts, text = self.run_rekey()
        self.assertEqual([r["id"] for r, _ in moves], ["a"])
        self.assertEqual(self.snapshot(), before)
        self.assertIn("nothing written", text)
        self.assertIn("key: old|a -> acme|senior data engineer", text)


if __name__ == "__main__":
    unittest.main()
