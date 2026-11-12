"""Wave 200 adapters (ats_api.py: mokahr, jibe) parsed from saved responses in tests/fixtures/ats_api/wave200_*.
No network: _post_json and get are replaced by the fixtures or by canned replies. Cornerstone is not here:
its career site answers 401 without the page's own session token, so it is reported, not built."""
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ats_api  # noqa: E402
import fetch_companies  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "ats_api"
BINGX_PAGE = "https://hire-r1.mokahr.com/social-recruitment/bingx/100008641"


def fixture_json(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def company(ats, feed_url, **extra):
    return {"id": "test-co", "name": "Test Co", "niche": "test", "top": False, "ats": ats,
            "feed_url": feed_url, **extra}


def moka_posting(i, status="open", title=None, locations=None):
    return {"id": f"uuid-{i}", "title": title or f"Role {i}", "status": status, "locations": locations or [],
            "createdAt": "2026-10-15T17:57:29", "orgId": "bingx"}


def moka_reply(postings, total):
    return {"code": 0, "codeType": 0, "data": {"jobStats": {"orgId": "bingx", "total": total}, "jobs": postings}}


class FakeMoka:
    """The mokahr POST API: offset/limit paging over `postings`, the stated total on every reply."""

    def __init__(self, postings, total=None, refuse=False):
        self.postings, self.total, self.refuse = postings, len(postings) if total is None else total, refuse
        self.bodies = []

    def post(self, url, body, timeout=40):
        self.bodies.append(dict(body))
        if self.refuse:
            return {"code": 703002, "codeType": 0, "msg": "refused", "success": False}
        off, lim = body["offset"], body["limit"]
        return moka_reply(self.postings[off:off + lim], self.total)


class Mokahr(unittest.TestCase):
    def run_board(self, fake, url=BINGX_PAGE):
        with mock.patch.object(ats_api, "_post_json", side_effect=fake.post), mock.patch("ats_api.time.sleep"):
            return ats_api.mokahr(company("mokahr", url))

    def test_bingx_fixture_reads_all_24_open_jobs(self):
        data = fixture_json("wave200_mokahr_bingx.json")
        fake = FakeMoka(data["data"]["jobs"], total=data["data"]["jobStats"]["total"])
        feed = self.run_board(fake)
        self.assertEqual(len(feed), 24)
        self.assertFalse(feed.capped)
        self.assertEqual(feed.bad, 0)
        first = feed[0]
        self.assertEqual(first["title"], "CFD Country Manager - Middle East")
        self.assertEqual(first["posted"], "2026-10-15")
        self.assertEqual(first["location"], "")  # the feed gives no location for any BingX posting
        self.assertEqual(first["url"], BINGX_PAGE + "?jobId=88545d3b-1955-4c5b-bc14-575e86db5bd6"
                         "#/job/88545d3b-1955-4c5b-bc14-575e86db5bd6")
        self.assertEqual(fake.bodies[0], {"orgId": "bingx", "siteId": 100008641, "limit": 100, "offset": 0})

    def test_every_bingx_link_carries_its_uuid_as_query_parameter(self):
        data = fixture_json("wave200_mokahr_bingx.json")
        fake = FakeMoka(data["data"]["jobs"], total=data["data"]["jobStats"]["total"])
        feed = self.run_board(fake)
        for j in feed:
            uuid = j["url"].split("#/job/")[1]
            self.assertIn(f"?jobId={uuid}#/job/", j["url"])

    def test_jobs_get_unique_links(self):
        fake = FakeMoka([moka_posting(i) for i in range(3)])
        feed = self.run_board(fake)
        self.assertEqual(len({j["url"] for j in feed}), 3)

    def test_feed_url_with_a_query_gets_jobId_appended_with_ampersand(self):
        fake = FakeMoka([moka_posting(7)])
        with mock.patch.object(ats_api, "_post_json", side_effect=fake.post), mock.patch("ats_api.time.sleep"):
            feed = ats_api.mokahr(company("mokahr", BINGX_PAGE + "?lang=en"))
        self.assertEqual(feed[0]["url"], BINGX_PAGE + "?lang=en&jobId=uuid-7#/job/uuid-7")

    def test_a_board_over_one_page_is_read_in_two_requests(self):
        fake = FakeMoka([moka_posting(i) for i in range(150)])
        feed = self.run_board(fake)
        self.assertEqual(len(feed), 150)
        self.assertFalse(feed.capped)
        self.assertEqual([b["offset"] for b in fake.bodies], [0, 100])

    def test_board_over_the_request_cap_is_capped(self):
        # 5 requests of 100 = 500 postings read; the stated total is 620: not complete, so capped
        fake = FakeMoka([moka_posting(i) for i in range(620)])
        feed = self.run_board(fake)
        self.assertEqual(len(feed), 500)
        self.assertTrue(feed.capped)
        self.assertEqual(len(fake.bodies), ats_api.MOKA_MAX_PAGES)

    def test_closed_postings_are_not_listed(self):
        fake = FakeMoka([moka_posting(1), moka_posting(2, status="closed"), moka_posting(3)])
        feed = self.run_board(fake)
        self.assertEqual([j["title"] for j in feed], ["Role 1", "Role 3"])
        self.assertFalse(feed.capped)

    def test_stated_total_larger_than_the_postings_read_is_capped(self):
        fake = FakeMoka([moka_posting(i) for i in range(4)], total=6)
        feed = self.run_board(fake)
        self.assertEqual(len(feed), 4)
        self.assertTrue(feed.capped)

    def test_location_is_read_when_the_feed_gives_one(self):
        fake = FakeMoka([moka_posting(1, locations=[{"name": "Wien"}])])
        feed = self.run_board(fake)
        self.assertEqual(feed[0]["location"], "Wien")

    def test_refused_request_raises(self):
        with self.assertRaises(ValueError):
            self.run_board(FakeMoka([], refuse=True))

    def test_feed_url_must_be_a_social_recruitment_page(self):
        with self.assertRaises(ValueError):
            self.run_board(FakeMoka([]), url="https://hire-r1.mokahr.com/somewhere/else")


def jibe_page(name):
    return json.dumps(fixture_json(name)).encode("utf-8")


class FakeJibe:
    """Jibe /api/jobs: `page` is 1-based, `limit` is the page size; pages past the last are empty."""

    def __init__(self, pages, total):
        self.pages, self.total, self.calls = pages, total, []

    def get(self, url, *args, **kwargs):
        self.calls.append(url)
        page = int(url.split("page=")[1].split("&")[0])
        data = dict(self.pages[page - 1]) if page <= len(self.pages) else {"jobs": [], "totalCount": self.total}
        data["totalCount"] = self.total
        return json.dumps(data).encode("utf-8")


class Jibe(unittest.TestCase):
    BOOKING = "https://jobs.booking.com/api/jobs?limit=100&page=1"

    def run_board(self, fake, url=BOOKING):
        with mock.patch.object(ats_api, "get", side_effect=fake.get), mock.patch("ats_api.time.sleep"):
            return ats_api.jibe(company("jibe", url))

    def test_booking_fixture_pages_are_read_and_built(self):
        pages = [fixture_json("wave200_jibe_booking_page1.json"), fixture_json("wave200_jibe_booking_page2.json")]
        fake = FakeJibe(pages, total=5)
        feed = self.run_board(fake)
        self.assertEqual(len(feed), 5)
        self.assertFalse(feed.capped)
        first = feed[0]
        self.assertEqual(first["title"], "Corporate Systems Engineer")
        self.assertEqual(first["location"], "Amsterdam, Netherlands")
        self.assertEqual(first["url"], "https://jobs.booking.com/booking/jobs/30585?lang=en-us")
        self.assertEqual(first["posted"], "2026-11-09")
        self.assertEqual(fake.calls[:2], ["https://jobs.booking.com/api/jobs?limit=100&page=1",
                                          "https://jobs.booking.com/api/jobs?limit=100&page=2"])

    def test_total_of_109_takes_two_pages_and_is_complete_when_all_read(self):
        def page(n):
            return {"jobs": [{"data": {"req_id": str(n * 1000 + i), "title": f"Job {n}-{i}",
                                       "full_location": "Vienna, Austria", "posted_date": "2026-11-01T00:00:00+0000",
                                       "apply_url": f"https://jobs.booking.com/x/{n}{i}"}} for i in range(100)]}
        fake = FakeJibe([page(1), page(2)], total=109)
        fake.pages[1]["jobs"] = fake.pages[1]["jobs"][:9]  # 109 = 100 + 9
        feed = self.run_board(fake)
        self.assertEqual(len(feed), 109)
        self.assertFalse(feed.capped)
        self.assertEqual(len(fake.calls), 2)

    def test_request_cap_stops_and_caps(self):
        def page(n):
            return {"jobs": [{"data": {"req_id": str(n * 1000 + i), "title": f"Job {n}-{i}",
                                       "apply_url": f"https://jobs.booking.com/x/{n}{i}"}} for i in range(100)]}
        fake = FakeJibe([page(n) for n in range(1, 10)], total=900)
        feed = self.run_board(fake)
        self.assertEqual(len(feed), ats_api.JIBE_MAX_PAGES * 100)
        self.assertTrue(feed.capped)
        self.assertEqual(len(fake.calls), ats_api.JIBE_MAX_PAGES)

    def test_empty_board_is_complete(self):
        fake = FakeJibe([], total=0)
        feed = self.run_board(fake)
        self.assertEqual(len(feed), 0)
        self.assertFalse(feed.capped)

    def test_missing_total_is_capped(self):
        fake = FakeJibe([{"jobs": [{"data": {"req_id": "1", "title": "A", "apply_url": "https://jobs.booking.com/a"}}]}],
                        total=None)
        feed = self.run_board(fake)
        self.assertEqual(len(feed), 1)
        self.assertTrue(feed.capped)


class Registration(unittest.TestCase):
    def test_new_adapters_are_registered(self):
        self.assertIs(ats_api.ADAPTERS["mokahr"], ats_api.mokahr)
        self.assertIs(ats_api.ADAPTERS["jibe"], ats_api.jibe)

    def test_existing_adapters_are_still_there(self):
        for name in ("join", "workday", "erecruiter", "bamboohr", "gohire", "kula", "factorial", "personio_json"):
            self.assertIn(name, ats_api.ADAPTERS)


if __name__ == "__main__":
    unittest.main()
