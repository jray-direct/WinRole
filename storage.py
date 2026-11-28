"""Private S3 storage for resumes, cover letters, and a database backup.

Production sets S3_BUCKET and AWS_DEFAULT_REGION on the server. The server's
IAM role supplies credentials. There is no access key in the app.

s3.env is only for a developer machine, and it should name a dev bucket.
Without S3_BUCKET, files stay in the local materials folder.

Objects use SSE-S3, which is included with S3. Region defaults to us-east-2,
same as Cognito. Keys are materials/{user}/..., backups/jobscraper.db, and
_health/... for the probe.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / "s3.env"
CACHE_DIR = ROOT / "materials" / "cache"

_client_override: Any = None


def _load_env_file() -> None:
    if not ENV_FILE.is_file():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and not os.environ.get(key):
            os.environ[key] = value


_load_env_file()


def bucket_name() -> str:
    return os.environ.get("S3_BUCKET", "").strip()


def region_name() -> str:
    return os.environ.get("AWS_DEFAULT_REGION", "").strip() or "us-east-2"


def enabled() -> bool:
    return bool(bucket_name())


def set_client(client: Any) -> None:
    """Tests pass a fake client. Production leaves this empty."""
    global _client_override
    _client_override = client


def _client() -> Any:
    if _client_override is not None:
        return _client_override
    import boto3

    return boto3.client("s3", region_name=region_name())


def _split(uri: str) -> tuple[str, str]:
    rest = uri[len("s3://") :]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        raise ValueError(f"Not an S3 location: {uri}")
    return bucket, key


def _cache_path(key: str) -> Path:
    return CACHE_DIR / Path(key).name


def is_s3(stored: str | None) -> bool:
    return bool(stored and stored.startswith("s3://"))


def put_material(user_id: int, filename: str, data: bytes) -> str:
    """Upload one resume or cover letter. Returns the s3:// location."""
    if not enabled():
        raise RuntimeError("S3_BUCKET is not set")
    ext = Path(filename).suffix.lower()
    key = f"materials/{int(user_id)}/{uuid.uuid4().hex}{ext}"
    try:
        _client().put_object(
            Bucket=bucket_name(),
            Key=key,
            Body=data,
            ServerSideEncryption="AES256",
        )
    except Exception as exc:
        raise ValueError(f"Could not store the file in S3: {exc}") from exc
    dest = _cache_path(key)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return f"s3://{bucket_name()}/{key}"


def resolve_stored(stored: str | None) -> Path | None:
    """Local path for a stored file. S3 objects are downloaded into the cache."""
    if not stored:
        return None
    if is_s3(stored):
        _bucket, key = _split(stored)
        dest = _cache_path(key)
        if dest.is_file():
            return dest
        try:
            response = _client().get_object(Bucket=_bucket, Key=key)
            body = response["Body"].read()
        except Exception:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        return dest
    path = Path(stored)
    if not path.is_absolute():
        path = ROOT / path
    return path if path.is_file() else None


def remove_stored(stored: str | None) -> None:
    if not stored:
        return
    if is_s3(stored):
        _bucket, key = _split(stored)
        try:
            _client().delete_object(Bucket=_bucket, Key=key)
        except Exception as exc:
            raise ValueError(f"Could not delete the file from S3: {exc}") from exc
        cache = _cache_path(key)
        if cache.is_file():
            cache.unlink()
        return
    path = Path(stored)
    if not path.is_absolute():
        path = ROOT / path
    if path.is_file():
        path.unlink()


def backup_database(db_path: Path | None = None) -> str:
    """Upload the SQLite file to backups/jobscraper.db. Overwrites the previous copy."""
    if not enabled():
        raise RuntimeError("S3_BUCKET is not set")
    path = db_path or (ROOT / "jobscraper.db")
    if not path.is_file():
        raise ValueError(f"Database file is missing: {path}")
    key = "backups/jobscraper.db"
    try:
        _client().put_object(
            Bucket=bucket_name(),
            Key=key,
            Body=path.read_bytes(),
            ServerSideEncryption="AES256",
        )
    except Exception as exc:
        raise ValueError(f"Could not back up the database to S3: {exc}") from exc
    return f"s3://{bucket_name()}/{key}"


def verify() -> str:
    """Put a probe object, read it back, and delete it. Raises when S3 is not usable."""
    if not enabled():
        raise RuntimeError("S3_BUCKET is not set. Add it to s3.env or the environment.")
    key = f"_health/{uuid.uuid4().hex}.txt"
    payload = b"winrole-s3-ok"
    client = _client()
    name = bucket_name()
    try:
        client.put_object(Bucket=name, Key=key, Body=payload, ServerSideEncryption="AES256")
        body = client.get_object(Bucket=name, Key=key)["Body"].read()
    except Exception as exc:
        raise RuntimeError(f"S3 check failed for bucket {name}: {exc}") from exc
    if body != payload:
        raise RuntimeError("S3 returned different bytes than were uploaded")
    try:
        client.delete_object(Bucket=name, Key=key)
    except Exception as exc:
        raise RuntimeError(f"S3 check could not delete the probe in {name}: {exc}") from exc
    try:
        client.get_object(Bucket=name, Key=key)
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code == "NoSuchKey" or "NoSuchKey" in str(exc):
            return f"S3 bucket {name} in {region_name()} stored, returned, and deleted a probe object."
        raise RuntimeError(f"S3 check failed while confirming delete in {name}: {exc}") from exc
    raise RuntimeError("S3 delete did not remove the probe object")


def status_line() -> str:
    if not enabled():
        return "S3: not configured. Resume files stay in the local materials folder."
    return f"S3: resumes and cover letters upload to {bucket_name()} ({region_name()})."


if __name__ == "__main__":
    print(verify())
