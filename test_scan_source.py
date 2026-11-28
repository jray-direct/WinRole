import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import app as appmod
import db
from db import decode_scan, encode_scan, ensure_user, list_keywords, query_jobs, scan_label, set_all_locations_enabled, upsert_jobs


class ScanSourceTests(unittest.TestCase):
    def test_label_and_picker_value(self):
        self.assertEqual(scan_label("Network", "spacex"), "Network · SpaceX")
        self.assertEqual(scan_label("Network+", "indeed"), "Network+ · Indeed")
        self.assertEqual(scan_label("cloud", "amazon"), "cloud · Amazon")
        self.assertEqual(scan_label("AI", "ibm"), "AI · IBM")
        self.assertEqual(encode_scan("spacex", "Network"), "spacex|Network")
        self.assertEqual(decode_scan("spacex|Network"), ("spacex", "Network"))
        self.assertEqual(decode_scan("Network"), ("", "Network"))
        self.assertEqual(decode_scan("nope|Network"), ("", "nope|Network"))

    def test_same_keyword_on_two_boards_stays_two_scans(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "scans.db"
        uid = db.local_user_id(path)
        upsert_jobs(
            uid,
            [{"_dedupe_key": "sx", "title": "Network Engineer", "company": "SpaceX", "site": "spacex", "description": "Network"}],
            "Network",
            db_path=path,
            source="spacex",
        )
        upsert_jobs(
            uid,
            [{"_dedupe_key": "in", "title": "Network Analyst", "company": "Other", "site": "indeed", "description": "Network"}],
            "Network",
            db_path=path,
            source="indeed",
        )
        scans = list_keywords(uid, path)
        labels = sorted(row["label"] for row in scans)
        self.assertEqual(labels, ["Network · Indeed", "Network · SpaceX"])
        spacex, _total = query_jobs(uid, {"keyword": "Network", "scan_source": "spacex"}, db_path=path)
        self.assertEqual([row["company"] for row in spacex], ["SpaceX"])
        indeed, _total = query_jobs(uid, {"keyword": "Network", "scan_source": "indeed"}, db_path=path)
        self.assertEqual([row["company"] for row in indeed], ["Other"])
        db.delete_jobs_for_keyword(uid, "Network", path, source="spacex")
        left = list_keywords(uid, path)
        self.assertEqual([row["label"] for row in left], ["Network · Indeed"])

    def test_bulk_delete_drops_jobs_shared_only_by_the_selected_scans(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "scans.db"
        uid = db.local_user_id(path)
        upsert_jobs(
            uid,
            [{"_dedupe_key": "only-a", "title": "Only A", "company": "A", "description": "Security+"}],
            "Security+",
            db_path=path,
            source="indeed",
        )
        upsert_jobs(
            uid,
            [{"_dedupe_key": "only-b", "title": "Only B", "company": "B", "description": "Network+"}],
            "Network+",
            db_path=path,
            source="indeed",
        )
        upsert_jobs(
            uid,
            [{"_dedupe_key": "both", "title": "Both", "company": "Both", "description": "Security+ Network+"}],
            "Security+",
            db_path=path,
            source="indeed",
        )
        upsert_jobs(
            uid,
            [{"_dedupe_key": "both", "title": "Both", "company": "Both", "description": "Security+ Network+"}],
            "Network+",
            db_path=path,
            source="indeed",
        )
        upsert_jobs(
            uid,
            [{"_dedupe_key": "kept", "title": "Kept", "company": "Kept", "description": "Security+ CEH"}],
            "Security+",
            db_path=path,
            source="indeed",
        )
        upsert_jobs(
            uid,
            [{"_dedupe_key": "kept", "title": "Kept", "company": "Kept", "description": "Security+ CEH"}],
            "CEH",
            db_path=path,
            source="indeed",
        )
        result = db.delete_jobs_for_keywords(
            uid,
            [("Security+", "indeed"), ("Network+", "indeed")],
            path,
        )
        self.assertEqual(result["deleted"], 3)
        self.assertEqual(result["kept"], 1)
        left = list_keywords(uid, path)
        self.assertEqual([row["label"] for row in left], ["CEH · Indeed"])
        rows, _total = query_jobs(uid, {}, db_path=path)
        self.assertEqual([row["company"] for row in rows], ["Kept"])

    def test_delete_while_another_connection_is_writing(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "scans.db"
        uid = db.local_user_id(path)
        upsert_jobs(
            uid,
            [{"_dedupe_key": "only-a", "title": "Only A", "company": "A", "description": "Security+"}],
            "Security+",
            db_path=path,
            source="indeed",
        )
        stop = threading.Event()
        errors: list[BaseException] = []

        def writer() -> None:
            try:
                while not stop.is_set():
                    with db.connect(path) as conn:
                        conn.execute(
                            "UPDATE jobs SET last_seen_at = ? WHERE dedupe_key = ?",
                            (db.utc_now(), "only-a"),
                        )
                        conn.commit()
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            result = db.delete_jobs_for_keywords(uid, [("Security+", "indeed")], path)
        finally:
            stop.set()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result["deleted"], 1)


class MultiTargetScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "scans.db"
        self._old = db.DB_PATH
        db.DB_PATH = self.path
        self.addCleanup(setattr, db, "DB_PATH", self._old)
        db.init_db(self.path)
        appmod.app.config["TESTING"] = True

    def _client(self):
        client = appmod.app.test_client()
        with client.session_transaction() as sess:
            sess["user"] = {"sub": "scan-targets", "email": "scan-targets@example.com"}
        return client

    def test_delete_form_selects_scans_in_bulk(self):
        uid = ensure_user("scan-targets", "scan-targets@example.com", self.path)
        upsert_jobs(
            uid,
            [{"_dedupe_key": "sec", "title": "Analyst", "company": "Acme", "description": "Security+"}],
            "Security+",
            db_path=self.path,
            source="indeed",
        )
        html = self._client().get("/jobs").get_data(as_text=True)
        self.assertIn('id="delete-all"', html)
        self.assertIn('id="delete-none"', html)
        self.assertIn('name="keyword" value="indeed|Security+"', html)
        self.assertIn("delete-pick", html)
        self.assertNotIn("data-ask-delete", html)

    def test_picker_is_a_checkbox_list(self):
        html = self._client().get("/jobs?scan=1").get_data(as_text=True)
        self.assertIn('id="scan-targets"', html)
        self.assertIn('id="sources-all"', html)
        self.assertIn(">Select all<", html)
        self.assertIn('id="sources-none"', html)
        self.assertIn(">Select none<", html)
        self.assertIn('name="source" value="indeed"', html)
        self.assertIn('name="source" value="spacex"', html)
        self.assertIn('name="source" value="oracle"', html)
        self.assertNotIn("<select", html[html.find('id="scan-targets"'):html.find('id="scan-keyword"')])
        self.assertIn('name="scan_name"', html)
        self.assertNotIn(">Title filters<", html)

    def test_starting_a_scan_saves_its_name(self):
        client = self._client()
        with patch.object(appmod, "_enqueue_scan", return_value=0) as enqueue:
            res = client.post(
                "/scrape",
                data={
                    "source": ["spacex", "amazon"],
                    "keyword": "Network",
                    "scan_name": "  Cert hunt  ",
                    "title_include": "analyst",
                    "title_exclude": "intern",
                },
                follow_redirects=True,
            )
        batch = enqueue.call_args.args[1]
        self.assertEqual(batch[0].title_include, [])
        self.assertEqual(batch[0].title_exclude, [])
        self.assertIn("Scan started for &#39;Cert hunt&#39; on SpaceX and Amazon.", res.get_data(as_text=True))
        uid = ensure_user("scan-targets", "scan-targets@example.com", self.path)
        conn = db.connect(self.path)
        try:
            rows = conn.execute(
                "SELECT source, name FROM scan_names WHERE user_id = ? ORDER BY source",
                (uid,),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual([(row["source"], row["name"]) for row in rows], [("amazon", "Cert hunt"), ("spacex", "Cert hunt")])

    def test_existing_scans_can_be_renamed(self):
        uid = ensure_user("scan-targets", "scan-targets@example.com", self.path)
        upsert_jobs(
            uid,
            [{"_dedupe_key": "sec", "title": "Analyst", "company": "Acme", "description": "Security+"}],
            "Security+",
            db_path=self.path,
            source="indeed",
        )
        client = self._client()
        res = client.post(
            "/scans/names",
            data={"scan": ["indeed|Security+"], "name": ["Cert hunt"]},
            follow_redirects=True,
        )
        html = res.get_data(as_text=True)
        self.assertIn("Scan names saved.", html)
        self.assertIn("Cert hunt · Indeed", html)
        rows = list_keywords(uid, self.path)
        self.assertEqual(rows[0]["name"], "Cert hunt")
        self.assertEqual(rows[0]["label"], "Cert hunt · Indeed")
        client.post(
            "/scans/names",
            data={"scan": ["indeed|Security+"], "name": ["  "]},
            follow_redirects=True,
        )
        rows = list_keywords(uid, self.path)
        self.assertEqual(rows[0]["name"], "")
        self.assertEqual(rows[0]["label"], "Security+ · Indeed")

    def test_checked_targets_become_one_batch(self):
        client = self._client()
        with patch.object(appmod, "_enqueue_scan", return_value=0) as enqueue:
            res = client.post(
                "/scrape",
                data={
                    "source": ["spacex", "amazon"],
                    "keyword": "Network",
                    "sx_programs": "Starlink",
                    "amz_categories": "Software Development",
                },
                follow_redirects=True,
            )
        self.assertEqual(enqueue.call_count, 1)
        batch = enqueue.call_args.args[1]
        self.assertEqual([row.source for row in batch], ["spacex", "amazon"])
        self.assertEqual(batch[0].keyword, "Network")
        self.assertEqual(batch[1].keyword, "Network")
        self.assertEqual(batch[0].board_programs, ["Starlink"])
        self.assertEqual(batch[1].board_programs, [])
        self.assertEqual(batch[1].board_categories, ["Software Development"])
        self.assertEqual(batch[0].board_categories, [])
        self.assertIn("Scan started for &#39;Network&#39; on SpaceX and Amazon.", res.get_data(as_text=True))

    def test_a_pasted_link_fills_only_that_target(self):
        client = self._client()
        with patch.object(appmod, "_enqueue_scan", return_value=0) as enqueue:
            res = client.post(
                "/scrape",
                data={
                    "source": "spacex",
                    "keyword": "",
                    "board_url": "https://www.spacex.com/careers/jobs?keyword=Network",
                },
                follow_redirects=True,
            )
        batch = enqueue.call_args.args[1]
        self.assertEqual(batch[0].keyword, "Network")
        self.assertEqual(batch[0].source, "spacex")
        self.assertIn("on the SpaceX careers page", res.get_data(as_text=True))

    def test_missing_keyword_on_one_target_does_not_start(self):
        client = self._client()
        with patch.object(appmod, "_enqueue_scan", return_value=0) as enqueue:
            res = client.post(
                "/scrape",
                data={
                    "source": ["spacex", "amazon"],
                    "keyword": "",
                    "board_url": "https://www.spacex.com/careers/jobs?keyword=Network",
                },
                follow_redirects=True,
            )
        enqueue.assert_not_called()
        self.assertIn("Enter a keyword.", res.get_data(as_text=True))

    def test_no_target_and_indeed_without_locations(self):
        client = self._client()
        with patch.object(appmod, "_enqueue_scan", return_value=0) as enqueue:
            empty = client.post("/scrape", data={"keyword": "Network"}, follow_redirects=True)
            enqueue.assert_not_called()
            self.assertIn("Pick at least one place to scan.", empty.get_data(as_text=True))
            uid = ensure_user("scan-targets", "scan-targets@example.com", self.path)
            set_all_locations_enabled(uid, False, db_path=self.path)
            blocked = client.post(
                "/scrape",
                data={"source": "indeed", "keyword": "Network"},
                follow_redirects=True,
            )
        enqueue.assert_not_called()
        self.assertIn("Enable at least one location first.", blocked.get_data(as_text=True))

    def test_where_text_and_combined_result(self):
        self.assertEqual(appmod._scan_where_text(["indeed"]), "")
        self.assertEqual(appmod._scan_where_text(["spacex"]), " on the SpaceX careers page")
        self.assertEqual(appmod._scan_where_text(["indeed", "ibm"]), " on Indeed and IBM")
        self.assertEqual(
            appmod._selected_scan_sources(["Amazon", "nope", "amazon", "hpe"]),
            ["amazon", "hpe"],
        )
        combined = appmod._combine_scan_results(
            [
                {"run_id": 4, "keyword": "Network", "matched": 2, "filtered": 1, "inserted": 1, "updated": 0},
                {"run_id": 5, "keyword": "Network", "matched": 3, "filtered": 3, "inserted": 2, "updated": 1},
            ]
        )
        self.assertEqual(combined["run_id"], 5)
        self.assertEqual(combined["sources"], 2)
        self.assertEqual(combined["matched"], 5)
        self.assertEqual(combined["filtered"], 4)
        self.assertEqual(combined["inserted"], 3)


if __name__ == "__main__":
    unittest.main()
