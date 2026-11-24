"""Rail and the ten model jobs. Run: python -m unittest test_ai -v"""

import gc
import tempfile
import unittest
from pathlib import Path

import ai_client
import db
from app import app
from resume import parse_resume, score_job_resume
from textutil import _KEYWORD_STANCE_POINTS, score_job


def _score_span(html: str) -> str:
    marker = 'class="n score '
    start = html.find(marker)
    if start < 0:
        return ""
    open_end = html.find(">", start)
    close = html.find("<", open_end)
    return html[open_end + 1 : close].strip()


class Rail(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ai.db"
        self._old_transport = ai_client._transport
        ai_client._transport = None
        self._env = {
            key: __import__("os").environ.get(key)
            for key in ("AI_API_KEY", "AI_MODEL", "AI_LIGHT_MODEL", "AI_MEDIUM_MODEL")
        }

    def tearDown(self):
        ai_client._transport = self._old_transport
        import os

        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        gc.collect()
        self.tmp.cleanup()

    def test_models_fall_back_and_failure_returns_nothing(self):
        import os

        os.environ["AI_MODEL"] = "fallback-model"
        os.environ.pop("AI_LIGHT_MODEL", None)
        os.environ.pop("AI_MEDIUM_MODEL", None)
        os.environ.pop("AI_API_KEY", None)
        cfg = ai_client.ai_settings()
        self.assertEqual(cfg["model"], "fallback-model")
        self.assertEqual(cfg["light"], "fallback-model")
        self.assertEqual(cfg["medium"], "fallback-model")
        self.assertIsNone(ai_client.ask_model("t", "light", "hi", system="s", db_path=self.path))

        os.environ["AI_LIGHT_MODEL"] = "light-model"
        os.environ["AI_MEDIUM_MODEL"] = "medium-model"
        calls = []

        def explode(_payload):
            calls.append("fail")
            raise OSError("down")

        ai_client._transport = explode
        self.assertIsNone(ai_client.ask_model("t", "light", "hi", system="s", db_path=self.path))
        self.assertEqual(calls, ["fail"])
        self.assertIsNone(db.ai_cache_get("t", "light-model", "missing", self.path))

    def test_cache_is_task_model_and_input_hash(self):
        import os

        os.environ["AI_LIGHT_MODEL"] = "light-model"
        calls = []

        def transport(payload):
            calls.append(payload["model"])
            return '{"text":"cached answer"}'

        ai_client._transport = transport
        first = ai_client.ask_model("demo", "light", "same input", system="sys", db_path=self.path)
        second = ai_client.ask_model("demo", "light", "same input", system="sys", db_path=self.path)
        self.assertEqual(first, {"text": "cached answer"})
        self.assertEqual(second, first)
        self.assertEqual(calls, ["light-model"])
        conn = db.connect(self.path)
        try:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(ai_cache)")]
        finally:
            conn.close()
        self.assertEqual(cols, ["task", "model", "input_hash", "answer", "created_at"])
        self.assertNotIn("score", cols)
        self.assertNotIn("points", cols)

    def test_banned_phrase_and_parser_fields_are_dropped(self):
        self.assertIsNone(ai_client.reject_banned("Heat Score is 80", ["heat score"]))
        self.assertEqual(ai_client.reject_banned("The posting is on-call.", ["heat score"]), "The posting is on-call.")
        ai_client._transport = lambda _payload: '{"years": 4, "text": "fine"}'
        self.assertIsNone(ai_client.ask_model("t", "light", "in", system="s", db_path=self.path))
        ai_client._transport = lambda _payload: '{"text": "Heat Score is 10"}'
        self.assertIsNone(
            ai_client.ask_model("t", "light", "in", system="s", db_path=self.path, banned=["heat score"])
        )


class FitAndSections(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "fit.db"
        self._old = ai_client._transport
        ai_client._transport = None

    def tearDown(self):
        ai_client._transport = self._old
        gc.collect()
        self.tmp.cleanup()

    def test_fit_sentence_uses_four_fields_and_drops_other_skills(self):
        seen = {}

        def transport(payload):
            seen["user"] = payload["messages"][1]["content"]
            if seen.get("bad"):
                return '{"text":"You match Python. You also use Terraform."}'
            return '{"text":"You match Python. SQL is still required."}'

        ai_client._transport = transport
        kept = ai_client.fit_sentence(
            ["Python"],
            ["SQL"],
            "Bachelor's meets Bachelor's",
            "5 yrs meets 3+ req",
            db_path=self.path,
        )
        self.assertEqual(kept, "You match Python. SQL is still required.")
        self.assertIn("Matched: Python", seen["user"])
        self.assertIn("Missing required: SQL", seen["user"])
        self.assertIn("Degree:", seen["user"])
        self.assertIn("Years:", seen["user"])
        self.assertNotIn("Description", seen["user"])
        self.assertNotIn("Title:", seen["user"])
        seen["bad"] = True
        # Different resume signature so the bad reply is not served from cache.
        self.assertIsNone(
            ai_client.fit_sentence(
                ["Python"],
                ["SQL"],
                "Bachelor's meets Bachelor's",
                "5 yrs meets 3+ req",
                resume_sig="other",
                db_path=self.path,
            )
        )

    def test_section_labels_skip_experience_headings_and_project_dates(self):
        text = "Campus portal\nJan 2020 - May 2020\nNetwork Engineer\nJan 2018 - Dec 2019\n"

        def labeler(lines):
            labels = {}
            for index, line in enumerate(lines):
                if "2020" in line or "portal" in line.lower():
                    labels[index] = "projects"
                else:
                    labels[index] = "experience"
            return labels

        plain = parse_resume(text, section_labeler=None)
        guided = parse_resume(text, section_labeler=labeler)
        self.assertEqual(guided.experience_years, 2.0)
        self.assertGreater(plain.experience_years, guided.experience_years)

        calls = []

        def transport(payload):
            calls.append(payload["messages"][1]["content"])
            return '{"years": 9, "lines": [{"index": 0, "section": "experience"}]}'

        ai_client._transport = transport
        old_db = db.DB_PATH
        db.DB_PATH = self.path
        try:
            dropped = parse_resume(text)
            self.assertEqual(dropped.experience_years, plain.experience_years)
            self.assertNotEqual(dropped.experience_years, 9)

            headed = (
                "Experience\nNetwork Engineer\nJan 2018 - Dec 2019\n"
                "Projects\nCampus portal\nJan 2020 - May 2020\n"
            )
            before = len(calls)
            parsed = parse_resume(headed)
            self.assertEqual(len(calls), before)
            self.assertEqual(parsed.experience_years, 2.0)
        finally:
            db.DB_PATH = old_db


class StanceKeywordsAndNotes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "stance.db"
        self._old = ai_client._transport
        ai_client._transport = None

    def tearDown(self):
        ai_client._transport = self._old
        gc.collect()
        self.tmp.cleanup()

    def test_regex_wins_and_points_are_not_stored(self):
        self.assertEqual(
            _KEYWORD_STANCE_POINTS,
            {"required": 10, "preferred": 4, "equivalent": 2, "mentioned": 0},
        )
        _score, parts = score_job({"title": "Engineer", "kw_stance": "required"}, "Python", None)
        self.assertIn(("Python required", 10.0), parts)
        calls = []

        def transport(payload):
            calls.append(payload["messages"][1]["content"])
            if "tools list" in payload["messages"][1]["content"]:
                return '{"stance":"required","points":10}'
            return '{"stance":"preferred"}'

        ai_client._transport = transport
        uid = db.local_user_id(self.path)
        db.upsert_jobs(
            uid,
            [
                {
                    "_dedupe_key": "mentioned",
                    "title": "Engineer",
                    "description": "We use Python in production.",
                },
                {
                    "_dedupe_key": "required-already",
                    "title": "Engineer",
                    "description": "Python is required for this role.",
                },
                {
                    "_dedupe_key": "points",
                    "title": "Engineer",
                    "description": "Python appears in the tools list.",
                },
            ],
            "Python",
            db_path=self.path,
        )
        conn = db.connect(self.path)
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(job_keywords)")}
            rows = {
                row["dedupe_key"]: row["stance"]
                for row in conn.execute(
                    """
                    SELECT j.dedupe_key, jk.stance
                    FROM jobs j JOIN job_keywords jk ON jk.job_id = j.id
                    """
                )
            }
        finally:
            conn.close()
        self.assertNotIn("points", cols)
        self.assertNotIn("score", cols)
        self.assertEqual(rows["mentioned"], "preferred")
        self.assertEqual(rows["required-already"], "required")
        self.assertEqual(rows["points"], "mentioned")
        self.assertTrue(all("Python is required" not in text for text in calls))


class ClickJobs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "click.db"
        db._INIT_PATHS.discard(str(db.DB_PATH))
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["user"] = {"sub": "ai-test", "email": "ai@example.com"}
        self._old = ai_client._transport
        ai_client._transport = None
        self._key = __import__("os").environ.get("AI_API_KEY")

    def tearDown(self):
        import os

        ai_client._transport = self._old
        if self._key is None:
            os.environ.pop("AI_API_KEY", None)
        else:
            os.environ["AI_API_KEY"] = self._key
        db.DB_PATH = self.old_path
        gc.collect()
        self.tmp.cleanup()

    def _resume(self, body: str, name: str = "Resume") -> str:
        saved = self.client.post(
            "/materials",
            data={"kind": "resume", "name": name, "body": body, "is_default": "1"},
            follow_redirects=True,
        )
        self.assertEqual(saved.status_code, 200)
        return saved.request.path

    def _job(self, description: str, *, status: str = "new", notes: str = "") -> int:
        conn = db.connect()
        try:
            user_id = conn.execute("SELECT id FROM users").fetchone()[0]
            conn.execute(
                """
                INSERT INTO jobs (
                    dedupe_key, title, company, description, first_seen_at, last_seen_at
                ) VALUES (?, 'Platform engineer', 'Example Co', ?, '2026-01-01T00:00:00', '2026-01-01T00:00:00')
                """,
                (description[:24], description),
            )
            job_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                """
                INSERT INTO user_jobs (user_id, job_id, added_at, status, notes)
                VALUES (?, ?, '2026-01-01T00:00:00', ?, ?)
                """,
                (user_id, job_id, status, notes or None),
            )
            conn.commit()
            return job_id
        finally:
            conn.close()

    def test_suggestion_does_not_change_heat_until_add(self):
        import app as app_module

        page = self._resume("Python\nAcmeFlow\nBachelor's degree in Computer Science\n")
        original = app_module.suggest_unknown_phrases

        def fake(_phrases, **_kwargs):
            return [{"phrase": "AcmeFlow", "label": "Docker"}]

        app_module.suggest_unknown_phrases = fake
        try:
            job_id = self._job("Kubernetes is required.\nPython is required.\n")
            conn = db.connect()
            try:
                user_id = conn.execute("SELECT user_id FROM materials").fetchone()[0]
            finally:
                conn.close()
            resume = db.get_user_resume_profile(user_id)
            before = score_job_resume({"title": "Platform engineer", "description": "Kubernetes is required."}, resume)
            shown = self.client.get(page)
            body = shown.get_data(as_text=True)
            self.assertIn("AcmeFlow → Docker", body)
            self.assertIn('name="action" value="add_skill"', body)
            self.assertIn('value="Docker"', body)
            self.assertIn("data-dismiss-suggestion", body)
            self.assertNotIn('action" value="remove_skill"', body[body.find("AcmeFlow") :])
            after = score_job_resume({"title": "Platform engineer", "description": "Kubernetes is required."}, resume)
            self.assertEqual(before["score"], after["score"])
            conn = db.connect()
            try:
                overrides = conn.execute("SELECT overrides FROM materials").fetchone()[0]
            finally:
                conn.close()
            self.assertFalse(overrides)
            added = self.client.post(page, data={"action": "add_skill", "value": "Docker"}, follow_redirects=True)
            self.assertIn("Docker", added.get_data(as_text=True))
            self.assertNotEqual(job_id, 0)
        finally:
            app_module.suggest_unknown_phrases = original

    def test_click_jobs_do_not_move_the_score_or_start_work(self):
        import os

        self._resume(
            "Experience\nNetwork Engineer\nMaintained firewalls for the campus network\n"
            "Skills\nPython\n",
            name="Exp resume",
        )
        job_id = self._job(
            "Kubernetes is required.\nPython is used on the team.\n"
            "This role is on-call and requires a clearance. Travel is rare.\n",
            status="applied",
            notes="Left a voicemail.",
        )
        pane = self.client.get(f"/jobs/{job_id}/pane")
        score_before = _score_span(pane.get_data(as_text=True))
        self.assertTrue(score_before)

        state = {"explain": "bad"}

        def transport(payload):
            system = payload["messages"][0]["content"]
            if "two or three sentences" in system:
                return '{"text":"You match Python. Kubernetes is still required."}'
            if "Explain this posting" in system:
                if state["explain"] == "bad":
                    return '{"text":"This role is a 90/100 match."}'
                return '{"text":"The posting is on-call and requires a clearance. Travel is rare."}'
            if "cover letter" in system:
                return '{"text":"I maintained firewalls and use Python. I would like to join Example Co."}'
            if "Rephrase the experience" in system:
                return '{"text":"Maintained campus firewalls for the network."}'
            if "interview questions" in system:
                return '{"questions":["Tell me about a time you used Python to maintain a network."]}'
            if "follow-up note" in system:
                return '{"note":"Call again next week about the voicemail.","status":"interview"}'
            if "Do not start a search" in system:
                return '{"keyword":"Python"}'
            return '{"text":""}'

        ai_client._transport = transport
        os.environ["AI_API_KEY"] = "test-key"

        explained = self.client.post(f"/jobs/{job_id}/explain")
        self.assertFalse(explained.get_json()["ok"])
        self.assertNotIn("score", explained.get_json())
        again = self.client.get(f"/jobs/{job_id}/pane")
        again_body = again.get_data(as_text=True)
        self.assertEqual(_score_span(again_body), score_before)
        self.assertLess(again_body.index("You match Python"), again_body.index("Matched."))

        state["explain"] = "good"
        clean = self.client.post(f"/jobs/{job_id}/explain")
        self.assertTrue(clean.get_json()["ok"])
        self.assertNotIn("score", clean.get_json())
        self.assertNotIn("90/100", clean.get_json()["text"])
        self.assertEqual(_score_span(self.client.get(f"/jobs/{job_id}/pane").get_data(as_text=True)), score_before)

        cover = self.client.post(f"/jobs/{job_id}/cover-draft")
        self.assertTrue(cover.get_json()["ok"])
        self.assertIn("firewalls", cover.get_json()["text"])
        self.assertIn("Save to vault", self.client.get(f"/jobs/{job_id}/pane").get_data(as_text=True))

        ai_client._transport = lambda _payload: '{"text":"I have deep Kubernetes experience from 1999."}'
        bad_cover = ai_client.draft_cover(
            {"id": 99, "company": "Example Co", "title": "Platform engineer"},
            {"matched": ["Python"], "missing_required": ["Kubernetes"], "degree": "", "years": ""},
            "Maintained firewalls for the campus network",
        )
        self.assertIsNone(bad_cover)
        ai_client._transport = transport

        bullets = self.client.post(f"/jobs/{job_id}/bullets")
        self.assertTrue(bullets.get_json()["ok"])
        self.assertIn("firewalls", bullets.get_json()["text"])
        ai_client._transport = lambda _payload: '{"text":"Administered Kubernetes clusters."}'
        bad_bullets = ai_client.tailor_bullets(
            "Maintained firewalls for the campus network",
            "Kubernetes clusters",
            job_id="bad-bullets",
        )
        self.assertIsNone(bad_bullets)
        ai_client._transport = transport
        conn = db.connect()
        try:
            before_text = conn.execute("SELECT extracted_text, path FROM materials").fetchone()
        finally:
            conn.close()
        rejected = self.client.post(
            f"/jobs/{job_id}/bullets-save",
            json={"body": "Administered Kubernetes clusters."},
        )
        self.assertFalse(rejected.get_json()["ok"])
        conn = db.connect()
        try:
            after_reject = conn.execute("SELECT extracted_text, path FROM materials").fetchone()
        finally:
            conn.close()
        self.assertEqual(after_reject["path"], before_text["path"])
        self.assertEqual(after_reject["extracted_text"], before_text["extracted_text"])
        self.assertEqual(_score_span(self.client.get(f"/jobs/{job_id}/pane").get_data(as_text=True)), score_before)
        saved = self.client.post(
            f"/jobs/{job_id}/bullets-save",
            json={"body": bullets.get_json()["text"]},
        )
        self.assertTrue(saved.get_json()["ok"])
        conn = db.connect()
        try:
            after_save = conn.execute("SELECT extracted_text, path FROM materials").fetchone()
        finally:
            conn.close()
        self.assertEqual(after_save["path"], before_text["path"])
        self.assertIn("firewalls", after_save["extracted_text"] or "")

        questions = self.client.post(f"/jobs/{job_id}/interview")
        self.assertTrue(questions.get_json()["ok"])
        self.assertIn("Python", " ".join(questions.get_json()["questions"]))
        ai_client._transport = lambda _payload: '{"questions":["Tell me about your experience with Kubernetes."]}'
        bad_questions = ai_client.interview_questions(
            ["Python"],
            ["Kubernetes"],
            "The posting asks for more.",
            job_id="bad-ask",
        )
        self.assertIsNone(bad_questions)
        ai_client._transport = transport

        conn = db.connect()
        try:
            runs_before = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        finally:
            conn.close()
        suggested = self.client.post("/scrape/suggest-keyword")
        self.assertEqual(suggested.get_json().get("keyword"), "Python")
        ai_client._transport = lambda _payload: '{"keyword":"Terraform"}'
        self.assertIsNone(ai_client.suggest_scan_keyword(["Python", "only-this"], [], []))
        ai_client._transport = transport
        conn = db.connect()
        try:
            runs_after = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(runs_before, runs_after)

        note = self.client.post(f"/jobs/{job_id}/follow-up-note")
        self.assertEqual(note.get_json().get("notes"), "Call again next week about the voicemail.")
        conn = db.connect()
        try:
            row = conn.execute("SELECT status, notes FROM user_jobs WHERE job_id = ?", (job_id,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "applied")
        self.assertEqual(row["notes"], "Left a voicemail.")


if __name__ == "__main__":
    unittest.main()
