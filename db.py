"""SQLite storage for locations, jobs, keywords, and scrape runs."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from resume import (
    ResumeProfile,
    extract_resume_detail,
    extract_resume_text,
    heat_cache_sig,
    heat_tier,
    load_overrides,
    normalize_overrides,
    parse_resume,
    score_job_resume,
)
from textutil import (
    CERT_STACK,
    CLEARANCE_LABELS,
    CLEARANCE_ORDER,
    CLEARANCE_RANK,
    STANCE_LABELS,
    cert_stances,
    classify_clearance,
    clean_description,
    has_affirmative_mention,
    has_any_phrase,
    keyword_pattern,
    make_snippet,
    mention_stance,
    parse_phrases,
    score_job,
    score_tooltip,
    should_exclude,
)
from years import parse_years, years_chips

DB_PATH = Path(__file__).resolve().parent / "jobscraper.db"

NATIONWIDE_LOCATIONS: tuple[str, ...] = ("United States", "Remote")

REGION_ORDER: tuple[str, ...] = (
    "southwest",
    "northwest",
    "midwest",
    "southeast",
    "northeast",
)

REGION_LABELS: dict[str, str] = {
    "southwest": "Southwest",
    "northwest": "Northwest",
    "midwest": "Midwest",
    "southeast": "Southeast",
    "northeast": "Northeast",
}

REGION_STATES: dict[str, tuple[tuple[str, str], ...]] = {
    "southwest": (
        ("Arizona", "AZ"),
        ("California", "CA"),
        ("Colorado", "CO"),
        ("Hawaii", "HI"),
        ("Nevada", "NV"),
        ("New Mexico", "NM"),
        ("Oklahoma", "OK"),
        ("Texas", "TX"),
        ("Utah", "UT"),
    ),
    "northwest": (
        ("Alaska", "AK"),
        ("Idaho", "ID"),
        ("Montana", "MT"),
        ("Oregon", "OR"),
        ("Washington", "WA"),
        ("Wyoming", "WY"),
    ),
    "midwest": (
        ("Illinois", "IL"),
        ("Indiana", "IN"),
        ("Iowa", "IA"),
        ("Kansas", "KS"),
        ("Michigan", "MI"),
        ("Minnesota", "MN"),
        ("Missouri", "MO"),
        ("Nebraska", "NE"),
        ("North Dakota", "ND"),
        ("Ohio", "OH"),
        ("South Dakota", "SD"),
        ("Wisconsin", "WI"),
    ),
    "southeast": (
        ("Alabama", "AL"),
        ("Arkansas", "AR"),
        ("Florida", "FL"),
        ("Georgia", "GA"),
        ("Kentucky", "KY"),
        ("Louisiana", "LA"),
        ("Mississippi", "MS"),
        ("North Carolina", "NC"),
        ("South Carolina", "SC"),
        ("Tennessee", "TN"),
        ("Virginia", "VA"),
        ("West Virginia", "WV"),
    ),
    "northeast": (
        ("Connecticut", "CT"),
        ("Delaware", "DE"),
        ("Maine", "ME"),
        ("Maryland", "MD"),
        ("Massachusetts", "MA"),
        ("New Hampshire", "NH"),
        ("New Jersey", "NJ"),
        ("New York", "NY"),
        ("Pennsylvania", "PA"),
        ("Rhode Island", "RI"),
        ("Vermont", "VT"),
        ("Washington, DC", "DC"),
    ),
}

STATE_ABBR_TO_NAME: dict[str, str] = {
    abbr: name
    for states in REGION_STATES.values()
    for name, abbr in states
}
STATE_NAME_TO_ABBR: dict[str, str] = {
    name.lower(): abbr
    for states in REGION_STATES.values()
    for name, abbr in states
}
STATE_NAME_TO_REGION: dict[str, str] = {
    name.lower(): region
    for region, states in REGION_STATES.items()
    for name, _abbr in states
}

DEFAULT_LOCATIONS: list[str] = [
    *NATIONWIDE_LOCATIONS,
    *(name for region in REGION_ORDER for name, _abbr in REGION_STATES[region]),
]

LEGACY_DEFAULT_CITIES: frozenset[str] = frozenset(
    name.lower()
    for name in (
        "New York, NY",
        "Los Angeles, CA",
        "Chicago, IL",
        "Houston, TX",
        "Phoenix, AZ",
        "Philadelphia, PA",
        "San Antonio, TX",
        "San Diego, CA",
        "Dallas, TX",
        "San Jose, CA",
        "Austin, TX",
        "Jacksonville, FL",
        "Fort Worth, TX",
        "Columbus, OH",
        "Charlotte, NC",
        "Indianapolis, IN",
        "San Francisco, CA",
        "Seattle, WA",
        "Denver, CO",
        "Boston, MA",
        "El Paso, TX",
        "Nashville, TN",
        "Detroit, MI",
        "Oklahoma City, OK",
        "Portland, OR",
        "Las Vegas, NV",
        "Memphis, TN",
        "Louisville, KY",
        "Baltimore, MD",
        "Milwaukee, WI",
        "Albuquerque, NM",
        "Tucson, AZ",
        "Fresno, CA",
        "Sacramento, CA",
        "Mesa, AZ",
        "Kansas City, MO",
        "Atlanta, GA",
        "Miami, FL",
        "Raleigh, NC",
        "Omaha, NE",
        "Colorado Springs, CO",
        "Minneapolis, MN",
        "Tampa, FL",
        "New Orleans, LA",
        "Cleveland, OH",
        "Orlando, FL",
        "Salt Lake City, UT",
        "Pittsburgh, PA",
        "Cincinnati, OH",
        "St. Louis, MO",
        "Riverside, CA",
        "Richmond, VA",
        "Buffalo, NY",
        "Birmingham, AL",
        "Boise, ID",
        "Des Moines, IA",
        "Anchorage, AK",
        "Honolulu, HI",
    )
)

STATUSES = (
    "new",
    "interested",
    "applied",
    "followed_up",
    "interviewing",
    "offered",
    "accepted",
    "rejected",
    "ghosted",
    "withdrawn",
    "ignored",
)
STATUS_LABELS = {
    "new": "New",
    "interested": "Interested",
    "applied": "Applied",
    "followed_up": "Followed up",
    "interviewing": "Interviewing",
    "offered": "Offer",
    "accepted": "Accepted",
    "rejected": "Rejected",
    "ghosted": "Ghosted",
    "withdrawn": "Withdrawn",
    "ignored": "Ignored",
}
HEARD_BACK = (
    ("none", "No reply"),
    ("auto_reject", "Auto-reject"),
    ("recruiter", "Recruiter"),
    ("hiring_manager", "Hiring manager"),
    ("interview", "Interview"),
    ("offer", "Offer"),
    ("rejected", "Rejected"),
)
HEARD_BACK_LABELS = dict(HEARD_BACK)
PIPELINE_STATUSES = (
    "interested",
    "applied",
    "followed_up",
    "interviewing",
    "offered",
)
MATERIALS_DIR = Path(__file__).resolve().parent / "materials"
JOB_TRACKER_COLS = (
    "applied_on",
    "follow_up_on",
    "heard_back",
    "notes",
    "resume_id",
    "cover_id",
)
_INIT_PATHS: set[str] = set()
_INSIGHTS_CACHE: dict[str, dict[str, Any]] = {}
_RESUME_PROFILE_CACHE: dict[str, ResumeProfile] = {}
REPAIR_VERSION = 2
# Bump when the years parser or text enrichment changes; init_db re-runs it once.
ENRICH_VERSION = 5
# Bump when default locations migrate from cities to states organized by region.
LOCATIONS_STATE_VERSION = 6

# Where a stored pay figure came from. "direct_data" is what every job saved before
# the split was marked with; it can be either of the other two until it is seen again.
SALARY_SOURCES = ("employer", "estimate", "direct_data")
PROFILE_CLEARANCES = ("none", "public_trust", "confidential", "secret", "ts", "sci")
REMOTE_PREFS = (("any", "No preference"), ("remote", "Remote"), ("onsite", "On-site"))
_PART_TIME_RE = re.compile(r"part[\s_-]?time|adjunct|per[\s_-]?diem", re.I)
_REMOTE_WORD = re.compile(r"\bremote\b", re.I)
# "this is a remote role" is the job. "remote support" and "not a remote position" are not.
_REMOTE_ROLE = re.compile(
    r"\b(?:fully|100%|completely)\s+remote\s+(?:role|position|job|work|opportunity)\b|"
    r"\bremote\s+(?:role|position|job|opportunity)\b|"
    r"\bremotely\s+(?:from|within|in|anywhere)\b",
    re.I,
)
_REMOTE_NEGATED = re.compile(
    r"\b(?:not|no|non|never)\b(?:\s+\w+){0,4}\s+remote\b|"
    r"\bremote\b(?:\s+\w+){0,5}\s+not\b",
    re.I,
)
JOB_TYPES = (
    "fulltime",
    "parttime",
    "contract",
    "temporary",
    "internship",
    "perdiem",
    "other",
    "volunteer",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _invalidate_caches() -> None:
    _INSIGHTS_CACHE.clear()
    _RESUME_PROFILE_CACHE.clear()


LOCAL_EXTERNAL_ID = "local-owner"
_LOCAL_UID: dict[str, int] = {}

# Tracker fields that used to live on `jobs`. After migration they are renamed to
# legacy_* (kept for safety) and the per-user copy in user_jobs is authoritative.
_LEGACY_JOB_COLS = ("status", "applied_on", "follow_up_on", "heard_back", "notes", "resume_id", "cover_id")
_LEGACY_JOB_DECLS = (
    "applied_on TEXT",
    "follow_up_on TEXT",
    "heard_back TEXT NOT NULL DEFAULT 'none'",
    "notes TEXT",
    "resume_id INTEGER REFERENCES materials(id) ON DELETE SET NULL",
    "cover_id INTEGER REFERENCES materials(id) ON DELETE SET NULL",
)


def init_db(db_path: Path | None = None) -> Path:
    path = db_path or DB_PATH
    key = str(path)
    if key in _INIT_PATHS:
        return path
    with connect(path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                external_id TEXT NOT NULL UNIQUE,
                email TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                sort_order INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedupe_key TEXT NOT NULL UNIQUE,
                job_url TEXT,
                job_url_direct TEXT,
                site TEXT,
                site_job_id TEXT,
                title TEXT,
                company TEXT,
                location TEXT,
                date_posted TEXT,
                job_type TEXT,
                is_remote INTEGER NOT NULL DEFAULT 0,
                salary_min REAL,
                salary_max REAL,
                salary_interval TEXT,
                salary_currency TEXT,
                salary_annual_min REAL,
                salary_annual_max REAL,
                salary_source TEXT,
                emails TEXT,
                description TEXT,
                company_industry TEXT,
                company_url TEXT,
                company_num_employees TEXT,
                company_revenue TEXT,
                company_rating REAL,
                company_reviews_count INTEGER,
                skills TEXT,
                experience_range TEXT,
                search_location TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS job_keywords (
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                keyword TEXT NOT NULL COLLATE NOCASE,
                PRIMARY KEY (job_id, keyword)
            );

            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                locations_count INTEGER,
                raw_fetched INTEGER DEFAULT 0,
                matched INTEGER DEFAULT 0,
                inserted INTEGER DEFAULT 0,
                updated INTEGER DEFAULT 0,
                filtered INTEGER DEFAULT 0,
                filters_json TEXT,
                errors TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
            CREATE INDEX IF NOT EXISTS idx_jobs_location ON jobs(location);
            CREATE INDEX IF NOT EXISTS idx_jobs_date_posted ON jobs(date_posted);
            CREATE INDEX IF NOT EXISTS idx_jobs_remote ON jobs(is_remote);
            CREATE INDEX IF NOT EXISTS idx_jobs_last_seen ON jobs(last_seen_at);
            CREATE INDEX IF NOT EXISTS idx_jobs_salary ON jobs(salary_annual_min);
            CREATE INDEX IF NOT EXISTS idx_keywords_keyword ON job_keywords(keyword);

            CREATE TABLE IF NOT EXISTS materials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL CHECK (kind IN ('resume', 'cover')),
                name TEXT NOT NULL,
                filename TEXT,
                path TEXT,
                body TEXT,
                created_at TEXT NOT NULL,
                is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1))
            );
            """
        )
        _migrate_schema(conn)
        _migrate_multiuser(conn)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < REPAIR_VERSION:
            _backfill_clearance(conn)
            _repair_stored_jobs(conn)
        if version < ENRICH_VERSION:
            _backfill_enrichment(conn)
        if version < LOCATIONS_STATE_VERSION:
            _migrate_locations_to_states(conn)
        target_version = max(REPAIR_VERSION, ENRICH_VERSION, LOCATIONS_STATE_VERSION)
        if version < target_version:
            conn.execute(f"PRAGMA user_version = {target_version}")
            conn.commit()
            _invalidate_caches()
        _INIT_PATHS.add(key)
    return path


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Columns and tables that are the same for every user. Safe to run every start."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    for col, sql in (
        ("clearance", "ALTER TABLE jobs ADD COLUMN clearance TEXT"),
        ("years_min", "ALTER TABLE jobs ADD COLUMN years_min REAL"),
        ("years_max", "ALTER TABLE jobs ADD COLUMN years_max REAL"),
        ("years_pref", "ALTER TABLE jobs ADD COLUMN years_pref REAL"),
        ("cert_stances", "ALTER TABLE jobs ADD COLUMN cert_stances TEXT"),
    ):
        if col not in cols:
            conn.execute(sql)
    kw_cols = {row[1] for row in conn.execute("PRAGMA table_info(job_keywords)")}
    if "stance" not in kw_cols:
        conn.execute("ALTER TABLE job_keywords ADD COLUMN stance TEXT")
    if "source" not in kw_cols:
        conn.execute(
            "ALTER TABLE job_keywords ADD COLUMN source TEXT NOT NULL DEFAULT 'indeed'"
        )
    _backfill_scan_sources(conn)
    mat_cols = {row[1] for row in conn.execute("PRAGMA table_info(materials)")}
    if "extracted_text" not in mat_cols:
        conn.execute("ALTER TABLE materials ADD COLUMN extracted_text TEXT")
    if "overrides" not in mat_cols:
        conn.execute("ALTER TABLE materials ADD COLUMN overrides TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_years_min ON jobs(years_min)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS profile (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            years REAL,
            certs TEXT,
            clearance TEXT NOT NULL DEFAULT 'none',
            min_pay REAL,
            remote_pref TEXT NOT NULL DEFAULT 'any',
            updated_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_materials_kind ON materials(kind)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS filter_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL CHECK (kind IN ('title_include', 'title_exclude', 'exclude')),
            name TEXT NOT NULL,
            value TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(kind, name COLLATE NOCASE)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_filter_templates_kind ON filter_templates(kind)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS job_heat_scores (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            resume_sig TEXT NOT NULL,
            score REAL NOT NULL,
            tier TEXT NOT NULL,
            tooltip TEXT,
            details_json TEXT,
            PRIMARY KEY (user_id, job_id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_job_heat_sig ON job_heat_scores(user_id, resume_sig)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS embedding_cache (
            owner TEXT NOT NULL,
            phrase TEXT NOT NULL,
            vector TEXT NOT NULL,
            PRIMARY KEY (owner, phrase)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_cache (
            task TEXT NOT NULL,
            model TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            answer TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (task, model, input_hash)
        )
        """
    )


def _table_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _rebuild(
    conn: sqlite3.Connection,
    table: str,
    create_sql: str,
    copy_sql: str,
    indexes: tuple[str, ...] = (),
) -> None:
    """Replace a table whose shape changed. Rows are copied across by copy_sql.

    The old table is renamed first so copy_sql can read it as `<table>__old`; dropping
    it also drops its indexes, which is why indexes are recreated last.
    """
    conn.execute(f"ALTER TABLE {table} RENAME TO {table}__old")
    conn.execute(create_sql)
    conn.execute(copy_sql)
    conn.execute(f"DROP TABLE {table}__old")
    for sql in indexes:
        conn.execute(sql)


def _seed_user(conn: sqlite3.Connection, user_id: int) -> None:
    """Give a user the default city list and default title-exclude template, if they have none."""
    if conn.execute("SELECT 1 FROM locations WHERE user_id = ? LIMIT 1", (user_id,)).fetchone() is None:
        conn.executemany(
            "INSERT INTO locations (user_id, name, enabled, sort_order) VALUES (?, ?, 1, ?)",
            [(user_id, name, i) for i, name in enumerate(DEFAULT_LOCATIONS)],
        )
    if conn.execute("SELECT 1 FROM filter_templates WHERE user_id = ? LIMIT 1", (user_id,)).fetchone() is None:
        conn.execute(
            """
            INSERT INTO filter_templates (user_id, kind, name, value, created_at)
            VALUES (?, 'title_exclude', 'Senior / leadership', ?, ?)
            """,
            (user_id, "Senior, Sr., Sr, Lead, Manager, Principal, Staff, Director", utc_now()),
        )


def _migrate_multiuser(conn: sqlite3.Connection) -> None:
    """Add per-user ownership. Idempotent: each step checks whether it is already done.

    Everything that existed before this change belongs to the local owner account.
    """
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing_owner = conn.execute(
            "SELECT id FROM users WHERE external_id = ?", (LOCAL_EXTERNAL_ID,)
        ).fetchone()
        created = existing_owner is None
        if created:
            conn.execute(
                "INSERT INTO users (external_id, created_at) VALUES (?, ?)",
                (LOCAL_EXTERNAL_ID, utc_now()),
            )
        owner = int(
            conn.execute("SELECT id FROM users WHERE external_id = ?", (LOCAL_EXTERNAL_ID,)).fetchone()[0]
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_jobs (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                added_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                applied_on TEXT,
                follow_up_on TEXT,
                heard_back TEXT NOT NULL DEFAULT 'none',
                notes TEXT,
                resume_id INTEGER REFERENCES materials(id) ON DELETE SET NULL,
                cover_id INTEGER REFERENCES materials(id) ON DELETE SET NULL,
                PRIMARY KEY (user_id, job_id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_jobs_status ON user_jobs(user_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_jobs_follow_up ON user_jobs(user_id, follow_up_on)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_jobs_applied ON user_jobs(user_id, applied_on)")

        if "user_id" not in _table_cols(conn, "job_keywords"):
            _rebuild(
                conn,
                "job_keywords",
                """
                CREATE TABLE job_keywords (
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    keyword TEXT NOT NULL COLLATE NOCASE,
                    stance TEXT,
                    source TEXT NOT NULL DEFAULT 'indeed',
                    PRIMARY KEY (user_id, job_id, keyword)
                )
                """,
                f"INSERT INTO job_keywords (user_id, job_id, keyword, stance, source) "
                f"SELECT {owner}, job_id, keyword, stance, "
                f"COALESCE(source, 'indeed') FROM job_keywords__old",
                (
                    "CREATE INDEX IF NOT EXISTS idx_keywords_keyword ON job_keywords(keyword)",
                    "CREATE INDEX IF NOT EXISTS idx_job_keywords_job ON job_keywords(job_id)",
                ),
            )

        if "user_id" not in _table_cols(conn, "locations"):
            _rebuild(
                conn,
                "locations",
                """
                CREATE TABLE locations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL COLLATE NOCASE,
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (user_id, name)
                )
                """,
                f"INSERT INTO locations (id, user_id, name, enabled, sort_order) "
                f"SELECT id, {owner}, name, enabled, sort_order FROM locations__old",
            )

        if "user_id" not in _table_cols(conn, "profile"):
            _rebuild(
                conn,
                "profile",
                """
                CREATE TABLE profile (
                    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    years REAL,
                    certs TEXT,
                    clearance TEXT NOT NULL DEFAULT 'none',
                    min_pay REAL,
                    remote_pref TEXT NOT NULL DEFAULT 'any',
                    updated_at TEXT
                )
                """,
                f"INSERT INTO profile (user_id, years, certs, clearance, min_pay, remote_pref, updated_at) "
                f"SELECT {owner}, years, certs, clearance, min_pay, remote_pref, updated_at "
                f"FROM profile__old ORDER BY id LIMIT 1",
            )

        if "user_id" not in _table_cols(conn, "filter_templates"):
            _rebuild(
                conn,
                "filter_templates",
                """
                CREATE TABLE filter_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK (kind IN ('title_include', 'title_exclude', 'exclude')),
                    name TEXT NOT NULL,
                    value TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, kind, name COLLATE NOCASE)
                )
                """,
                f"INSERT INTO filter_templates (id, user_id, kind, name, value, created_at) "
                f"SELECT id, {owner}, kind, name, value, created_at FROM filter_templates__old",
                ("CREATE INDEX IF NOT EXISTS idx_filter_templates_kind ON filter_templates(kind)",),
            )

        if "user_id" not in _table_cols(conn, "runs"):
            conn.execute("ALTER TABLE runs ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
            conn.execute("UPDATE runs SET user_id = ? WHERE user_id IS NULL", (owner,))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_user ON runs(user_id, id)")

        if "user_id" not in _table_cols(conn, "materials"):
            conn.execute("ALTER TABLE materials ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
            conn.execute("UPDATE materials SET user_id = ? WHERE user_id IS NULL", (owner,))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_materials_user ON materials(user_id, kind)")

        jobs_cols = _table_cols(conn, "jobs")
        if "status" in jobs_cols:
            # One-time move of tracker fields from jobs to the owner's user_jobs rows.
            for col, decl in zip(_LEGACY_JOB_COLS[1:], _LEGACY_JOB_DECLS):
                if col not in jobs_cols:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {decl}")
            for name in ("idx_jobs_status", "idx_jobs_follow_up", "idx_jobs_applied_on"):
                conn.execute(f"DROP INDEX IF EXISTS {name}")
            for col in _LEGACY_JOB_COLS:
                conn.execute(f"ALTER TABLE jobs RENAME COLUMN {col} TO legacy_{col}")
            conn.execute(
                """
                INSERT OR IGNORE INTO user_jobs (
                    user_id, job_id, added_at, status, applied_on, follow_up_on,
                    heard_back, notes, resume_id, cover_id
                )
                SELECT ?, id, first_seen_at, COALESCE(legacy_status, 'new'), legacy_applied_on,
                       legacy_follow_up_on, COALESCE(legacy_heard_back, 'none'), legacy_notes,
                       legacy_resume_id, legacy_cover_id
                FROM jobs
                """,
                (owner,),
            )

        if created:
            _seed_user(conn, owner)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def ensure_user(external_id: str, email: str | None = None, db_path: Path | None = None) -> int:
    """Return the user's id, creating the account (with default cities and templates) if new.

    The first real sign-in takes over the local placeholder account, so jobs already
    scraped in this database stay with that person. Later sign-ups get their own rows.
    """
    external_id = (external_id or "").strip()
    if not external_id:
        raise ValueError("Missing user id")
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, email FROM users WHERE external_id = ?", (external_id,)
        ).fetchone()
        if row is not None:
            cleaned_email = _blank(email)
            if cleaned_email and row["email"] != cleaned_email:
                conn.execute("UPDATE users SET email = ? WHERE id = ?", (cleaned_email, row["id"]))
                conn.commit()
            return int(row["id"])
        owner = conn.execute(
            "SELECT id FROM users WHERE external_id = ?", (LOCAL_EXTERNAL_ID,)
        ).fetchone()
        others = conn.execute(
            "SELECT COUNT(*) FROM users WHERE external_id <> ?", (LOCAL_EXTERNAL_ID,)
        ).fetchone()
        if owner is not None and int(others[0]) == 0 and external_id != LOCAL_EXTERNAL_ID:
            conn.execute(
                "UPDATE users SET external_id = ?, email = ? WHERE id = ?",
                (external_id, _blank(email), owner[0]),
            )
            conn.commit()
            _LOCAL_UID.pop(str(db_path or DB_PATH), None)
            return int(owner[0])
        cur = conn.execute(
            "INSERT INTO users (external_id, email, created_at) VALUES (?, ?, ?)",
            (external_id, _blank(email), utc_now()),
        )
        user_id = int(cur.lastrowid)
        _seed_user(conn, user_id)
        conn.commit()
    return user_id


def local_user_id(db_path: Path | None = None) -> int:
    """The placeholder account used until Cognito sign-in is wired in."""
    key = str(db_path or DB_PATH)
    if key not in _LOCAL_UID:
        _LOCAL_UID[key] = ensure_user(LOCAL_EXTERNAL_ID, db_path=db_path)
    return _LOCAL_UID[key]


def _backfill_clearance(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT id, title, description FROM jobs
        WHERE clearance IS NULL OR TRIM(IFNULL(clearance,'')) = ''
        """
    ).fetchall()
    for row in rows:
        level = classify_clearance(f"{row['title'] or ''}\n{row['description'] or ''}")
        conn.execute("UPDATE jobs SET clearance = ? WHERE id = ?", (level, row["id"]))


def enrich_text(title: Any, description: Any) -> dict[str, Any]:
    """Everything derived from a posting's text: cleaned description, years, cert stances.

    One function so the one-time backfill and every scan insert/update agree.
    """
    text = clean_description(description if isinstance(description, str) else None)
    # The title is part of the posting. "8+ Years" and "Remote" live there as often
    # as they live in the body, and the body is what the cert and clearance reads use.
    hay = f"{title or ''}\n{text or ''}"
    years = parse_years(hay)
    certs = cert_stances(hay) if text or title else {}
    return {
        "description": text,
        "years_min": years.years_min,
        "years_max": years.years_max,
        "years_pref": years.years_pref,
        "cert_stances": json.dumps(certs) if certs else None,
        "clearance": classify_clearance(hay),
    }


SCAN_SOURCES = {
    "indeed": "Indeed",
    "spacex": "SpaceX",
    "amazon": "Amazon",
    "ibm": "IBM",
    "oracle": "Oracle",
    "hpe": "HPE",
    "verizon": "Verizon",
    "dell": "Dell",
    "cisco": "Cisco",
    "tesla": "Tesla",
    "walmart": "Walmart",
    "unitedhealth": "UnitedHealth Group",
    "wellsfargo": "Wells Fargo",
    "spectrum": "Spectrum",
}


def normalize_scan_source(source: str | None) -> str:
    key = (source or "indeed").strip().lower()
    return key if key in SCAN_SOURCES else "indeed"


def scan_label(keyword: str, source: str | None) -> str:
    """How a saved scan is named: the keyword, then the board it was run on."""
    name = SCAN_SOURCES.get(normalize_scan_source(source), "Indeed")
    return f"{keyword} · {name}"


def encode_scan(source: str | None, keyword: str) -> str:
    return f"{normalize_scan_source(source)}|{keyword}"


def decode_scan(raw: str | None) -> tuple[str, str]:
    """Split a scan picker value into (source, keyword). A bare keyword has no source."""
    text = (raw or "").strip()
    if "|" in text:
        source, keyword = text.split("|", 1)
        source = source.strip().lower()
        keyword = keyword.strip()
        if source in SCAN_SOURCES and keyword:
            return source, keyword
    return "", text


def _backfill_scan_sources(conn: sqlite3.Connection) -> None:
    """Tag company-board jobs that were saved before scans remembered their source."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(job_keywords)")}
    if "source" not in cols:
        return
    conn.execute(
        """
        UPDATE job_keywords
        SET source = (
            SELECT lower(j.site) FROM jobs j WHERE j.id = job_keywords.job_id
        )
        WHERE EXISTS (
            SELECT 1 FROM jobs j
            WHERE j.id = job_keywords.job_id
              AND lower(j.site) IN (
                    'spacex', 'amazon', 'ibm', 'oracle', 'hpe',
                    'verizon', 'dell', 'cisco', 'tesla',
                    'walmart', 'unitedhealth', 'wellsfargo', 'spectrum'
              )
        )
          AND (source IS NULL OR source = '' OR source = 'indeed')
        """
    )


def keyword_stance(title: Any, description: Any, keyword: str) -> str:
    return mention_stance(f"{title or ''}\n{description or ''}", keyword, pat=keyword_pattern(keyword))


def stance_for_new_posting(
    title: Any,
    description: Any,
    keyword: str,
    *,
    allow_model: bool,
    db_path: Path | None = None,
) -> str:
    """Regex stance. On a new posting, the model may only fill a stance the regex left as mentioned."""
    stance = keyword_stance(title, description, keyword)
    if not allow_model or stance != "mentioned":
        return stance
    try:
        from ai_client import requirement_stance

        filled = requirement_stance(title, description, keyword, db_path=db_path)
    except Exception:
        return stance
    if filled in ("required", "preferred", "mentioned"):
        return filled
    return stance


def _backfill_enrichment(conn: sqlite3.Connection) -> dict[str, int]:
    """Parse years and certs for every stored job, clean leftover markup, tag keyword stance."""
    jobs = conn.execute(
        "SELECT id, title, description, location, is_remote FROM jobs"
    ).fetchall()
    cleaned = 0
    for row in jobs:
        data = enrich_text(row["title"], row["description"])
        if data["description"] != row["description"]:
            cleaned += 1
        data["is_remote"] = (
            1
            if infer_remote(row["is_remote"], row["location"], row["title"], data["description"])
            else 0
        )
        conn.execute(
            """
            UPDATE jobs SET description = :description, years_min = :years_min,
                years_max = :years_max, years_pref = :years_pref, cert_stances = :cert_stances,
                clearance = :clearance, is_remote = :is_remote
            WHERE id = :id
            """,
            {**data, "id": row["id"]},
        )
        for tag in conn.execute(
            "SELECT keyword FROM job_keywords WHERE job_id = ?", (row["id"],)
        ).fetchall():
            conn.execute(
                "UPDATE job_keywords SET stance = ? WHERE job_id = ? AND keyword = ?",
                (
                    keyword_stance(row["title"], data["description"], tag["keyword"]),
                    row["id"],
                    tag["keyword"],
                ),
            )
    return {"jobs": len(jobs), "descriptions_cleaned": cleaned}


def _repair_stored_jobs(conn: sqlite3.Connection) -> None:
    jobs = conn.execute(
        """
        SELECT id, title, description, location, job_type, is_remote,
               salary_min, salary_max, salary_interval
        FROM jobs
        """
    ).fetchall()
    by_id = {row["id"]: row for row in jobs}
    for row in jobs:
        remote = 1 if infer_remote(row["is_remote"], row["location"], row["title"]) else 0
        annual_min = annualize(
            row["salary_min"], row["salary_interval"],
            job_type=row["job_type"], title=row["title"],
        )
        annual_max = annualize(
            row["salary_max"], row["salary_interval"],
            job_type=row["job_type"], title=row["title"],
        )
        conn.execute(
            """
            UPDATE jobs SET is_remote = ?, salary_annual_min = ?, salary_annual_max = ?
            WHERE id = ?
            """,
            (remote, annual_min, annual_max, row["id"]),
        )
    tags = conn.execute("SELECT job_id, keyword FROM job_keywords").fetchall()
    for tag in tags:
        job = by_id.get(tag["job_id"])
        if job is None:
            conn.execute(
                "DELETE FROM job_keywords WHERE job_id = ? AND keyword = ?",
                (tag["job_id"], tag["keyword"]),
            )
            continue
        hay = f"{job['title'] or ''}\n{job['description'] or ''}"
        if not keyword_pattern(tag["keyword"]).search(hay):
            conn.execute(
                "DELETE FROM job_keywords WHERE job_id = ? AND keyword = ?",
                (tag["job_id"], tag["keyword"]),
            )
    conn.execute("DELETE FROM jobs WHERE id NOT IN (SELECT job_id FROM job_keywords)")


def _migrate_locations_to_states(conn: sqlite3.Connection) -> None:
    """Replace legacy default city rows with US states organized by region.

    Preserves 'United States', 'Remote', any custom locations the user added, and
    which states were enabled based on their previously enabled legacy cities.
    """
    users = [row[0] for row in conn.execute("SELECT id FROM users").fetchall()]
    for uid in users:
        rows = conn.execute(
            "SELECT id, name, enabled FROM locations WHERE user_id = ?",
            (uid,),
        ).fetchall()
        if not rows:
            continue
        by_lower = {r["name"].strip().lower(): dict(r) for r in rows}
        legacy_rows = [r for key, r in by_lower.items() if key in LEGACY_DEFAULT_CITIES]
        if not legacy_rows:
            # Ensure any missing default states are present without touching existing choices.
            for idx, name in enumerate(DEFAULT_LOCATIONS):
                if name.lower() not in by_lower:
                    conn.execute(
                        "INSERT OR IGNORE INTO locations (user_id, name, enabled, sort_order) VALUES (?, ?, 1, ?)",
                        (uid, name, idx),
                    )
            continue

        enabled_abbrs: set[str] = set()
        for r in legacy_rows:
            if r["enabled"]:
                parts = r["name"].rsplit(",", 1)
                if len(parts) == 2:
                    enabled_abbrs.add(parts[1].strip().upper())
        all_legacy_on = all(bool(r["enabled"]) for r in legacy_rows)

        legacy_ids = [r["id"] for r in legacy_rows]
        placeholders = ",".join("?" * len(legacy_ids))
        conn.execute(f"DELETE FROM locations WHERE id IN ({placeholders})", legacy_ids)

        for idx, name in enumerate(DEFAULT_LOCATIONS):
            key = name.lower()
            existing = by_lower.get(key)
            if existing is not None:
                conn.execute(
                    "UPDATE locations SET sort_order = ? WHERE id = ?",
                    (idx, existing["id"]),
                )
                continue
            if name in NATIONWIDE_LOCATIONS:
                is_on = 1
            else:
                abbr = STATE_NAME_TO_ABBR.get(key, "")
                is_on = 1 if (all_legacy_on or abbr in enabled_abbrs) else 0
            conn.execute(
                "INSERT OR IGNORE INTO locations (user_id, name, enabled, sort_order) VALUES (?, ?, ?, ?)",
                (uid, name, is_on, idx),
            )

        # Keep custom user-added locations after default states
        default_keys = {n.lower() for n in DEFAULT_LOCATIONS}
        custom_order = len(DEFAULT_LOCATIONS)
        for key, r in by_lower.items():
            if key not in LEGACY_DEFAULT_CITIES and key not in default_keys:
                conn.execute(
                    "UPDATE locations SET sort_order = ? WHERE id = ?",
                    (custom_order, r["id"]),
                )
                custom_order += 1


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


# --- locations (per user) ---


def get_enabled_locations(user_id: int, db_path: Path | None = None) -> list[str]:
    init_db(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT name FROM locations
            WHERE user_id = ? AND enabled = 1
            ORDER BY sort_order ASC, name ASC
            """,
            (user_id,),
        ).fetchall()
    return [row["name"] for row in rows]


def get_all_locations(user_id: int, db_path: Path | None = None) -> list[dict[str, Any]]:
    init_db(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, name, enabled, sort_order
            FROM locations
            WHERE user_id = ?
            ORDER BY sort_order ASC, name ASC
            """,
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_grouped_locations(user_id: int, db_path: Path | None = None) -> dict[str, Any]:
    """Locations grouped into nationwide, the 5 US regions (states), and custom."""
    locs = get_all_locations(user_id, db_path)
    by_lower = {loc["name"].strip().lower(): loc for loc in locs}
    seen_ids: set[int] = set()

    nationwide: list[dict[str, Any]] = []
    for name in NATIONWIDE_LOCATIONS:
        row = by_lower.get(name.lower())
        if row:
            nationwide.append(row)
            seen_ids.add(row["id"])

    regions: list[dict[str, Any]] = []
    for key in REGION_ORDER:
        items: list[dict[str, Any]] = []
        for state_name, abbr in REGION_STATES[key]:
            row = by_lower.get(state_name.lower())
            if row:
                items.append({**row, "abbr": abbr})
                seen_ids.add(row["id"])
        en = sum(1 for item in items if item["enabled"])
        regions.append(
            {
                "key": key,
                "label": REGION_LABELS[key],
                "locations": items,
                "enabled_count": en,
                "total_count": len(items),
                "all_enabled": bool(items) and en == len(items),
            }
        )

    custom = [loc for loc in locs if loc["id"] not in seen_ids]
    enabled_total = sum(1 for loc in locs if loc["enabled"])
    return {
        "nationwide": nationwide,
        "regions": regions,
        "custom": custom,
        "enabled_count": enabled_total,
        "total_count": len(locs),
    }


def set_region_locations_enabled(
    user_id: int,
    region_key: str,
    enabled: bool,
    db_path: Path | None = None,
) -> None:
    states = REGION_STATES.get((region_key or "").strip().lower())
    if not states:
        raise ValueError(f"Unknown region: {region_key}")
    init_db(db_path)
    names = [name for name, _abbr in states]
    with connect(db_path) as conn:
        conn.executemany(
            "UPDATE locations SET enabled = ? WHERE user_id = ? AND name = ? COLLATE NOCASE",
            [(1 if enabled else 0, user_id, name) for name in names],
        )
        conn.commit()


def delete_location(user_id: int, loc_id: int, db_path: Path | None = None) -> None:
    init_db(db_path)
    with connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM locations WHERE id = ? AND user_id = ?",
            (loc_id, user_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"Location id not found: {loc_id}")


def add_location(user_id: int, name: str, db_path: Path | None = None) -> None:
    name = name.strip()
    if not name:
        raise ValueError("Location name cannot be empty")
    init_db(db_path)
    with connect(db_path) as conn:
        max_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) FROM locations WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO locations (user_id, name, enabled, sort_order)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(user_id, name) DO UPDATE SET enabled = 1
            """,
            (user_id, name, max_order + 1),
        )
        conn.commit()


def set_location_enabled(user_id: int, name: str, enabled: bool, db_path: Path | None = None) -> None:
    init_db(db_path)
    with connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE locations SET enabled = ? WHERE user_id = ? AND name = ? COLLATE NOCASE",
            (1 if enabled else 0, user_id, name.strip()),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"Location not found: {name}")


def set_location_enabled_by_id(user_id: int, loc_id: int, enabled: bool, db_path: Path | None = None) -> None:
    init_db(db_path)
    with connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE locations SET enabled = ? WHERE id = ? AND user_id = ?",
            (1 if enabled else 0, loc_id, user_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"Location id not found: {loc_id}")


def set_all_locations_enabled(
    user_id: int,
    enabled: bool,
    except_names: list[str] | None = None,
    db_path: Path | None = None,
) -> None:
    init_db(db_path)
    except_names = except_names or []
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE locations SET enabled = ? WHERE user_id = ?",
            (1 if enabled else 0, user_id),
        )
        if except_names and not enabled:
            conn.executemany(
                "UPDATE locations SET enabled = 1 WHERE user_id = ? AND name = ? COLLATE NOCASE",
                [(user_id, n) for n in except_names],
            )
        conn.commit()


def _clean(value: Any) -> Any:
    if value is None:
        return None
    try:
        import math

        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lower() in ("", "nan", "none", "null"):
            return None
        return stripped
    if hasattr(value, "isoformat") and not isinstance(value, datetime):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    if isinstance(value, (list, tuple, set)):
        parts = [str(x) for x in value if x is not None and str(x).lower() != "nan"]
        return ", ".join(parts) if parts else None
    return value


def is_part_time(job_type: Any = None, title: Any = None) -> bool:
    return bool(_PART_TIME_RE.search(f"{job_type or ''} {title or ''}"))


def infer_remote(
    is_remote: Any = None,
    location: Any = None,
    title: Any = None,
    description: Any = None,
) -> bool:
    """Indeed's remote flag, or the posting says the role itself is remote.

    A bare "remote" in the title or location counts ("Network Engineer - Remote").
    In the description, only an arrangement phrase counts, so "remote support
    tools" stays on-site. "Not a remote position" does not count.
    """
    if is_remote in (True, 1, "1", "true", "True", "yes", "Yes"):
        return True
    title_loc = f"{location or ''} {title or ''}"
    if _REMOTE_WORD.search(title_loc) and not _REMOTE_NEGATED.search(title_loc):
        return True
    text = description if isinstance(description, str) else ""
    for match in _REMOTE_ROLE.finditer(text):
        window = text[max(0, match.start() - 48) : match.end() + 48]
        if not _REMOTE_NEGATED.search(window):
            return True
    return False


def annualize(
    amount: Any,
    interval: Any,
    *,
    job_type: Any = None,
    title: Any = None,
) -> float | None:
    """Comparable full-time annual pay, or None when the listing should not be mixed in."""
    amount = _clean(amount)
    if amount is None:
        return None
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None
    label = str(_clean(interval) or "").lower()
    part = is_part_time(job_type, title)

    if label in {"hourly", "hour"}:
        if value < 7.25 or part:
            return None
        return round(value * 2080, 2)
    if label in {"daily", "day"}:
        if part or value < 400:
            return None
        return round(value * 260, 2)
    if label in {"monthly", "month"}:
        if part or value < 2000:
            return None
        return round(value * 12, 2)
    if label in {"weekly", "week"}:
        if part:
            return None
        return round(value * 52, 2)
    if label in {"yearly", "year", "annual", "annually"}:
        return round(value, 2) if value >= 10000 else None
    if not label:
        return round(value, 2) if value >= 15000 else None
    return None


def format_pay(job: dict[str, Any]) -> str:
    interval = str(job.get("salary_interval") or "").lower()
    lo = job.get("salary_min")
    hi = job.get("salary_max")
    if lo is None:
        lo = job.get("salary_annual_min")
        hi = job.get("salary_annual_max")
        if lo is None:
            return "—"
        interval = interval or "yearly"
    try:
        lo_f = float(lo)
    except (TypeError, ValueError):
        return "—"
    hi_f: float | None
    try:
        hi_f = float(hi) if hi is not None else None
    except (TypeError, ValueError):
        hi_f = None
    hourlyish = interval in {"hourly", "hour", "daily", "day"} or lo_f < 1000
    suffix = {
        "hourly": "/hr",
        "hour": "/hr",
        "daily": "/day",
        "day": "/day",
        "monthly": "/mo",
        "month": "/mo",
        "weekly": "/wk",
        "week": "/wk",
    }.get(interval, "")

    def _fmt(value: float) -> str:
        if hourlyish:
            text = f"${value:,.2f}".rstrip("0").rstrip(".")
            return text
        return f"${value:,.0f}"

    text = _fmt(lo_f)
    if hi_f is not None and round(hi_f, 2) != round(lo_f, 2):
        text += f"-{_fmt(hi_f)}"
    return text + suffix


def pay_hint(job: dict[str, Any]) -> str:
    """Hover text for the pay label: what the number is, never a replacement for it.

    Hourly pay stays "$40/hr" on the row; the annual-equivalent used for filters,
    sorting, and the median only appears here.
    """
    bits: list[str] = []
    interval = str(job.get("salary_interval") or "").lower()
    annual = job.get("salary_annual_min")
    if interval in {"hourly", "hour", "daily", "day", "weekly", "week", "monthly", "month"}:
        if annual:
            bits.append(f"≈ ${float(annual):,.0f}/yr full-time equivalent, used for filters and medians")
        else:
            bits.append("Part-time or too low to annualize; not used in pay filters or medians")
    source = job.get("salary_source")
    if source == "estimate":
        bits.append("Indeed's estimate, not a range the employer posted")
    elif source == "employer":
        bits.append("Range posted by the employer")
    elif source == "direct_data":
        bits.append("Saved before estimates were tagged; may be an employer range or an Indeed estimate")
    return ". ".join(bits)


def _json_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def job_record_from_scrape(rec: dict[str, Any], keyword: str) -> dict[str, Any]:
    interval = _clean(rec.get("interval"))
    salary_min = _clean(rec.get("min_amount"))
    salary_max = _clean(rec.get("max_amount"))
    title = _clean(rec.get("title"))
    location = _clean(rec.get("location"))
    job_type = _clean(rec.get("job_type"))
    is_remote = 1 if infer_remote(
        rec.get("is_remote"), location, title, rec.get("description")
    ) else 0
    date_posted = _clean(rec.get("date_posted"))
    return {
        "dedupe_key": rec.get("_dedupe_key") or "",
        "job_url": _clean(rec.get("job_url")),
        "job_url_direct": _clean(rec.get("job_url_direct")),
        "site": _clean(rec.get("site")),
        "site_job_id": _clean(rec.get("id")),
        "title": title,
        "company": _clean(rec.get("company")),
        "location": location,
        "date_posted": str(date_posted) if date_posted is not None else None,
        "job_type": job_type,
        "is_remote": is_remote,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_interval": interval,
        "salary_currency": _clean(rec.get("currency")),
        "salary_annual_min": annualize(salary_min, interval, job_type=job_type, title=title),
        "salary_annual_max": annualize(salary_max, interval, job_type=job_type, title=title),
        "salary_source": _clean(rec.get("salary_source")),
        "emails": _clean(rec.get("emails")),
        "description": clean_description(_clean(rec.get("description"))),
        "company_industry": _clean(rec.get("company_industry")),
        "company_url": _clean(rec.get("company_url") or rec.get("company_url_direct")),
        "company_num_employees": _clean(rec.get("company_num_employees")),
        "company_revenue": _clean(rec.get("company_revenue")),
        "company_rating": _clean(rec.get("company_rating")),
        "company_reviews_count": _clean(rec.get("company_reviews_count")),
        "skills": _clean(rec.get("skills")),
        "experience_range": _clean(rec.get("experience_range")),
        "search_location": _clean(rec.get("_search_location")),
        "clearance": classify_clearance(
            f"{_clean(rec.get('title')) or ''}\n{_clean(rec.get('description')) or ''}"
        ),
        "keyword": keyword,
    }


_UPSERT_BATCH = 200


def upsert_jobs(
    user_id: int,
    records: list[dict[str, Any]],
    keyword: str,
    db_path: Path | None = None,
    source: str | None = None,
) -> dict[str, int]:
    """Store postings once (shared), then link them to this user's scan.

    `inserted` counts postings new to this user; `updated` counts ones they already had.
    """
    init_db(db_path)
    now = utc_now()
    inserted = 0
    updated = 0
    scan_source = normalize_scan_source(source)

    for start in range(0, len(records), _UPSERT_BATCH):
        chunk = records[start : start + _UPSERT_BATCH]
        parsed: list[dict[str, Any]] = []
        for rec in chunk:
            row = job_record_from_scrape(rec, keyword)
            row.pop("keyword", None)
            if row["dedupe_key"]:
                parsed.append(row)
        if not parsed:
            continue

        keys = list({row["dedupe_key"] for row in parsed})
        existing_map: dict[str, dict[str, Any]] = {}
        with connect(db_path) as conn:
            placeholders = ",".join("?" * len(keys))
            for existing_row in conn.execute(
                f"SELECT id, dedupe_key, title, description, is_remote "
                f"FROM jobs WHERE dedupe_key IN ({placeholders})",
                keys,
            ).fetchall():
                existing_map[existing_row["dedupe_key"]] = dict(existing_row)

        # Run text enrichment outside any SQLite transaction so CPU work never holds a write lock.
        prepared: list[tuple[bool, dict[str, Any], str]] = []
        for row in parsed:
            key = row["dedupe_key"]
            existing = existing_map.get(key)
            if existing is None:
                enriched = enrich_text(row["title"], row["description"])
                payload = {**row, **enriched, "now": now}
                desc = payload["description"]
                stance = stance_for_new_posting(
                    row["title"], desc, keyword, allow_model=True, db_path=db_path
                )
                prepared.append((True, payload, stance))
                existing_map[key] = {
                    "id": None,
                    "title": row["title"],
                    "description": desc,
                    "is_remote": row["is_remote"],
                }
            else:
                new_desc = row["description"] or ""
                old_desc = existing["description"] or ""
                description = new_desc if len(new_desc) >= len(old_desc) else old_desc
                is_remote = 1 if (existing["is_remote"] or row["is_remote"]) else 0
                title_for_enrich = row.get("title") or existing["title"]
                enriched = enrich_text(title_for_enrich, description)
                payload = {
                    **row,
                    **enriched,
                    "is_remote": is_remote,
                    "now": now,
                    "id": existing["id"],
                }
                desc = payload["description"]
                stance = stance_for_new_posting(
                    row.get("title"), desc, keyword, allow_model=False, db_path=db_path
                )
                prepared.append((False, payload, stance))
                existing["title"] = title_for_enrich
                existing["description"] = desc
                existing["is_remote"] = is_remote

        with connect(db_path) as conn:
            inserted_ids: dict[str, int] = {}
            for is_new, payload, stance in prepared:
                key = payload["dedupe_key"]
                if is_new:
                    cur = conn.execute(
                        """
                        INSERT INTO jobs (
                            dedupe_key, job_url, job_url_direct, site, site_job_id,
                            title, company, location, date_posted, job_type, is_remote,
                            salary_min, salary_max, salary_interval, salary_currency,
                            salary_annual_min, salary_annual_max, salary_source, emails,
                            description, company_industry, company_url,
                            company_num_employees, company_revenue, company_rating,
                            company_reviews_count, skills, experience_range,
                            search_location, clearance, years_min, years_max, years_pref,
                            cert_stances, first_seen_at, last_seen_at
                        ) VALUES (
                            :dedupe_key, :job_url, :job_url_direct, :site, :site_job_id,
                            :title, :company, :location, :date_posted, :job_type, :is_remote,
                            :salary_min, :salary_max, :salary_interval, :salary_currency,
                            :salary_annual_min, :salary_annual_max, :salary_source, :emails,
                            :description, :company_industry, :company_url,
                            :company_num_employees, :company_revenue, :company_rating,
                            :company_reviews_count, :skills, :experience_range,
                            :search_location, :clearance, :years_min, :years_max, :years_pref,
                            :cert_stances, :now, :now
                        )
                        """,
                        payload,
                    )
                    job_id = int(cur.lastrowid)
                    inserted_ids[key] = job_id
                else:
                    job_id = payload["id"]
                    if job_id is None:
                        job_id = inserted_ids[key]
                        payload = {**payload, "id": job_id}
                    conn.execute(
                        """
                        UPDATE jobs SET
                            job_url = COALESCE(:job_url, job_url),
                            job_url_direct = COALESCE(:job_url_direct, job_url_direct),
                            site = COALESCE(:site, site),
                            site_job_id = COALESCE(:site_job_id, site_job_id),
                            title = COALESCE(:title, title),
                            company = COALESCE(:company, company),
                            location = COALESCE(:location, location),
                            date_posted = COALESCE(:date_posted, date_posted),
                            job_type = COALESCE(:job_type, job_type),
                            is_remote = :is_remote,
                            salary_min = COALESCE(:salary_min, salary_min),
                            salary_max = COALESCE(:salary_max, salary_max),
                            salary_interval = COALESCE(:salary_interval, salary_interval),
                            salary_currency = COALESCE(:salary_currency, salary_currency),
                            salary_annual_min = COALESCE(:salary_annual_min, salary_annual_min),
                            salary_annual_max = COALESCE(:salary_annual_max, salary_annual_max),
                            salary_source = COALESCE(:salary_source, salary_source),
                            emails = COALESCE(:emails, emails),
                            description = :description,
                            company_industry = COALESCE(:company_industry, company_industry),
                            company_url = COALESCE(:company_url, company_url),
                            company_num_employees = COALESCE(:company_num_employees, company_num_employees),
                            company_revenue = COALESCE(:company_revenue, company_revenue),
                            company_rating = COALESCE(:company_rating, company_rating),
                            company_reviews_count = COALESCE(:company_reviews_count, company_reviews_count),
                            skills = COALESCE(:skills, skills),
                            experience_range = COALESCE(:experience_range, experience_range),
                            search_location = COALESCE(:search_location, search_location),
                            clearance = :clearance,
                            years_min = :years_min,
                            years_max = :years_max,
                            years_pref = :years_pref,
                            cert_stances = :cert_stances,
                            last_seen_at = :now
                        WHERE id = :id
                        """,
                        payload,
                    )

                linked = conn.execute(
                    "SELECT 1 FROM user_jobs WHERE user_id = ? AND job_id = ?",
                    (user_id, job_id),
                ).fetchone()
                if linked is None:
                    conn.execute(
                        "INSERT INTO user_jobs (user_id, job_id, added_at, status) VALUES (?, ?, ?, 'new')",
                        (user_id, job_id, now),
                    )
                    inserted += 1
                else:
                    updated += 1
                conn.execute(
                    """
                    INSERT INTO job_keywords (user_id, job_id, keyword, stance, source)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, job_id, keyword) DO UPDATE SET
                        stance = excluded.stance,
                        source = excluded.source
                    """,
                    (user_id, job_id, keyword, stance, scan_source),
                )
            conn.commit()
    _invalidate_caches()
    return {"inserted": inserted, "updated": updated}


def start_run(
    user_id: int,
    keyword: str,
    locations_count: int,
    filters: dict[str, Any],
    db_path: Path | None = None,
) -> int:
    init_db(db_path)
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO runs (user_id, keyword, started_at, locations_count, filters_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, keyword, utc_now(), locations_count, json.dumps(filters, default=str)),
        )
        conn.commit()
        return int(cur.lastrowid)


def finish_run(
    run_id: int,
    *,
    raw_fetched: int = 0,
    matched: int = 0,
    inserted: int = 0,
    updated: int = 0,
    filtered: int = 0,
    errors: str | None = None,
    db_path: Path | None = None,
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE runs SET
                ended_at = ?,
                raw_fetched = ?,
                matched = ?,
                inserted = ?,
                updated = ?,
                filtered = ?,
                errors = ?
            WHERE id = ?
            """,
            (
                utc_now(),
                raw_fetched,
                matched,
                inserted,
                updated,
                filtered,
                errors,
                run_id,
            ),
        )
        conn.commit()
    _invalidate_caches()


_TRACKER_COLS = (
    "uj.status, uj.applied_on, uj.follow_up_on, uj.heard_back, "
    "uj.notes, uj.resume_id, uj.cover_id, uj.added_at"
)


def get_job(user_id: int, job_id: int, db_path: Path | None = None) -> dict[str, Any] | None:
    """The job as this user sees it, or None if they have not scanned it."""
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            f"""
            SELECT j.*, {_TRACKER_COLS}
            FROM jobs j
            JOIN user_jobs uj ON uj.job_id = j.id AND uj.user_id = ?
            WHERE j.id = ?
            """,
            (user_id, job_id),
        ).fetchone()
        if row is None:
            return None
        job = dict(row)
        kws = conn.execute(
            """
            SELECT keyword, COALESCE(NULLIF(source, ''), 'indeed') AS source
            FROM job_keywords WHERE user_id = ? AND job_id = ? ORDER BY keyword
            """,
            (user_id, job_id),
        ).fetchall()
        job["keywords"] = [k["keyword"] for k in kws]
        job["scan_labels"] = [scan_label(k["keyword"], k["source"]) for k in kws]
        job["pay_label"] = format_pay(job)
        job["pay_estimated"] = job.get("salary_source") == "estimate"
        job["pay_hint"] = pay_hint(job)
        job["years_chips"] = years_chips(job.get("years_min"), job.get("years_max"), job.get("years_pref"))
        job["status_label"] = STATUS_LABELS.get(job.get("status") or "", job.get("status") or "")
        job["heard_back_label"] = HEARD_BACK_LABELS.get(
            job.get("heard_back") or "none", job.get("heard_back") or "No reply"
        )
        job["resume"] = _material_row(conn, user_id, job.get("resume_id"))
        job["cover"] = _material_row(conn, user_id, job.get("cover_id"))
        return job


def set_job_status(user_id: int, job_id: int, status: str, db_path: Path | None = None) -> None:
    update_job_tracker(user_id, job_id, {"status": status}, db_path=db_path)


def _blank(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    raw = _blank(value)
    if raw is None:
        return None
    return int(raw)


def _owned_material(conn: sqlite3.Connection, user_id: int, material_id: int | None) -> int | None:
    if material_id is None:
        return None
    row = conn.execute(
        "SELECT 1 FROM materials WHERE id = ? AND user_id = ?",
        (material_id, user_id),
    ).fetchone()
    if row is None:
        raise ValueError("Material not found")
    return material_id


def update_job_tracker(
    user_id: int,
    job_id: int,
    fields: dict[str, Any],
    db_path: Path | None = None,
) -> None:
    allowed = {"status", *JOB_TRACKER_COLS}
    payload = {key: fields[key] for key in allowed if key in fields}
    if not payload:
        raise ValueError("No tracker fields to update")
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM user_jobs WHERE user_id = ? AND job_id = ?",
            (user_id, job_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"Job not found: {job_id}")
        current = dict(row)
        if "status" in payload:
            status = (payload["status"] or "").strip()
            if status not in STATUSES:
                raise ValueError(f"Invalid status: {status}")
            payload["status"] = status
        else:
            status = current.get("status") or "new"
        if "heard_back" in payload:
            heard = (payload["heard_back"] or "none").strip() or "none"
            if heard not in HEARD_BACK_LABELS:
                raise ValueError(f"Invalid heard-back value: {heard}")
            payload["heard_back"] = heard
        if "applied_on" in payload:
            payload["applied_on"] = _blank(payload["applied_on"])
        if "follow_up_on" in payload:
            payload["follow_up_on"] = _blank(payload["follow_up_on"])
        if "notes" in payload:
            payload["notes"] = _blank(payload["notes"])
        if "resume_id" in payload:
            payload["resume_id"] = _owned_material(conn, user_id, _int_or_none(payload["resume_id"]))
        if "cover_id" in payload:
            payload["cover_id"] = _owned_material(conn, user_id, _int_or_none(payload["cover_id"]))
        if status == "applied":
            if not payload.get("applied_on", current.get("applied_on")):
                payload["applied_on"] = date.today().isoformat()
            if not payload.get("follow_up_on", current.get("follow_up_on")):
                payload["follow_up_on"] = (date.today() + timedelta(days=7)).isoformat()
        assignments = ", ".join(f"{key} = ?" for key in payload)
        values = list(payload.values())
        values.extend([user_id, job_id])
        conn.execute(
            f"UPDATE user_jobs SET {assignments} WHERE user_id = ? AND job_id = ?",
            values,
        )
        conn.commit()
    _invalidate_caches()


def _material_row(conn: sqlite3.Connection, user_id: int, material_id: Any) -> dict[str, Any] | None:
    if not material_id:
        return None
    row = conn.execute(
        "SELECT * FROM materials WHERE id = ? AND user_id = ?",
        (material_id, user_id),
    ).fetchone()
    return dict(row) if row else None


def _resolve_material_path(stored: str | None) -> Path | None:
    if not stored:
        return None
    path = Path(stored)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return path if path.is_file() else None


def _material_text(mat: dict[str, Any], *, persist: bool = False, db_path: Path | None = None) -> str:
    """Best available plain text for a materials row (paste, cached extract, or file)."""
    cached = (mat.get("extracted_text") or "").strip()
    if cached:
        return cached
    body = (mat.get("body") or "").strip()
    if body:
        return body
    path = _resolve_material_path(mat.get("path"))
    if path is None:
        mat["extract_note"] = "The uploaded file is missing on disk."
        return ""
    text, note = extract_resume_detail(path)
    text = text.strip()
    mat["extract_note"] = None if text else note
    if text and persist and mat.get("id") is not None:
        with connect(db_path) as conn:
            conn.execute(
                "UPDATE materials SET extracted_text = ? WHERE id = ?",
                (text, mat["id"]),
            )
            conn.commit()
        mat["extracted_text"] = text
    return text


def get_user_resume_profile(
    user_id: int,
    profile: dict[str, Any] | None = None,
    db_path: Path | None = None,
) -> ResumeProfile | None:
    """Parse the user's default (or newest) resume into a ResumeProfile for Heat Score."""
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM materials
            WHERE user_id = ? AND kind = 'resume'
            ORDER BY is_default DESC, created_at DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    mat = dict(row)
    text = _material_text(mat, persist=True, db_path=db_path)
    if not text:
        return None
    if profile is None:
        profile = get_profile(user_id, db_path)
    # parse_resume builds a content signature from text, profile supplements, and readout edits.
    probe = parse_resume(text, profile, load_overrides(mat.get("overrides")))
    cached = _RESUME_PROFILE_CACHE.get(probe.signature)
    if cached is not None:
        return cached
    _RESUME_PROFILE_CACHE[probe.signature] = probe
    return probe


def _ensure_job_descriptions(
    rows: list[dict[str, Any]],
    db_path: Path | None = None,
) -> None:
    missing = [row["id"] for row in rows if "description" not in row]
    if not missing:
        return
    placeholders = ",".join("?" * len(missing))
    with connect(db_path) as conn:
        desc_rows = conn.execute(
            f"SELECT id, description FROM jobs WHERE id IN ({placeholders})",
            missing,
        ).fetchall()
    by_id = {r["id"]: (r["description"] or "") for r in desc_rows}
    for row in rows:
        if "description" not in row:
            row["description"] = by_id.get(row["id"], "")


# A fresh resume invalidates every cached Heat Score. Scoring that set inside the
# page request is what freezes the Jobs tab, so anything past this stays on a
# background thread and the list renders from the cache it already has.
HEAT_SCORE_INLINE_LIMIT = 80
_heat_guard = threading.Lock()
_heat_active: set[tuple[int, str, str]] = set()
_heat_threads: dict[tuple[int, str, str], threading.Thread] = {}


def _heat_key(user_id: int, sig: str, db_path: Path | None) -> tuple[int, str, str]:
    return (user_id, sig, str(db_path) if db_path else "")


def heat_scores_pending(user_id: int | None = None) -> bool:
    """True while a background rescore is still writing this user's Heat Scores."""
    with _heat_guard:
        if user_id is None:
            return bool(_heat_active)
        return any(item[0] == user_id for item in _heat_active)


def wait_for_heat_scores(user_id: int, timeout: float = 30) -> None:
    with _heat_guard:
        threads = [thread for key, thread in _heat_threads.items() if key[0] == user_id]
    for thread in threads:
        thread.join(timeout)


def _mark_score_pending(row: dict[str, Any]) -> None:
    row["score"] = None
    row["score_pending"] = True
    row["score_tip"] = "Updating heat score"
    row["heat_tier"] = ""
    row["score_details"] = {}


def _jobs_for_heat(job_ids: list[int], db_path: Path | None) -> list[dict[str, Any]]:
    if not job_ids:
        return []
    placeholders = ",".join("?" * len(job_ids))
    with connect(db_path) as conn:
        fetched = conn.execute(
            f"""
            SELECT id, title, description, years_min, years_max, years_pref,
                   is_remote, clearance
            FROM jobs WHERE id IN ({placeholders})
            """,
            job_ids,
        ).fetchall()
    return [dict(row) for row in fetched]


def _score_heat_rows(
    user_id: int,
    rows: list[dict[str, Any]],
    resume: ResumeProfile,
    keyword: str,
    db_path: Path | None,
    *,
    ignore_errors: bool = False,
) -> None:
    if not rows:
        return
    sig = heat_cache_sig(resume)
    _ensure_job_descriptions(rows, db_path)
    upserts: list[tuple[Any, ...]] = []
    for row in rows:
        try:
            result = score_job_resume(
                row,
                resume,
                keyword,
                similar_lookup=_similar_lookup(row, resume, db_path),
            )
        except Exception:
            if not ignore_errors:
                raise
            continue
        row["score"] = result["score"]
        row["score_tip"] = result["tooltip"]
        row["heat_tier"] = result["tier"]
        row["score_details"] = result.get("details") or {}
        row["score_pending"] = False
        upserts.append(
            (
                user_id,
                row["id"],
                sig,
                result["score"],
                result["tier"],
                result["tooltip"],
                json.dumps(row["score_details"], ensure_ascii=False),
            )
        )
    if not upserts:
        return
    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO job_heat_scores
                (user_id, job_id, resume_sig, score, tier, tooltip, details_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, job_id) DO UPDATE SET
                resume_sig = excluded.resume_sig,
                score = excluded.score,
                tier = excluded.tier,
                tooltip = excluded.tooltip,
                details_json = excluded.details_json
            """,
            upserts,
        )
        conn.commit()


def _heat_refresh_worker(
    user_id: int,
    resume: ResumeProfile,
    keyword: str,
    job_ids: list[int],
    db_path: Path | None,
    key: tuple[int, str, str],
) -> None:
    sig = key[1]
    try:
        step = 40
        for start in range(0, len(job_ids), step):
            fresh = get_user_resume_profile(user_id, None, db_path)
            if fresh is None or heat_cache_sig(fresh) != sig:
                return
            rows = _jobs_for_heat(job_ids[start : start + step], db_path)
            _score_heat_rows(user_id, rows, fresh, keyword, db_path, ignore_errors=True)
    finally:
        with _heat_guard:
            _heat_active.discard(key)
            _heat_threads.pop(key, None)


def _schedule_heat_refresh(
    user_id: int,
    resume: ResumeProfile,
    keyword: str,
    job_ids: list[int],
    db_path: Path | None,
) -> None:
    if not job_ids:
        return
    key = _heat_key(user_id, heat_cache_sig(resume), db_path)
    with _heat_guard:
        if key in _heat_active:
            return
        thread = threading.Thread(
            target=_heat_refresh_worker,
            args=(user_id, resume, keyword, list(job_ids), db_path, key),
            name="heat-scores",
            daemon=True,
        )
        _heat_active.add(key)
        _heat_threads[key] = thread
    thread.start()


def _apply_resume_heat_scores(
    user_id: int,
    rows: list[dict[str, Any]],
    resume: ResumeProfile,
    keyword: str = "",
    db_path: Path | None = None,
    *,
    defer: bool = False,
) -> bool:
    """Attach resume Heat Score to each row, using job_heat_scores as a cache.

    Returns True when the uncached rows were left for a background rescore.
    """
    if not rows:
        return False
    ids = [row["id"] for row in rows]
    placeholders = ",".join("?" * len(ids))
    with connect(db_path) as conn:
        cached_rows = conn.execute(
            f"""
            SELECT job_id, resume_sig, score, tier, tooltip, details_json
            FROM job_heat_scores
            WHERE user_id = ? AND job_id IN ({placeholders})
            """,
            [user_id, *ids],
        ).fetchall()
    cached = {r["job_id"]: dict(r) for r in cached_rows}

    sig = heat_cache_sig(resume)
    need: list[dict[str, Any]] = []
    for row in rows:
        hit = cached.get(row["id"])
        if hit and hit.get("resume_sig") == sig:
            row["score"] = float(hit["score"])
            row["score_tip"] = hit.get("tooltip") or ""
            row["heat_tier"] = hit.get("tier") or heat_tier(float(hit["score"]))
            row["score_details"] = _json_dict(hit.get("details_json"))
            row["score_pending"] = False
        else:
            need.append(row)

    if not need:
        return False
    if defer and len(need) > HEAT_SCORE_INLINE_LIMIT:
        _schedule_heat_refresh(user_id, resume, keyword, [int(row["id"]) for row in need], db_path)
        for row in need:
            _mark_score_pending(row)
        return True

    _score_heat_rows(user_id, need, resume, keyword, db_path)
    return False


def _vectors_for(owner: str, phrases: list[str], db_path: Path | None = None) -> dict[str, list[float]]:
    """Cached embedding vectors for one owner. Missing phrases are embedded once."""
    wanted = [phrase for phrase in phrases if phrase]
    if not wanted:
        return {}
    init_db(db_path)
    placeholders = ",".join("?" * len(wanted))
    with connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT phrase, vector FROM embedding_cache WHERE owner = ? AND phrase IN ({placeholders})",
            [owner, *wanted],
        ).fetchall()
    found: dict[str, list[float]] = {}
    for row in rows:
        try:
            vec = json.loads(row["vector"])
        except (TypeError, ValueError):
            continue
        if isinstance(vec, list):
            found[row["phrase"]] = vec
    missing = [phrase for phrase in wanted if phrase not in found]
    if not missing:
        return found
    from embeddings import embed_phrases

    fresh = embed_phrases(missing)
    if not fresh:
        return found
    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO embedding_cache (owner, phrase, vector)
            VALUES (?, ?, ?)
            ON CONFLICT(owner, phrase) DO UPDATE SET vector = excluded.vector
            """,
            [(owner, phrase, json.dumps(vec)) for phrase, vec in fresh.items()],
        )
        conn.commit()
    found.update(fresh)
    return found


def cached_vectors(owner: str, phrases: list[str], db_path: Path | None = None) -> dict[str, list[float]]:
    return _vectors_for(owner, phrases, db_path)


def ai_cache_get(
    task: str,
    model: str,
    input_hash: str,
    db_path: Path | None = None,
) -> str | None:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT answer FROM ai_cache WHERE task = ? AND model = ? AND input_hash = ?",
            (task, model, input_hash),
        ).fetchone()
    if row is None:
        return None
    return str(row["answer"])


def ai_cache_put(
    task: str,
    model: str,
    input_hash: str,
    answer: str,
    db_path: Path | None = None,
) -> None:
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO ai_cache (task, model, input_hash, answer, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(task, model, input_hash) DO UPDATE SET
                answer = excluded.answer,
                created_at = excluded.created_at
            """,
            (task, model, input_hash, answer, utc_now()),
        )
        conn.commit()


def similar_skill_pairs(
    job: dict[str, Any],
    resume: ResumeProfile,
    job_phrases: list[str],
    resume_phrases: list[str],
    db_path: Path | None = None,
) -> list[tuple[str, str, float]]:
    """Cosine matches for unknown skills only. Empty when the local model is unavailable."""
    if not job_phrases or not resume_phrases:
        return []
    from embeddings import best_similar_pairs

    desc = f"{job.get('title') or ''}\n{job.get('description') or ''}"
    digest = hashlib.sha256(desc.encode("utf-8", errors="replace")).hexdigest()[:16]
    job_owner = f"job:{job.get('id') or 0}:{digest}"
    resume_owner = f"resume:{resume.signature}"
    vectors = _vectors_for(job_owner, job_phrases, db_path)
    vectors.update(_vectors_for(resume_owner, resume_phrases, db_path))
    if not vectors:
        return []
    return best_similar_pairs(job_phrases, resume_phrases, vectors)


def _similar_lookup(job: dict[str, Any], resume: ResumeProfile, db_path: Path | None = None):
    def _lookup(job_phrases: list[str], resume_phrases: list[str]):
        return similar_skill_pairs(job, resume, job_phrases, resume_phrases, db_path)

    return _lookup


def job_fit(
    user_id: int,
    job: dict[str, Any],
    keyword: str = "",
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Fit note for one job: matched, missing required, degree, and years."""
    resume = get_user_resume_profile(user_id, None, db_path)
    if resume is None:
        profile = get_profile(user_id, db_path)
        score, parts = score_job(job, keyword, profile)
        tip = score_tooltip(parts)
        note = (
            "No resume uploaded — using profile fit score.\n"
            "Upload a default resume for a Heat Score based on degree, "
            "languages, certifications, skills, and experience.\n"
            + tip
        )
        return {
            "has_resume": False,
            "score": score,
            "tier": heat_tier(float(score or 0)),
            "note": note,
            "sentence": "",
            "matched": [],
            "missing_required": [],
            "degree": "",
            "years": "",
            "similar": [],
        }
    _apply_resume_heat_scores(user_id, [job], resume, keyword, db_path)
    details = job.get("score_details") or {}
    matched: list[str] = []
    for key in ("languages", "skills", "certifications"):
        for item in details.get(key) or []:
            if item not in matched:
                matched.append(item)
    missing: list[str] = list(details.get("missing_required_skills") or [])
    for cert in details.get("missing_required_certs") or []:
        if cert not in missing:
            missing.append(cert)
    degree = details.get("degree_status") or ""
    years = details.get("experience_note") or ""
    score = job.get("score")
    sentence = ""
    try:
        from ai_client import fit_sentence

        written = fit_sentence(
            matched,
            missing,
            degree,
            years,
            job_id=job.get("id"),
            resume_sig=resume.signature,
            db_path=db_path,
            timeout=3,
        )
        sentence = written or ""
    except Exception:
        sentence = ""
    job["score"] = score
    return {
        "has_resume": True,
        "score": score,
        "tier": job.get("heat_tier") or heat_tier(float(score or 0)),
        "note": "",
        "sentence": sentence,
        "matched": matched,
        "missing_required": missing,
        "degree": degree,
        "years": years,
        "similar": details.get("similar") or [],
    }


def _job_filters_sql(user_id: int, filters: dict[str, Any]) -> tuple[str, list[Any]]:
    """WHERE clause for the list. Expects FROM jobs j JOIN user_jobs uj ... (aliases j, uj)."""
    clauses: list[str] = ["1=1"]
    args: list[Any] = []

    keyword = (filters.get("keyword") or "").strip()
    scan_source = normalize_scan_source(filters.get("scan_source")) if filters.get("scan_source") else ""
    if keyword:
        stance = (filters.get("stance") or "").strip().lower()
        extra = ""
        if stance == "required":
            extra = " AND IFNULL(jk.stance, 'mentioned') = 'required'"
        elif stance == "preferred":
            extra = " AND IFNULL(jk.stance, 'mentioned') IN ('required', 'preferred')"
        if scan_source:
            extra += " AND jk.source = ?"
        clauses.append(
            "j.id IN (SELECT jk.job_id FROM job_keywords jk "
            "WHERE jk.user_id = ? AND jk.keyword = ? COLLATE NOCASE"
            f"{extra})"
        )
        args.extend([user_id, keyword])
        if scan_source:
            args.append(scan_source)

    q = (filters.get("q") or "").strip()
    if q:
        like = f"%{q}%"
        clauses.append(
            "(j.title LIKE ? OR j.company LIKE ? OR j.location LIKE ? OR IFNULL(j.description,'') LIKE ?)"
        )
        args.extend([like, like, like, like])

    if filters.get("remote_only"):
        clauses.append("j.is_remote = 1")

    # A minimum salary drops jobs that pay less, not jobs that list no pay.
    min_salary = filters.get("min_salary")
    if min_salary not in (None, ""):
        clauses.append("(j.salary_annual_min IS NULL OR j.salary_annual_min >= ?)")
        args.append(float(min_salary))
    if filters.get("hide_nopay"):
        clauses.append("j.salary_annual_min IS NOT NULL")

    # "I have N years": drop jobs whose required minimum is above it.
    my_years = filters.get("my_years")
    if my_years not in (None, ""):
        clauses.append("(j.years_min IS NULL OR j.years_min <= ?)")
        args.append(float(my_years))

    company = (filters.get("company") or "").strip()
    if company:
        clauses.append("LOWER(TRIM(IFNULL(j.company,''))) = ?")
        args.append(company.lower())

    place = (filters.get("loc") or "").strip().lower()
    if place:
        clauses.append("LOWER(TRIM(IFNULL(j.location,''))) IN (?, ?, ?, ?)")
        args.extend([place, f"{place}, usa", f"{place} usa", f"{place},usa"])

    cert = (filters.get("cert") or "").strip()
    if cert:
        known = {label.lower(): label for label, _term in CERT_STACK}
        label = known.get(cert.lower())
        if label:
            clauses.append("json_extract(j.cert_stances, ?) IS NOT NULL")
            args.append(f'$."{label}"')
        else:
            clauses.append("0")

    days = filters.get("posted_within_days")
    if days not in (None, ""):
        clauses.append("j.date_posted IS NOT NULL AND date(j.date_posted) >= date('now', ?)")
        args.append(f"-{int(days)} day")

    job_type = (filters.get("job_type") or "").strip()
    if job_type:
        clauses.append("LOWER(IFNULL(j.job_type,'')) LIKE ?")
        args.append(f"%{job_type.lower()}%")

    status = (filters.get("status") or "").strip()
    if status:
        clauses.append("uj.status = ?")
        args.append(status)

    seen_after = (filters.get("seen_after") or filters.get("first_seen_after") or "").strip()
    if seen_after:
        clauses.append("j.last_seen_at >= ?")
        args.append(seen_after)

    clearance = (filters.get("clearance") or "").strip()
    if clearance:
        clauses.append("j.clearance = ?")
        args.append(clearance)

    statuses = filters.get("statuses") or []
    if statuses:
        placeholders = ",".join("?" * len(statuses))
        clauses.append(f"uj.status IN ({placeholders})")
        args.extend(statuses)
    elif filters.get("pipeline_only"):
        placeholders = ",".join("?" * len(PIPELINE_STATUSES))
        clauses.append(f"uj.status IN ({placeholders})")
        args.extend(PIPELINE_STATUSES)

    heard_back = (filters.get("heard_back") or "").strip()
    if heard_back:
        clauses.append("IFNULL(uj.heard_back, 'none') = ?")
        args.append(heard_back)

    if filters.get("overdue"):
        clauses.append(
            """
            uj.follow_up_on IS NOT NULL
            AND date(uj.follow_up_on) < date('now')
            AND uj.status NOT IN ('rejected', 'ignored', 'accepted', 'withdrawn', 'ghosted')
            """
        )

    if filters.get("hide_closed"):
        clauses.append("uj.status NOT IN ('ignored', 'applied')")

    return " AND ".join(clauses), args


def _parse_exclude(filters: dict[str, Any]) -> list[str]:
    return parse_phrases(filters.get("exclude_keywords"))


def _sort_jobs(rows: list[dict[str, Any]], order: str) -> None:
    if order == "date":
        rows.sort(key=lambda j: j.get("date_posted") or "", reverse=True)
    elif order == "pay":
        rows.sort(
            key=lambda j: (
                j.get("salary_annual_min")
                if j.get("salary_annual_min") is not None
                else -1
            ),
            reverse=True,
        )
    elif order == "company":
        rows.sort(key=lambda j: (j.get("company") or "").lower())
    elif order == "seen":
        rows.sort(key=lambda j: j.get("first_seen_at") or "", reverse=True)
    elif order == "follow_up":
        rows.sort(key=lambda j: (j.get("follow_up_on") or "9999-99-99", j.get("applied_on") or ""))
    elif order == "applied":
        rows.sort(key=lambda j: j.get("applied_on") or "", reverse=True)
    else:
        rows.sort(key=lambda j: j.get("score") or 0, reverse=True)


_LIST_COLS = """
j.id, j.title, j.company, j.location, j.date_posted, j.job_type, j.is_remote,
j.salary_min, j.salary_max, j.salary_annual_min, j.salary_annual_max,
j.salary_interval, j.salary_currency, j.site, j.job_url, j.job_url_direct,
j.first_seen_at, j.last_seen_at, j.company_industry, j.clearance,
j.years_min, j.years_max, j.years_pref, j.cert_stances, j.salary_source,
j.company_rating, j.company_reviews_count,
uj.status, uj.applied_on, uj.follow_up_on, uj.heard_back, uj.notes,
uj.resume_id, uj.cover_id
"""

_NO_PROFILE = object()


class JobPage(list):
    """One page of jobs. scores_pending is set when a background rescore is running."""

    scores_pending: bool = False


def query_jobs(
    user_id: int,
    filters: dict[str, Any] | None = None,
    *,
    limit: int = 50,
    offset: int = 0,
    profile: Any = _NO_PROFILE,
    db_path: Path | None = None,
    defer_large_scores: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    init_db(db_path)
    filters = filters or {}
    if profile is _NO_PROFILE:
        profile = get_profile(user_id, db_path)
    where, args = _job_filters_sql(user_id, filters)
    keyword = (filters.get("keyword") or "").strip()
    scan_source = normalize_scan_source(filters.get("scan_source")) if filters.get("scan_source") else ""
    exclude = _parse_exclude(filters)
    title_include = parse_phrases(filters.get("title_include"))
    title_exclude = parse_phrases(filters.get("title_exclude"))
    order = filters.get("order") or "score"
    cols = _LIST_COLS + (", j.description" if exclude else "")
    select_args: list[Any] = []
    if keyword:
        # How this user's scan framed the keyword (required / preferred / ...) feeds the score.
        source_sql = " AND jk.source = ?" if scan_source else ""
        cols += (
            ", (SELECT jk.stance FROM job_keywords jk WHERE jk.user_id = ? "
            "AND jk.job_id = j.id AND jk.keyword = ? COLLATE NOCASE"
            f"{source_sql}) AS kw_stance"
        )
        select_args.extend([user_id, keyword])
        if scan_source:
            select_args.append(scan_source)
    sql = (
        f"SELECT {cols} FROM jobs j "
        f"JOIN user_jobs uj ON uj.job_id = j.id AND uj.user_id = ? "
        f"WHERE {where}"
    )
    with connect(db_path) as conn:
        fetched = conn.execute(sql, select_args + [user_id] + args).fetchall()
    rows = [dict(r) for r in fetched]
    if exclude or title_include or title_exclude:
        kept = []
        for row in rows:
            title = row.get("title") or ""
            if title_include and not has_any_phrase(title, title_include):
                continue
            if title_exclude and has_any_phrase(title, title_exclude):
                continue
            if exclude:
                blob = f"{title}\n{row.get('description') or ''}"
                if should_exclude(blob, exclude):
                    continue
            kept.append(row)
        rows = kept
    today = date.today().isoformat()
    resume = get_user_resume_profile(user_id, profile if isinstance(profile, dict) else None, db_path)
    scores_pending = False
    if resume is not None:
        scores_pending = _apply_resume_heat_scores(
            user_id,
            rows,
            resume,
            keyword,
            db_path,
            defer=defer_large_scores,
        )
    if scores_pending and order == "score":
        order = "date"
    for row in rows:
        if resume is None:
            row["score"], parts = score_job(row, keyword, profile)
            tip = score_tooltip(parts)
            row["score_tip"] = (
                "No resume uploaded — using profile fit score.\n"
                "Upload a default resume for a Heat Score based on degree, "
                "languages, certifications, skills, and experience.\n"
                + tip
            )
            row["heat_tier"] = heat_tier(float(row["score"] or 0))
        row["pay_label"] = format_pay(row)
        row["pay_estimated"] = row.get("salary_source") == "estimate"
        row["pay_hint"] = pay_hint(row)
        row["years_chips"] = years_chips(row.get("years_min"), row.get("years_max"), row.get("years_pref"))
        row["certs"] = list(_json_dict(row.get("cert_stances")))
        stance = row.get("kw_stance") or ""
        row["kw_stance_label"] = {
            "required": "Required",
            "preferred": "Preferred",
            "equivalent": "Equivalent",
            "mentioned": "Mentioned",
        }.get(stance, "")
        row["clearance"] = row.get("clearance") or "unspecified"
        row["clearance_label"] = CLEARANCE_LABELS.get(row["clearance"], row["clearance"])
        row["status_label"] = STATUS_LABELS.get(row.get("status") or "", row.get("status") or "")
        row["heard_back"] = row.get("heard_back") or "none"
        row["heard_back_label"] = HEARD_BACK_LABELS.get(row["heard_back"], row["heard_back"])
        follow = row.get("follow_up_on") or ""
        row["overdue"] = bool(
            follow
            and follow < today
            and (row.get("status") or "") not in ("rejected", "ignored", "accepted", "withdrawn", "ghosted")
        )
        row.pop("description", None)
    _sort_jobs(rows, order)
    total = len(rows)
    page = JobPage(rows[offset : offset + limit])
    page.scores_pending = scores_pending
    if page:
        ids = [row["id"] for row in page]
        placeholders = ",".join("?" * len(ids))
        with connect(db_path) as conn:
            desc_rows = conn.execute(
                f"SELECT id, description FROM jobs WHERE id IN ({placeholders})",
                ids,
            ).fetchall()
        descs = {row["id"]: row["description"] or "" for row in desc_rows}
        for row in page:
            hay = f"{row.get('title') or ''}. {descs.get(row['id'], '')}"
            row["snippet"] = make_snippet(hay, keyword) if keyword else None
    return page, total


def get_latest_run(user_id: int, db_path: Path | None = None) -> dict[str, Any] | None:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    data = dict(row)
    raw = data.get("filters_json")
    try:
        data["filters"] = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        data["filters"] = {}
    return data


def list_keywords(user_id: int, db_path: Path | None = None) -> list[dict[str, Any]]:
    """This user's scans, with how many of their postings also appear in another of their scans."""
    init_db(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                k.keyword,
                COALESCE(NULLIF(k.source, ''), 'indeed') AS source,
                COUNT(*) AS n,
                SUM(CASE WHEN extra.n > 1 THEN 1 ELSE 0 END) AS overlap
            FROM job_keywords k
            JOIN (
                SELECT job_id, COUNT(*) AS n
                FROM job_keywords
                WHERE user_id = ?
                GROUP BY job_id
            ) extra ON extra.job_id = k.job_id
            WHERE k.user_id = ?
            GROUP BY k.keyword, COALESCE(NULLIF(k.source, ''), 'indeed')
            ORDER BY n DESC, keyword ASC
            """,
            (user_id, user_id),
        ).fetchall()
    scans = []
    for row in rows:
        item = dict(row)
        source = normalize_scan_source(item.get("source"))
        keyword = item["keyword"]
        item["source"] = source
        item["label"] = scan_label(keyword, source)
        item["value"] = encode_scan(source, keyword)
        scans.append(item)
    return scans


def get_profile(user_id: int, db_path: Path | None = None) -> dict[str, Any] | None:
    """The saved profile, or None when nothing has been saved yet."""
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM profile WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        return None
    data = dict(row)
    try:
        certs = json.loads(data.get("certs") or "[]")
    except ValueError:
        certs = []
    data["certs"] = [c for c in certs if isinstance(c, str)]
    data["clearance"] = data.get("clearance") or "none"
    data["remote_pref"] = data.get("remote_pref") or "any"
    return data


def save_profile(
    user_id: int,
    *,
    years: Any = None,
    certs: list[str] | None = None,
    clearance: str = "none",
    min_pay: Any = None,
    remote_pref: str = "any",
    db_path: Path | None = None,
) -> dict[str, Any]:
    known = {label.lower(): label for label, _term in CERT_STACK}
    kept = []
    for cert in certs or []:
        label = known.get(str(cert).strip().lower())
        if label and label not in kept:
            kept.append(label)
    clearance = clearance if clearance in PROFILE_CLEARANCES else "none"
    remote_pref = remote_pref if remote_pref in dict(REMOTE_PREFS) else "any"

    def _num(value: Any, lo: float, hi: float) -> float | None:
        raw = _blank(value)
        if raw is None:
            return None
        try:
            number = float(raw.replace(",", "").replace("$", ""))
        except ValueError:
            raise ValueError(f"Not a number: {raw}") from None
        if not lo <= number <= hi:
            raise ValueError(f"{raw} is outside {lo:g}-{hi:g}")
        return number

    years_v = _num(years, 0, 60)
    pay_v = _num(min_pay, 0, 5_000_000)
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO profile (user_id, years, certs, clearance, min_pay, remote_pref, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                years = excluded.years, certs = excluded.certs,
                clearance = excluded.clearance, min_pay = excluded.min_pay,
                remote_pref = excluded.remote_pref, updated_at = excluded.updated_at
            """,
            (user_id, years_v, json.dumps(kept), clearance, pay_v, remote_pref, utc_now()),
        )
        conn.commit()
    _invalidate_caches()
    return get_profile(user_id, db_path) or {}


def clear_profile(user_id: int, db_path: Path | None = None) -> None:
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute("DELETE FROM profile WHERE user_id = ?", (user_id,))
        conn.commit()
    _invalidate_caches()


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    values = sorted(values)
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return round((values[mid - 1] + values[mid]) / 2, 2)


def _with_pct(rows: list[dict[str, Any]], total: int) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        n = int(row.get("n") or 0)
        pct = round(100 * n / total, 1) if total else 0.0
        out.append({**row, "n": n, "pct": pct})
    return out


def _percentiles(values: list[float]) -> dict[str, Any]:
    """p25 / median / p75 (linear interpolation) plus the sample size."""
    ordered = sorted(values)
    n = len(ordered)
    if not n:
        return {"n": 0, "p25": None, "median": None, "p75": None}

    def at(p: float) -> float:
        pos = (n - 1) * p
        lo = int(pos)
        hi = min(lo + 1, n - 1)
        return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo), 0)

    return {"n": n, "p25": at(0.25), "median": at(0.5), "p75": at(0.75)}


def _scaled(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add `w`, the bar width as a share of the longest bar shown (not of every job)."""
    top = max((row["n"] for row in rows), default=0)
    return [{**row, "w": round(100 * row["n"] / top, 1) if top else 0} for row in rows]


_YEAR_BUCKETS = (
    ("0–1", 0, 1),
    ("2", 2, 2),
    ("3", 3, 3),
    ("4", 4, 4),
    ("5", 5, 5),
    ("6–7", 6, 7),
    ("8–9", 8, 9),
    ("10+", 10, 999),
)


def get_insights(
    user_id: int,
    keyword: str | None = None,
    db_path: Path | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    init_db(db_path)
    if source is None and keyword and "|" in keyword:
        source, keyword = decode_scan(keyword)
    keyword = (keyword or "").strip()
    scan_source = normalize_scan_source(source) if source else ""
    cache_key = f"{db_path or DB_PATH}::{user_id}::{keyword.lower()}::{scan_source}"
    cached = _INSIGHTS_CACHE.get(cache_key)
    if cached is not None:
        return cached
    profile = get_profile(user_id, db_path)

    scope = "1=1"
    scope_args: list[Any] = []
    if keyword:
        scope = (
            "j.id IN (SELECT jk.job_id FROM job_keywords jk "
            "WHERE jk.user_id = ? AND jk.keyword = ? COLLATE NOCASE"
            + (" AND jk.source = ?" if scan_source else "")
            + ")"
        )
        scope_args = [user_id, keyword]
        if scan_source:
            scope_args.append(scan_source)
    from_sql = "FROM jobs j JOIN user_jobs uj ON uj.job_id = j.id AND uj.user_id = ?"

    with connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT j.id, j.company, j.location, j.job_type, j.company_industry, uj.status,
                   j.clearance, j.is_remote, j.salary_annual_min, j.salary_source,
                   j.years_min, j.years_max, j.years_pref, j.cert_stances,
                   CASE WHEN j.date_posted IS NULL THEN NULL
                        ELSE julianday('now') - julianday(date(j.date_posted)) END AS age_days
            {from_sql}
            WHERE {scope}
            """,
            [user_id] + scope_args,
        ).fetchall()
        stance_rows = (
            conn.execute(
                """
                SELECT IFNULL(stance, 'mentioned') AS stance, COUNT(*) AS n
                FROM job_keywords WHERE user_id = ? AND keyword = ? COLLATE NOCASE
                """
                + (" AND source = ?" if scan_source else "")
                + " GROUP BY 1",
                (user_id, keyword, scan_source) if scan_source else (user_id, keyword),
            ).fetchall()
            if keyword
            else []
        )
        job_count = conn.execute(
            "SELECT COUNT(*) FROM user_jobs WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
        recent_runs = []
        for row in conn.execute(
            """
            SELECT id, keyword, started_at, ended_at, matched, inserted, updated,
                   filtered, locations_count, filters_json
            FROM runs WHERE user_id = ? ORDER BY id DESC LIMIT 8
            """,
            (user_id,),
        ).fetchall():
            run = dict(row)
            raw_filters = run.pop("filters_json", None)
            try:
                stored = json.loads(raw_filters) if raw_filters else {}
            except json.JSONDecodeError:
                stored = {}
            run_source = (stored.get("source") if isinstance(stored, dict) else "") or "indeed"
            run["source"] = normalize_scan_source(run_source)
            run["label"] = scan_label(run.get("keyword") or "", run["source"])
            recent_runs.append(run)
        loc_counts = conn.execute(
            """
            SELECT
                SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) AS enabled,
                COUNT(*) AS total
            FROM locations WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()

    total = len(rows)
    my_clearance = CLEARANCE_RANK.get((profile or {}).get("clearance") or "none", 0)
    my_years = (profile or {}).get("years")
    my_pay = (profile or {}).get("min_pay")
    held = {c.lower() for c in (profile or {}).get("certs") or []}

    cert_counts = {label: 0 for label, _term in CERT_STACK}
    clearance_counts = {key: 0 for key in CLEARANCE_ORDER}
    companies: dict[str, int] = {}
    locations: dict[str, int] = {}
    types: dict[str, int] = {}
    industries: dict[str, int] = {}
    statuses: dict[str, int] = {}
    year_counts = {label: 0 for label, _lo, _hi in _YEAR_BUCKETS}
    years_none = years_pref_only = 0
    remote = 0
    posted_7 = posted_14 = posted_30 = new_24h = 0
    pay_by_source: dict[str, list[float]] = {"employer": [], "estimate": [], "direct_data": []}
    pay_remote: list[float] = []
    pay_onsite: list[float] = []
    fit = {"years": 0, "pay": 0, "pay_known": 0, "clearance": 0, "all": 0}

    for row in rows:
        stances = _json_dict(row["cert_stances"])
        for label in stances:
            if label in cert_counts:
                cert_counts[label] += 1
        level = row["clearance"] or "unspecified"
        clearance_counts[level] = clearance_counts.get(level, 0) + 1
        company = (row["company"] or "").strip() or "(unknown)"
        companies[company] = companies.get(company, 0) + 1
        loc = re.sub(r",?\s*USA$", "", (row["location"] or "").strip(), flags=re.I) or "(unknown)"
        locations[loc] = locations.get(loc, 0) + 1
        job_type = row["job_type"] or "(unspecified)"
        types[job_type] = types.get(job_type, 0) + 1
        industry = row["company_industry"] or "(unspecified)"
        industries[industry] = industries.get(industry, 0) + 1
        status = row["status"] or "new"
        statuses[status] = statuses.get(status, 0) + 1

        if row["is_remote"]:
            remote += 1
        age = row["age_days"]
        if age is not None:
            if age <= 1:
                new_24h += 1
            if age <= 7:
                posted_7 += 1
            if age <= 14:
                posted_14 += 1
            if age <= 30:
                posted_30 += 1

        pay = row["salary_annual_min"]
        pay_ok = pay is not None and pay >= 15000
        if pay_ok:
            source = row["salary_source"] if row["salary_source"] in pay_by_source else "direct_data"
            pay_by_source[source].append(float(pay))
            (pay_remote if row["is_remote"] else pay_onsite).append(float(pay))

        need = row["years_min"]
        if need is None:
            if row["years_pref"] is not None:
                years_pref_only += 1
            else:
                years_none += 1
        else:
            for label, lo, hi in _YEAR_BUCKETS:
                if lo <= need <= hi:
                    year_counts[label] += 1
                    break

        if profile:
            years_fit = my_years is None or need is None or need <= my_years
            clear_fit = CLEARANCE_RANK.get(level, 0) <= my_clearance
            pay_fit = True
            if my_pay and pay_ok:
                fit["pay_known"] += 1
                pay_fit = pay >= my_pay
                fit["pay"] += 1 if pay_fit else 0
            fit["years"] += 1 if years_fit else 0
            fit["clearance"] += 1 if clear_fit else 0
            fit["all"] += 1 if (years_fit and clear_fit and pay_fit) else 0

    def share(n: int, of: int | None = None) -> float:
        base = total if of is None else of
        return round(100 * n / base, 1) if base else 0.0

    def top_map(src: dict[str, int], limit: int = 12, skip: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        ranked = sorted(
            ((k, v) for k, v in src.items() if k not in skip),
            key=lambda kv: (-kv[1], kv[0]),
        )[:limit]
        return _scaled(_with_pct([{"label": k, "n": v} for k, v in ranked], total))

    all_pay = [p for vals in pay_by_source.values() for p in vals]

    cert_rows = [
        {"label": label, "n": cert_counts[label], "held": label.lower() in held}
        for label, _ in CERT_STACK
        if not (keyword and label.lower() == keyword.lower())
    ]
    cert_rows.sort(key=lambda r: -r["n"])
    cert_rows = _scaled(_with_pct(cert_rows, total))

    # Required / preferred / ... for the scanned keyword; "not in description" is its own stat.
    stance_n = {key: 0 for key in STANCE_LABELS}
    for r in stance_rows:
        stance_n[r["stance"] if r["stance"] in stance_n else "mentioned"] += r["n"]
    stance_n["absent"] = max(total - sum(v for k, v in stance_n.items() if k != "absent"), 0)
    stance_with = total - stance_n["absent"]
    heat = _with_pct(
        [
            {"key": key, "label": STANCE_LABELS[key], "n": stance_n[key]}
            for key in STANCE_LABELS
            if key != "absent"
        ],
        stance_with or 1,
    )

    unspecified_clearance = clearance_counts.pop("unspecified", 0)
    clearance_with = total - unspecified_clearance
    climate = _with_pct(
        [
            {"key": key, "label": CLEARANCE_LABELS[key], "n": clearance_counts.get(key, 0)}
            for key in CLEARANCE_ORDER
            if key != "unspecified"
        ],
        clearance_with or 1,
    )

    years_stated = total - years_none - years_pref_only
    years_rows = _scaled(
        _with_pct(
            [{"label": label, "n": year_counts[label]} for label, _lo, _hi in _YEAR_BUCKETS],
            years_stated or 1,
        )
    )

    unspecified_industry = industries.get("(unspecified)", 0)
    unspecified_type = types.get("(unspecified)", 0)

    status_rows = []
    for key in STATUSES:
        if statuses.get(key):
            status_rows.append({"key": key, "label": STATUS_LABELS.get(key, key), "n": statuses[key]})
    status_rows = _scaled(_with_pct(status_rows, total))

    keyword_rows = list_keywords(user_id, db_path)
    overlap_rows = [
        {
            "label": r["label"],
            "value": r["value"],
            "n": r["n"],
            "overlap": r["overlap"] or 0,
            "pct": round(100 * (r["overlap"] or 0) / r["n"], 1) if r["n"] else 0.0,
        }
        for r in keyword_rows
    ]

    result = {
        "keyword": keyword,
        "scan": encode_scan(scan_source, keyword) if keyword and scan_source else keyword,
        "scan_label": scan_label(keyword, scan_source) if keyword and scan_source else keyword,
        "total": total,
        "remote": remote,
        "remote_pct": share(remote),
        "with_salary": len(all_pay),
        "salary_disclose_pct": share(len(all_pay)),
        "median_salary": _median(all_pay),
        "avg_salary": round(sum(all_pay) / len(all_pay), 0) if all_pay else None,
        "pay_posted": _percentiles(pay_by_source["employer"]),
        "pay_estimate": _percentiles(pay_by_source["estimate"]),
        "pay_untagged": _percentiles(pay_by_source["direct_data"]),
        "pay_remote": _percentiles(pay_remote),
        "pay_onsite": _percentiles(pay_onsite),
        "new_24h": new_24h,
        "posted_7": posted_7,
        "posted_14": posted_14,
        "posted_30": posted_30,
        "top_companies": top_map(companies, 10),
        "top_locations": top_map(locations, 10),
        "job_types": top_map(types, 8, skip=("(unspecified)",)),
        "job_type_unspecified": unspecified_type,
        "job_type_unspecified_pct": share(unspecified_type),
        "industries": top_map(industries, 10, skip=("(unspecified)",)),
        "industry_unspecified": unspecified_industry,
        "industry_unspecified_pct": share(unspecified_industry),
        "statuses": status_rows,
        "keywords": _with_pct(
            [{"label": k["label"], "value": k["value"], "n": k["n"]} for k in keyword_rows],
            job_count or 1,
        ),
        "keyword_overlap": overlap_rows,
        "recent_runs": recent_runs,
        "locations_enabled": loc_counts["enabled"] if loc_counts else 0,
        "locations_total": loc_counts["total"] if loc_counts else 0,
        "cert_stack": cert_rows,
        "heat": heat,
        "heat_absent": stance_n["absent"],
        "heat_absent_pct": share(stance_n["absent"]),
        "heat_with": stance_with,
        "clearance": climate,
        "clearance_unspecified": unspecified_clearance,
        "clearance_unspecified_pct": share(unspecified_clearance),
        "clearance_with": clearance_with,
        "years": years_rows,
        "years_stated": years_stated,
        "years_none": years_none,
        "years_none_pct": share(years_none),
        "years_pref_only": years_pref_only,
        "years_pref_only_pct": share(years_pref_only),
        "profile": profile,
        "fit": {**fit, "total": total} if profile else None,
    }
    _INSIGHTS_CACHE[cache_key] = result
    return result


def tracker_summary(user_id: int, db_path: Path | None = None) -> dict[str, Any]:
    init_db(db_path)
    with connect(db_path) as conn:
        by_status = {
            row["status"]: row["n"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM user_jobs WHERE user_id = ? GROUP BY status",
                (user_id,),
            )
        }
        overdue = conn.execute(
            """
            SELECT COUNT(*) FROM user_jobs
            WHERE user_id = ?
              AND follow_up_on IS NOT NULL
              AND date(follow_up_on) < date('now')
              AND status NOT IN ('rejected', 'ignored', 'accepted', 'withdrawn', 'ghosted')
            """,
            (user_id,),
        ).fetchone()[0]
        applied_7d = conn.execute(
            """
            SELECT COUNT(*) FROM user_jobs
            WHERE user_id = ?
              AND applied_on IS NOT NULL
              AND date(applied_on) >= date('now', '-7 day')
            """,
            (user_id,),
        ).fetchone()[0]
        waiting = conn.execute(
            """
            SELECT COUNT(*) FROM user_jobs
            WHERE user_id = ?
              AND status IN ('applied', 'followed_up')
              AND IFNULL(heard_back, 'none') IN ('none', 'auto_reject')
            """,
            (user_id,),
        ).fetchone()[0]
    pipeline = sum(by_status.get(status, 0) for status in PIPELINE_STATUSES)
    return {
        "by_status": by_status,
        "pipeline": pipeline,
        "overdue": overdue,
        "applied_7d": applied_7d,
        "waiting": waiting,
        "interested": by_status.get("interested", 0),
        "applied": by_status.get("applied", 0),
        "interviewing": by_status.get("interviewing", 0),
        "offered": by_status.get("offered", 0),
    }


def list_runs(user_id: int, limit: int = 40, db_path: Path | None = None) -> list[dict[str, Any]]:
    init_db(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM runs WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def _drop_orphan_jobs(conn: sqlite3.Connection) -> None:
    """Remove postings no user is linked to any more. Shared rows go only when unused."""
    conn.execute("DELETE FROM jobs WHERE id NOT IN (SELECT job_id FROM user_jobs)")


def delete_job(user_id: int, job_id: int, db_path: Path | None = None) -> None:
    init_db(db_path)
    with connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM user_jobs WHERE user_id = ? AND job_id = ?",
            (user_id, job_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"Job not found: {job_id}")
        conn.execute(
            "DELETE FROM job_keywords WHERE user_id = ? AND job_id = ?",
            (user_id, job_id),
        )
        _drop_orphan_jobs(conn)
        conn.commit()
    _invalidate_caches()


def delete_jobs_for_keyword(
    user_id: int,
    keyword: str,
    db_path: Path | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Remove this user's link to postings found only by this scan.

    Postings that also appear in another of the user's scans stay. Postings no
    user is linked to any more are deleted outright. `source` limits the delete
    to one board (Indeed, SpaceX, Amazon, IBM).
    """
    if source is None and "|" in (keyword or ""):
        source, keyword = decode_scan(keyword)
    keyword = (keyword or "").strip()
    if not keyword:
        raise ValueError("Keyword is required")
    scan_source = normalize_scan_source(source) if source else ""
    init_db(db_path)
    source_sql = " AND source = ?" if scan_source else ""
    with connect(db_path) as conn:
        kept_args: list[Any] = [user_id, keyword]
        if scan_source:
            kept_args.append(scan_source)
        kept_args.extend([user_id, keyword])
        if scan_source:
            kept_args.append(scan_source)
        other = (
            "NOT (keyword = ? COLLATE NOCASE" + source_sql + ")"
            if scan_source
            else "keyword <> ? COLLATE NOCASE"
        )
        kept = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM job_keywords
            WHERE user_id = ? AND keyword = ? COLLATE NOCASE{source_sql}
              AND job_id IN (
                  SELECT job_id FROM job_keywords
                  WHERE user_id = ? AND {other}
              )
            """,
            kept_args,
        ).fetchone()[0]
        delete_args: list[Any] = [user_id, user_id, keyword]
        if scan_source:
            delete_args.append(scan_source)
        delete_args.extend([user_id, keyword])
        if scan_source:
            delete_args.append(scan_source)
        cur = conn.execute(
            f"""
            DELETE FROM user_jobs
            WHERE user_id = ?
              AND job_id IN (
                  SELECT job_id FROM job_keywords
                  WHERE user_id = ? AND keyword = ? COLLATE NOCASE{source_sql}
              )
              AND job_id NOT IN (
                  SELECT job_id FROM job_keywords
                  WHERE user_id = ? AND {other}
              )
            """,
            delete_args,
        )
        deleted = cur.rowcount
        conn.execute(
            f"DELETE FROM job_keywords WHERE user_id = ? AND keyword = ? COLLATE NOCASE{source_sql}",
            [user_id, keyword] + ([scan_source] if scan_source else []),
        )
        _drop_orphan_jobs(conn)
        conn.commit()
    _invalidate_caches()
    return {
        "keyword": keyword,
        "source": scan_source,
        "label": scan_label(keyword, scan_source) if scan_source else keyword,
        "deleted": deleted,
        "kept": int(kept or 0),
    }


def delete_run(user_id: int, run_id: int, db_path: Path | None = None) -> None:
    init_db(db_path)
    with connect(db_path) as conn:
        cur = conn.execute("DELETE FROM runs WHERE id = ? AND user_id = ?", (run_id, user_id))
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"Run not found: {run_id}")
    _invalidate_caches()


def list_materials(user_id: int, kind: str | None = None, db_path: Path | None = None) -> list[dict[str, Any]]:
    init_db(db_path)
    sql = "SELECT * FROM materials WHERE user_id = ?"
    args: list[Any] = [user_id]
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    sql += " ORDER BY is_default DESC, created_at DESC, name COLLATE NOCASE"
    with connect(db_path) as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def get_material(user_id: int, material_id: int, db_path: Path | None = None) -> dict[str, Any] | None:
    init_db(db_path)
    with connect(db_path) as conn:
        return _material_row(conn, user_id, material_id)


def resume_profile_for_material(
    user_id: int,
    material_id: int,
    db_path: Path | None = None,
) -> tuple[dict[str, Any], ResumeProfile] | None:
    """Parsed resume for the readout page, including saved add/remove edits."""
    mat = get_material(user_id, material_id, db_path)
    if mat is None or mat.get("kind") != "resume":
        return None
    text = _material_text(mat, persist=True, db_path=db_path)
    profile = get_profile(user_id, db_path) or {}
    parsed = parse_resume(text, profile, load_overrides(mat.get("overrides")))
    return mat, parsed


def save_resume_extracted_text(
    user_id: int,
    material_id: int,
    text: str,
    db_path: Path | None = None,
) -> None:
    """Store tailored resume text. The uploaded file path is left as it is."""
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT path FROM materials WHERE id = ? AND user_id = ? AND kind = 'resume'",
            (material_id, user_id),
        ).fetchone()
        if row is None:
            raise ValueError("Resume not found")
        conn.execute(
            "UPDATE materials SET extracted_text = ? WHERE id = ? AND user_id = ? AND kind = 'resume'",
            (text, material_id, user_id),
        )
        conn.commit()
    _RESUME_PROFILE_CACHE.clear()


def save_resume_overrides(
    user_id: int,
    material_id: int,
    overrides: dict[str, Any],
    db_path: Path | None = None,
) -> None:
    payload = json.dumps(normalize_overrides(overrides), ensure_ascii=False)
    init_db(db_path)
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE materials SET overrides = ?
            WHERE id = ? AND user_id = ? AND kind = 'resume'
            """,
            (payload, material_id, user_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError("Resume not found")
    _RESUME_PROFILE_CACHE.clear()


def add_material(
    user_id: int,
    kind: str,
    name: str,
    *,
    filename: str | None = None,
    path: str | None = None,
    body: str | None = None,
    is_default: bool = False,
    db_path: Path | None = None,
) -> int:
    if kind not in ("resume", "cover"):
        raise ValueError("Kind must be resume or cover")
    name = (name or "").strip()
    if not name:
        raise ValueError("Give this file a name")
    filename = _blank(filename)
    path = _blank(path)
    body = _blank(body)
    if not path and not body:
        raise ValueError("Upload a file or paste the text")
    extracted: str | None = body
    if not extracted and path:
        resolved = _resolve_material_path(path)
        if resolved is not None:
            extracted = extract_resume_text(resolved).strip() or None
    init_db(db_path)
    MATERIALS_DIR.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        if is_default:
            conn.execute(
                "UPDATE materials SET is_default = 0 WHERE user_id = ? AND kind = ?",
                (user_id, kind),
            )
        else:
            existing = conn.execute(
                "SELECT COUNT(*) FROM materials WHERE user_id = ? AND kind = ?",
                (user_id, kind),
            ).fetchone()[0]
            is_default = existing == 0
        cur = conn.execute(
            """
            INSERT INTO materials
                (user_id, kind, name, filename, path, body, created_at, is_default, extracted_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                kind,
                name,
                filename,
                path,
                body,
                utc_now(),
                1 if is_default else 0,
                extracted,
            ),
        )
        conn.commit()
        material_id = int(cur.lastrowid)
    if kind == "resume":
        _RESUME_PROFILE_CACHE.clear()
    return material_id


def set_default_material(user_id: int, material_id: int, db_path: Path | None = None) -> None:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT kind FROM materials WHERE id = ? AND user_id = ?",
            (material_id, user_id),
        ).fetchone()
        if row is None:
            raise ValueError("Material not found")
        conn.execute(
            "UPDATE materials SET is_default = 0 WHERE user_id = ? AND kind = ?",
            (user_id, row["kind"]),
        )
        conn.execute(
            "UPDATE materials SET is_default = 1 WHERE id = ? AND user_id = ?",
            (material_id, user_id),
        )
        conn.commit()
    if row["kind"] == "resume":
        _RESUME_PROFILE_CACHE.clear()


def delete_material(user_id: int, material_id: int, db_path: Path | None = None) -> None:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM materials WHERE id = ? AND user_id = ?",
            (material_id, user_id),
        ).fetchone()
        if row is None:
            raise ValueError("Material not found")
        conn.execute(
            "UPDATE user_jobs SET resume_id = NULL WHERE user_id = ? AND resume_id = ?",
            (user_id, material_id),
        )
        conn.execute(
            "UPDATE user_jobs SET cover_id = NULL WHERE user_id = ? AND cover_id = ?",
            (user_id, material_id),
        )
        conn.execute("DELETE FROM materials WHERE id = ? AND user_id = ?", (material_id, user_id))
        conn.commit()
    if row["kind"] == "resume":
        _RESUME_PROFILE_CACHE.clear()
    stored = row["path"]
    path = _resolve_material_path(stored)
    if path is not None:
        path.unlink()


FILTER_TEMPLATE_KINDS = ("title_include", "title_exclude", "exclude")


def list_filter_templates(
    user_id: int,
    kind: str | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    init_db(db_path)
    sql = "SELECT * FROM filter_templates WHERE user_id = ?"
    args: list[Any] = [user_id]
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    sql += " ORDER BY kind, name COLLATE NOCASE"
    with connect(db_path) as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def templates_by_kind(user_id: int, db_path: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    grouped = {key: [] for key in FILTER_TEMPLATE_KINDS}
    for row in list_filter_templates(user_id, db_path=db_path):
        grouped.setdefault(row["kind"], []).append(row)
    return grouped


def save_filter_template(
    user_id: int,
    kind: str,
    name: str,
    value: str,
    db_path: Path | None = None,
) -> int:
    if kind not in FILTER_TEMPLATE_KINDS:
        raise ValueError("Unknown template type")
    name = (name or "").strip()
    value = (value or "").strip()
    if not name:
        raise ValueError("Give the template a name")
    if not value:
        raise ValueError("Template is empty")
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM filter_templates WHERE user_id = ? AND kind = ? AND name = ? COLLATE NOCASE",
            (user_id, kind, name),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE filter_templates SET value = ? WHERE id = ? AND user_id = ?",
                (value, row["id"], user_id),
            )
            conn.commit()
            return int(row["id"])
        cur = conn.execute(
            """
            INSERT INTO filter_templates (user_id, kind, name, value, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, kind, name, value, utc_now()),
        )
        conn.commit()
        return int(cur.lastrowid)


def delete_filter_template(user_id: int, template_id: int, db_path: Path | None = None) -> None:
    init_db(db_path)
    with connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM filter_templates WHERE id = ? AND user_id = ?",
            (template_id, user_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError("Template not found")


if __name__ == "__main__":
    path = init_db()
    uid = local_user_id()
    locs = get_all_locations(uid)
    print(f"Database: {path}")
    enabled = sum(1 for loc in locs if loc["enabled"])
    print(f"{enabled}/{len(locs)} locations enabled")
    for loc in locs:
        flag = "on " if loc["enabled"] else "off"
        print(f"  [{flag}] {loc['id']:3} {loc['name']}")
    insights = get_insights(uid)
    print(f"\nJobs stored: {insights['total']}")
