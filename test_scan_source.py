import tempfile
import unittest
from pathlib import Path

import db
from db import decode_scan, encode_scan, list_keywords, query_jobs, scan_label, upsert_jobs


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


if __name__ == "__main__":
    unittest.main()
