"""IBM careers search (https://www.ibm.com/careers/search).

The jobs list on ibm.com/careers is this search. The page posts to:

    POST https://www-api.ibm.com/search/api/v2

with appId "careers", scope "careers2", and lang "zz" (every locale, not one
country). Each hit includes `body`, the full posting. `description` is only
the 250-character teaser, so a scan reads `body`.

The board is about 5,800 jobs. A call returns at most 100, and paging with
`from` walks the rest. Filters on the page are OR inside a group and AND
across groups:

- field_keyword_05   country (United States, India, ...)
- field_keyword_08   career area (Software Engineering, Consulting, ...)
- field_keyword_17   Hybrid, Remote, or blank for onsite
- field_keyword_18   Professional, Entry Level, Internship
- field_keyword_19   city, as the page spells it ("Dallas, US")
- q                  the keyword box

The keyword is applied after the download, against the full posting, the
same way the other company scans work.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

API_URL = "https://www-api.ibm.com/search/api/v2"
CAREERS_URL = "https://www.ibm.com/careers/search"
HOME_URL = "https://www.ibm.com/careers/"
PAGE_SIZE = 100
_PAUSE = 0.15

_SOURCE = [
    "title",
    "url",
    "body",
    "description",
    "dcdate",
    "field_text_01",
    "field_keyword_05",
    "field_keyword_08",
    "field_keyword_17",
    "field_keyword_18",
    "field_keyword_19",
]

# Order matches the careers search page, then any area the index adds.
_CATEGORY_ORDER = [
    "Software Engineering",
    "Consulting",
    "Infrastructure & Technology",
    "Project Management",
    "Product Management",
    "Data & Analytics",
    "Enterprise Operations",
    "Sales",
    "Cloud",
    "Research",
    "Design & UX",
    "Security",
]
_LEVEL_ORDER = ["Professional", "Entry Level", "Internship"]
_LEVEL_ALIASES = {
    "professional": "Professional",
    "entry level": "Entry Level",
    "entry-level": "Entry Level",
    "intern": "Internship",
    "internship": "Internship",
}
_MODE_ORDER = [
    {"id": "Hybrid", "label": "Hybrid"},
    {"id": "Remote", "label": "Remote"},
    {"id": "Onsite", "label": "Onsite"},
]
# The index stores onsite as an empty work-arrangement value.
_MODE_API = {"hybrid": "Hybrid", "remote": "Remote", "onsite": ""}

_ROOT = Path(__file__).resolve().parent
_SNAPSHOT = _ROOT / "ibm_filters.json"
_CACHE = _ROOT / "output" / "ibm_filters.json"

ProgressFn = Callable[[str], None]


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def display_location(city: str, country: str) -> str:
    """City line from the search row. A bare city gets the country name."""
    city = city.strip()
    country = country.strip()
    if city and re.search(r",\s*[A-Za-z]{2}$", city):
        return city
    if city and country:
        return f"{city}, {country}"
    return city or country


def job_type_of(level: str) -> str:
    if level.strip().lower() in {"internship", "intern"}:
        return "internship"
    return "fulltime"


def mode_label(value: str) -> str:
    text = value.strip()
    if not text:
        return "Onsite"
    return text


def api_mode(value: str) -> str | None:
    """Page label to the value the search index stores. None if it is unknown."""
    key = value.strip().lower().replace("_", " ")
    if key in _MODE_API:
        return _MODE_API[key]
    if key in {"on-site", "on site"}:
        return ""
    if value.strip() in {"Hybrid", "Remote"}:
        return value.strip()
    return None


def level_name(value: str) -> str:
    text = value.strip()
    return _LEVEL_ALIASES.get(text.lower(), text)


def _job_id(url: str, posted_id: str) -> str:
    posted = posted_id.strip()
    if posted:
        return posted
    match = re.search(r"[?&]jobId=(\d+)", url)
    return match.group(1) if match else ""


def opening_to_record(src: dict[str, Any]) -> dict[str, Any] | None:
    title = _text(src.get("title"))
    url = _text(src.get("url"))
    if not title or not url:
        return None
    if url.startswith("/"):
        url = "https://careers.ibm.com" + url
    body = _text(src.get("body")) or _text(src.get("description"))
    country = _text(src.get("field_keyword_05"))
    category = _text(src.get("field_keyword_08"))
    arrangement = _text(src.get("field_keyword_17"))
    level = _text(src.get("field_keyword_18"))
    city = _text(src.get("field_keyword_19"))
    posted = _text(src.get("dcdate"))
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", posted):
        posted = ""
    lines = []
    if category:
        lines.append(f"Area: {category}")
    if level:
        lines.append(f"Level: {level}")
    if arrangement or "field_keyword_17" in src:
        lines.append(f"Work arrangement: {mode_label(arrangement)}")
    if country:
        lines.append(f"Country: {country}")
    header = "\n".join(lines)
    description = f"{header}\n\n{body}".strip() if header else body
    remote = arrangement.lower() == "remote"
    return {
        "id": _job_id(url, _text(src.get("field_text_01"))),
        "title": title,
        "company": "IBM",
        "location": display_location(city, country),
        "job_url": url,
        "job_url_direct": url,
        "site": "ibm",
        "description": description,
        "date_posted": posted or None,
        "job_type": job_type_of(level),
        "is_remote": remote,
        "company_industry": category or None,
        "company_url": CAREERS_URL,
        "company_url_direct": CAREERS_URL,
    }


def _qs_values(qs: dict[str, list[str]], name: str) -> list[str]:
    out: list[str] = []
    for key, values in qs.items():
        base = key.split("[", 1)[0]
        if base != name:
            continue
        for value in values:
            text = str(value).strip()
            if text and text not in out:
                out.append(text)
    return out


def parse_ibm_link(url: str) -> dict[str, Any]:
    """Filters from an ibm.com/careers/search link. Empty when it is not one."""
    raw = (url or "").strip()
    if not raw:
        return {}
    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in {"ibm.com", "careers.ibm.com"}:
        return {}
    path = (parsed.path or "").lower()
    if "career" not in path and host != "careers.ibm.com":
        return {}
    qs = parse_qs(parsed.query, keep_blank_values=True)
    keyword = ""
    for name in ("q", "query"):
        for value in _qs_values(qs, name):
            keyword = value
            break
        if keyword:
            break
    modes: list[str] = []
    saw_mode = "field_keyword_17" in {key.split("[", 1)[0] for key in qs}
    for value in _qs_values(qs, "field_keyword_17"):
        mapped = api_mode(value)
        label = "Onsite" if mapped == "" else mapped
        if label and label not in modes:
            modes.append(label)
    if saw_mode and not _qs_values(qs, "field_keyword_17") and "Onsite" not in modes:
        modes.append("Onsite")
    return {
        "keyword": keyword,
        "countries": _unique(_qs_values(qs, "field_keyword_05")),
        "categories": _unique(_qs_values(qs, "field_keyword_08")),
        "cities": _unique(_qs_values(qs, "field_keyword_19")),
        "levels": _unique(level_name(item) for item in _qs_values(qs, "field_keyword_18")),
        "modes": modes,
    }


def merge_ibm_filters(
    categories: list[str],
    countries: list[str],
    cities: list[str],
    levels: list[str],
    modes: list[str],
    board_url: str,
) -> dict[str, Any]:
    parsed = parse_ibm_link(board_url)
    return {
        "categories": _unique(list(categories) + list(parsed.get("categories") or [])),
        "countries": _unique(list(countries) + list(parsed.get("countries") or [])),
        "cities": _unique(list(cities) + list(parsed.get("cities") or [])),
        "levels": _unique(level_name(item) for item in list(levels) + list(parsed.get("levels") or [])),
        "modes": _unique(list(modes) + list(parsed.get("modes") or [])),
        "keyword": str(parsed.get("keyword") or "").strip(),
    }


def _read_filters(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _ordered(names: list[str], preferred: list[str]) -> list[str]:
    found = _unique(names)
    head = [name for name in preferred if name in found]
    tail = sorted((name for name in found if name not in head), key=str.casefold)
    return head + tail


def filter_options() -> dict[str, Any]:
    """Career areas, countries, cities, levels, and work arrangements for the form."""
    for path in (_CACHE, _SNAPSHOT):
        data = _read_filters(path)
        if data and data.get("categories") and data.get("countries"):
            return {
                "categories": list(data.get("categories") or []),
                "countries": list(data.get("countries") or []),
                "cities": list(data.get("cities") or []),
                "levels": list(data.get("levels") or _LEVEL_ORDER),
                "modes": list(data.get("modes") or _MODE_ORDER),
            }
    return {
        "categories": list(_CATEGORY_ORDER),
        "countries": ["United States"],
        "cities": [],
        "levels": list(_LEVEL_ORDER),
        "modes": list(_MODE_ORDER),
    }


def _save_filters(payload: dict[str, Any]) -> None:
    try:
        _CACHE.parent.mkdir(exist_ok=True)
        _CACHE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        return


def _bucket_keys(agg: Any) -> list[str]:
    if not isinstance(agg, dict):
        return []
    keys = []
    for bucket in agg.get("buckets") or []:
        if isinstance(bucket, dict):
            key = _text(bucket.get("key"))
            if key:
                keys.append(key)
    return keys


def _post(body: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://www.ibm.com",
            "Referer": CAREERS_URL,
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("hits"), dict):
        raise ValueError("IBM search did not return a job list")
    return payload


def _refresh_options(log: ProgressFn) -> None:
    body = {
        "appId": "careers",
        "scopes": ["careers2"],
        "query": {"bool": {"must": []}},
        "size": 1,
        "from": 0,
        "lang": "zz",
        "localeSelector": {},
        "sm": {"query": "", "lang": "zz"},
        "_source": ["title"],
        "aggs": {
            "country": {"terms": {"field": "field_keyword_05", "size": 80}},
            "category": {"terms": {"field": "field_keyword_08", "size": 40}},
            "city": {"terms": {"field": "field_keyword_19", "size": 400}},
            "level": {"terms": {"field": "field_keyword_18", "size": 10}},
        },
    }
    try:
        payload = _post(body)
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        log(f"Could not refresh IBM filters ({exc}); using the saved list")
        return
    aggs = payload.get("aggregations") or {}
    current = filter_options()
    countries = _bucket_keys(aggs.get("country")) or list(current.get("countries") or [])
    if "United States" in countries:
        countries = ["United States"] + sorted(
            (name for name in countries if name != "United States"),
            key=str.casefold,
        )
    payload_out = {
        "categories": _ordered(_bucket_keys(aggs.get("category")), _CATEGORY_ORDER)
        or list(current.get("categories") or []),
        "countries": countries,
        "cities": sorted(_bucket_keys(aggs.get("city")) or list(current.get("cities") or []), key=str.casefold),
        "levels": _ordered(_bucket_keys(aggs.get("level")), _LEVEL_ORDER) or list(_LEVEL_ORDER),
        "modes": list(_MODE_ORDER),
    }
    _save_filters(payload_out)


def _clause(field: str, values: list[str]) -> dict[str, Any] | None:
    if not values:
        return None
    if len(values) == 1:
        return {"term": {field: values[0]}}
    return {"terms": {field: values}}


def _post_filter(
    countries: list[str],
    categories: list[str],
    cities: list[str],
    levels: list[str],
    modes: list[str],
) -> dict[str, Any]:
    must: list[dict[str, Any]] = []
    for field, values in (
        ("field_keyword_05", countries),
        ("field_keyword_08", categories),
        ("field_keyword_19", cities),
        ("field_keyword_18", levels),
    ):
        clause = _clause(field, values)
        if clause:
            must.append(clause)
    if modes:
        should = []
        for mode in modes:
            api_value = api_mode(mode)
            if api_value is None:
                continue
            should.append({"term": {"field_keyword_17": api_value}})
        if should:
            must.append({"bool": {"should": should, "minimum_should_match": 1}})
    return {"bool": {"must": must}}


def fetch_openings(
    *,
    categories: list[str] | None = None,
    countries: list[str] | None = None,
    cities: list[str] | None = None,
    levels: list[str] | None = None,
    modes: list[str] | None = None,
    progress: ProgressFn | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Openings from ibm.com/careers/search. Blank filters read the whole board."""
    log = progress or (lambda _msg: None)
    errors: list[str] = []
    chosen_areas = _unique(list(categories or []))
    chosen_countries = _unique(list(countries or []))
    chosen_cities = _unique(list(cities or []))
    chosen_levels = _unique(level_name(item) for item in (levels or []))
    chosen_modes = _unique(list(modes or []))

    log("Loading IBM careers search")
    _refresh_options(log)
    bits = []
    bits.append("countries " + ", ".join(chosen_countries) if chosen_countries else "every country")
    if chosen_areas:
        bits.append("areas " + ", ".join(chosen_areas))
    if chosen_cities:
        bits.append("cities " + ", ".join(chosen_cities))
    if chosen_levels:
        bits.append("levels " + ", ".join(chosen_levels))
    if chosen_modes:
        bits.append("work arrangement " + ", ".join(chosen_modes))
    log("Board filters: " + "; ".join(bits))

    post_filter = _post_filter(
        chosen_countries, chosen_areas, chosen_cities, chosen_levels, chosen_modes
    )
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    start = 0
    total: int | None = None
    while start < 20000:
        body = {
            "appId": "careers",
            "scopes": ["careers2"],
            "query": {"bool": {"must": []}},
            "post_filter": post_filter,
            "size": PAGE_SIZE,
            "from": start,
            "lang": "zz",
            "localeSelector": {},
            "sm": {"query": "", "lang": "zz"},
            "sort": [{"dcdate": "desc"}, {"_score": "desc"}],
            "_source": _SOURCE,
        }
        try:
            payload = _post(body)
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            if start == 0:
                raise RuntimeError(f"IBM careers search failed: {exc}") from exc
            errors.append(f"IBM page at {start} failed: {exc}")
            log(errors[-1])
            break
        hits_wrap = payload.get("hits") or {}
        hits = hits_wrap.get("hits") or []
        reported = hits_wrap.get("total") or {}
        if isinstance(reported, dict) and isinstance(reported.get("value"), int):
            total = reported["value"]
        if not hits:
            break
        for hit in hits:
            src = hit.get("_source") if isinstance(hit, dict) else None
            if not isinstance(src, dict):
                continue
            rec = opening_to_record(src)
            if rec is None:
                continue
            key = rec["id"] or rec["job_url"]
            if key in seen:
                continue
            seen.add(key)
            records.append(rec)
        start += len(hits)
        log(f"IBM careers: {len(records)} openings read" + (f" of {total}" if total else ""))
        if len(hits) < PAGE_SIZE or (total is not None and start >= total):
            break
        time.sleep(_PAUSE)

    return records, {"raw_fetched": len(records), "errors": errors}
