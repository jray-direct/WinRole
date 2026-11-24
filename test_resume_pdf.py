"""PDF resumes round-trip through extraction and parsing."""

from __future__ import annotations

import unittest
from pathlib import Path


class PdfResumeTests(unittest.TestCase):
    def test_text_pdf_is_extracted_and_parsed(self) -> None:
        pymupdf = __import__("pymupdf")
        from resume import extract_resume_detail, parse_resume

        path = Path(__file__).resolve().parent / "_tmp_resume_test.pdf"
        doc = pymupdf.open()
        try:
            page = doc.new_page()
            page.insert_text(
                (72, 72),
                "Alex Kim\nPython, SQL, PowerShell\n"
                "Bachelor of Science in Computer Science\n"
                "CompTIA Security+\n2018 - 2022 Network Engineer",
            )
            doc.save(path)
        finally:
            doc.close()
        try:
            text, problem = extract_resume_detail(path)
            self.assertIsNone(problem)
            self.assertIn("Python", text)
            parsed = parse_resume(text)
            self.assertIn("Python", parsed.languages)
            self.assertIn("SQL", parsed.languages)
            self.assertTrue(parsed.degree_label)
        finally:
            path.unlink(missing_ok=True)

    def test_missing_file_explains_why(self) -> None:
        from resume import extract_resume_detail

        text, problem = extract_resume_detail(Path("no-such-resume.pdf"))
        self.assertEqual(text, "")
        self.assertIn("missing", problem or "")


if __name__ == "__main__":
    unittest.main()
