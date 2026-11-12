"""Stage 8 seed (scripts/import_applications.py): dry run by default, --apply writes, a second --apply changes nothing.
Temp DB with the real schema, a temp seed file, and one check that the real research/applications_seed.json loads."""
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
from scripts import import_applications as seed  # noqa: E402

SEED_ROWS = [
    {"company": "Acme Labs", "title": "Brand & Marketing Intern", "url": "https://acme.example/careers/brand-marketing-intern",
     "applied_at": "2026-10-26", "status": "applied", "channel": "email + CV", "note": "chat"},
    {"company": "Bluebird AI", "title": "Customer Success Manager", "url": "https://bluebird.example/en/careers",
     "applied_at": "2026-10-26", "status": "applied", "channel": "email", "note": "chat"},
    {"company": "Cobalt Capital", "title": "Associate, Growth & Intelligence",
     "url": "https://join.com/companies/cobalt/16842036-associate", "applied_at": "2026-10-26", "status": "applied",
     "channel": "JOIN form", "note": "chat"},
    {"company": "Driftwood", "title": "Growth & Operations Manager @ Tidepool",
     "url": "https://www.driftwood.ai/en/jobs/growth-operations-manager-tidepool/", "applied_at": "2026-10-26",
     "status": "applied", "channel": "form", "note": "chat", "existing_id": "9c7bd2de10"},
    {"company": "Ember Studio", "title": "Open application", "url": "https://www.ember.example/", "applied_at": "2026-10-15",
     "status": "replied", "status_at": "2026-10-16", "channel": "email", "note": "chat"},
]


class Seed(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        self.patch = mock.patch.object(store, "DB", d / "data" / "state.db")
        self.patch.start()
        self.seed_file = d / "seed.json"
        self.seed_file.write_text(json.dumps(SEED_ROWS, ensure_ascii=False), encoding="utf-8")
        con = store.connect()
        # the Driftwood card the bot already has (Adzuna, interested, sent): the seed attaches to it
        con.execute("insert into jobs (id, key, company, title, url, open_url, source, first_seen, sent_at, verdict,"
                    " data) values ('9c7bd2de10', ?, 'Driftwood.io', 'Growth & Operations Manager @ Tidepool', ?, ?,"
                    " 'adzuna', '2026-10-10T06:31:56', '2026-10-10T07:30:05', 'interested', '{}')",
                    (store.dedupe_key({"company": "Driftwood.io", "title": "Growth & Operations Manager @ Tidepool"}),
                     "https://www.adzuna.at/details/5920640808", "https://www.adzuna.at/details/5920640808"))
        con.commit()
        con.close()

    def tearDown(self):
        self.patch.stop()
        self.dir.cleanup()

    def run_seed(self, *extra):
        out = io.StringIO()
        code = seed.main(["--file", str(self.seed_file), *extra], out=out)
        return code, out.getvalue()

    def con(self):
        return store.connect()

    def counts(self):
        con = self.con()
        try:
            return (con.execute("select count(*) from jobs").fetchone()[0],
                    con.execute("select count(*) from jobs where source='manual'").fetchone()[0],
                    con.execute("select count(*) from status_log").fetchone()[0],
                    con.execute("select status from jobs where id='9c7bd2de10'").fetchone()[0])
        finally:
            con.close()

    def test_dry_run_prints_every_row_with_its_outcome_and_candidates_and_writes_nothing(self):
        before = self.counts()
        code, out = self.run_seed()
        self.assertEqual(code, 0)
        for n in range(1, 6):
            self.assertIn(f"[{n}]", out)
        self.assertIn("would be created", out)
        self.assertIn("would be attached", out)
        self.assertIn("candidates:", out)
        self.assertIn("9c7bd2de10", out)  # the Driftwood candidate is listed
        self.assertIn("DRY RUN", out)
        self.assertEqual(self.counts(), before)

    def test_apply_creates_four_rows_and_attaches_driftwood_and_sets_ember(self):
        code, out = self.run_seed("--apply")
        self.assertEqual(code, 0, out)
        jobs, manual, _, wg_status = self.counts()
        self.assertEqual((manual, wg_status), (4, "applied"))
        con = self.con()
        try:
            sp = con.execute("select status, status_at, applied_at from jobs where source='manual' and company='Ember Studio'"
                             ).fetchone()
            self.assertEqual((sp["status"], sp["status_at"], sp["applied_at"]),
                             ("replied", "2026-10-16T12:00:00", "2026-10-15T12:00:00"))
            wg = con.execute("select verdict, status, applied_at from jobs where id='9c7bd2de10'").fetchone()
            self.assertEqual((wg["verdict"], wg["status"]), ("interested", "applied"))
        finally:
            con.close()

    def test_a_second_apply_changes_nothing(self):
        self.run_seed("--apply")
        before = self.counts()
        code, out = self.run_seed("--apply")
        self.assertEqual(code, 0)
        self.assertEqual(self.counts(), before)
        self.assertEqual(out.count("-> unchanged"), 5, out)

    def test_a_row_removed_by_misha_is_not_created_again(self):
        self.run_seed("--apply")
        con = self.con()
        rid = con.execute("select id from jobs where source='manual' and company='Acme Labs'").fetchone()[0]
        store.remove_manual(con, rid)
        con.commit()
        con.close()
        code, out = self.run_seed("--apply")
        self.assertEqual(code, 0)
        con = self.con()
        try:
            self.assertEqual(con.execute("select closed_by from jobs where id=?", (rid,)).fetchone()[0],
                             "manual_removed")
            self.assertEqual(con.execute("select count(*) from jobs where company='Acme Labs'").fetchone()[0], 1)
        finally:
            con.close()

    def test_a_status_reset_by_misha_is_not_undone_by_the_seed(self):
        con = self.con()
        store.set_status(con, "9c7bd2de10", "applied")
        store.set_status(con, "9c7bd2de10", None)  # Сбросить
        con.close()
        code, out = self.run_seed("--apply")
        self.assertEqual(code, 0)
        self.assertEqual(self.counts()[3], None)
        self.assertIn("you reset its status", out)

    def test_an_existing_id_that_is_not_there_is_an_error_and_the_other_rows_still_run(self):
        rows = [dict(SEED_ROWS[0]), dict(SEED_ROWS[3], existing_id="nope")]
        self.seed_file.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        code, out = self.run_seed("--apply")
        self.assertEqual(code, 1)
        self.assertIn("error", out)
        self.assertEqual(self.counts()[1], 1)  # Acme Labs created

    def test_a_bad_row_stops_the_run_before_anything_is_written(self):
        rows = [dict(SEED_ROWS[0]), {"company": "NoTitle", "applied_at": "2026-10-26"}]
        self.seed_file.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        before = self.counts()
        code, out = self.run_seed("--apply")
        self.assertEqual(code, 2)
        self.assertIn("row 2", out)
        self.assertEqual(self.counts(), before)

    # research/ is never deployed (scripts/deploy.sh excludes it): on the server this check has nothing to read.
    @unittest.skipUnless((ROOT / "research" / "applications_seed.json").exists(), "research/ is not deployed")
    def test_the_real_seed_file_loads_with_the_five_rows(self):
        rows = seed.load_rows(ROOT / "research" / "applications_seed.json")
        self.assertEqual([r["company"] for r in rows],
                         ["Acme Labs", "Bluebird AI", "Cobalt Capital", "Driftwood", "Ember Studio"])
        self.assertEqual(rows[3]["existing_id"], "9c7bd2de10")
        self.assertEqual((rows[4]["status"], rows[4]["status_at"]), ("replied", "2026-10-16"))


if __name__ == "__main__":
    unittest.main()
