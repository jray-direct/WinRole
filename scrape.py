"""
JobSpy Indeed scraper — keyword in title/description, US locations from SQLite.

Usage:
    python scrape.py
    python app.py          # local UI
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd
from jobspy import scrape_jobs

from db import (
    DB_PATH,
    JOB_TYPES,
    annualize,
    finish_run,
    get_enabled_locations,
    infer_remote,
    init_db,
    local_user_id,
    start_run,
    upsert_jobs,
)
from textutil import (
    STANCE_RANK,
    clean_description,
    has_any_phrase,
    keyword_pattern,
    mention_stance,
    parse_phrases,
    should_exclude,
)
from years import parse_years


def _tag_indeed_pay() -> bool:
    """Make JobSpy say whether Indeed's pay is the employer's range or its own estimate.

    JobSpy's Indeed scraper quietly falls back to Indeed's estimate when the employer
    posted no pay, then labels every row "direct_data". We wrap its pay helper so the
    row says "employer" or "estimate" instead. Returns False (and leaves JobSpy alone)
    if the library's internals are not what this expects.
    """
    import importlib

    try:
        indeed = importlib.import_module("jobspy.indeed")
        original = indeed.get_compensation
        from jobspy.model import Compensation
        from jobspy.model import JobPost

        if "salary_source" not in JobPost.model_fields:
            return False
    except (ImportError, AttributeError):
        return False
    if getattr(original, "_tags_pay_source", False):
        return True

    class TaggedCompensation(Compensation):
        salary_source: str | None = None

    def get_compensation(compensation):  # noqa: ANN001
        base = original(compensation)
        if base.min_amount is None and base.max_amount is None:
            return base
        posted = getattr(compensation, "baseSalary", None)
        return TaggedCompensation(
            **base.model_dump(),
            salary_source="employer" if posted else "estimate",
        )

    get_compensation._tags_pay_source = True  # type: ignore[attr-defined]
    indeed.get_compensation = get_compensation
    return True


PAY_TAGGING = _tag_indeed_pay()

SITES = ["indeed"]
RESULTS_WANTED = 1000
REQUEST_DELAY = 0.8

ProgressFn = Callable[[str], None]


@dataclass
class SearchFilters:
    keyword: str
    remote_only: bool = False
    min_salary: float | None = None
    posted_within_days: int | None = None
    exclude_keywords: list[str] = field(default_factory=list)
    title_include: list[str] = field(default_factory=list)
    title_exclude: list[str] = field(default_factory=list)
    job_type: str | None = None
    # "I have N years": drop jobs whose required minimum is above N. Jobs with no
    # stated number, or only a preferred one, are kept.
    my_years: float | None = None
    # any = mention in title or description; title = title only;
    # preferred / required = posting treats the keyword as at least that strong.
    kw_match: str = "any"
    fetch_description: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def haystack(rec: dict[str, Any]) -> str:
    title = rec.get("title") or ""
    desc = rec.get("description") or ""
    return f"{title}\n{desc}"


def text_matches(text: Any, pattern: re.Pattern[str]) -> bool:
    if text is None or (isinstance(text, float) and pd.isna(text)):
        return False
    return bool(pattern.search(str(text)))


def looks_us(location: Any) -> bool:
    if location is None or (isinstance(location, float) and pd.isna(location)):
        return True
    loc = str(location).strip()
    if not loc:
        return True
    if re.search(
        r"\b(Canada|UK|United Kingdom|India|Germany|Australia|Mexico|"
        r"Philippines|Poland|Brazil|France|Spain|Italy|Netherlands)\b",
        loc,
        re.IGNORECASE,
    ):
        return False
    return True


def job_key(row: dict[str, Any]) -> str:
    url = str(row.get("job_url") or row.get("job_url_direct") or "").strip().lower()
    if url:
        return url
    title = str(row.get("title") or "").strip().lower()
    company = str(row.get("company") or "").strip().lower()
    location = str(row.get("location") or "").strip().lower()
    return f"{title}|{company}|{location}"


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in {"y", "yes", "true", "1"}


def ask_filters() -> SearchFilters:
    print("\nIndeed US job search")
    print("Only jobs that pass these filters are saved and shown in the UI.")
    print('Keyword examples: Network+   CCNA   AWS   Security+\n')
    while True:
        keyword = input("Filter keyword: ").strip()
        if keyword:
            break
        print("Please enter a non-empty keyword.")

    remote_raw = input("Remote only? [y/N]: ").strip()
    remote_only = _parse_bool(remote_raw) if remote_raw else False

    min_salary = None
    salary_raw = input("Minimum annual salary USD (blank = any): ").strip().replace(",", "")
    if salary_raw:
        try:
            min_salary = float(salary_raw)
        except ValueError:
            print("  (ignored invalid salary)")

    posted_within_days = None
    days_raw = input("Posted within N days (blank = any): ").strip()
    if days_raw:
        try:
            posted_within_days = int(days_raw)
        except ValueError:
            print("  (ignored invalid days)")

    my_years = None
    years_raw = input(
        "Years of experience you have (blank = any)\n"
        "  hides jobs that require more; jobs with no stated number stay: "
    ).strip()
    if years_raw:
        try:
            my_years = float(years_raw)
        except ValueError:
            print("  (ignored invalid years)")

    exclude_raw = input(
        "Exclude phrases, comma-separated (blank = none)\n"
        "  e.g. secret clearance, senior, relocation\n"
        "  (ignores 'no clearance required' style phrasing): "
    ).strip()
    exclude_keywords = parse_phrases(exclude_raw)

    title_include = parse_phrases(
        input("Title must include (comma-separated, blank = any): ").strip()
    )
    title_exclude = parse_phrases(
        input("Title exclude (comma-separated, blank = none): ").strip()
    )

    print(f"Job types: {', '.join(JOB_TYPES)}")
    job_type_raw = input("Job type (blank = any): ").strip().lower()
    job_type = job_type_raw if job_type_raw in JOB_TYPES else None
    if job_type_raw and job_type is None:
        print("  (unknown type ignored)")

    return SearchFilters(
        keyword=keyword,
        remote_only=remote_only,
        min_salary=min_salary,
        posted_within_days=posted_within_days,
        exclude_keywords=exclude_keywords,
        title_include=title_include,
        title_exclude=title_exclude,
        job_type=job_type,
        my_years=my_years,
    )


def _posted_date(posted: Any):
    if posted is None or (isinstance(posted, float) and pd.isna(posted)):
        return None
    try:
        if hasattr(posted, "to_pydatetime"):
            return posted.to_pydatetime().date()
        if isinstance(posted, datetime):
            return posted.date()
        return datetime.fromisoformat(str(posted)[:10]).date()
    except (TypeError, ValueError):
        return None


def passes_post_filters(rec: dict[str, Any], filters: SearchFilters) -> bool:
    if filters.remote_only:
        if not infer_remote(
            rec.get("is_remote"), rec.get("location"), rec.get("title"), rec.get("description")
        ):
            return False

    if filters.job_type:
        jt = str(rec.get("job_type") or "").lower()
        if filters.job_type not in jt:
            return False

    # A minimum salary drops jobs that pay less, not jobs that list no pay.
    if filters.min_salary is not None:
        annual = annualize(
            rec.get("min_amount"),
            rec.get("interval"),
            job_type=rec.get("job_type"),
            title=rec.get("title"),
        )
        if annual is not None and annual < filters.min_salary:
            return False

    if filters.my_years is not None:
        description = rec.get("description")
        if isinstance(description, str) and description:
            blob = f"{rec.get('title') or ''}\n{clean_description(description)}"
            need = parse_years(blob).years_min
            if need is not None and need > filters.my_years:
                return False

    if filters.posted_within_days:
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=filters.posted_within_days)
        posted_date = _posted_date(rec.get("date_posted"))
        if posted_date is None or posted_date < cutoff:
            return False

    if should_exclude(haystack(rec), filters.exclude_keywords):
        return False

    title = str(rec.get("title") or "")
    if filters.title_include and not has_any_phrase(title, filters.title_include):
        return False
    if filters.title_exclude and has_any_phrase(title, filters.title_exclude):
        return False

    match_how = (filters.kw_match or "any").strip().lower()
    if match_how == "title":
        if not text_matches(title, keyword_pattern(filters.keyword)):
            return False
    elif match_how in {"preferred", "required"}:
        stance = mention_stance(haystack(rec), filters.keyword)
        need = 4 if match_how == "required" else 3
        if STANCE_RANK.get(stance, 0) < need:
            return False
    return True


def apply_post_filters(jobs: list[dict[str, Any]], filters: SearchFilters) -> list[dict[str, Any]]:
    return [rec for rec in jobs if passes_post_filters(rec, filters)]


# Indeed drops a "+" and then searches the leftover word, so "Security+" comes back
# as security-officer jobs. These certs are searched under the spellings Indeed
# actually indexes. The two queries barely overlap, so both run.
_PLUS_CERTS = {
    "a+": "A+",
    "network+": "Network+",
    "security+": "Security+",
    "linux+": "Linux+",
    "server+": "Server+",
    "cysa+": "CySA+",
    "pentest+": "PenTest+",
    "project+": "Project+",
    "cloud+": "Cloud+",
    "data+": "Data+",
    "secai+": "SecAI+",
}


def _cert_key(keyword: str) -> str:
    text = keyword.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1].strip()
    text = re.sub(r"^comptia\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip().lower().rstrip(".")
    return re.sub(r"\s*\+$", "+", text)


def indeed_search_terms(keyword: str) -> list[str]:
    """Indeed queries for a keyword. A bare plus is never sent as-is."""
    text = keyword.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1].strip()
    official = _PLUS_CERTS.get(_cert_key(text))
    if official:
        stem = official[:-1].rstrip()
        return [f"CompTIA {official}", f'"{stem} Plus"']
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*\+", text):
        return [f'"{text[:-1]} Plus"']
    if "+" in text:
        return [f'"{re.sub(r"\+", " Plus", text)}"']
    return [text]


def scrape_all_for_keyword(
    keyword: str,
    locations: list[str],
    filters: SearchFilters | None = None,
    progress: ProgressFn | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    log = progress or (lambda msg: print(msg, flush=True))
    pattern = keyword_pattern(keyword)
    collected: dict[str, dict[str, Any]] = {}
    seen_keyword: set[str] = set()
    raw_fetched = 0
    keyword_hits = 0
    errors: list[str] = []
    filters = filters or SearchFilters(keyword=keyword)

    terms = indeed_search_terms(keyword)
    log(
        f"Searching Indeed × {len(locations)} locations "
        f"for {keyword!r} in title/description ..."
    )
    if terms != [keyword.strip()]:
        log(f"Indeed queries: {', '.join(terms)}")
    if (
        filters.remote_only
        or filters.job_type
        or filters.exclude_keywords
        or filters.title_include
        or filters.title_exclude
        or filters.my_years is not None
        or filters.kw_match not in ("", "any")
        or filters.fetch_description
    ):
        log(
            "Keeping only jobs that pass filters "
            f"(remote={filters.remote_only}, type={filters.job_type or 'any'}, "
            f"my_years={filters.my_years if filters.my_years is not None else 'any'}, "
            f"kw_match={filters.kw_match}, "
            f"full_text={filters.fetch_description}, "
            f"exclude={filters.exclude_keywords or []}, "
            f"title_include={filters.title_include or []}, "
            f"title_exclude={filters.title_exclude or []})"
        )

    for loc_i, location in enumerate(locations, start=1):
        extra: dict[str, Any] = {}
        if filters.remote_only:
            extra["is_remote"] = True
        if filters.job_type:
            extra["job_type"] = filters.job_type
        if filters.posted_within_days:
            extra["hours_old"] = filters.posted_within_days * 24
        radius = 3000 if location.lower() in {"united states", "remote"} else 100
        for term in terms:
            shown = f"{term!r} @ {location!r}" if len(terms) > 1 else f"{location!r}"
            log(f"[{loc_i}/{len(locations)}] {shown}  kept={len(collected)}")
            try:
                df = scrape_jobs(
                    site_name=SITES,
                    search_term=term,
                    location=location,
                    results_wanted=RESULTS_WANTED,
                    offset=0,
                    country_indeed="USA",
                    description_format="markdown",
                    fetch_description=bool(filters.fetch_description),
                    distance=radius,
                    verbose=0,
                    **extra,
                )
            except Exception as exc:  # noqa: BLE001
                msg = f"scrape error at {location!r} for {term!r}: {exc}"
                errors.append(msg)
                log(f"  ! {msg}")
                continue

            if df is None or df.empty:
                log("  (no more results)")
                time.sleep(REQUEST_DELAY)
                continue

            board_errors = getattr(df, "attrs", {}).get("errors") or {}
            if board_errors:
                errors.append(str(board_errors))
                log(f"  board errors: {board_errors}")

            raw_fetched += len(df)
            if len(df) >= RESULTS_WANTED:
                log(
                    f"  hit the {RESULTS_WANTED}-job cap for {term!r} in this city; "
                    "Indeed may have had more"
                )
            before = len(collected)
            for _, row in df.iterrows():
                rec = row.to_dict()
                if not looks_us(rec.get("location")):
                    continue
                if not text_matches(haystack(rec), pattern):
                    continue
                key = job_key(rec)
                if key not in seen_keyword:
                    seen_keyword.add(key)
                    keyword_hits += 1
                if not passes_post_filters(rec, filters):
                    continue
                if key not in collected:
                    rec["_filter_keyword"] = keyword
                    rec["_search_location"] = location
                    rec["_dedupe_key"] = key
                    collected[key] = rec

            log(f"  raw={len(df)}  +kept={len(collected) - before}")
            time.sleep(REQUEST_DELAY)

    stats = {
        "raw_fetched": raw_fetched,
        "keyword_hits": keyword_hits,
        "matched": len(collected),
        "errors": errors,
    }
    return list(collected.values()), stats


def save_outputs(jobs: list[dict[str, Any]], keyword: str, started: datetime) -> tuple[Path, Path] | None:
    if not jobs:
        return None
    stamp = started.strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r"[^\w+=.-]+", "_", keyword.strip(), flags=re.UNICODE).strip("_") or "jobs"
    out_dir = Path("output")
    out_dir.mkdir(exist_ok=True)
    csv_path = out_dir / f"{slug}_{stamp}.csv"
    json_path = out_dir / f"{slug}_{stamp}.json"
    df = pd.DataFrame(jobs)
    df.to_csv(csv_path, index=False)
    with json_path.open("w", encoding="utf-8") as fh:
        json.dump(df.to_dict(orient="records"), fh, ensure_ascii=False, indent=2, default=str)
    return csv_path, json_path


def run_scrape(
    filters: SearchFilters,
    locations: list[str] | None = None,
    progress: ProgressFn | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    log = progress or (lambda msg: print(msg, flush=True))
    init_db()
    uid = user_id if user_id is not None else local_user_id()
    locations = locations if locations is not None else get_enabled_locations(uid)
    if not locations:
        raise RuntimeError(f"No enabled locations for user {uid} in {DB_PATH}")

    started = datetime.now(timezone.utc)
    run_id = start_run(uid, filters.keyword, len(locations), filters.as_dict())
    log(f"run #{run_id}  started {started.isoformat()}")
    log(f"locations: {len(locations)} enabled in {DB_PATH}")

    jobs, stats = scrape_all_for_keyword(
        filters.keyword, locations, filters=filters, progress=log
    )
    for rec in jobs:
        rec.setdefault("_dedupe_key", job_key(rec))

    if jobs:
        log(f"Saving {len(jobs)} jobs to database...")
    db_counts = upsert_jobs(uid, jobs, filters.keyword) if jobs else {"inserted": 0, "updated": 0}
    finish_run(
        run_id,
        raw_fetched=stats["raw_fetched"],
        matched=stats.get("keyword_hits", stats["matched"]),
        inserted=db_counts["inserted"],
        updated=db_counts["updated"],
        filtered=len(jobs),
        errors="; ".join(stats["errors"]) if stats["errors"] else None,
    )

    paths = save_outputs(jobs, filters.keyword, started)
    result = {
        "run_id": run_id,
        "keyword": filters.keyword,
        "matched": stats.get("keyword_hits", stats["matched"]),
        "filtered": len(jobs),
        "inserted": db_counts["inserted"],
        "updated": db_counts["updated"],
        "raw_fetched": stats["raw_fetched"],
        "errors": stats["errors"],
        "csv": str(paths[0]) if paths else None,
        "json": str(paths[1]) if paths else None,
        "jobs": jobs,
    }
    log(
        f"Done. {stats.get('keyword_hits', 0)} keyword hits, "
        f"{len(jobs)} passed filters and were saved "
        f"({db_counts['inserted']} new, {db_counts['updated']} updated)."
    )
    return result


def main() -> int:
    init_db()
    uid = local_user_id()
    locations = get_enabled_locations(uid)
    if not locations:
        print(f"No enabled locations in {DB_PATH}. Enable some in the UI or db.py.")
        return 1

    filters = ask_filters()
    result = run_scrape(filters, locations, user_id=uid)

    if result["csv"]:
        print(f"CSV  -> {result['csv']}")
        print(f"JSON -> {result['json']}")
    print(f"SQLite -> {DB_PATH}")
    print("Browse: python app.py\n")

    preview = result["jobs"][:25]
    for rec in preview:
        print(f"- {rec.get('title')} @ {rec.get('company')} ({rec.get('location')})")
        url = rec.get("job_url") or rec.get("job_url_direct") or ""
        if url:
            print(f"  {url}")
    extra = result["filtered"] - len(preview)
    if extra > 0:
        print(f"... +{extra} more in SQLite / output files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
