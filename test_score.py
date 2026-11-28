"""Run with: python -m unittest test_score -v"""

import gc
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
from app import app
from embeddings import best_similar_pairs, cosine
from resume import (
    canonical_skill,
    parse_resume,
    required_skill_penalty,
    score_job_resume,
)


FULL_RESUME = """Bachelor's degree in Computer Science
10 years of experience as a Software Engineer
Python
Kubernetes
Docker
Terraform
AWS
Linux
"""


def _job(skills: list[str]) -> dict:
    lines = [
        "Software Engineer",
        "Bachelor's degree in Computer Science required.",
        "5+ years of experience required.",
    ]
    lines.extend(f"{skill} is required." for skill in skills)
    lines.append("AWS and Linux in a production environment.")
    return {"id": 1, "title": "Software Engineer", "description": "\n".join(lines)}


def _without(text: str, *words: str) -> str:
    skip = set(words)
    return "\n".join(line for line in text.splitlines() if line.strip() not in skip)


class Sections(unittest.TestCase):
    def test_projects_do_not_count_as_work_or_a_degree(self):
        resume = parse_resume(
            """
            Experience
            Network Engineer
            Jan 2020 - Jan 2024
            Built firewalls with Docker
            Skills
            Kubernetes
            Education
            Bachelor of Science in Computer Science
            University of Example
            Sep 2014 - May 2018
            Projects
            PhD research assistant
            Jan 2014 - Jan 2018
            Python
            """
        )
        self.assertEqual(resume.experience_years, 4.0)
        self.assertIn("Docker", resume.skills)
        self.assertIn("Kubernetes", resume.skills)
        self.assertNotIn("Python", resume.languages)
        self.assertEqual(resume.degree_level, 2)
        self.assertIn("Computer Science", resume.degree_fields)

    def test_unsectioned_resume_still_parses(self):
        resume = parse_resume(FULL_RESUME)
        self.assertIn("Python", resume.languages)
        self.assertGreaterEqual(resume.degree_level, 2)
        self.assertEqual(resume.experience_years, 10.0)

    def test_real_heading_shapes_and_bullets(self):
        resume = parse_resume(
            """
            M.S. Computer Science
            1. PROFESSIONAL EXPERIENCE
            Security Engineer
            Jan 2019 - Present
            Projects: built an internal platform for the detection team.
            Python
            TECHNICAL PROJECTS
            Research assistant
            Jan 2012 - Dec 2015
            Java
            SKILLS & TOOLS
            Kubernetes, Docker
            E D U C A T I O N
            Bachelor of Science in Cybersecurity
            Security+
            """
        )
        self.assertGreaterEqual(resume.experience_years, 7.0)
        self.assertLess(resume.experience_years, 10.0)
        self.assertIn("Python", resume.languages)
        self.assertNotIn("Java", resume.languages)
        self.assertIn("Kubernetes", resume.skills)
        self.assertIn("Docker", resume.skills)
        self.assertGreaterEqual(resume.degree_level, 3)
        self.assertIn("Computer Science", resume.degree_fields)
        self.assertTrue(any("Security" in cert for cert in resume.certifications))

    def test_pdf_heading_shapes_from_a_real_resume(self):
        resume = parse_resume(
            "Professional\n"
            "Network Engineer, Jan 2020 - Jan 2024\n"
            "Built tooling with Python\n"
            "Education\u200b\n"
            "M.S. Cybersecurity\n"
            "B.S. Software Engineering\n"
            "Certifications\n"
            "Network+, Security+\n"
            "Technical Projects & Contributions\n"
            "Jan 2012 - Dec 2015\n"
            "Java\n"
        )
        self.assertEqual(resume.experience_years, 4.0)
        self.assertIn("Python", resume.languages)
        self.assertNotIn("Java", resume.languages)
        self.assertGreaterEqual(resume.degree_level, 3)
        self.assertIn("Cybersecurity", resume.degree_fields)
        self.assertTrue(any("Security" in cert for cert in resume.certifications))

    def test_job_dates_only_and_degree_lines_only(self):
        resume = parse_resume(
            """
            Summary
            Mastered detection engineering for a college SOC.
            Experience
            Analyst
            Jan 2022 - Jan 2024
            Mentored PhD students on the research desk.
            Certifications
            Security+
            Jan 2010 - Jan 2016
            Projects
            8 years of experience building Java tools
            Jan 2010 - Jan 2018
            Education
            Bachelor of Science in Computer Science
            """
        )
        self.assertEqual(resume.experience_years, 2.0)
        self.assertEqual(resume.degree_level, 2)
        self.assertIn("Computer Science", resume.degree_fields)
        self.assertNotIn("Java", resume.languages)
        self.assertTrue(any("Security" in cert for cert in resume.certifications))

        listed = parse_resume("Skills\nAcmeFlow\nBachelor's degree in Computer Science")
        self.assertIn("AcmeFlow", listed.extra_keywords)
        self.assertEqual(listed.degree_level, 2)
        self.assertIn("Computer Science", listed.degree_fields)

    def test_inline_skills_heading_keeps_the_list(self):
        resume = parse_resume("Experience\nNetwork Engineer\nSkills: Python, SQL\nProjects\nJava")
        self.assertIn("Python", resume.languages)
        self.assertIn("SQL", resume.languages)
        self.assertNotIn("Java", resume.languages)


class Overrides(unittest.TestCase):
    def test_add_and_remove_change_profile_and_signature(self):
        raw = "Python\nSecurity+\nBachelor's degree in Computer Science"
        detected = parse_resume(raw)
        edited = parse_resume(
            raw,
            None,
            {"remove_languages": ["Python"], "add_skills": ["Kubernetes"], "clear_degree": True},
        )
        self.assertIn("Python", detected.languages)
        self.assertNotIn("Python", edited.languages)
        self.assertIn("Kubernetes", edited.skills)
        self.assertGreaterEqual(detected.degree_level, 2)
        self.assertEqual(edited.degree_level, -1)
        self.assertEqual(edited.degree_fields, [])
        self.assertNotEqual(detected.signature, edited.signature)

    def test_added_alias_lands_on_the_catalog_label(self):
        edited = parse_resume("Built internal tools.", None, {"add_skills": ["k8s"]})
        self.assertIn("Kubernetes", edited.skills)
        self.assertNotIn("k8s", edited.skills)


class Aliases(unittest.TestCase):
    def test_postgres_and_postgresql_count_as_one_skill(self):
        self.assertEqual(canonical_skill("Postgres"), "PostgreSQL")
        self.assertEqual(canonical_skill("psql"), "PostgreSQL")
        resume = parse_resume("Daily psql administration and backups.")
        self.assertIn("PostgreSQL", resume.skills)
        job = {
            "title": "Database administrator",
            "description": "PostgreSQL is required. Manage backups and schemas for the team.",
        }
        result = score_job_resume(job, resume)
        self.assertIn("PostgreSQL", result["details"]["skills"])
        self.assertNotIn("PostgreSQL", result["details"]["missing_required_skills"])

        other_way = score_job_resume(
            {"title": "DBA", "description": "psql is required. Manage databases and backups for the team."},
            parse_resume("PostgreSQL administration and schema design."),
        )
        self.assertIn("PostgreSQL", other_way["details"]["skills"])
        self.assertNotIn("PostgreSQL", other_way["details"]["missing_required_skills"])


class RequiredPenalty(unittest.TestCase):
    def test_penalty_steps_of_eight_cap_at_twenty_four(self):
        self.assertEqual(required_skill_penalty(0), 0)
        self.assertEqual(required_skill_penalty(1), 8)
        self.assertEqual(required_skill_penalty(2), 16)
        self.assertEqual(required_skill_penalty(4), 24)
        self.assertEqual(required_skill_penalty(6), 24)

        asked = ["Python", "Kubernetes", "Docker", "Terraform"]
        job = _job(asked)
        full = score_job_resume(job, parse_resume(FULL_RESUME))
        miss1 = score_job_resume(job, parse_resume(_without(FULL_RESUME, "Python")))
        miss2 = score_job_resume(job, parse_resume(_without(FULL_RESUME, "Python", "Kubernetes")))
        miss4 = score_job_resume(job, parse_resume(_without(FULL_RESUME, *asked)))

        self.assertEqual(full["details"]["required_penalty"], 0)
        self.assertEqual(miss1["details"]["required_penalty"], 8)
        self.assertEqual(len(miss1["details"]["missing_required_skills"]), 1)
        self.assertEqual(miss2["details"]["required_penalty"], 16)
        self.assertEqual(miss4["details"]["required_penalty"], 24)
        self.assertLess(miss4["details"]["required_penalty"], 8 * 4)
        self.assertGreaterEqual(round(full["score"] - miss1["score"], 1), 8)
        self.assertGreaterEqual(round(miss1["score"] - miss2["score"], 1), 8)
        self.assertIn("Missing required:", miss1["tooltip"])


class SimilarSkills(unittest.TestCase):
    def test_threshold_and_required_skill_stays_missing(self):
        same = [1.0, 0.0]
        side = [0.0, 1.0]
        self.assertAlmostEqual(cosine(same, same), 1.0)
        self.assertEqual(
            best_similar_pairs(["AcmeFlowX"], ["AcmeFlow"], {"AcmeFlowX": same, "AcmeFlow": same}),
            [("AcmeFlowX", "AcmeFlow", 1.0)],
        )
        self.assertEqual(
            best_similar_pairs(["AcmeFlowX"], ["AcmeFlow"], {"AcmeFlowX": same, "AcmeFlow": side}),
            [],
        )

        resume = parse_resume("Skills:\nAcmeFlow\nBachelor's degree in Computer Science")
        job = {
            "title": "Platform engineer",
            "description": "Kubernetes is required.\nWe run AcmeFlowX in production every day.",
        }
        plain = score_job_resume(job, resume)
        with_sim = score_job_resume(
            job,
            resume,
            similar_pairs=[("AcmeFlowX", "AcmeFlow", 0.86)],
        )
        self.assertEqual(plain["details"]["required_penalty"], with_sim["details"]["required_penalty"])
        self.assertIn("Kubernetes", with_sim["details"]["missing_required_skills"])
        self.assertEqual(with_sim["details"]["similar"][0]["job"], "AcmeFlowX")
        self.assertIn("Similar:", with_sim["tooltip"])
        self.assertGreaterEqual(with_sim["score"], plain["score"])


class ReadoutAndFit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "score.db"
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["user"] = {"sub": "score-test", "email": "score@example.com"}

    def tearDown(self):
        db.DB_PATH = self.old_path
        gc.collect()
        self.tmp.cleanup()

    def test_readout_edit_fit_note_and_ai_without_a_key(self):
        saved = self.client.post(
            "/materials",
            data={
                "kind": "resume",
                "name": "Test resume",
                "body": "Python\nKubernetes\nBachelor's degree in Computer Science\n",
            },
            follow_redirects=True,
        )
        self.assertEqual(saved.status_code, 200)
        self.assertIn(b"Python", saved.data)
        self.assertIn(b"Kubernetes", saved.data)
        page = saved.request.path

        removed = self.client.post(
            page,
            data={"action": "remove_language", "value": "Python"},
            follow_redirects=True,
        )
        self.assertNotIn(b'value="Python"', removed.data)
        self.assertIn(b"Kubernetes", removed.data)

        cleared = self.client.post(
            page,
            data={"action": "set_degree", "degree_level": "none"},
            follow_redirects=True,
        )
        self.assertIn(b"no degree", cleared.data)

        conn = db.connect()
        try:
            user_id = conn.execute("SELECT id FROM users").fetchone()[0]
            conn.execute(
                """
                INSERT INTO jobs (dedupe_key, title, company, description, first_seen_at, last_seen_at)
                VALUES ('fit-1', 'Platform engineer', 'Example Co', ?, '2026-01-01T00:00:00', '2026-01-01T00:00:00')
                """,
                ("Kubernetes is required.\nPython is required.\nKeep the platform running.",),
            )
            job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO user_jobs (user_id, job_id, added_at) VALUES (?, ?, ?)",
                (user_id, job_id, "2026-01-01T00:00:00"),
            )
            conn.commit()
        finally:
            conn.close()

        pane = self.client.get(f"/jobs/{job_id}/pane")
        self.assertEqual(pane.status_code, 200)
        body = pane.get_data(as_text=True)
        self.assertIn("Matched.", body)
        self.assertIn("Missing required.", body)
        self.assertIn("Degree.", body)
        self.assertIn("Years.", body)
        self.assertIn("Python", body)
        self.assertIn("Explain this job", body)
        self.assertIn("Draft cover letter", body)
        self.assertIn('data-ai-ready="0"', body)

        explain = self.client.post(f"/jobs/{job_id}/explain")
        self.assertIn("AI_API_KEY", explain.get_json()["message"])
        self.assertFalse(explain.get_json()["ok"])

        saved_cover = self.client.post(
            f"/jobs/{job_id}/cover-save",
            json={"body": "Hello hiring team. I use Kubernetes."},
        )
        payload = saved_cover.get_json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["name"].startswith("Cover"))


class HeatListDeferTests(unittest.TestCase):
    def test_large_miss_does_not_block_the_list(self):
        resume = "EXPERIENCE\nNetwork Engineer\nJan 2018 - Dec 2019\nPython\n"
        old_limit = db.HEAT_SCORE_INLINE_LIMIT
        db.HEAT_SCORE_INLINE_LIMIT = 1
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "heat.db"
                uid = db.ensure_user("heat-defer", db_path=path)
                now = "2026-01-01T00:00:00"
                conn = db.connect(path)
                try:
                    conn.execute(
                        """
                        INSERT INTO materials
                            (user_id, kind, name, created_at, is_default, extracted_text)
                        VALUES (?, 'resume', 'Resume', ?, 1, ?)
                        """,
                        (uid, now, resume),
                    )
                    for index, posted in enumerate(("2024-01-01", "2024-06-01", "2024-03-01")):
                        conn.execute(
                            """
                            INSERT INTO jobs (
                                dedupe_key, title, description, date_posted, first_seen_at, last_seen_at
                            ) VALUES (?, 'Engineer', 'Python is required.', ?, ?, ?)
                            """,
                            (f"heat-{index}", posted, now, now),
                        )
                        job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                        conn.execute(
                            "INSERT INTO user_jobs (user_id, job_id, added_at) VALUES (?, ?, ?)",
                            (uid, job_id, now),
                        )
                    conn.commit()
                finally:
                    conn.close()

                rows, total = db.query_jobs(
                    uid,
                    {"order": "score"},
                    limit=10,
                    db_path=path,
                    defer_large_scores=True,
                )
                self.assertEqual(total, 3)
                self.assertTrue(rows.scores_pending)
                self.assertTrue(all(row.get("score_pending") for row in rows))
                self.assertEqual(rows[0]["date_posted"], "2024-06-01")
                db.wait_for_heat_scores(uid, timeout=20)
                again, _total = db.query_jobs(
                    uid,
                    {"order": "score"},
                    limit=10,
                    db_path=path,
                    defer_large_scores=True,
                )
                self.assertFalse(again.scores_pending)
                self.assertTrue(all(isinstance(row["score"], float) for row in again))
                db.wait_for_heat_scores(uid, timeout=20)
                gc.collect()
        finally:
            db.HEAT_SCORE_INLINE_LIMIT = old_limit
            gc.collect()


class HeatScoreWriteTests(unittest.TestCase):
    def test_score_write_skips_jobs_that_were_deleted(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "heat.db"
        uid = db.ensure_user("heat-fk", db_path=path)
        now = "2026-01-01 00:00:00"
        conn = db.connect(path)
        try:
            conn.execute(
                """
                INSERT INTO jobs (
                    dedupe_key, title, description, first_seen_at, last_seen_at
                ) VALUES ('live', 'Engineer', 'Python is required.', ?, ?)
                """,
                (now, now),
            )
            live_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                "INSERT INTO user_jobs (user_id, job_id, added_at) VALUES (?, ?, ?)",
                (uid, live_id, now),
            )
            conn.commit()
            indexes = {row[1] for row in conn.execute("PRAGMA index_list(user_jobs)")}
        finally:
            conn.close()
        self.assertIn("idx_user_jobs_job", indexes)
        resume = parse_resume("Python\n", None)
        db._score_heat_rows(
            uid,
            [
                {"id": live_id, "title": "Engineer", "description": "Python is required."},
                {"id": live_id + 999, "title": "Engineer", "description": "Python is required."},
            ],
            resume,
            "",
            path,
        )
        conn = db.connect(path)
        try:
            saved = [
                row[0]
                for row in conn.execute(
                    "SELECT job_id FROM job_heat_scores WHERE user_id = ?",
                    (uid,),
                )
            ]
        finally:
            conn.close()
        self.assertEqual(saved, [live_id])
        gc.collect()

    def test_jobs_page_does_not_bind_every_posting(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "heat.db"
        uid = db.ensure_user("heat-page", db_path=path)
        now = "2026-01-01 00:00:00"
        conn = db.connect(path)
        try:
            conn.execute(
                """
                INSERT INTO materials
                    (user_id, kind, name, created_at, is_default, extracted_text)
                VALUES (?, 'resume', 'Resume', ?, 1, ?)
                """,
                (uid, now, "Python\n"),
            )
            conn.executemany(
                """
                INSERT INTO jobs (
                    dedupe_key, title, description, date_posted, first_seen_at, last_seen_at
                ) VALUES (?, 'Engineer', 'Python is required.', ?, ?, ?)
                """,
                [(f"j{i}", f"2024-01-{(i % 28) + 1:02d}", now, now) for i in range(60)],
            )
            job_ids = [row[0] for row in conn.execute("SELECT id FROM jobs")]
            conn.executemany(
                "INSERT INTO user_jobs (user_id, job_id, added_at) VALUES (?, ?, ?)",
                [(uid, job_id, now) for job_id in job_ids],
            )
            conn.commit()
        finally:
            conn.close()

        real_connect = db.connect

        def limited(db_path=None):
            opened = real_connect(db_path)
            opened.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 50)
            return opened

        old_limit = db.HEAT_SCORE_INLINE_LIMIT
        db.HEAT_SCORE_INLINE_LIMIT = 1
        try:
            with patch.object(db, "connect", limited):
                rows, total = db.query_jobs(
                    uid,
                    {"order": "score"},
                    limit=5,
                    offset=5,
                    db_path=path,
                    defer_large_scores=True,
                )
                self.assertEqual(total, 60)
                self.assertEqual(len(rows), 5)
                self.assertTrue(rows.scores_pending)
                self.assertTrue(all(row.get("score_pending") for row in rows))
                db.wait_for_heat_scores(uid, timeout=30)
        finally:
            db.HEAT_SCORE_INLINE_LIMIT = old_limit
            gc.collect()


if __name__ == "__main__":
    unittest.main()
