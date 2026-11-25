import unittest

from ibm import (
    display_location,
    job_type_of,
    merge_ibm_filters,
    mode_label,
    opening_to_record,
    parse_ibm_link,
)


class IbmCareersTests(unittest.TestCase):
    def test_record_uses_the_full_posting(self):
        teaser = "A career in IBM Consulting is built on long-term client relationships."
        body = teaser + "\n\nRequired technical and professional expertise\n\nPython and Kubernetes."
        src = {
            "title": "Application Developer-IBM Cloud FullStack",
            "url": "https://careers.ibm.com/careers/JobDetail?jobId=145078",
            "description": teaser,
            "body": body,
            "dcdate": "2026-11-17",
            "field_text_01": "145078",
            "field_keyword_05": "United States",
            "field_keyword_08": "Software Engineering",
            "field_keyword_17": "Hybrid",
            "field_keyword_18": "Professional",
            "field_keyword_19": "Dallas, US",
        }
        rec = opening_to_record(src)
        self.assertEqual(rec["company"], "IBM")
        self.assertEqual(rec["site"], "ibm")
        self.assertEqual(rec["id"], "145078")
        self.assertEqual(rec["location"], "Dallas, US")
        self.assertEqual(rec["date_posted"], "2026-11-17")
        self.assertEqual(rec["job_type"], "fulltime")
        self.assertFalse(rec["is_remote"])
        self.assertIn("Python and Kubernetes.", rec["description"])
        self.assertIn("Area: Software Engineering", rec["description"])
        self.assertIn("Work arrangement: Hybrid", rec["description"])
        self.assertGreater(len(rec["description"]), len(teaser))

    def test_internship_remote_and_onsite(self):
        remote = opening_to_record({
            "title": "Software Engineer Intern",
            "url": "https://careers.ibm.com/careers/JobDetail?jobId=99",
            "body": "Build tools.",
            "field_keyword_17": "Remote",
            "field_keyword_18": "Internship",
            "field_keyword_19": "Multiple Cities",
            "field_keyword_05": "United States",
        })
        self.assertEqual(remote["job_type"], "internship")
        self.assertTrue(remote["is_remote"])
        self.assertEqual(remote["location"], "Multiple Cities, United States")
        self.assertEqual(job_type_of("Entry Level"), "fulltime")
        self.assertEqual(mode_label(""), "Onsite")
        self.assertEqual(display_location("Dallas, US", "United States"), "Dallas, US")

    def test_pasted_search_link(self):
        url = (
            "https://www.ibm.com/careers/search?q=software"
            "&field_keyword_05[0]=United+States"
            "&field_keyword_08[0]=Software+Engineering"
            "&field_keyword_08[1]=Consulting"
            "&field_keyword_18[0]=Entry+Level"
            "&field_keyword_17[0]=Remote"
            "&field_keyword_19[0]=Dallas,+US"
        )
        parsed = parse_ibm_link(url)
        self.assertEqual(parsed["keyword"], "software")
        self.assertEqual(parsed["countries"], ["United States"])
        self.assertEqual(parsed["categories"], ["Software Engineering", "Consulting"])
        self.assertEqual(parsed["levels"], ["Entry Level"])
        self.assertEqual(parsed["modes"], ["Remote"])
        self.assertEqual(parsed["cities"], ["Dallas, US"])
        home = parse_ibm_link("https://www.ibm.com/careers/")
        self.assertEqual(home["keyword"], "")
        self.assertEqual(home["countries"], [])
        self.assertEqual(parse_ibm_link("https://example.com/careers"), {})
        merged = merge_ibm_filters(
            ["Research"],
            [],
            [],
            ["Internship"],
            [],
            url,
        )
        self.assertEqual(merged["categories"], ["Research", "Software Engineering", "Consulting"])
        self.assertEqual(merged["countries"], ["United States"])
        self.assertEqual(merged["levels"], ["Internship", "Entry Level"])
        self.assertEqual(merged["keyword"], "software")


if __name__ == "__main__":
    unittest.main()
