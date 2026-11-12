"""Wave 200 site configs (task B): link cards that the generic 'site' adapter had to learn to read.
Inline HTML trimmed from the real pages (sclable.com/careers, agentur-loop.com/careers, walls.io/jobs). No network."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import site_jobs as sj  # noqa: E402

SCLABLE = """<h2>Open positions</h2><ul>
<li><a href=/careers/product-owner-digital-advisor/ class=row-list-item-header>Product Owner / Digital Advisor (m/f/d)
Open</a></li>
<li><a href=/careers/ai-product-owner-digital-advisor/ class=row-list-item-header>AI Product Owner (m/f/d) Open</a></li>
</ul><a href="mailto:careers@sclable.com">Mail</a>"""

LOOP = """<a onclick="if (window.dataLayer && window.dataLayer.find(element => element['gtm.start'])) { x(); }"
href="https://www.agentur-loop.com/careers/account-project-lead" class="jobs__item"><div>Brand Team</div>
<div>Account &amp; Project Lead</div></a>
<a onclick="y()" href="https://www.agentur-loop.com/careers/internship" class="jobs__item"><div>Internship</div></a>
<a href="https://www.agentur-loop.com/careers/seo-geo-manager" class="jobs__item"><div>Growth &amp; Analytics</div>
<div>SEO/GEO Manager</div></a>"""

WALLS = """<div class="card"><div>Technical GEO Growth Specialist (f/m/d) - full-time (38,5 hrs/week)</div>
<div>Full-time</div><div>Vienna (or around)</div>
<a href="/jobs/technical-geo-growth-specialist" class="button">see more</a></div>"""


def company(**over):
    c = {"id": "x-test", "name": "X Test GmbH", "niche": "startups", "top": False, "ats": "site",
         "feed_url": "https://example.test/careers/"}
    c.update(over)
    return c


SCLABLE_CFG = company(link_pattern=r"sclable\.com/careers/[a-z0-9-]+/?$", title_clean=r"^(.*?)\s+Open$",
                      default_location="Vienna, Austria")
LOOP_CFG = company(link_pattern=r"agentur-loop\.com/careers/(?!internship$|proactive-application$)[a-z0-9-]+$",
                   title_clean=r"^(?:Brand Team|Growth & Analytics)\s+(.+)$", default_location="Österreich")
WALLS_CFG = company(link_pattern=r"walls\.io/jobs/[a-z0-9-]+$",
                    title_pattern=r"([^|]+?\((?:f/m/d|m/f/d|m/w/d)\))", location_pattern=r"(Vienna|Wien)")


class BareHref(unittest.TestCase):
    def test_unquoted_href_cards_are_read_with_title_and_default_place(self):
        found = sj._link_records(SCLABLE, SCLABLE_CFG, "https://sclable.com/careers/")
        self.assertEqual(set(found), {"https://sclable.com/careers/product-owner-digital-advisor/",
                                      "https://sclable.com/careers/ai-product-owner-digital-advisor/"})
        jobs, bad = sj._jobs_from_records(SCLABLE_CFG, found)
        self.assertEqual(bad, 0)
        self.assertEqual(sorted(j["title"] for j in jobs),
                         ["AI Product Owner (m/f/d)", "Product Owner / Digital Advisor (m/f/d)"])
        self.assertTrue(all(j["location"] == "Vienna, Austria" for j in jobs))

    def test_mailto_is_not_a_job(self):
        found = sj._link_records(SCLABLE, SCLABLE_CFG, "https://sclable.com/careers/")
        self.assertFalse(any("mailto" in u for u in found))


class AnchorWithArrow(unittest.TestCase):
    def test_gt_inside_quoted_onclick_does_not_hide_the_href(self):
        found = sj._link_records(LOOP, LOOP_CFG, "https://www.agentur-loop.com/careers")
        self.assertIn("https://www.agentur-loop.com/careers/account-project-lead", found)
        self.assertEqual(found["https://www.agentur-loop.com/careers/account-project-lead"]["title"],
                         "Account & Project Lead")

    def test_excluded_pages_are_not_jobs(self):
        found = sj._link_records(LOOP, LOOP_CFG, "https://www.agentur-loop.com/careers")
        self.assertFalse(any(u.endswith("/internship") for u in found))
        self.assertEqual(len(found), 2)


class SeeMoreLink(unittest.TestCase):
    def test_see_more_is_generic_so_the_title_comes_from_the_card(self):
        found = sj._link_records(WALLS, WALLS_CFG, "https://walls.io/jobs")
        rec = found["https://walls.io/jobs/technical-geo-growth-specialist"]
        self.assertEqual(rec["title"], "Technical GEO Growth Specialist (f/m/d)")
        self.assertEqual(rec["location"], "Vienna")


if __name__ == "__main__":
    unittest.main()
