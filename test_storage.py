import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import storage


class FakeBody:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeS3:
    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, Bucket, Key, Body, ServerSideEncryption=None):
        data = Body if isinstance(Body, bytes) else Body.read()
        self.objects[(Bucket, Key)] = data
        return {}

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise RuntimeError("NoSuchKey")
        return {"Body": FakeBody(self.objects[(Bucket, Key)])}

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)
        return {}


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeS3()
        storage.set_client(self.fake)
        self._bucket = storage.bucket_name()
        os_bucket = "winrole-test"
        import os

        os.environ["S3_BUCKET"] = os_bucket
        self.addCleanup(self._restore_bucket, os_bucket)

    def _restore_bucket(self, _name: str) -> None:
        import os

        if self._bucket:
            os.environ["S3_BUCKET"] = self._bucket
        else:
            os.environ.pop("S3_BUCKET", None)
        storage.set_client(None)

    def test_put_read_and_delete_round_trip(self) -> None:
        uri = storage.put_material(7, "resume.pdf", b"%PDF-1.4 resume body")
        self.assertTrue(uri.startswith("s3://winrole-test/materials/7/"))
        path = storage.resolve_stored(uri)
        self.assertIsNotNone(path)
        assert path is not None
        self.assertEqual(path.read_bytes(), b"%PDF-1.4 resume body")
        path.unlink()
        again = storage.resolve_stored(uri)
        assert again is not None
        self.assertEqual(again.read_bytes(), b"%PDF-1.4 resume body")
        storage.remove_stored(uri)
        self.assertEqual(self.fake.objects, {})
        self.assertIsNone(storage.resolve_stored(uri))

    def test_verify_probe_is_removed(self) -> None:
        message = storage.verify()
        self.assertIn("winrole-test", message)
        self.assertEqual(self.fake.objects, {})

    def test_local_file_still_resolves(self) -> None:
        import os

        os.environ.pop("S3_BUCKET", None)
        with TemporaryDirectory() as folder:
            path = Path(folder) / "note.txt"
            path.write_text("local", encoding="utf-8")
            found = storage.resolve_stored(str(path))
            self.assertEqual(found, path)
            storage.remove_stored(str(path))
            self.assertFalse(path.exists())

    def test_backup_uploads_the_database_bytes(self) -> None:
        with TemporaryDirectory() as folder:
            db = Path(folder) / "jobscraper.db"
            db.write_bytes(b"SQLite format 3\x00demo")
            uri = storage.backup_database(db)
        self.assertEqual(uri, "s3://winrole-test/backups/jobscraper.db")
        self.assertEqual(self.fake.objects[("winrole-test", "backups/jobscraper.db")], b"SQLite format 3\x00demo")


if __name__ == "__main__":
    unittest.main()
