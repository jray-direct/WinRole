import unittest
from unittest.mock import patch

import boards
import db
from boards import (
    _place_matches,
    ce_to_record,
    merge_company_filters,
    parse_company_link,
    phenom_to_record,
    tesla_to_record,
    verizon_to_record,
)
from db import decode_scan, encode_scan, scan_label  # noqa: F401
from scrape import SearchFilters, scrape_company_board


class _Form:
    def __init__(self, data):
        self.data = data

    def get(self, key, default=None):
        value = self.data.get(key, default)
        return value if isinstance(value, str) else default

    def getlist(self, key):
        value = self.data.get(key, [])
        return list(value) if isinstance(value, list) else []


HPE = {
    "id": "hpe",
    "company": "Hewlett Packard Enterprise",
    "job_base": "https://careers.hpe.com/us/en/job/",
    "careers": "https://careers.hpe.com/us/en/search-results",
}


class CompanyBoardTests(unittest.TestCase):
    def test_scan_tags_keep_the_company(self):
        for source, name in (
            ("oracle", "Oracle"),
            ("hpe", "HPE"),
            ("verizon", "Verizon"),
            ("dell", "Dell"),
            ("cisco", "Cisco"),
            ("tesla", "Tesla"),
        ):
            self.assertEqual(encode_scan(source, "network"), f"{source}|network")
            self.assertEqual(decode_scan(f"{source}|network"), (source, "network"))
            self.assertEqual(scan_label("network", source), f"network · {name}")

    def test_phenom_record_keeps_the_full_posting(self):
        rec = phenom_to_record(
            {
                "title": "Network Support Engineer",
                "jobId": "1201873",
                "jobSeqNo": "HPE1US1201873EXTERNALENUS",
                "descriptionTeaser": "Short teaser only.",
                "cityStateCountry": "Spring, Texas, United States of America",
                "category": "Engineering",
                "type": "Full time",
                "postedDate": "2026-11-16T00:00:00.000Z",
                "applyUrl": "https://hpe.wd5.myworkdayjobs.com/example",
                "ml_skills": ["BGP"],
            },
            HPE,
            "Full posting. Must have BGP and Python.",
        )
        self.assertEqual(rec["site"], "hpe")
        self.assertEqual(rec["company"], "Hewlett Packard Enterprise")
        self.assertEqual(rec["id"], "1201873")
        self.assertEqual(rec["date_posted"], "2026-11-16")
        self.assertIn("Must have BGP and Python.", rec["description"])
        self.assertNotIn("Short teaser only.", rec["description"])
        self.assertIn("Category: Engineering", rec["description"])

    def test_oracle_record_and_country_match(self):
        rec = ce_to_record(
            {
                "Id": "3001",
                "Title": "Software Developer",
                "PostedDate": "2026-11-20",
                "PrimaryLocation": "Austin, TX, United States",
                "WorkplaceType": "On-site",
                "Category": "Product Development",
                "ShortDescriptionStr": "Cut off after a thousand characters",
            },
            {
                "company": "Oracle",
                "job_base": "https://careers.oracle.com/en/sites/jobsearch/job/",
                "careers": "https://careers.oracle.com/en/sites/jobsearch/jobs",
            },
            "The full requisition asks for Python and SQL.",
        )
        self.assertEqual(rec["id"], "3001")
        self.assertEqual(rec["date_posted"], "2026-11-20")
        self.assertFalse(rec["is_remote"])
        self.assertIn("Python and SQL.", rec["description"])
        self.assertTrue(_place_matches("Austin, TX, United States", {"united states"}))
        self.assertFalse(_place_matches("Indianapolis, IN, United States", {"india"}))

    def test_verizon_and_tesla_records(self):
        rec = verizon_to_record(
            {
                "Id": "r-1103372",
                "Title": "Principal Engineer, Network Design",
                "Teams": ["Network"],
                "Locations": [
                    {"Identifier": "Southlake, Texas", "City": "Southlake", "Region": "Texas", "Country": "United States of America"}
                ],
                "Urls": [{"Url": "/jobs/r-1103372/principal-engineer-network-design/", "IsDefault": True}],
            },
            "What you'll be doing: design the network.",
        )
        self.assertEqual(rec["site"], "verizon")
        self.assertIn("What you'll be doing", rec["description"])
        self.assertIn("Team: Network", rec["description"])
        self.assertTrue(rec["job_url"].endswith("/jobs/r-1103372/principal-engineer-network-design/"))

        tesla = tesla_to_record(
            {"id": "998", "t": "Network Engineer", "l": "1", "d": "2", "r": "3"},
            {
                "locations": {"1": "Fremont, California"},
                "departments": {"2": "Engineering"},
                "regions": {"3": "North America"},
            },
            "Build vehicle networks. Python required.",
        )
        self.assertEqual(tesla["site"], "tesla")
        self.assertIn("Fremont, California", tesla["location"])
        self.assertIn("Python required.", tesla["description"])
        self.assertIn("Department: Engineering", tesla["description"])

    def test_pasted_link_fills_keyword_and_filters(self):
        parsed = parse_company_link(
            "hpe",
            "https://careers.hpe.com/us/en/search-results?keywords=network&country=United%20States%20of%20America",
        )
        self.assertEqual(parsed["keyword"], "network")
        self.assertEqual(parsed["countries"], ["United States of America"])
        self.assertEqual(parse_company_link("hpe", "https://www.cisco.com/jobs?keywords=nope"), {})
        merged = merge_company_filters(
            "oracle",
            _Form(
                {
                    "oracle_url": "https://careers.oracle.com/en/sites/jobsearch/jobs?keyword=python&location=United%20States",
                    "oracle_categories": ["Sales"],
                    "oracle_countries": [],
                }
            ),
        )
        self.assertEqual(merged["keyword"], "python")
        self.assertEqual(merged["countries"], ["United States"])
        self.assertEqual(merged["categories"], ["Sales"])

    def test_keyword_runs_on_the_full_posting(self):
        rows = [
            {
                "id": "1",
                "title": "Analyst",
                "company": "Oracle",
                "location": "Austin, TX, United States",
                "description": "The full posting requires Python.",
                "site": "oracle",
                "job_url": "https://careers.oracle.com/job/1",
            },
            {
                "id": "2",
                "title": "Analyst",
                "company": "Oracle",
                "location": "Austin, TX, United States",
                "description": "This posting is about finance only.",
                "site": "oracle",
                "job_url": "https://careers.oracle.com/job/2",
            },
        ]
        with patch("boards.fetch_openings", return_value=(rows, {"raw_fetched": 2, "errors": []})):
            kept, stats = scrape_company_board(SearchFilters(keyword="Python", source="oracle"))
        self.assertEqual(stats["keyword_hits"], 1)
        self.assertEqual([row["id"] for row in kept], ["1"])


class WorkdayRecordTests(unittest.TestCase):
    def test_walmart_posting_keeps_the_full_description(self) -> None:
        record = boards.workday_to_record(
            {
                "title": "Principal, Software Engineer",
                "jobReqId": "R-2650514",
                "location": "IN-KA-BANGALORE",
                "country": {"descriptor": "India"},
                "timeType": "Full time",
                "startDate": "2026-11-18",
                "externalUrl": "https://walmart.wd504.myworkdayjobs.com/WalmartExternal/job/example",
                "jobDescription": "<p>Build the catalog service and mentor engineers.</p>",
            },
            boards.WORKDAY["walmart"],
            "Technology",
        )
        self.assertEqual(record["site"], "")
        self.assertIn("Build the catalog service", record["description"])
        self.assertIn("India", record["location"])
        self.assertEqual(record["date_posted"], "2026-11-18")
        self.assertEqual(record["job_type"], "fulltime")

    def test_empty_walmart_filters_mean_technology(self) -> None:
        categories, countries = boards._with_defaults("walmart", [], [])
        self.assertEqual(categories, ["Technology", "Data Analytics and Management"])
        self.assertEqual(countries, [])

    def test_empty_wells_filters_mean_us_technology(self) -> None:
        categories, countries = boards._with_defaults("wellsfargo", [], [])
        self.assertIn("Technology", categories)
        self.assertEqual(countries, ["United States of America"])

    def test_walmart_all_query_is_not_a_keyword(self) -> None:
        parsed = boards.parse_company_link(
            "walmart",
            "https://careers.walmart.com/us/en/results?searchQuery=All&careerareas=technology",
        )
        self.assertEqual(parsed["keyword"], "")
        self.assertEqual(parsed["categories"], ["technology"])


class RadancyRecordTests(unittest.TestCase):
    def test_unitedhealth_card_html(self) -> None:
        html = """
        <a href="/job/dublin/data-engineer/34088/103" data-job-id="103" class="brand-facet">
        <div><h2>Data Engineer</h2>
        <span class="job-location 1">Dublin, Leinster</span>
        </div></a>
        """
        cards = boards._radancy_cards(html)
        self.assertEqual(cards[0]["id"], "103")
        self.assertEqual(cards[0]["title"], "Data Engineer")
        self.assertIn("Dublin", cards[0]["location"])

    def test_jsonld_description_is_the_posting(self) -> None:
        page = """
        <script type="application/ld+json">{"@type":"JobPosting","datePosted":"2026-11-25","description":"<p>Primary Responsibilities include building pipelines for claims data.</p>"}</script>
        <div class="ats-description"><span>Requisition number: 2403541</span></div>
        """
        body, posted = boards._posting_text(page)
        self.assertIn("building pipelines", body)
        self.assertEqual(posted, "2026-11-25")

    def test_card_becomes_a_posting(self) -> None:
        record = boards.radancy_to_record(
            {
                "href": "/job/dublin/data-engineer/34088/103",
                "id": "103",
                "title": "Data Engineer",
                "location": "Dublin, Ireland",
                "date": "11/05/2026",
            },
            boards.RADANCY["unitedhealth"],
            "Primary Responsibilities include building pipelines.",
            "Technology",
        )
        self.assertEqual(record["date_posted"], "2026-11-05")
        self.assertIn("Primary Responsibilities", record["description"])
        self.assertTrue(record["job_url"].startswith("https://careers.unitedhealthgroup.com/job/"))

    def test_empty_spectrum_filters_mean_the_united_states(self) -> None:
        categories, countries = boards._with_defaults("spectrum", [], [])
        self.assertEqual(categories, [])
        self.assertEqual(countries, ["United States"])

    def test_spectrum_link_keeps_the_country(self) -> None:
        parsed = boards.parse_company_link(
            "spectrum",
            "https://jobs.spectrum.com/search-jobs?k=&l=United%20States&orgIds=4673",
        )
        self.assertEqual(parsed["countries"], ["United States"])
        self.assertEqual(parsed["keyword"], "")


class NewScanLabelTests(unittest.TestCase):
    def test_each_new_board_keeps_its_own_label(self) -> None:
        for source, label in (
            ("walmart", "Walmart"),
            ("unitedhealth", "UnitedHealth Group"),
            ("wellsfargo", "Wells Fargo"),
            ("spectrum", "Spectrum"),
        ):
            token = db.encode_scan(source, "python")
            self.assertEqual(db.decode_scan(token), (source, "python"))
            self.assertEqual(db.scan_label("python", source), f"python · {label}")
            self.assertEqual(db.normalize_scan_source(source), source)


if __name__ == "__main__":
    unittest.main()
