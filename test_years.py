"""Run with: python -m unittest test_years -v"""

import unittest

from textutil import clean_description
from years import parse_years, years_chips


def parse(text: str):
    info = parse_years(text)
    return info.years_min, info.years_max, info.years_pref


class ParseYears(unittest.TestCase):
    def test_plain_minimum(self):
        self.assertEqual(parse("Requirements:\n* 3+ years of experience in IT support"), (3, None, None))

    def test_range(self):
        self.assertEqual(parse("You bring 1-5 years of experience with Linux."), (1, 5, None))

    def test_en_dash_and_mojibake_range(self):
        self.assertEqual(parse("2\u20133 years of help desk experience"), (2, 3, None))
        self.assertEqual(parse("2\ufffd3 years of help desk experience"), (2, 3, None))

    def test_words_and_parens(self):
        self.assertEqual(parse("Must possess five (5) years of progressive experience."), (5, None, None))
        self.assertEqual(parse("Three years of experience in networking"), (3, None, None))

    def test_preferred_only_leaves_minimum_empty(self):
        self.assertEqual(
            parse("Nice to have:\n* 5+ years of experience with Terraform"), (None, None, 5)
        )

    def test_required_and_preferred(self):
        text = "3+ years of experience required. 5+ years of experience preferred."
        self.assertEqual(parse(text), (3, None, 5))

    def test_preferred_heading_then_required_heading(self):
        text = (
            "Preferred Qualifications\n* 7 years of experience with Kubernetes\n"
            "Minimum Qualifications\n* 2 years of experience in DevOps\n"
        )
        self.assertEqual(parse(text), (2, None, 7))

    def test_strictest_requirement_wins(self):
        text = "* 2+ years of experience with Python\n* 5+ years of experience in networking"
        self.assertEqual(parse(text), (5, None, None))

    def test_degree_alternatives_take_the_lowest(self):
        text = "Bachelor's degree and 5 years of experience, or a Master's degree and 3 years of experience."
        self.assertEqual(parse(text), (3, None, None))

    def test_degree_substitute_experience_is_not_the_requirement(self):
        text = (
            "* Minimum 3 years of experience in cloud engineering\n"
            "* Bachelor's degree or equivalent (minimum 12 years) work experience."
        )
        self.assertEqual(parse(text), (3, None, None))

    def test_degree_or_years_alone_states_nothing_binding(self):
        self.assertEqual(parse("Bachelor's degree, or 4 years of experience."), (None, None, None))

    def test_up_to_is_a_maximum(self):
        self.assertEqual(parse("Candidates with up to 2 years of experience are encouraged."), (None, 2, None))

    def test_not_experience(self):
        for text in (
            "Must be at least 18 years of age.",
            "Paid sabbatical after 5 years of service.",
            "We have been in business for over 30 years.",
            "For over 50 years, Acme has been the leading provider of widgets.",
            "With over 30 years of experience in solar, we build more than panels.",
            "Clearance investigation completed within the last 5 years.",
            "Security+ certification is valid for 3 years.",
            "Contract Term: 4 Years",
        ):
            self.assertEqual(parse(text), (None, None, None), text)

    def test_candidate_sentence_with_with_over_is_kept(self):
        self.assertEqual(
            parse("You are a candidate with over 10 years of experience in security."), (10, None, None)
        )

    def test_service_desk_is_experience(self):
        self.assertEqual(parse("2+ years of service desk experience"), (2, None, None))

    def test_yoe_glued_years_and_bare_experience(self):
        self.assertEqual(parse("3+ YOE in a SOC"), (3, None, None))
        self.assertEqual(parse("2-4 YOE required"), (2, 4, None))
        self.assertEqual(parse("6+yrs Field Network Engineer"), (6, None, None))
        self.assertEqual(parse("11Yrs Software Asset Manager"), (11, None, None))
        self.assertEqual(parse("Experience: 5+"), (5, None, None))
        self.assertEqual(parse("Open to 3-5 year canidate"), (3, 5, None))

    def test_title_is_read_with_the_description(self):
        from db import enrich_text

        data = enrich_text("Systems Administrator (3-6 Years experience)", "Benefits listed here.")
        self.assertEqual(data["years_min"], 3)
        self.assertEqual(data["years_max"], 6)

    def test_no_numbers(self):
        self.assertEqual(parse("Entry level role, no prior experience needed."), (None, None, None))
        self.assertEqual(parse(""), (None, None, None))
        self.assertEqual(parse(None), (None, None, None))


class Chips(unittest.TestCase):
    def texts(self, *args):
        return [c["text"] for c in years_chips(*args)]

    def test_required_range_preferred(self):
        self.assertEqual(self.texts(3, None, None), ["3+ req"])
        self.assertEqual(self.texts(1, 5, None), ["1\u20135"])
        self.assertEqual(self.texts(None, None, 5), ["5+ preferred"])
        self.assertEqual(self.texts(3, None, 5), ["3+ req", "5+ preferred"])
        self.assertEqual(self.texts(None, None, None), [])
        self.assertEqual(self.texts(0, None, None), [])


class KeywordAndClearance(unittest.TestCase):
    def test_cert_spellings(self):
        from textutil import classify_clearance, keyword_pattern

        self.assertTrue(keyword_pattern("Security+").search("Must hold Sec+"))
        self.assertTrue(keyword_pattern("Security+").search("Security + required"))
        self.assertTrue(keyword_pattern("Security+").search("CompTIA Security plus"))
        self.assertFalse(keyword_pattern("Security+").search("Cybersecurity+ tools"))
        self.assertTrue(keyword_pattern("Network+").search("Net+ or N+"))
        self.assertTrue(keyword_pattern("Network+").search("A+, Network+, Security+"))
        self.assertFalse(keyword_pattern("Network+").search("Network Administrator"))
        self.assertFalse(keyword_pattern("Network+").search("computer networking experience"))
        self.assertTrue(keyword_pattern("CCNA").search("Cisco Certified Network Associate"))
        self.assertFalse(keyword_pattern("CCNA").search("CCNA Security"))
        self.assertTrue(keyword_pattern("AI-Assisted").search("AI assisted workflows"))
        self.assertTrue(keyword_pattern("Node.js").search("Node.js experience"))
        self.assertFalse(keyword_pattern("Node.js").search("Node js experience"))
        self.assertTrue(keyword_pattern("senior", whole_word=True).search("Sr. Engineer"))
        self.assertFalse(keyword_pattern("senior", whole_word=True).search("seniority track"))

    def test_aws_solutions_architect_spellings(self):
        from resume import CERTIFICATIONS
        from scrape import indeed_search_terms
        from textutil import keyword_pattern

        spellings = [
            "AWS Solutions Architect - Associate",
            "AWS Solutions Architect \u2013 Associate",
            "AWS Solutions Architect Associate",
            "AWS Solutions Architect (Associate)",
            "AWS Solutions Architect: Associate",
            "AWS Solutions Architect, Associate",
            "AWS Solutions Architect / Associate",
            "AWS Solutions Architect \u2013 Associate Level",
            "AWS Certified Solutions Architect Associate",
            "Amazon Web Services Certified Solutions Architect - Associate",
            "Amazon Web Services (AWS) Certified Solutions Architect - Associate",
            "Amazon Web Services (AWS) Solutions Architect - Associate",
            "SAA-C03",
        ]
        pattern = keyword_pattern("AWS Solutions Architect - Associate")
        cert = dict(CERTIFICATIONS)["AWS Solutions Architect"]
        for text in spellings:
            self.assertTrue(pattern.search(text), text)
            self.assertTrue(cert.search(text), text)
        self.assertFalse(pattern.search("AWS Solutions Architect"))
        self.assertFalse(pattern.search("AWS Solutions Architect Professional"))
        self.assertFalse(pattern.search("Senior Solutions Architect"))
        self.assertFalse(pattern.search("AWS Solutions Architect associated with the account"))
        long_name = keyword_pattern(
            "Amazon Web Services (AWS) Certified Solutions Architect - Associate"
        )
        self.assertTrue(long_name.search("AWS Solutions Architect (Associate)"))
        queries = [
            "AWS Solutions Architect Associate",
            "Amazon Web Services Solutions Architect Associate",
        ]
        self.assertEqual(indeed_search_terms("AWS Solutions Architect - Associate"), queries)
        self.assertEqual(indeed_search_terms("AWS Solutions Architect \u2013 Associate Level"), queries)
        self.assertEqual(indeed_search_terms("AWS"), ["AWS"])

    def test_network_plus_stance(self):
        from textutil import mention_stance

        laundry = "Certifications: A+, Network+, Security+ a plus"
        self.assertEqual(mention_stance(laundry, "Network+"), "preferred")
        self.assertEqual(
            mention_stance("Must hold CompTIA Network+ or equivalent", "Network+"),
            "required",
        )
        self.assertEqual(
            mention_stance("Experience with computer networks and TCP/IP", "Network+"),
            "absent",
        )

    def test_indeed_plus_queries(self):
        from scrape import indeed_search_terms

        self.assertEqual(
            indeed_search_terms("Security+"),
            ["CompTIA Security+", '"Security Plus"'],
        )
        self.assertEqual(indeed_search_terms("CompTIA Security+"), indeed_search_terms("Security+"))
        self.assertEqual(indeed_search_terms("Network+"), ["CompTIA Network+", '"Network Plus"'])
        self.assertEqual(indeed_search_terms("A+"), ["CompTIA A+", '"A Plus"'])
        self.assertEqual(indeed_search_terms("CySA+"), ["CompTIA CySA+", '"CySA Plus"'])
        self.assertEqual(indeed_search_terms("AWS"), ["AWS"])
        self.assertEqual(indeed_search_terms("Foo+"), ['"Foo Plus"'])
        self.assertNotIn('"Security+"', indeed_search_terms("Security+"))

    def test_clearance_codes(self):
        from textutil import classify_clearance

        self.assertEqual(classify_clearance("Security Clearance: minimum T3 required"), "secret")
        self.assertEqual(classify_clearance("Active TS/SCI"), "sci")
        self.assertEqual(classify_clearance("Clearance: Must be able to obtain a DOE L/Q Security Clearance"), "ts")
        self.assertEqual(classify_clearance("Tier 3 help desk support"), "unspecified")
        self.assertEqual(classify_clearance("TS//SCI eligibility required"), "sci")
        self.assertEqual(classify_clearance("Top-Secret clearance required"), "ts")
        self.assertEqual(classify_clearance("Must hold a Secret"), "secret")
        self.assertEqual(classify_clearance("Active Secret clearance required. TS/SCI preferred."), "secret")
        self.assertEqual(classify_clearance("Secret clearance required; Top Secret is a plus."), "secret")
        self.assertEqual(classify_clearance("MBI required"), "public_trust")
        self.assertEqual(classify_clearance("SF-86 required for this Secret position"), "secret")
        self.assertEqual(classify_clearance("TS/SCI preferred"), "sci")
        self.assertEqual(classify_clearance("The T3 conference room is on the third floor."), "unspecified")
        self.assertEqual(classify_clearance("Work is performed at the T5 building."), "unspecified")
        self.assertEqual(classify_clearance("Security Clearance: N/A"), "none")
        self.assertEqual(classify_clearance("NACLC"), "secret")
        self.assertEqual(classify_clearance("Position Sensitivity: Noncritical-Sensitive"), "secret")
        self.assertEqual(classify_clearance("Position Sensitivity: Critical-Sensitive"), "ts")
        self.assertEqual(classify_clearance("CI polygraph required"), "sci")
        self.assertEqual(classify_clearance("We protect trade secrets and confidential information."), "unspecified")
        self.assertEqual(classify_clearance("Secret Service liaison experience is a plus."), "unspecified")

    def test_years_phrases_and_cert_names(self):
        self.assertEqual(parse("10-plus years of experience in systems administration"), (10, None, None))
        self.assertEqual(parse("over 10 years of experience in cybersecurity"), (10, None, None))
        self.assertEqual(parse("a decade of experience in information security"), (10, None, None))
        self.assertEqual(parse("at least a year of experience with Active Directory"), (1, None, None))
        self.assertEqual(parse("a year of help desk experience"), (1, None, None))
        self.assertEqual(parse("Y.O.E.: 5+"), (5, None, None))
        self.assertEqual(parse("not required: 5+ years of Kubernetes"), (None, None, None))
        self.assertEqual(parse("6 months of experience in a SOC"), (0.5, None, None))
        self.assertEqual(parse("18 months of relevant experience"), (1.5, None, None))
        self.assertEqual(parse("36 months of progressive experience"), (3, None, None))
        self.assertEqual(parse("52 weeks of specialized experience"), (1, None, None))
        self.assertEqual(parse("two and a half years of experience"), (2.5, None, None))
        self.assertEqual(parse("3+ y.o.e. in networking"), (3, None, None))
        self.assertEqual(parse("eighteen years of experience in defense programs"), (18, None, None))
        self.assertEqual(parse("no less than 3 years of experience"), (3, None, None))
        self.assertEqual(parse("5+ years of experience is not required"), (None, None, None))
        self.assertEqual(parse("does not require 5 years of experience"), (None, None, None))
        self.assertEqual(parse("Software Engineer (3-5 years)"), (3, 5, None))
        self.assertEqual(parse("celebrating 25 years of delivering cyber solutions"), (None, None, None))
        self.assertEqual(
            parse("With over 30 years of experience in solar, we build more than panels."),
            (None, None, None),
        )

        from textutil import cert_stances

        self.assertEqual(cert_stances("Network+ a plus"), {"Network+": "preferred"})
        self.assertNotIn("A+", cert_stances("Network+ a plus"))
        self.assertEqual(cert_stances("CySA preferred"), {"CySA+": "preferred"})
        self.assertIn("CISSP", cert_stances("Certified Information Systems Security Professional"))
        self.assertIn("GCIH", cert_stances("GIAC Certified Incident Handler"))
        self.assertNotIn("CISA", cert_stances("per CISA guidelines and the CISA directive"))
        self.assertIn("CISA", cert_stances("CISA certification required"))
        self.assertNotIn("CSM", cert_stances("Customer Success Manager (CSM) will own the book"))
        self.assertIn("CSM", cert_stances("Certified Scrum Master"))
        self.assertNotIn("AZ-104", cert_stances("Senior Azure Administrator"))
        self.assertIn("AZ-104", cert_stances("AZ-104 or Azure Administrator Associate"))
        self.assertNotIn("A+", cert_stances("An A+ player who writes clean code"))
        self.assertIn("Security+", cert_stances("CompTIA Security+ SY0-701"))
        self.assertEqual(cert_stances("no CISSP required"), {})

    def test_remote_role_phrase(self):
        from db import infer_remote

        self.assertTrue(infer_remote(False, "Austin, TX", "Network Administrator", "This is an exempt remote role."))
        self.assertFalse(infer_remote(False, "Austin, TX", "Help Desk", "Experience with remote support tools."))
        self.assertFalse(infer_remote(False, "Austin, TX", "Help Desk", "This is not a remote position."))
        self.assertTrue(infer_remote(False, "Remote", "Help Desk", ""))
        self.assertTrue(infer_remote(True, "Austin, TX", "Help Desk", ""))


class CleanDescription(unittest.TestCase):
    def test_strips_known_fragments_only(self):
        raw = (
            'Great team. <Cite refs={["turn1search0"]}/>\n\nPay: $80,000\n'
            "<div>Our mission</div><br />Cloud &amp; Infra&nbsp;&nbsp;Ops\n"
            "Send a resume to <jobs@example.com>.\n**<Job Summary>**"
        )
        out = clean_description(raw)
        self.assertNotIn("Cite", out)
        self.assertNotIn("<div>", out)
        self.assertNotIn("<br", out)
        self.assertNotIn("&amp;", out)
        self.assertNotIn("&nbsp;", out)
        self.assertIn("Cloud & Infra Ops", out)
        self.assertIn("<jobs@example.com>", out)
        self.assertIn("<Job Summary>", out)


if __name__ == "__main__":
    unittest.main()
