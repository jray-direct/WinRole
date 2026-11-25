import unittest

from spacex import (
    board_key,
    expand_disciplines,
    filter_posts,
    greenhouse_text,
    location_matches,
    merge_board_filters,
    opening_to_record,
    parse_careers_link,
)


META = {
    "disciplines": {
        "Engineering": {"disciplines": ["Network", "Security"]},
        "Operations": {"disciplines": ["Facilities"]},
    }
}


class SpaceXPageTests(unittest.TestCase):
    def test_location_rule_matches_the_jobs_page(self):
        self.assertTrue(location_matches("Hawthorne, CA", "Hawthorne, CA"))
        self.assertTrue(location_matches("Southaven, MS; Memphis, TN", "Memphis, TN"))
        self.assertFalse(location_matches("Hawthorne, CA", "Memphis, TN"))
        self.assertTrue(location_matches("Remote, United States", "Remote"))
        self.assertTrue(location_matches("Remote, International", "remote"))
        self.assertFalse(location_matches("Hawthorne, CA", "Remote"))
        self.assertTrue(location_matches("Canandaigua, NY", "Canandaigua, NY "))

    def test_board_filters_are_or_within_a_group(self):
        posts = [
            {"title": "Net", "location": "Hawthorne, CA", "programs": ["Starlink"], "discipline": "Network"},
            {"title": "Weld", "location": "Starbase, TX", "programs": ["Starship"], "discipline": "Welding"},
            {"title": "Both", "location": "Remote, United States", "programs": ["Starlink", "Dragon"], "discipline": "Network"},
        ]
        starlink = filter_posts(posts, programs=["Starlink"])
        self.assertEqual([p["title"] for p in starlink], ["Net", "Both"])
        sites = filter_posts(posts, locations=["Remote", "Starbase, TX"])
        self.assertEqual([p["title"] for p in sites], ["Weld", "Both"])
        both = filter_posts(posts, programs=["Starship"], disciplines=["Network"])
        self.assertEqual(both, [])

    def test_category_expands_to_its_disciplines(self):
        self.assertEqual(
            expand_disciplines(["category:Engineering", "Facilities"], META),
            ["Network", "Security", "Facilities"],
        )

    def test_pasted_jobs_link_keeps_page_filters(self):
        parsed = parse_careers_link(
            "https://www.spacex.com/careers/jobs?keyword=network&programs=Starlink|Starship"
            "&locations=Hawthorne,%20CA|Remote&disciplines=Network"
        )
        self.assertEqual(parsed["keyword"], "network")
        self.assertEqual(parsed["programs"], ["Starlink", "Starship"])
        self.assertEqual(parsed["locations"], ["Hawthorne, CA", "Remote"])
        self.assertEqual(parsed["disciplines"], ["Network"])
        programs, locations, disciplines, keyword = merge_board_filters(
            ["Falcon"],
            [],
            [],
            "https://www.spacex.com/careers/jobs?programs=Starlink&keyword=Network%2B",
        )
        self.assertEqual(programs, ["Falcon", "Starlink"])
        self.assertEqual(keyword, "Network+")
        self.assertEqual(locations, [])
        self.assertEqual(disciplines, [])
        self.assertEqual(parse_careers_link("https://example.com/jobs"), {})

    def test_greenhouse_html_becomes_posting_text(self):
        raw = "&lt;p&gt;CompTIA Network+&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Routing&lt;/li&gt;&lt;/ul&gt;"
        text = greenhouse_text(raw)
        self.assertIn("CompTIA Network+", text)
        self.assertIn("- Routing", text)
        self.assertNotIn("<", text)

    def test_record_uses_page_fields_and_greenhouse_writeup(self):
        post = {
            "greenhouseId": 5,
            "title": "Network Engineer",
            "location": "Remote, United States",
            "programs": ["Starlink"],
            "discipline": "Network",
            "employementType": "Regular",
            "category": "Engineering",
            "board": "https://boards.greenhouse.io/spacex/jobs/5",
        }
        gh = {
            "id": 5,
            "company_name": "SpaceX",
            "absolute_url": "https://boards.greenhouse.io/spacex/jobs/5",
            "first_published": "2026-11-01T12:00:00-05:00",
            "content": "&lt;p&gt;CCNA or Network+ required.&lt;/p&gt;",
        }
        rec = opening_to_record(post, gh)
        self.assertEqual(rec["company"], "SpaceX")
        self.assertEqual(rec["site"], "spacex")
        self.assertEqual(rec["job_type"], "fulltime")
        self.assertTrue(rec["is_remote"])
        self.assertEqual(rec["date_posted"], "2026-11-01")
        self.assertIn("Program: Starlink", rec["description"])
        self.assertIn("Network+ required", rec["description"])
        self.assertEqual(rec["company_industry"], "Engineering · Network")

        xai = opening_to_record(
            {**post, "board": "https://job-boards.greenhouse.io/xai/jobs/5", "employementType": ""},
            {"company_name": "SpaceXSI", "content": ""},
        )
        self.assertEqual(board_key(xai and {"board": post["board"].replace("spacex", "xai"), "employementType": ""}), "xai")
        self.assertEqual(xai["company"], "SpaceXSI")
        global_co = opening_to_record(
            {**post, "board": "https://job-boards.greenhouse.io/spacexglobal/jobs/9", "employementType": "International"},
            {"company_name": "SpaceX_Global"},
        )
        self.assertEqual(global_co["company"], "SpaceX")
        self.assertEqual(global_co["job_type"], "fulltime")
        listed = {
            "board": "https://boards.greenhouse.io/spacex/jobs/9",
            "employementType": "International",
        }
        self.assertEqual(board_key(listed), "spacex")
        self.assertEqual(board_key({"board": "spacexglobal", "employementType": "Regular"}), "spacexglobal")


if __name__ == "__main__":
    unittest.main()
