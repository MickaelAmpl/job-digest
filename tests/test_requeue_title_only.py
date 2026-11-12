"""scripts/requeue_title_only.py (stage 7 step 1.4): the one-off requeue of title-only jobs. Scratch DB only."""
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
from scripts import requeue_title_only as rq  # noqa: E402


class Requeue(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "data").mkdir()
        self.patches = [mock.patch.object(store, "DB", d / "data" / "state.db"),
                        mock.patch.object(store, "OUT", d)]
        for p in self.patches:
            p.start()
        self.con = store.connect()

    def tearDown(self):
        self.con.close()
        for p in reversed(self.patches):
            p.stop()
        self.dir.cleanup()

    def add(self, jid, **cols):
        cols = {"id": jid, "key": f"ACME|PM|{jid}", "company": "ACME", "title": f"PM {jid}", "url": "",
                "source": "linkedin", "first_seen": store.now(), "rule": "keep", "has_desc": 0,
                "enrich_tries": 1, "data": json.dumps({"title": f"PM {jid}", "enrich_tries": 1,
                                                       "enrich_errors": 5}), "sent_at": store.now(),
                "listed": 1, **cols}
        self.con.execute(f"insert into jobs ({','.join(cols)}) values ({','.join('?' * len(cols))})",
                         list(cols.values()))
        self.con.commit()

    def row(self, jid):
        return self.con.execute("select * from jobs where id=?", (jid,)).fetchone()

    def run_script(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rows = rq.main(list(argv))
        return rows, out.getvalue()

    def test_qualifying_row_is_requeued_and_others_are_not(self):
        self.add("ok")                              # sent, listed, title only: requeued
        self.add("unsent", sent_at=None, listed=0)  # never sent: requeued too
        self.add("closed", closed=1)
        self.add("judged", verdict="interested")
        self.add("has_desc", has_desc=1)
        self.add("senior", rule="senior")
        self.add("old", first_seen="2020-01-01T00:00:00")
        rows, text = self.run_script()
        self.assertEqual(sorted(r["id"] for r in rows), ["ok", "unsent"])
        self.assertIn("to requeue: 2", text)
        ok = self.row("ok")
        self.assertEqual(ok["enrich_tries"], 0)
        data = json.loads(ok["data"])
        self.assertEqual((data["enrich_tries"], data["enrich_errors"]), (0, 0))
        self.assertEqual(self.row("closed")["enrich_tries"], 1)  # untouched

    def test_dry_run_writes_nothing(self):
        self.add("ok")
        rows, text = self.run_script("--dry-run")
        self.assertEqual(len(rows), 1)
        self.assertIn("nothing written", text)
        self.assertEqual(self.row("ok")["enrich_tries"], 1)
        self.assertIsNone(self.con.execute("select v from meta where k='requeue_title_only'").fetchone())

    def test_second_run_is_a_no_op(self):
        self.add("ok")
        self.run_script()
        self.con.execute("update jobs set enrich_tries=2 where id='ok'")  # enrich ran again since
        self.con.commit()
        rows, _ = self.run_script()
        self.assertEqual(rows, [])
        self.assertEqual(self.row("ok")["enrich_tries"], 2)

    def test_requeued_row_is_in_prepare_queue_even_when_sent(self):
        """prepare.py's queue: has_desc 0, enrich_tries < 3, and (sent, listed, or enrich_tries 0)."""
        self.add("ok")
        self.run_script()
        cond, params = store.in_window()
        ids = [r["id"] for r in self.con.execute(
            f"select id from jobs where rule='keep' and {cond} and (scored_at is null or coalesce(score_tries,0) "
            "between 1 and 2 or (coalesce(has_desc,1)=0 and coalesce(enrich_tries,0) < 3 and (sent_at is null "
            "or listed=1 or coalesce(enrich_tries,0)=0)))", params)]
        self.assertEqual(ids, ["ok"])


if __name__ == "__main__":
    unittest.main()
