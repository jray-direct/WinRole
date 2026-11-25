"""Amazon Jobs search (https://www.amazon.jobs/en/search).

The page the user asked for is the United States board:

    /en/search?base_query=&loc_query=united+states&type=area
        &longitude=-77.03196&latitude=38.89036&country=USA

Those coordinates are the map pin for "United States", not a radius around
Washington. The list itself comes from:

    GET https://www.amazon.jobs/en/search.json

Each row already includes the description, basic qualifications, preferred
qualifications, team, category, schedule, and posted date. No second request
per job.

The API will not return more than 100 jobs per call, and it refuses any
window past 10,000 (offset + limit). The US board is larger than that
(about 12,600). Every opening has one job category, and the largest
category is a few thousand, so a full scan walks one category at a time.
Filters on the page are OR inside a group and AND across groups:

- category[]                 job category
- business_category[]        team (aws, fulfillment-ops, ...)
- normalized_location[]      "Seattle, Washington, USA"
- schedule_type_id[]         Full-Time, Part-Time (20-29 hours), ...
- is_intern[]=1              internships
- employee_class[]           Seasonal/Short-Term
- loc_query                  the location box (default "united states")
- base_query                 the keyword box
- sort=recent|relevant
"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from textutil import has_any_phrase, keyword_pattern

SEARCH_URL = "https://www.amazon.jobs/en/search.json"
CAREERS_URL = (
    "https://www.amazon.jobs/en/search?base_query=&loc_query=united+states"
    "&country=USA&latitude=38.89036&longitude=-77.03196"
)
USER_AGENT = "WinRole job search (public Amazon Jobs search)"
PAGE_SIZE = 100
MAX_WINDOW = 10000
DEFAULT_LOC = "united states"
# Map center the US search page sends. It does not limit results unless a radius is set.
MAP_LAT = "38.89036"
MAP_LON = "-77.03196"

_ROOT = Path(__file__).resolve().parent
_SNAPSHOT = _ROOT / "amazon_filters.json"
_CACHE = _ROOT / "output" / "amazon_filters.json"

ProgressFn = Callable[[str], None]

_HOURLY = re.compile(
    r"(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*USD\s+hourly",
    re.I,
)
_HOURLY_DOLLAR = re.compile(
    r"\$\s*(\d+(?:\.\d+)?)\s*[-–]\s*\$\s*(\d+(?:\.\d+)?)\s*(?:/\s*hour|per hour|hourly)",
    re.I,
)
_ANNUAL = re.compile(
    r"\$\s*(\d{1,3}(?:,\d{3})+|\d{4,})(?:\.\d+)?\s*[-–]\s*\$\s*(\d{1,3}(?:,\d{3})+|\d{4,})(?:\.\d+)?",
)
_USD_ANNUAL = re.compile(
    r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?)\s*[-–]\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?)\s*USD\s+annually",
    re.I,
)


def _get_json(params: list[tuple[str, str]], timeout: int = 60) -> dict[str, Any]:
    url = SEARCH_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Amazon Jobs did not return a search object")
    return data


def html_to_text(raw: str | None) -> str:
    if not raw:
        return ""
    text = str(raw)
    for _ in range(2):
        nxt = html.unescape(text)
        if nxt == text:
            break
        text = nxt
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = re.sub(r"(?i)</h[1-6]>", "\n\n", text)
    text = re.sub(r"(?i)</li>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _money(raw: str) -> float:
    return float(raw.replace(",", ""))


def parse_pay(text: str) -> tuple[float | None, float | None, str | None]:
    """Employer pay range when Amazon printed one in the posting."""
    if not text:
        return None, None, None
    annual_usd = list(_USD_ANNUAL.finditer(text))
    if annual_usd:
        lows = [_money(match.group(1)) for match in annual_usd]
        highs = [_money(match.group(2)) for match in annual_usd]
        return min(lows), max(highs), "yearly"
    hourly = _HOURLY.search(text) or _HOURLY_DOLLAR.search(text)
    if hourly:
        return _money(hourly.group(1)), _money(hourly.group(2)), "hourly"
    for line in text.splitlines():
        if not re.search(r"pay|salary|compensation|annually|/\s*year|per year", line, re.I):
            continue
        annual = _ANNUAL.search(line)
        if annual:
            lo, hi = _money(annual.group(1)), _money(annual.group(2))
            if hi >= lo >= 1000:
                return lo, hi, "yearly"
    return None, None, None


def parse_posted(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _location_objects(job: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for raw in job.get("locations") or []:
        if isinstance(raw, dict):
            found.append(raw)
            continue
        if not isinstance(raw, str) or not raw.strip().startswith("{"):
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            found.append(obj)
    return found


def is_remote_job(job: dict[str, Any]) -> bool:
    blob = " ".join(
        str(job.get(key) or "")
        for key in ("normalized_location", "location", "title")
    ).lower()
    if "virtual" in blob or re.search(r"\bremote\b", blob):
        return True
    for obj in _location_objects(job):
        kind = str(obj.get("type") or "").upper()
        if kind in {"REMOTE", "VIRTUAL"}:
            return True
    return False


def display_location(job: dict[str, Any]) -> str:
    names: list[str] = []
    primary = str(job.get("normalized_location") or "").strip()
    if primary:
        names.append(primary)
    for obj in _location_objects(job):
        label = str(obj.get("normalizedLocation") or obj.get("normalizedCityName") or "").strip()
        if label and label not in names:
            names.append(label)
    if names:
        return "; ".join(names)
    return str(job.get("location") or "").strip()


def job_type_of(job: dict[str, Any]) -> str | None:
    if str(job.get("is_intern") or "") in {"1", "true", "True"}:
        return "internship"
    schedule = str(job.get("job_schedule_type") or "").strip().lower()
    if "intern" in schedule:
        return "internship"
    if schedule in {"full-time", "full time"}:
        return "fulltime"
    if any(word in schedule for word in ("part", "flex", "reduced")):
        return "parttime"
    return None


def _unique(items: list[str]) -> list[str]:
    found: list[str] = []
    for item in items:
        text = (item or "").strip()
        if text and text not in found:
            found.append(text)
    return found


def parse_amazon_link(url: str) -> dict[str, Any]:
    """Read filters off an amazon.jobs search link."""
    raw = (url or "").strip()
    if not raw:
        return {}
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    host = (parsed.netloc or "").lower()
    if host and "amazon.jobs" not in host:
        return {}
    qs = parse_qs(parsed.query, keep_blank_values=False)

    def pieces(*keys: str) -> list[str]:
        found: list[str] = []
        for key in keys:
            for item in qs.get(key) or []:
                for bit in str(item).split("|"):
                    bit = bit.strip()
                    if bit and bit not in found:
                        found.append(bit)
        return found

    loc = ""
    for item in qs.get("loc_query") or []:
        loc = str(item).strip()
        if loc:
            break
    if loc.lower() in {"", "united states", "usa", "us"}:
        loc = ""
    keyword = ""
    for item in qs.get("base_query") or []:
        keyword = str(item).strip()
        if keyword:
            break
    schedules = pieces("schedule_type_id[]", "schedule_type_id")
    employee = [item.lower() for item in pieces("employee_class[]", "employee_class")]
    interns = pieces("is_intern[]", "is_intern")
    return {
        "keyword": keyword,
        "categories": pieces("category[]", "category"),
        "locations": pieces("normalized_location[]", "normalized_location"),
        "teams": pieces("business_category[]", "business_category"),
        "schedules": schedules,
        "loc_query": loc,
        "intern": any(item == "1" for item in interns),
        "seasonal": any("season" in item for item in employee),
    }


def merge_amazon_filters(
    categories: list[str],
    locations: list[str],
    teams: list[str],
    schedules: list[str],
    loc_query: str,
    intern: bool,
    seasonal: bool,
    board_url: str,
) -> dict[str, Any]:
    parsed = parse_amazon_link(board_url)
    return {
        "categories": _unique(list(categories) + list(parsed.get("categories") or [])),
        "locations": _unique(list(locations) + list(parsed.get("locations") or [])),
        "teams": _unique(list(teams) + list(parsed.get("teams") or [])),
        "schedules": _unique(list(schedules) + list(parsed.get("schedules") or [])),
        "loc_query": (loc_query or parsed.get("loc_query") or "").strip(),
        "intern": bool(intern or parsed.get("intern")),
        "seasonal": bool(seasonal or parsed.get("seasonal")),
        "keyword": str(parsed.get("keyword") or "").strip(),
    }


def _read_filters(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _save_filters(payload: dict[str, Any]) -> None:
    try:
        _CACHE.parent.mkdir(exist_ok=True)
        _CACHE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        return


def filter_options() -> dict[str, Any]:
    """Categories, teams, schedules, and cities for the scan form."""
    for path in (_CACHE, _SNAPSHOT):
        data = _read_filters(path)
        if data and data.get("categories"):
            return {
                "categories": list(data.get("categories") or []),
                "teams": list(data.get("teams") or []),
                "schedules": list(data.get("schedules") or []),
                "locations": list(data.get("locations") or []),
            }
    return {"categories": [], "teams": [], "schedules": [], "locations": []}


def _facet_names(rows: Any) -> list[str]:
    names: list[str] = []
    if not isinstance(rows, list):
        return names
    for row in rows:
        if isinstance(row, dict) and len(row) == 1:
            name = str(next(iter(row.keys()))).strip()
            if name and name not in names:
                names.append(name)
    return names


def _refresh_options(log: ProgressFn) -> list[str]:
    """Category names from the live US board. Falls back to the snapshot."""
    params = [
        ("loc_query", DEFAULT_LOC),
        ("country", "USA"),
        ("latitude", MAP_LAT),
        ("longitude", MAP_LON),
        ("sort", "recent"),
        ("offset", "0"),
        ("result_limit", "1"),
        ("facets[]", "category"),
        ("facets[]", "business_category"),
        ("facets[]", "schedule_type_id"),
        ("facets[]", "normalized_location"),
    ]
    try:
        data = _get_json(params)
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        log(f"Could not refresh Amazon filters ({exc}); using the saved list")
        return list(filter_options().get("categories") or [])
    facets = data.get("facets") or {}
    categories = _facet_names(facets.get("category_facet"))
    current = filter_options()
    labels = {str(team.get("id")): str(team.get("label") or "") for team in current.get("teams") or []}
    teams = []
    for slug in _facet_names(facets.get("business_category_facet")):
        teams.append({"id": slug, "label": labels.get(slug) or slug.replace("-", " ").title()})
    payload = {
        "categories": categories or list(current.get("categories") or []),
        "teams": teams or list(current.get("teams") or []),
        "schedules": _facet_names(facets.get("schedule_type_id_facet")) or list(current.get("schedules") or []),
        "locations": _facet_names(facets.get("normalized_location_facet")) or list(current.get("locations") or []),
    }
    if payload["categories"]:
        _save_filters(payload)
    return payload["categories"]


def _pairs(
    *,
    category: str | None,
    locations: list[str],
    teams: list[str],
    schedules: list[str],
    loc_query: str,
    intern: bool,
    seasonal: bool,
    offset: int,
) -> list[tuple[str, str]]:
    pairs = [
        ("loc_query", loc_query or DEFAULT_LOC),
        ("country", "USA"),
        ("latitude", MAP_LAT),
        ("longitude", MAP_LON),
        ("sort", "recent"),
        ("offset", str(offset)),
        ("result_limit", str(PAGE_SIZE)),
    ]
    if category:
        pairs.append(("category[]", category))
    for place in locations:
        pairs.append(("normalized_location[]", place))
    for team in teams:
        pairs.append(("business_category[]", team))
    for schedule in schedules:
        pairs.append(("schedule_type_id[]", schedule))
    if intern:
        pairs.append(("is_intern[]", "1"))
    if seasonal:
        pairs.append(("employee_class[]", "Seasonal/Short-Term"))
    return pairs


def _page(pairs: list[tuple[str, str]]) -> tuple[list[dict[str, Any]], str | None]:
    try:
        data = _get_json(pairs)
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        return [], str(exc)
    error = data.get("error")
    if error:
        return [], str(error)
    jobs = [job for job in (data.get("jobs") or []) if isinstance(job, dict)]
    return jobs, None


def opening_to_record(job: dict[str, Any], team_labels: dict[str, str] | None = None) -> dict[str, Any]:
    team_labels = team_labels or {}
    icims = str(job.get("id_icims") or job.get("id") or "").strip()
    title = str(job.get("title") or "").strip()
    category = str(job.get("job_category") or "").strip()
    team_id = str(job.get("business_category") or "").strip()
    team = team_labels.get(team_id) or (team_id.replace("-", " ").title() if team_id else "")
    schedule = str(job.get("job_schedule_type") or "").strip()
    description_body = html_to_text(job.get("description"))
    basic = html_to_text(job.get("basic_qualifications"))
    preferred = html_to_text(job.get("preferred_qualifications"))
    header = []
    if category:
        header.append("Category: " + category)
    if team:
        header.append("Team: " + team)
    if schedule:
        header.append("Schedule: " + schedule)
    chunks = ["\n".join(header)]
    if description_body:
        chunks.append(description_body)
    if basic:
        chunks.append("Basic qualifications\n" + basic)
    if preferred:
        chunks.append("Preferred qualifications\n" + preferred)
    description = "\n\n".join(part for part in chunks if part.strip())
    pay_lo, pay_hi, interval = parse_pay(description)
    path = str(job.get("job_path") or "").strip()
    if path.startswith("http"):
        url = path
    elif path.startswith("/"):
        url = "https://www.amazon.jobs" + path
    elif icims:
        url = f"https://www.amazon.jobs/en/jobs/{icims}"
    else:
        url = CAREERS_URL
    company = str(job.get("company_name") or "").strip() or "Amazon"
    return {
        "id": icims or None,
        "title": title,
        "company": company,
        "location": display_location(job),
        "job_url": url,
        "job_url_direct": str(job.get("url_next_step") or url),
        "site": "amazon",
        "description": description,
        "date_posted": parse_posted(job.get("posted_date")),
        "job_type": job_type_of(job),
        "is_remote": is_remote_job(job),
        "company_industry": category or None,
        "company_url": CAREERS_URL,
        "company_url_direct": CAREERS_URL,
        "min_amount": pay_lo,
        "max_amount": pay_hi,
        "interval": interval,
        "currency": "USD" if interval else None,
        "salary_source": "employer" if interval else None,
    }


def fetch_openings(
    *,
    categories: list[str] | None = None,
    locations: list[str] | None = None,
    teams: list[str] | None = None,
    schedules: list[str] | None = None,
    loc_query: str = "",
    intern: bool = False,
    seasonal: bool = False,
    title_include: list[str] | None = None,
    title_exclude: list[str] | None = None,
    keyword: str = "",
    kw_match: str = "any",
    progress: ProgressFn | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """United States openings from amazon.jobs, walked by job category."""
    log = progress or (lambda _msg: None)
    errors: list[str] = []
    chosen = _unique(list(categories or []))
    places = _unique(list(locations or []))
    team_ids = _unique(list(teams or []))
    schedule_ids = _unique(list(schedules or []))
    where = (loc_query or "").strip()

    log("Loading Amazon Jobs for the United States")
    catalog = _refresh_options(log)
    labels = {str(team.get("id")): str(team.get("label") or "") for team in filter_options().get("teams") or []}
    partitions = chosen or catalog
    if not partitions:
        raise RuntimeError("Amazon Jobs did not return any job categories")

    bits = []
    if chosen:
        bits.append("categories " + ", ".join(chosen))
    else:
        bits.append(f"all {len(partitions)} categories")
    if places:
        bits.append("cities " + ", ".join(places))
    if team_ids:
        bits.append("teams " + ", ".join(labels.get(team, team) for team in team_ids))
    if schedule_ids:
        bits.append("schedules " + ", ".join(schedule_ids))
    if where:
        bits.append("location box " + where)
    if intern:
        bits.append("internships")
    if seasonal:
        bits.append("seasonal")
    log("Board filters: " + "; ".join(bits))

    pattern = keyword_pattern(keyword) if (kw_match or "any") == "title" and keyword.strip() else None
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    fetched = 0
    for index, category in enumerate(partitions, start=1):
        offset = 0
        category_count = 0
        while offset < MAX_WINDOW:
            jobs, error = _page(
                _pairs(
                    category=category,
                    locations=places,
                    teams=team_ids,
                    schedules=schedule_ids,
                    loc_query=where,
                    intern=intern,
                    seasonal=seasonal,
                    offset=offset,
                )
            )
            if error:
                errors.append(f"{category}: {error}")
                log(f"! {category}: {error}")
                break
            if not jobs:
                break
            fetched += len(jobs)
            for job in jobs:
                title = str(job.get("title") or "")
                if title_include and not has_any_phrase(title, title_include):
                    continue
                if title_exclude and has_any_phrase(title, title_exclude):
                    continue
                if pattern and not pattern.search(title):
                    continue
                key = str(job.get("id_icims") or job.get("id") or "")
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                records.append(opening_to_record(job, labels))
                category_count += 1
            if len(jobs) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
            time.sleep(0.15)
        log(f"[{index}/{len(partitions)}] {category}: {category_count}")
    log(f"Amazon Jobs: {fetched} rows read, {len(records)} unique openings kept")
    return records, {"raw_fetched": fetched, "described": len(records), "errors": errors}
