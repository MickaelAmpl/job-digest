"""store.link_id / same_link / remember for BingX (Mokahr) links, where the job UUID sits in a query parameter.
Before the fix every BingX job got the same link_id (the board number 100008641), so they merged into one row.
Also pins the links that must not change: Greenhouse gh_jid, Lever UUID in the path."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import store  # noqa: E402

BINGX = "https://hire-r1.mokahr.com/social-recruitment/bingx/100008641"
U1 = "88545d3b-1955-4c5b-bc14-575e86db5bd6"
U2 = "1a2b3c4d-0000-4e5f-8a9b-0c1d2e3f4a5b"


def bingx(uuid):
    return f"{BINGX}?jobId={uuid}#/job/{uuid}"


class BingxLinks(unittest.TestCase):
    def test_two_bingx_links_with_different_uuids_are_different_jobs(self):
        self.assertNotEqual(store.same_link(bingx(U1)), store.same_link(bingx(U2)))
        self.assertTrue(store.same_link(bingx(U1)))

    def test_two_bingx_links_get_different_link_ids(self):
        a, b = store.link_id(bingx(U1)), store.link_id(bingx(U2))
        self.assertTrue(a and b)
        self.assertNotEqual(a, b)
        self.assertEqual(a, f"hire-r1.mokahr.com#{U1}")

    def test_uuid_in_query_is_taken_lowercased(self):
        self.assertEqual(store.link_id(bingx(U1.upper())), f"hire-r1.mokahr.com#{U1}")

    def test_greenhouse_gh_jid_link_keeps_its_id(self):
        url = "https://www.gostudent.org/en-gb/job-position/?gh_jid=4567890123"
        self.assertEqual(store.link_id(url), "gostudent.org#4567890123")

    def test_lever_uuid_in_path_keeps_its_id(self):
        url = f"https://jobs.lever.co/acme/{U1}"
        self.assertEqual(store.link_id(url), f"jobs.lever.co#{U1}")


class BingxRemember(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(store, "DB", Path(self.dir.name) / "data" / "state.db")
        self.patch.start()
        self.con = store.connect()

    def tearDown(self):
        self.con.close()
        self.patch.stop()
        self.dir.cleanup()

    def test_two_bingx_jobs_with_different_titles_are_two_rows(self):
        first = {"company": "BingX", "title": "CFD Country Manager - Middle East", "url": bingx(U1),
                 "source": "company"}
        second = {"company": "BingX", "title": "Backend Engineer", "url": bingx(U2), "source": "company"}
        self.assertTrue(store.remember(self.con, first))
        self.assertTrue(store.remember(self.con, second))
        self.con.commit()
        rows = self.con.execute("select title, url from jobs order by title").fetchall()
        self.assertEqual([r["title"] for r in rows], ["Backend Engineer", "CFD Country Manager - Middle East"])
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
