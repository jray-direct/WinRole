import json
import unittest

from amazon import (
    display_location,
    html_to_text,
    is_remote_job,
    job_type_of,
    merge_amazon_filters,
    opening_to_record,
    parse_amazon_link,
    parse_pay,
    parse_posted,
)


class AmazonJobsTests(unittest.TestCase):
    def test_posting_html_and_pay_and_date(self):
        text = html_to_text("<p>Lift up to 49 pounds<br/>Work weekends</p>")
        self.assertIn("Lift up to 49 pounds", text)
        self.assertIn("Work weekends", text)
        self.assertNotIn("<", text)
        hourly = (
            "USA, GA, Savannah - 24.00 - 26.00 USD hourly"
        )
        self.assertEqual(parse_pay(hourly), (24.0, 26.0, "hourly"))
        annual = "The base pay range for this position is $129,300 - $215,000/year in Seattle."
        self.assertEqual(parse_pay(annual), (129300.0, 215000.0, "yearly"))
        posted = "USA, WA, Seattle - 168,100.00 - 227,400.00 USD annually"
        self.assertEqual(parse_pay(posted), (168100.0, 227400.0, "yearly"))
        sites = (
            "USA, CA, Cupertino - 212,700.00 - 287,700.00 USD annually\n"
            "USA, WA, Seattle - 184,900.00 - 250,200.00 USD annually"
        )
        self.assertEqual(parse_pay(sites), (184900.0, 287700.0, "yearly"))
        self.assertEqual(parse_posted("August 18, 2026"), "2026-08-18")

    def test_remote_and_schedule(self):
        onsite = {
            "normalized_location": "Savannah, Georgia, USA",
            "job_schedule_type": "full-time",
            "locations": [
                json.dumps({"type": "ONSITE", "normalizedLocation": "Savannah, Georgia, USA"})
            ],
        }
        self.assertFalse(is_remote_job(onsite))
        self.assertEqual(job_type_of(onsite), "fulltime")
        remote = {
            "normalized_location": "Virtual, USA",
            "job_schedule_type": "part-time",
            "is_intern": None,
            "locations": [json.dumps({"type": "VIRTUAL", "normalizedLocation": "Virtual, USA"})],
        }
        self.assertTrue(is_remote_job(remote))
        self.assertEqual(job_type_of(remote), "parttime")
        self.assertEqual(job_type_of({"is_intern": "1", "job_schedule_type": "full-time"}), "internship")
        self.assertEqual(display_location(onsite), "Savannah, Georgia, USA")

    def test_pasted_search_link(self):
        url = (
            "https://www.amazon.jobs/en/search?base_query=network&loc_query=seattle"
            "&category[]=Software+Development&normalized_location[]=Seattle,+Washington,+USA"
            "&business_category[]=aws&schedule_type_id[]=Full-Time&is_intern[]=1"
        )
        parsed = parse_amazon_link(url)
        self.assertEqual(parsed["keyword"], "network")
        self.assertEqual(parsed["loc_query"], "seattle")
        self.assertEqual(parsed["categories"], ["Software Development"])
        self.assertEqual(parsed["locations"], ["Seattle, Washington, USA"])
        self.assertEqual(parsed["teams"], ["aws"])
        self.assertEqual(parsed["schedules"], ["Full-Time"])
        self.assertTrue(parsed["intern"])
        us = parse_amazon_link(
            "https://www.amazon.jobs/en/search?base_query=&loc_query=united+states"
            "&type=area&longitude=-77.03196&latitude=38.89036&country=USA"
        )
        self.assertEqual(us["loc_query"], "")
        self.assertEqual(us["keyword"], "")
        merged = merge_amazon_filters(
            ["Finance & Accounting"],
            [],
            [],
            [],
            "",
            False,
            False,
            url,
        )
        self.assertEqual(merged["categories"], ["Finance & Accounting", "Software Development"])
        self.assertEqual(merged["teams"], ["aws"])
        self.assertEqual(parse_amazon_link("https://example.com/search"), {})

    def test_record_keeps_the_posting(self):
        job = {
            "id_icims": "10505614",
            "title": "Process Assistant",
            "company_name": "Amazon.com Services LLC",
            "job_category": "Fulfillment & Operations Management",
            "business_category": "fulfillment-ops",
            "job_schedule_type": "full-time",
            "normalized_location": "Savannah, Georgia, USA",
            "posted_date": "August 18, 2026",
            "job_path": "/en/jobs/10505614/process-assistant",
            "description": "<p>Lead the shift.</p>",
            "basic_qualifications": "- High school<br/>- 18 years of age or older",
            "preferred_qualifications": "USA, GA, Savannah - 24.00 - 26.00 USD hourly",
            "locations": [json.dumps({"type": "ONSITE", "normalizedLocation": "Savannah, Georgia, USA"})],
        }
        rec = opening_to_record(job, {"fulfillment-ops": "Fulfillment Ops"})
        self.assertEqual(rec["company"], "Amazon.com Services LLC")
        self.assertEqual(rec["site"], "amazon")
        self.assertEqual(rec["job_type"], "fulltime")
        self.assertEqual(rec["date_posted"], "2026-08-18")
        self.assertEqual(rec["job_url"], "https://www.amazon.jobs/en/jobs/10505614/process-assistant")
        self.assertIn("Category: Fulfillment & Operations Management", rec["description"])
        self.assertIn("Team: Fulfillment Ops", rec["description"])
        self.assertIn("High school", rec["description"])
        self.assertEqual(rec["interval"], "hourly")
        self.assertEqual(rec["min_amount"], 24.0)
        self.assertEqual(rec["salary_source"], "employer")
        self.assertEqual(rec["company_industry"], "Fulfillment & Operations Management")


if __name__ == "__main__":
    unittest.main()
