"""Company career boards beyond SpaceX, Amazon, and IBM.

Each scan reads the public jobs search behind the company's careers page,
including the full posting, then the usual keyword rules run on that text.

- Oracle: the search the careers page opens, Oracle Cloud candidate experience
  (eeho.fa.us2.oraclecloud.com, site CX_45001).
- HPE and Cisco: the Phenom search on careers.hpe.com and careers.cisco.com.
- Verizon: mycareer.verizon.com/api/jobs/search, then each job page.
- Dell: the Oracle candidate-experience site on the Dell careers URL
  (site CX_1001).
- Tesla: www.tesla.com/cua-api/apps/careers/state, then each job.
- Walmart and Wells Fargo: the Workday jobs search behind those careers sites.
  Walmart stays on technology and data. Wells Fargo stays on technology and
  data in the United States.
- UnitedHealth Group and Spectrum: the search-results feed on their careers
  sites. UnitedHealth stays on the Technology category. Spectrum stays in
  the United States.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from amazon import html_to_text

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_ROOT = Path(__file__).resolve().parent
_SNAPSHOT = _ROOT / "company_filters.json"
_CACHE = _ROOT / "output" / "company_filters.json"
_WORKERS = 6

ProgressFn = Callable[[str], None]

PHENOM = {
    "hpe": {
        "name": "HPE",
        "company": "Hewlett Packard Enterprise",
        "widgets": "https://careers.hpe.com/widgets",
        "referer": "https://careers.hpe.com/us/en/search-results",
        "job_base": "https://careers.hpe.com/us/en/job/",
        "ref": "HPE1US",
        "country": "us",
        "locale": "en_us",
        "careers": "https://careers.hpe.com/us/en/search-results",
        "label": "HPE careers — careers.hpe.com",
        "blurb": "Reads every opening on the HPE careers search, then keeps the ones whose posting matches your keyword.",
        "hint": "The US careers site lists jobs in every country. Leave the filters empty to read that whole board.",
    },
    "cisco": {
        "name": "Cisco",
        "company": "Cisco",
        "widgets": "https://careers.cisco.com/widgets",
        "referer": "https://careers.cisco.com/global/en/search-results",
        "job_base": "https://careers.cisco.com/global/en/job/",
        "ref": "CISCISGLOBAL",
        "country": "global",
        "locale": "en_global",
        "careers": "https://careers.cisco.com/global/en/home",
        "label": "Cisco careers — careers.cisco.com",
        "blurb": "Reads every opening on Cisco's global careers search, then keeps the ones whose posting matches your keyword.",
        "hint": "Leave the filters empty to read the whole global board.",
    },
}

ORACLE_CE = {
    "oracle": {
        "name": "Oracle",
        "company": "Oracle",
        "host": "https://eeho.fa.us2.oraclecloud.com",
        "site": "CX_45001",
        "careers": "https://careers.oracle.com/en/sites/jobsearch/jobs",
        "job_base": "https://careers.oracle.com/en/sites/jobsearch/job/",
        "label": "Oracle careers — oracle.com/careers",
        "blurb": "Reads the Oracle job search linked from oracle.com/careers, then keeps openings whose posting matches your keyword.",
        "hint": "Leave locations empty to read every country. United States is the search the careers page opens.",
    },
    "dell": {
        "name": "Dell",
        "company": "Dell Technologies",
        "host": "https://enterpriseplatform.dell.com",
        "site": "CX_1001",
        "careers": "https://enterpriseplatform.dell.com/hcmUI/CandidateExperience/en/sites/careers",
        "job_base": "https://enterpriseplatform.dell.com/hcmUI/CandidateExperience/en/sites/careers/job/",
        "label": "Dell careers — dell.com/careers",
        "blurb": "Reads every opening on the Dell careers site, then keeps the ones whose posting matches your keyword.",
        "hint": "Leave the filters empty to read the whole Dell board.",
    },
}

VERIZON = {
    "name": "Verizon",
    "company": "Verizon",
    "search": "https://mycareer.verizon.com/api/jobs/search",
    "home": "https://mycareer.verizon.com",
    "careers": "https://mycareer.verizon.com/",
    "label": "Verizon careers — mycareer.verizon.com",
    "blurb": "Reads every opening on Verizon careers, then keeps the ones whose posting matches your keyword.",
    "hint": "Leave the filters empty to read the whole board. The site returns ten jobs at a time, then each posting is read in full.",
}

TESLA = {
    "name": "Tesla",
    "company": "Tesla",
    "state": "https://www.tesla.com/cua-api/apps/careers/state",
    "detail": "https://www.tesla.com/cua-api/careers/job/",
    "careers": "https://www.tesla.com/careers",
    "label": "Tesla careers — tesla.com/careers",
    "blurb": "Reads every opening on Tesla careers, then keeps the ones whose posting matches your keyword.",
    "hint": "Leave the filters empty to read the whole board. If Tesla refuses the connection, the scan says so instead of saving an empty list.",
}

WORKDAY = {
    "walmart": {
        "name": "Walmart",
        "company": "Walmart",
        "host": "https://walmart.wd504.myworkdayjobs.com",
        "tenant": "walmart",
        "site": "WalmartExternal",
        "careers": "https://careers.walmart.com/us/en/results?careerareas=technology",
        "label": "Walmart careers — technology",
        "blurb": "Reads Walmart technology and data openings, then keeps the ones whose posting matches your keyword.",
        "hint": "Technology and Data Analytics stay selected so store, club, and corporate finance roles stay out. Pick other career areas to scan those instead. Leaving both blank puts these two back.",
        "default_categories": ["Technology", "Data Analytics and Management"],
        "default_countries": [],
    },
    "wellsfargo": {
        "name": "Wells Fargo",
        "company": "Wells Fargo",
        "host": "https://wf.wd1.myworkdayjobs.com",
        "tenant": "wf",
        "site": "WellsFargoJobs",
        "careers": "https://www.wellsfargojobs.com/en/jobs/?country=United+States+of+America&team=Technology",
        "label": "Wells Fargo careers — technology, United States",
        "blurb": "Reads Wells Fargo technology and data openings in the United States, including remote roles listed there, then keeps the ones whose posting matches your keyword.",
        "hint": "Technology, Data & Analytics, and the United States stay selected, including remote roles listed in the United States. Pick other values to change the scan. Leaving them blank puts these back.",
        "default_categories": ["Technology", "Data & Analytics"],
        "default_countries": ["United States of America"],
    },
}

RADANCY = {
    "unitedhealth": {
        "name": "UnitedHealth Group",
        "company": "UnitedHealth Group",
        "results": "https://careers.unitedhealthgroup.com/search-jobs/results",
        "home": "https://careers.unitedhealthgroup.com",
        "org": "34088",
        "careers": "https://careers.unitedhealthgroup.com/search-jobs",
        "label": "UnitedHealth Group — technology",
        "blurb": "Reads UnitedHealth Group technology openings, then keeps the ones whose posting matches your keyword.",
        "hint": "Technology stays selected so clinical and operations roles stay out. Pick another category to scan that instead. Leave countries empty to read every country in the selected category.",
        "default_categories": ["Technology"],
        "default_countries": [],
    },
    "spectrum": {
        "name": "Spectrum",
        "company": "Spectrum",
        "results": "https://jobs.spectrum.com/search-jobs/results",
        "home": "https://jobs.spectrum.com",
        "org": "4673",
        "careers": "https://jobs.spectrum.com/search-jobs?l=United%20States&orgIds=4673",
        "label": "Spectrum careers — United States",
        "blurb": "Reads Spectrum openings in the United States, then keeps the ones whose posting matches your keyword.",
        "hint": "The United States stays selected. Leave categories empty to read every kind of opening in the country.",
        "default_categories": [],
        "default_countries": ["United States"],
    },
}

COMPANY_BOARDS = (
    "oracle",
    "hpe",
    "verizon",
    "dell",
    "cisco",
    "tesla",
    "walmart",
    "unitedhealth",
    "wellsfargo",
    "spectrum",
)


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _posted(value: str) -> str | None:
    match = re.match(r"(\d{4}-\d{2}-\d{2})", value.strip())
    return match.group(1) if match else None


def _job_type(value: str) -> str:
    text = value.lower()
    if "intern" in text:
        return "internship"
    if "part" in text:
        return "parttime"
    if "contract" in text or "temporary" in text or "temp " in text:
        return "contract"
    return "fulltime"


def _remote(*parts: str) -> bool:
    blob = " ".join(parts).lower()
    if "on-site" in blob or "onsite" in blob or "on site" in blob:
        return "remote" in blob and "hybrid" not in blob and "onsite only" not in blob and "on-site" not in blob
    return "remote" in blob


def _urlopen(req: urllib.request.Request, timeout: int):
    delay = 2.0
    for attempt in range(5):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code not in (429, 503) or attempt == 4:
                raise
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("Career search did not respond")


def _get(url: str, headers: dict[str, str] | None = None, timeout: int = 60) -> bytes:
    merged = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html"}
    if headers:
        merged.update(headers)
    req = urllib.request.Request(url, headers=merged)
    with _urlopen(req, timeout) as resp:
        return resp.read()


def _post_json(url: str, payload: dict[str, Any], referer: str) -> dict[str, Any]:
    origin = urllib.parse.urlsplit(referer).scheme + "://" + urllib.parse.urlsplit(referer).netloc
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": origin,
            "Referer": referer,
        },
    )
    with _urlopen(req, 60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Career search did not return a job list")
    return data


def _read_store() -> dict[str, Any]:
    for path in (_CACHE, _SNAPSHOT):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def _write_cache(data: dict[str, Any]) -> None:
    try:
        _CACHE.parent.mkdir(exist_ok=True)
        _CACHE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        return


def _meta(source: str) -> dict[str, str]:
    if source in PHENOM:
        row = PHENOM[source]
    elif source in ORACLE_CE:
        row = ORACLE_CE[source]
    elif source == "verizon":
        row = VERIZON
    elif source in WORKDAY:
        row = WORKDAY[source]
    elif source in RADANCY:
        row = RADANCY[source]
    else:
        row = TESLA
    return {
        "id": source,
        "name": row["name"],
        "label": row["label"],
        "blurb": row["blurb"],
        "hint": row["hint"],
        "careers": row["careers"],
        "default_categories": list(row.get("default_categories") or []),
        "default_countries": list(row.get("default_countries") or []),
    }


def board_catalog() -> list[dict[str, Any]]:
    """Filter lists for the scan form. Saved lists, not a live call."""
    stored = _read_store()
    catalog = []
    for source in COMPANY_BOARDS:
        row = _meta(source)
        saved = stored.get(source) if isinstance(stored.get(source), dict) else {}
        countries = list(saved.get("countries") or [])
        pinned = [name for name in countries if name.casefold() in {"united states", "united states of america"}]
        row.update(
            {
                "countries": pinned + [name for name in countries if name not in pinned],
                "categories": list(saved.get("categories") or []),
                "types": list(saved.get("types") or []),
                "locations": list(saved.get("locations") or []),
            }
        )
        catalog.append(row)
    return catalog


def _save_board(source: str, payload: dict[str, Any]) -> None:
    data: dict[str, Any] = {}
    for path in (_SNAPSHOT, _CACHE):
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(loaded, dict):
            data.update(loaded)
    data[source] = payload
    try:
        _SNAPSHOT.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass
    _write_cache(data)


def _names_from_agg(aggs: Any, field: str) -> list[str]:
    if not isinstance(aggs, list):
        return []
    for row in aggs:
        if not isinstance(row, dict) or row.get("field") != field:
            continue
        value = row.get("value") or {}
        if not isinstance(value, dict):
            return []
        ranked = sorted(
            ((name, count) for name, count in value.items() if name and isinstance(count, int) and count > 0),
            key=lambda item: (-item[1], item[0].casefold()),
        )
        return [name for name, _count in ranked]
    return []


def phenom_to_record(job: dict[str, Any], cfg: dict[str, str], description: str) -> dict[str, Any] | None:
    title = _text(job.get("title"))
    seq = _text(job.get("jobSeqNo")) or _text(job.get("jobId"))
    if not title or not seq:
        return None
    city = _text(job.get("cityStateCountry")) or _text(job.get("location"))
    category = _text(job.get("category"))
    schedule = _text(job.get("type"))
    arrangement = _text(job.get("RemoteType"))
    body = description.strip() or html_to_text(_text(job.get("descriptionTeaser")))
    skills = job.get("ml_skills") or []
    if isinstance(skills, list) and skills and "Skills:" not in body:
        body = (body + "\n\nSkills: " + ", ".join(str(item) for item in skills)).strip()
    lines = [part for part in (f"Category: {category}" if category else "", f"Schedule: {schedule}" if schedule else "", f"Work arrangement: {arrangement}" if arrangement else "") if part]
    if lines:
        body = "\n".join(lines) + ("\n\n" + body if body else "")
    return {
        "id": _text(job.get("jobId")) or seq,
        "title": title,
        "company": cfg["company"],
        "location": city,
        "job_url": cfg["job_base"] + seq,
        "job_url_direct": _text(job.get("applyUrl")) or cfg["job_base"] + seq,
        "site": cfg["id"] if "id" in cfg else "",
        "description": body,
        "date_posted": _posted(_text(job.get("postedDate"))),
        "job_type": _job_type(schedule or title),
        "is_remote": _remote(arrangement, city, schedule, title),
        "company_industry": category or None,
        "company_url": cfg["careers"],
        "company_url_direct": cfg["careers"],
    }


def ce_to_record(job: dict[str, Any], cfg: dict[str, str], description: str) -> dict[str, Any] | None:
    title = _text(job.get("Title"))
    job_id = _text(job.get("Id"))
    if not title or not job_id:
        return None
    workplace = _text(job.get("WorkplaceType"))
    category = _text(job.get("Category"))
    location = _text(job.get("PrimaryLocation"))
    body = description.strip() or html_to_text(_text(job.get("ShortDescriptionStr")))
    lines = [part for part in (f"Category: {category}" if category else "", f"Work arrangement: {workplace}" if workplace else "") if part]
    if lines:
        body = "\n".join(lines) + ("\n\n" + body if body else "")
    return {
        "id": job_id,
        "title": title,
        "company": cfg["company"],
        "location": location,
        "job_url": cfg["job_base"] + urllib.parse.quote(job_id),
        "job_url_direct": cfg["job_base"] + urllib.parse.quote(job_id),
        "site": "",
        "description": body,
        "date_posted": _posted(_text(job.get("PostedDate"))),
        "job_type": _job_type(title),
        "is_remote": _remote(workplace, location, title),
        "company_industry": category or None,
        "company_url": cfg["careers"],
        "company_url_direct": cfg["careers"],
    }


def verizon_to_record(job: dict[str, Any], description: str) -> dict[str, Any] | None:
    title = _text(job.get("Title"))
    job_id = _text(job.get("Id"))
    if not title or not job_id:
        return None
    places = job.get("Locations") or []
    if not isinstance(places, list):
        places = []
    cities = []
    countries = []
    for place in places:
        if not isinstance(place, dict):
            continue
        label = _text(place.get("Identifier")) or ", ".join(
            part for part in (_text(place.get("City")), _text(place.get("Region")), _text(place.get("Country"))) if part
        )
        if label:
            cities.append(label)
        country = _text(place.get("Country"))
        if country and country not in countries:
            countries.append(country)
    teams = job.get("Teams") or []
    team = ", ".join(_text(item) for item in teams if _text(item)) if isinstance(teams, list) else ""
    path = ""
    for url in job.get("Urls") or []:
        if isinstance(url, dict) and _text(url.get("Url")):
            path = _text(url.get("Url"))
            break
    if path.startswith("http"):
        link = path
    else:
        link = VERIZON["home"] + (path if path.startswith("/") else f"/jobs/{job_id}/")
    body = description.strip()
    if team:
        body = f"Team: {team}\n\n{body}".strip()
    location = "; ".join(cities)
    if countries and countries[0].lower() not in location.lower():
        location = ", ".join(part for part in (location, countries[0]) if part)
    return {
        "id": job_id,
        "title": title,
        "company": VERIZON["company"],
        "location": location,
        "job_url": link,
        "job_url_direct": link,
        "site": "verizon",
        "description": body,
        "date_posted": None,
        "job_type": _job_type(title),
        "is_remote": _remote(location, title, body[:400]),
        "company_industry": team or None,
        "company_url": VERIZON["careers"],
        "company_url_direct": VERIZON["careers"],
        "_country": countries[0] if countries else "",
        "_team": team,
    }


def tesla_to_record(listing: dict[str, Any], lookup: dict[str, Any], description: str) -> dict[str, Any] | None:
    title = _text(listing.get("t") or listing.get("title"))
    job_id = _text(listing.get("id"))
    if not title or not job_id:
        return None

    def look(table: str, key: Any) -> str:
        raw = (lookup.get(table) or {}).get(str(key)) if key is not None else None
        if isinstance(raw, dict):
            return _text(raw.get("name") or raw.get("city") or raw.get("label"))
        return _text(raw)

    location = look("locations", listing.get("l"))
    department = look("departments", listing.get("d"))
    region = look("regions", listing.get("r"))
    if region and region.lower() not in location.lower():
        location = ", ".join(part for part in (location, region) if part)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    link = f"https://www.tesla.com/careers/search/job/{slug}-{job_id}"
    body = description.strip()
    if department:
        body = f"Department: {department}\n\n{body}".strip()
    return {
        "id": job_id,
        "title": title,
        "company": TESLA["company"],
        "location": location,
        "job_url": link,
        "job_url_direct": link,
        "site": "tesla",
        "description": body,
        "date_posted": None,
        "job_type": _job_type(_text(listing.get("timeType")) or title),
        "is_remote": _remote(location, title, department),
        "company_industry": department or None,
        "company_url": TESLA["careers"],
        "company_url_direct": TESLA["careers"],
        "_region": region,
        "_department": department,
    }


def _stamp_site(records: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    for rec in records:
        rec["site"] = source
        rec.pop("_country", None)
        rec.pop("_team", None)
        rec.pop("_region", None)
        rec.pop("_department", None)
    return records


def _phenom_detail(cfg: dict[str, str], seq: str) -> str:
    data = _post_json(
        cfg["widgets"],
        {
            "lang": cfg["locale"],
            "deviceType": "desktop",
            "country": cfg["country"],
            "pageName": "job",
            "ddoKey": "jobDetail",
            "jobSeqNo": seq,
            "refNum": cfg["ref"],
            "siteType": "external",
        },
        cfg["referer"],
    )
    job = ((data.get("jobDetail") or {}).get("data") or {}).get("job") or {}
    return html_to_text(_text(job.get("description")))


def _fetch_phenom(
    source: str,
    countries: list[str],
    categories: list[str],
    locations: list[str],
    modes: list[str],
    log: ProgressFn,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cfg = dict(PHENOM[source])
    cfg["id"] = source
    selected: dict[str, list[str]] = {}
    if countries:
        selected["country"] = countries
    if categories:
        selected["category"] = categories
    if locations:
        selected["city"] = locations
    if modes:
        selected["type"] = modes
    log(f"Loading {cfg['name']} careers search")
    errors: list[str] = []
    rows: list[dict[str, Any]] = []
    offset = 0
    size = 100
    aggs: Any = None
    while offset < 20000:
        payload = {
            "lang": cfg["locale"],
            "deviceType": "desktop",
            "country": cfg["country"],
            "pageName": "search-results",
            "ddoKey": "refineSearch",
            "sortBy": "Most recent",
            "from": offset,
            "jobs": True,
            "counts": True,
            "all_fields": ["category", "country", "state", "city", "type"],
            "size": size,
            "siteType": "external",
            "keywords": "",
            "global": True,
            "selected_fields": selected,
            "locationData": {},
            "refNum": cfg["ref"],
        }
        try:
            data = _post_json(cfg["widgets"], payload, cfg["referer"])
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            if offset == 0:
                raise RuntimeError(f"{cfg['name']} careers search failed: {exc}") from exc
            errors.append(f"{cfg['name']} page at {offset} failed: {exc}")
            log(errors[-1])
            break
        inner = (data.get("refineSearch") or {}).get("data") or {}
        jobs = inner.get("jobs") or []
        if aggs is None:
            aggs = inner.get("aggregations")
        if not jobs:
            break
        rows.extend(job for job in jobs if isinstance(job, dict))
        log(f"{cfg['name']}: {len(rows)} openings listed")
        offset += len(jobs)
        if len(jobs) < size:
            break
        time.sleep(0.1)
    if aggs is not None and not selected:
        _save_board(
            source,
            {
                "countries": _names_from_agg(aggs, "country"),
                "categories": _names_from_agg(aggs, "category"),
                "types": _names_from_agg(aggs, "type"),
                "locations": _names_from_agg(aggs, "city"),
            },
        )
    log(f"{cfg['name']}: reading the full posting for {len(rows)} openings")
    descriptions: dict[str, str] = {}

    def one(job: dict[str, Any]) -> tuple[str, str]:
        seq = _text(job.get("jobSeqNo"))
        if not seq:
            return "", ""
        try:
            return seq, _phenom_detail(cfg, seq)
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
            return seq, ""

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futures = [pool.submit(one, job) for job in rows]
        done = 0
        for future in as_completed(futures):
            seq, text = future.result()
            if seq:
                descriptions[seq] = text
            done += 1
            if done % 100 == 0:
                log(f"{cfg['name']}: {done} of {len(rows)} postings read")
    records = []
    seen: set[str] = set()
    for job in rows:
        seq = _text(job.get("jobSeqNo"))
        rec = phenom_to_record(job, cfg, descriptions.get(seq, ""))
        if rec is None or rec["id"] in seen:
            continue
        seen.add(rec["id"])
        rec["site"] = source
        records.append(rec)
    return records, {"raw_fetched": len(records), "errors": errors}


def _place_matches(place: str, names: set[str]) -> bool:
    folded = place.casefold()
    for name in names:
        if folded == name or folded.endswith(", " + name):
            return True
    return False


def _ce_list(cfg: dict[str, str], offset: int, limit: int, extra: str) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    finder = (
        f"findReqs;siteNumber={cfg['site']},facetsList=LOCATIONS;CATEGORIES;WORKPLACE_TYPES;WORK_LOCATIONS,"
        f"limit={limit},offset={offset}{extra},sortBy=POSTING_DATES_DESC"
    )
    expand = "requisitionList.secondaryLocations,categoriesFacet,locationsFacet,workplaceTypesFacet"
    url = (
        f"{cfg['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        f"?onlyData=true&expand={expand}&finder={urllib.parse.quote(finder, safe=';=,')}"
    )
    data = json.loads(_get(url).decode("utf-8"))
    item = (data.get("items") or [{}])[0]
    reqs = item.get("requisitionList") or []
    total = int(item.get("TotalJobsCount") or 0)
    return reqs, total, item


def _ce_detail(cfg: dict[str, str], job_id: str) -> str:
    finder = f'ById;Id="{job_id}",siteNumber={cfg["site"]}'
    url = (
        f"{cfg['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
        f"?onlyData=true&expand=all&finder={urllib.parse.quote(finder, safe=';=,')}"
    )
    data = json.loads(_get(url).decode("utf-8"))
    items = data.get("items") or []
    if not items:
        return ""
    job = items[0]
    parts = [
        html_to_text(_text(job.get("ExternalDescriptionStr"))),
        html_to_text(_text(job.get("ExternalResponsibilitiesStr"))),
        html_to_text(_text(job.get("ExternalQualificationsStr"))),
    ]
    return "\n\n".join(part for part in parts if part)


def _facet_names(item: dict[str, Any], key: str, countries_only: bool = False) -> list[tuple[str, str]]:
    rows = item.get(key) or []
    if not isinstance(rows, list):
        return []
    named = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = _text(row.get("Name"))
        ident = _text(row.get("Id"))
        if not name or not ident:
            continue
        if countries_only and "," in name:
            continue
        if not countries_only and key == "locationsFacet" and "," not in name:
            continue
        named.append((name, ident))
    return named


def _ce_facet_ids(item: dict[str, Any], key: str) -> list[tuple[str, str]]:
    rows = item.get(key) or []
    if not isinstance(rows, list):
        return []
    named = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = _text(row.get("Name"))
        ident = _text(row.get("Id"))
        if name and ident:
            named.append((name, ident))
    return named


def _places_from_rows(rows: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    countries: set[str] = set()
    cities: set[str] = set()
    for job in rows:
        place = _text(job.get("PrimaryLocation"))
        if not place:
            continue
        cities.add(place)
        country = place.split(",")[-1].strip()
        if country:
            countries.add(country)
    country_list = sorted(countries, key=lambda name: (name.casefold() != "united states", name.casefold()))
    return country_list, sorted(cities, key=str.casefold)


def _ce_payload(rows: list[dict[str, Any]], facets: dict[str, Any]) -> dict[str, Any]:
    countries, cities = _places_from_rows(rows)
    country_ids = _facet_names(facets, "locationsFacet", countries_only=True)
    city_ids = _facet_names(facets, "locationsFacet", countries_only=False)
    category_ids = _ce_facet_ids(facets, "categoriesFacet")
    type_ids = _ce_facet_ids(facets, "workplaceTypesFacet")
    facet_countries = [name for name, _ident in country_ids]
    for name in facet_countries:
        if name not in countries:
            countries.append(name)
    countries.sort(key=lambda name: (name.casefold() != "united states", name.casefold()))
    return {
        "countries": countries,
        "categories": [name for name, _ident in category_ids],
        "types": [name for name, _ident in type_ids],
        "locations": cities,
        "location_ids": country_ids + city_ids,
        "category_ids": category_ids,
        "type_ids": type_ids,
    }


def _ce_pages(cfg: dict[str, str], extra: str, log: ProgressFn) -> tuple[list[dict[str, Any]], int, dict[str, Any] | None, list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    offset = 0
    limit = 25
    total = 0
    facets: dict[str, Any] | None = None
    while offset < 20000:
        try:
            reqs, reported, item = _ce_list(cfg, offset, limit, extra)
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            if offset == 0:
                raise RuntimeError(f"{cfg['name']} careers search failed: {exc}") from exc
            errors.append(f"{cfg['name']} page at {offset} failed: {exc}")
            log(errors[-1])
            break
        if facets is None:
            facets = item
            total = reported
        if not reqs:
            break
        rows.extend(job for job in reqs if isinstance(job, dict))
        offset += len(reqs)
        log(f"{cfg['name']}: {len(rows)} of {total or len(rows)} openings listed")
        if total and offset >= total:
            break
        if len(reqs) < limit:
            break
        time.sleep(0.05)
    return rows, total, facets, errors


def _fetch_ce(
    source: str,
    countries: list[str],
    categories: list[str],
    locations: list[str],
    modes: list[str],
    log: ProgressFn,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cfg = dict(ORACLE_CE[source])
    cfg["id"] = source
    log(f"Loading {cfg['name']} careers search")
    try:
        _seed, _total, seed = _ce_list(cfg, 0, 1, "")
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{cfg['name']} careers search failed: {exc}") from exc
    category_ids = dict(_ce_facet_ids(seed, "categoriesFacet"))
    queries: list[tuple[str, str]] = []
    errors: list[str] = []
    if categories:
        for name in categories:
            ident = category_ids.get(name)
            if not ident:
                errors.append(f"{cfg['name']} has no category named {name}")
                log(errors[-1])
                continue
            queries.append((f",selectedCategoriesFacet={ident}", name))
        if not queries:
            raise RuntimeError(errors[-1] if errors else f"{cfg['name']} category filter matched nothing")
    else:
        queries.append(("", ""))
    rows: list[dict[str, Any]] = []
    facets: dict[str, Any] | None = None
    seen_ids: set[str] = set()
    for extra, category_name in queries:
        batch, _reported, batch_facets, batch_errors = _ce_pages(cfg, extra, log)
        errors.extend(batch_errors)
        if facets is None and not extra:
            facets = batch_facets
        for job in batch:
            job_id = _text(job.get("Id"))
            if job_id and job_id in seen_ids:
                continue
            if job_id:
                seen_ids.add(job_id)
            if category_name:
                job = dict(job)
                job["Category"] = category_name
            rows.append(job)
    if facets is not None and not categories and not countries and not locations and not modes:
        _save_board(source, _ce_payload(rows, facets))
    wanted_locations = {name.casefold() for name in countries + locations}
    wanted_types = {name.casefold() for name in modes}
    kept_rows = []
    for job in rows:
        place = _text(job.get("PrimaryLocation"))
        workplace = _text(job.get("WorkplaceType"))
        if wanted_locations and not _place_matches(place, wanted_locations):
            continue
        if wanted_types and workplace and workplace.casefold() not in wanted_types:
            continue
        kept_rows.append(job)
    log(f"{cfg['name']}: reading the full posting for {len(kept_rows)} openings")
    descriptions: dict[str, str] = {}

    def one(job: dict[str, Any]) -> tuple[str, str]:
        job_id = _text(job.get("Id"))
        try:
            return job_id, _ce_detail(cfg, job_id)
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
            return job_id, ""

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futures = [pool.submit(one, job) for job in kept_rows]
        done = 0
        for future in as_completed(futures):
            job_id, text = future.result()
            descriptions[job_id] = text
            done += 1
            if done % 100 == 0:
                log(f"{cfg['name']}: {done} of {len(kept_rows)} postings read")
    records = []
    seen: set[str] = set()
    for job in kept_rows:
        rec = ce_to_record(job, cfg, descriptions.get(_text(job.get("Id")), ""))
        if rec is None or rec["id"] in seen:
            continue
        seen.add(rec["id"])
        rec["site"] = source
        records.append(rec)
    return records, {"raw_fetched": len(records), "errors": errors}


def _verizon_page(page: int, search: str, team: str, country: str) -> dict[str, Any]:
    params: dict[str, str] = {"page": str(page)}
    if search:
        params["search"] = search
    if team:
        params["team"] = team
    if country:
        params["country"] = country
    url = VERIZON["search"] + "?" + urllib.parse.urlencode(params)
    data = json.loads(_get(url).decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Verizon careers did not return a job list")
    return data


def _verizon_description(url: str) -> str:
    page = _get(url, headers={"Accept": "text/html"}).decode("utf-8", "replace")
    text = html_to_text(page)
    lowered = text.lower()
    start = lowered.find("what you'll be doing")
    if start < 0:
        start = lowered.find("what you’ll be doing")
    if start < 0:
        start = 0
    ends = []
    for marker in ("apply now", "similar jobs", "share this", "footer"):
        idx = lowered.find(marker, start + 40)
        if idx > start:
            ends.append(idx)
    end = min(ends) if ends else min(len(text), start + 12000)
    return text[start:end].strip()


def _fetch_verizon(
    countries: list[str],
    categories: list[str],
    locations: list[str],
    log: ProgressFn,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    log("Loading Verizon careers search")
    errors: list[str] = []
    country = countries[0] if len(countries) == 1 else ""
    team = categories[0] if len(categories) == 1 else ""
    rows: list[dict[str, Any]] = []
    page = 1
    total_pages = 1
    while page <= total_pages and page < 400:
        try:
            data = _verizon_page(page, "", team, country)
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            if page == 1:
                raise RuntimeError(f"Verizon careers search failed: {exc}") from exc
            errors.append(f"Verizon page {page} failed: {exc}")
            log(errors[-1])
            break
        total_pages = int(data.get("totalPages") or 1)
        jobs = data.get("jobs") or []
        if not jobs:
            break
        rows.extend(job for job in jobs if isinstance(job, dict))
        log(f"Verizon: {len(rows)} of {data.get('totalJobs') or len(rows)} openings listed")
        page += 1
        time.sleep(0.05)
    teams: set[str] = set()
    country_names: set[str] = set()
    cities: set[str] = set()
    for job in rows:
        for team_name in job.get("Teams") or []:
            if _text(team_name):
                teams.add(_text(team_name))
        for place in job.get("Locations") or []:
            if not isinstance(place, dict):
                continue
            if _text(place.get("Country")):
                country_names.add(_text(place.get("Country")))
            if _text(place.get("Identifier")):
                cities.add(_text(place.get("Identifier")))
    if not team and not country:
        _save_board(
            "verizon",
            {
                "countries": sorted(country_names, key=str.casefold),
                "categories": sorted(teams, key=str.casefold),
                "types": [],
                "locations": sorted(cities, key=str.casefold),
            },
        )
    wanted_countries = {name.casefold() for name in countries}
    wanted_teams = {name.casefold() for name in categories}
    wanted_cities = {name.casefold() for name in locations}
    kept = []
    for job in rows:
        rec = verizon_to_record(job, "")
        if rec is None:
            continue
        if wanted_countries and rec.get("_country", "").casefold() not in wanted_countries:
            continue
        if wanted_teams and not any(name in rec.get("_team", "").casefold() for name in wanted_teams):
            continue
        if wanted_cities and not any(name in rec.get("location", "").casefold() for name in wanted_cities):
            continue
        kept.append((job, rec))
    log(f"Verizon: reading the full posting for {len(kept)} openings")

    def one(pair: tuple[dict[str, Any], dict[str, Any]]) -> dict[str, Any]:
        _job, rec = pair
        try:
            rec["description"] = _verizon_description(rec["job_url"])
            if rec.get("company_industry"):
                rec["description"] = f"Team: {rec['company_industry']}\n\n{rec['description']}".strip()
        except (urllib.error.URLError, TimeoutError, ValueError):
            pass
        return rec

    records = []
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futures = [pool.submit(one, pair) for pair in kept]
        done = 0
        for future in as_completed(futures):
            rec = future.result()
            rec.pop("_country", None)
            rec.pop("_team", None)
            records.append(rec)
            done += 1
            if done % 100 == 0:
                log(f"Verizon: {done} of {len(kept)} postings read")
    return records, {"raw_fetched": len(records), "errors": errors}


def _fetch_tesla(
    countries: list[str],
    categories: list[str],
    locations: list[str],
    log: ProgressFn,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    log("Loading Tesla careers")
    try:
        raw = _get(
            TESLA["state"],
            headers={
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.tesla.com/careers/search/",
                "Origin": "https://www.tesla.com",
            },
        )
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            "Tesla careers refused the request. The public jobs feed is up, but Tesla's "
            f"site returned {exc.code} to this computer."
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Tesla careers search failed: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise RuntimeError("Tesla careers did not return the job list. The site sent a block page instead.") from exc
    listings = data.get("listings") or []
    lookup = data.get("lookup") or {}
    if not isinstance(listings, list):
        raise RuntimeError("Tesla careers did not return a job list")
    records = [tesla_to_record(row, lookup, "") for row in listings if isinstance(row, dict)]
    records = [rec for rec in records if rec]
    regions = sorted({rec.get("_region") or "" for rec in records if rec.get("_region")}, key=str.casefold)
    departments = sorted({rec.get("_department") or "" for rec in records if rec.get("_department")}, key=str.casefold)
    places = sorted({rec["location"] for rec in records if rec.get("location")}, key=str.casefold)
    _save_board(
        "tesla",
        {"countries": regions, "categories": departments, "types": [], "locations": places[:400]},
    )
    wanted_regions = {name.casefold() for name in countries}
    wanted_departments = {name.casefold() for name in categories}
    wanted_places = {name.casefold() for name in locations}
    kept = []
    for rec in records:
        if wanted_regions and rec.get("_region", "").casefold() not in wanted_regions:
            continue
        if wanted_departments and rec.get("_department", "").casefold() not in wanted_departments:
            continue
        if wanted_places and not any(name in rec.get("location", "").casefold() for name in wanted_places):
            continue
        kept.append(rec)
    log(f"Tesla: reading the full posting for {len(kept)} openings")
    errors: list[str] = []

    def one(rec: dict[str, Any]) -> dict[str, Any]:
        try:
            detail = json.loads(
                _get(
                    TESLA["detail"] + urllib.parse.quote(rec["id"]),
                    headers={"Accept": "application/json", "Referer": "https://www.tesla.com/careers/search/"},
                ).decode("utf-8")
            )
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
            detail = {}
        parts = [
            html_to_text(_text(detail.get("jobDescription"))),
            html_to_text(_text(detail.get("jobResponsibilities"))),
            html_to_text(_text(detail.get("jobRequirements"))),
            html_to_text(_text(detail.get("jobCompensationAndBenefits"))),
        ]
        body = "\n\n".join(part for part in parts if part)
        if rec.get("_department"):
            body = f"Department: {rec['_department']}\n\n{body}".strip()
        rec["description"] = body
        rec["is_remote"] = _remote(rec.get("location") or "", rec.get("title") or "", body[:400])
        if _text(detail.get("timeType")):
            rec["job_type"] = _job_type(_text(detail.get("timeType")))
        return rec

    done_records = []
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futures = [pool.submit(one, rec) for rec in kept]
        done = 0
        for future in as_completed(futures):
            done_records.append(future.result())
            done += 1
            if done % 100 == 0:
                log(f"Tesla: {done} of {len(kept)} postings read")
    for rec in done_records:
        rec.pop("_region", None)
        rec.pop("_department", None)
    return done_records, {"raw_fetched": len(done_records), "errors": errors}


def _with_defaults(source: str, categories: list[str], countries: list[str]) -> tuple[list[str], list[str]]:
    row = _meta(source)
    if not categories:
        categories = list(row.get("default_categories") or [])
    if not countries:
        countries = list(row.get("default_countries") or [])
    return categories, countries


def _workday_facets(cfg: dict[str, Any]) -> dict[str, list[tuple[str, str]]]:
    url = f"{cfg['host']}/wday/cxs/{cfg['tenant']}/{cfg['site']}/jobs"
    data = _post_json(url, {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}, cfg["host"] + "/")
    found: dict[str, list[tuple[str, str]]] = {}

    def take(param: str, values: list[Any]) -> None:
        pairs = []
        for value in values:
            if not isinstance(value, dict):
                continue
            name = _text(value.get("descriptor"))
            ident = _text(value.get("id"))
            if name and ident:
                pairs.append((name, ident))
        if param and pairs:
            found[param] = pairs

    for facet in data.get("facets") or []:
        if not isinstance(facet, dict):
            continue
        if facet.get("facetParameter") == "locationMainGroup":
            for group in facet.get("values") or []:
                if isinstance(group, dict):
                    take(_text(group.get("facetParameter")), group.get("values") or [])
        else:
            take(_text(facet.get("facetParameter")), facet.get("values") or [])
    return found


def _workday_ids(source: str, cfg: dict[str, Any]) -> dict[str, dict[str, str]]:
    saved = _read_store().get(source) if isinstance(_read_store().get(source), dict) else {}
    groups = {
        "jobFamilyGroup": {name: ident for name, ident in saved.get("category_ids") or []},
        "locationCountry": {name: ident for name, ident in saved.get("country_ids") or []},
    }
    if groups["jobFamilyGroup"] and (groups["locationCountry"] or source == "walmart"):
        return groups
    live = _workday_facets(cfg)
    payload = {
        "countries": [name for name, _ident in live.get("locationCountry") or []],
        "categories": [name for name, _ident in live.get("jobFamilyGroup") or []],
        "types": [name for name, _ident in live.get("timeType") or []],
        "locations": [name for name, _ident in (live.get("locationRegionStateProvince") or [])[:80]],
        "category_ids": live.get("jobFamilyGroup") or [],
        "country_ids": live.get("locationCountry") or [],
    }
    _save_board(source, payload)
    return {
        "jobFamilyGroup": {name: ident for name, ident in payload["category_ids"]},
        "locationCountry": {name: ident for name, ident in payload["country_ids"]},
    }


def _country_name(value: Any) -> str:
    if isinstance(value, dict):
        return _text(value.get("descriptor") or value.get("name"))
    return _text(value)


def workday_to_record(info: dict[str, Any], cfg: dict[str, str], category: str) -> dict[str, Any] | None:
    title = _text(info.get("title"))
    job_id = _text(info.get("jobReqId") or info.get("id"))
    if not title or not job_id:
        return None
    location = _text(info.get("location"))
    country = _country_name(info.get("country"))
    if country and country.casefold() not in location.casefold():
        location = ", ".join(part for part in (location, country) if part)
    extras = info.get("additionalLocations") or []
    if isinstance(extras, list):
        more = [_text(item.get("descriptor") if isinstance(item, dict) else item) for item in extras]
        more = [item for item in more if item and item.casefold() not in location.casefold()]
        if more:
            location = "; ".join([location, *more]) if location else "; ".join(more)
    body = html_to_text(_text(info.get("jobDescription")))
    schedule = _text(info.get("timeType"))
    lines = [part for part in (f"Category: {category}" if category else "", f"Schedule: {schedule}" if schedule else "") if part]
    if lines:
        body = "\n".join(lines) + ("\n\n" + body if body else "")
    link = _text(info.get("externalUrl"))
    return {
        "id": job_id,
        "title": title,
        "company": cfg["company"],
        "location": location,
        "job_url": link,
        "job_url_direct": link,
        "site": "",
        "description": body,
        "date_posted": _posted(_text(info.get("startDate"))),
        "job_type": _job_type(schedule or title),
        "is_remote": _remote(location, title, schedule),
        "company_industry": category or None,
        "company_url": cfg["careers"],
        "company_url_direct": cfg["careers"],
    }


def _fetch_workday(
    source: str,
    countries: list[str],
    categories: list[str],
    locations: list[str],
    modes: list[str],
    log: ProgressFn,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cfg = dict(WORKDAY[source])
    categories, countries = _with_defaults(source, categories, countries)
    log(f"Loading {cfg['name']} careers")
    ids = _workday_ids(source, cfg)
    missing = [name for name in categories if name not in ids["jobFamilyGroup"]]
    if missing:
        raise RuntimeError(f"{cfg['name']} has no career area named {missing[0]}")
    missing_countries = [name for name in countries if name not in ids["locationCountry"]]
    if missing_countries:
        raise RuntimeError(f"{cfg['name']} has no country named {missing_countries[0]}")
    applied: dict[str, list[str]] = {}
    if categories:
        applied["jobFamilyGroup"] = [ids["jobFamilyGroup"][name] for name in categories]
    if countries:
        applied["locationCountry"] = [ids["locationCountry"][name] for name in countries]
    url = f"{cfg['host']}/wday/cxs/{cfg['tenant']}/{cfg['site']}/jobs"
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    offset = 0
    total = None
    seen: set[str] = set()
    while offset < 2000:
        try:
            data = _post_json(
                url,
                {"appliedFacets": applied, "limit": 20, "offset": offset, "searchText": ""},
                cfg["host"] + "/",
            )
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            if offset == 0:
                raise RuntimeError(f"{cfg['name']} careers search failed: {exc}") from exc
            errors.append(f"{cfg['name']} page at {offset} failed: {exc}")
            log(errors[-1])
            break
        if total is None:
            total = int(data.get("total") or 0)
        postings = [row for row in (data.get("jobPostings") or []) if isinstance(row, dict)]
        fresh = []
        for row in postings:
            path = _text(row.get("externalPath"))
            if path and path not in seen:
                seen.add(path)
                fresh.append(row)
        if not fresh:
            break
        rows.extend(fresh)
        offset += 20
        log(f"{cfg['name']}: {len(rows)} of {total or len(rows)} openings listed")
        if total is not None and len(rows) >= total:
            break
        if len(postings) < 20:
            break
        time.sleep(0.05)
    if total and total >= 2000 and len(rows) >= 2000:
        log(f"{cfg['name']}: stopped at 2,000 openings, which is as far as this search will page")
    wanted_places = {name.casefold() for name in locations}
    wanted_modes = {name.casefold() for name in modes}
    log(f"{cfg['name']}: reading the full posting for {len(rows)} openings")
    category_label = ", ".join(categories)

    def one(row: dict[str, Any]) -> dict[str, Any] | None:
        path = _text(row.get("externalPath"))
        info: dict[str, Any] = {}
        try:
            detail = json.loads(_get(url[: url.rfind("/jobs")] + path).decode("utf-8"))
            info = detail.get("jobPostingInfo") or {}
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
            info = {"title": row.get("title"), "location": row.get("locationsText"), "externalUrl": cfg["host"] + path}
        rec = workday_to_record(info, cfg, category_label)
        if rec is None:
            return None
        if wanted_places and not any(name in rec["location"].casefold() for name in wanted_places):
            return None
        if wanted_modes and not any(name in rec["description"].casefold() or name in rec["title"].casefold() for name in wanted_modes):
            return None
        rec["site"] = source
        return rec

    records = []
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futures = [pool.submit(one, row) for row in rows]
        done = 0
        for future in as_completed(futures):
            rec = future.result()
            if rec:
                records.append(rec)
            done += 1
            if done % 100 == 0:
                log(f"{cfg['name']}: {done} of {len(rows)} postings read")
    return records, {"raw_fetched": len(records), "errors": errors}


_CARD = re.compile(
    r'<a href="(?P<href>/job/[^"]+)" data-job-id="(?P<id>[^"]+)"[^>]*>(?P<body>.*?)</a>',
    re.S,
)


def _radancy_cards(html: str) -> list[dict[str, str]]:
    cards = []
    for match in _CARD.finditer(html):
        body = match.group("body")
        title = re.search(r"<h2>(.*?)</h2>", body, flags=re.S)
        location = re.search(r'<span class="job-location[^"]*">(.*?)</span>', body, flags=re.S)
        posted = re.search(r'<span class="job-date-posted[^"]*">(.*?)</span>', body, flags=re.S)
        cards.append(
            {
                "href": match.group("href"),
                "id": match.group("id"),
                "title": title.group(1) if title else "",
                "location": location.group(1) if location else "",
                "date": posted.group(1) if posted else "",
            }
        )
    return cards


def _facet_inputs(html: str) -> list[tuple[str, str, str]]:
    found = []
    for tag in re.findall(r"<input\b[^>]*>", html, flags=re.I):
        def attr(name: str, tag: str = tag) -> str:
            match = re.search(rf'{name}="([^"]*)"', tag)
            return html_unescape(match.group(1)) if match else ""

        display = attr("data-display")
        ident = attr("data-id")
        kind = attr("data-facet-type")
        if display and ident and kind:
            found.append((kind, display, ident))
    return found


def html_unescape(value: str) -> str:
    import html as html_lib

    return html_lib.unescape(value)


def _jsonld_job(page: str) -> dict[str, Any]:
    for match in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', page, flags=re.S | re.I):
        try:
            data = json.loads(match.group(1))
        except ValueError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return {}


def _posting_text(page: str) -> tuple[str, str]:
    info = _jsonld_job(page)
    body = html_to_text(_text(info.get("description")))
    if not body:
        body = html_to_text(_div_inner(page, "ats-description"))
    return body, _text(info.get("datePosted"))


def _div_inner(page: str, marker: str) -> str:
    start_at = page.find(marker)
    if start_at < 0:
        return ""
    start = page.find(">", start_at)
    if start < 0:
        return ""
    start += 1
    depth = 1
    pos = start
    lower = page.lower()
    while depth and pos < len(page):
        next_open = lower.find("<div", pos)
        next_close = lower.find("</div>", pos)
        if next_close < 0:
            break
        if 0 <= next_open < next_close:
            depth += 1
            pos = next_open + 4
        else:
            depth -= 1
            if depth == 0:
                return page[start:next_close]
            pos = next_close + 6
    return page[start:start + 40000]


def _mdy(value: str) -> str | None:
    match = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", value.strip())
    if not match:
        return _posted(value)
    month, day, year = match.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def radancy_to_record(card: dict[str, str], cfg: dict[str, str], description: str, category: str) -> dict[str, Any] | None:
    title = html_unescape(re.sub(r"<[^>]+>", "", card.get("title") or "")).strip()
    job_id = _text(card.get("id"))
    if not title or not job_id:
        return None
    href = _text(card.get("href"))
    link = cfg["home"] + href if href.startswith("/") else href
    location = html_unescape(re.sub(r"<[^>]+>", "", card.get("location") or "")).strip()
    body = description.strip()
    if category:
        body = f"Category: {category}\n\n{body}".strip()
    return {
        "id": job_id,
        "title": title,
        "company": cfg["company"],
        "location": location,
        "job_url": link,
        "job_url_direct": link,
        "site": "",
        "description": body,
        "date_posted": _mdy(card.get("date") or ""),
        "job_type": _job_type(title),
        "is_remote": _remote(location, title),
        "company_industry": category or None,
        "company_url": cfg["careers"],
        "company_url_direct": cfg["careers"],
    }


def _radancy_page(cfg: dict[str, str], page: int, location: str, category: tuple[str, str, str] | None) -> str:
    params = {
        "CurrentPage": str(page),
        "RecordsPerPage": "100",
        "Keywords": "",
        "Location": location,
        "SearchType": "1" if location else "5",
        "OrganizationIds": cfg["org"],
        "ResultsType": "0",
        "SearchResultsModuleName": "Search Results",
        "SearchFiltersModuleName": "Search Filters",
    }
    if category:
        _name, ident, kind = category
        params.update(
            {
                "FacetFilters[0].ID": ident,
                "FacetFilters[0].FacetType": kind,
                "FacetFilters[0].Display": _name,
                "FacetFilters[0].IsApplied": "true",
                "SearchType": "5",
            }
        )
    url = cfg["results"] + "?" + urllib.parse.urlencode(params)
    raw = _get(url, headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"})
    data = json.loads(raw.decode("utf-8"))
    results = data.get("results")
    if not isinstance(results, str):
        raise ValueError(f"{cfg['name']} careers did not return a job list")
    return results


def _radancy_catalog(cfg: dict[str, str]) -> dict[str, Any]:
    raw = _get(
        cfg["results"] + "?" + urllib.parse.urlencode({
            "CurrentPage": "1",
            "RecordsPerPage": "1",
            "Keywords": "",
            "Location": "",
            "SearchType": "5",
            "OrganizationIds": cfg["org"],
            "ResultsType": "0",
            "SearchResultsModuleName": "Search Results",
            "SearchFiltersModuleName": "Search Filters",
        }),
        headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
    )
    payload = json.loads(raw.decode("utf-8"))
    inputs = _facet_inputs(payload.get("filters") or "")
    by_type: dict[str, list[tuple[str, str]]] = {}
    for kind, display, ident in inputs:
        by_type.setdefault(kind, [])
        if (display, ident) not in by_type[kind]:
            by_type[kind].append((display, ident))
    categories = by_type.get("1") or []
    countries = []
    for kind, pairs in by_type.items():
        names = {name.casefold() for name, _ident in pairs}
        if "united states" in names and len(pairs) <= 40:
            countries = pairs
            break
    return {
        "countries": [name for name, _ident in countries],
        "categories": [name for name, _ident in categories],
        "types": [],
        "locations": [],
        "category_ids": [(name, ident, "1") for name, ident in categories],
        "country_ids": [(name, ident) for name, ident in countries],
    }


def _fetch_radancy(
    source: str,
    countries: list[str],
    categories: list[str],
    locations: list[str],
    log: ProgressFn,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cfg = dict(RADANCY[source])
    categories, countries = _with_defaults(source, categories, countries)
    log(f"Loading {cfg['name']} careers")
    saved = _read_store().get(source) if isinstance(_read_store().get(source), dict) else {}
    category_ids = {}
    for row in saved.get("category_ids") or []:
        if len(row) >= 2:
            category_ids[row[0]] = (row[1], row[2] if len(row) > 2 and row[2] else "1")
    if categories and any(name not in category_ids for name in categories):
        catalog = _radancy_catalog(cfg)
        _save_board(source, catalog)
        category_ids = {name: (ident, kind or "1") for name, ident, kind in catalog.get("category_ids") or []}
    missing = [name for name in categories if name not in category_ids]
    if missing:
        raise RuntimeError(f"{cfg['name']} has no category named {missing[0]}")
    place = locations[0] if len(locations) == 1 else (countries[0] if len(countries) == 1 else "")
    queries: list[tuple[str, tuple[str, str, str] | None]] = []
    if categories:
        for name in categories:
            ident, kind = category_ids[name]
            queries.append((place, (name, ident, kind)))
    else:
        queries.append((place, None))
    cards: list[dict[str, str]] = []
    seen: set[str] = set()
    errors: list[str] = []
    for location, category in queries:
        page = 1
        total_pages = 1
        while page <= total_pages and page < 80:
            try:
                html = _radancy_page(cfg, page, location, category)
            except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                if page == 1 and not cards:
                    raise RuntimeError(f"{cfg['name']} careers search failed: {exc}") from exc
                errors.append(f"{cfg['name']} page {page} failed: {exc}")
                log(errors[-1])
                break
            pages = re.search(r'data-total-pages="(\d+)"', html)
            total = re.search(r'data-total-results="(\d+)"', html)
            total_pages = int(pages.group(1)) if pages else 1
            for card in _radancy_cards(html):
                if card["id"] in seen:
                    continue
                seen.add(card["id"])
                card["category"] = category[0] if category else ""
                cards.append(card)
            log(f"{cfg['name']}: {len(cards)} of {total.group(1) if total else len(cards)} openings listed")
            page += 1
            time.sleep(0.05)
    wanted_places = {name.casefold() for name in locations}
    wanted_countries = {name.casefold() for name in countries}
    kept = []
    for card in cards:
        location_text = html_unescape(card.get("location") or "")
        folded = location_text.casefold()
        if wanted_places and not any(name in folded for name in wanted_places):
            continue
        if len(countries) > 1 and wanted_countries and not any(name in folded for name in wanted_countries):
            continue
        kept.append(card)
    log(f"{cfg['name']}: reading the full posting for {len(kept)} openings")

    def one(card: dict[str, str]) -> dict[str, Any] | None:
        href = card["href"] if card["href"].startswith("/") else "/" + card["href"]
        description = ""
        try:
            page = _get(cfg["home"] + href, headers={"Accept": "text/html"}).decode("utf-8", "replace")
            description, posted = _posting_text(page)
            if not (card.get("date") or "").strip() and posted:
                card["date"] = posted
        except (urllib.error.URLError, TimeoutError, ValueError):
            description = ""
        rec = radancy_to_record(card, cfg, description, card.get("category") or "")
        if rec:
            rec["site"] = source
        return rec

    records = []
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futures = [pool.submit(one, card) for card in kept]
        done = 0
        for future in as_completed(futures):
            rec = future.result()
            if rec:
                records.append(rec)
            done += 1
            if done % 100 == 0:
                log(f"{cfg['name']}: {done} of {len(kept)} postings read")
    return records, {"raw_fetched": len(records), "errors": errors}


def fetch_openings(
    source: str,
    *,
    countries: list[str] | None = None,
    categories: list[str] | None = None,
    locations: list[str] | None = None,
    modes: list[str] | None = None,
    progress: ProgressFn | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Openings for one company careers page.

    Blank filters read that whole board. Walmart, UnitedHealth Group, Wells Fargo,
    and Spectrum put their technology or United States defaults back when the
    matching list is left empty.
    """
    log = progress or (lambda _msg: None)
    source = (source or "").strip().lower()
    countries = _unique(list(countries or []))
    categories = _unique(list(categories or []))
    locations = _unique(list(locations or []))
    modes = _unique(list(modes or []))
    if source in WORKDAY or source in RADANCY:
        categories, countries = _with_defaults(source, categories, countries)
    bits = []
    bits.append("countries " + ", ".join(countries) if countries else "every country")
    if categories:
        bits.append("categories " + ", ".join(categories))
    if locations:
        bits.append("cities " + ", ".join(locations))
    if modes:
        bits.append("schedule " + ", ".join(modes))
    log("Board filters: " + "; ".join(bits))
    if source in PHENOM:
        return _fetch_phenom(source, countries, categories, locations, modes, log)
    if source in ORACLE_CE:
        return _fetch_ce(source, countries, categories, locations, modes, log)
    if source == "verizon":
        return _fetch_verizon(countries, categories, locations, log)
    if source == "tesla":
        return _fetch_tesla(countries, categories, locations, log)
    if source in WORKDAY:
        return _fetch_workday(source, countries, categories, locations, modes, log)
    if source in RADANCY:
        return _fetch_radancy(source, countries, categories, locations, log)
    raise RuntimeError(f"Unknown company board: {source}")


def parse_company_link(source: str, url: str) -> dict[str, Any]:
    """Pull keyword and filters out of a pasted careers search link."""
    raw = (url or "").strip()
    if not raw:
        return {}
    parsed = urllib.parse.urlparse(raw)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    expected = {
        "oracle": "oracle.com",
        "hpe": "hpe.com",
        "verizon": "verizon.com",
        "dell": "dell.com",
        "cisco": "cisco.com",
        "tesla": "tesla.com",
        "walmart": "walmart.com",
        "unitedhealth": "unitedhealthgroup.com",
        "wellsfargo": "wellsfargo",
        "spectrum": "spectrum.com",
    }.get(source)
    if expected and expected not in host and "oraclecloud.com" not in host:
        return {}
    qs = urllib.parse.parse_qs(parsed.query)
    def first(*names: str) -> str:
        for name in names:
            for value in qs.get(name) or []:
                if str(value).strip():
                    return str(value).strip()
        return ""
    def many(*names: str) -> list[str]:
        out: list[str] = []
        for name in names:
            for value in qs.get(name) or []:
                text = str(value).strip()
                if text and text not in out:
                    out.append(text)
        return out
    keyword = first("keyword", "keywords", "q", "query", "search", "searchQuery", "k")
    if keyword.casefold() == "all":
        keyword = ""
    return {
        "keyword": keyword,
        "countries": many("country", "location", "l"),
        "categories": many("category", "team", "careerareas"),
        "locations": many("city"),
        "modes": many("type", "workplace"),
    }


def refresh_filters(source: str) -> dict[str, Any]:
    """Save the page's filter lists from one search call. Verizon walks every list page."""
    source = source.strip().lower()
    if source in PHENOM:
        cfg = PHENOM[source]
        data = _post_json(
            cfg["widgets"],
            {
                "lang": cfg["locale"],
                "deviceType": "desktop",
                "country": cfg["country"],
                "pageName": "search-results",
                "ddoKey": "refineSearch",
                "sortBy": "Most recent",
                "from": 0,
                "jobs": True,
                "counts": True,
                "all_fields": ["category", "country", "state", "city", "type"],
                "size": 1,
                "siteType": "external",
                "keywords": "",
                "global": True,
                "selected_fields": {},
                "locationData": {},
                "refNum": cfg["ref"],
            },
            cfg["referer"],
        )
        aggs = ((data.get("refineSearch") or {}).get("data") or {}).get("aggregations")
        payload = {
            "countries": _names_from_agg(aggs, "country"),
            "categories": _names_from_agg(aggs, "category"),
            "types": _names_from_agg(aggs, "type"),
            "locations": _names_from_agg(aggs, "city"),
        }
        _save_board(source, payload)
        return payload
    if source in ORACLE_CE:
        cfg = ORACLE_CE[source]
        rows, _total, facets, errors = _ce_pages(cfg, "", lambda message: print(message, flush=True))
        if errors and not rows:
            raise RuntimeError(errors[-1])
        payload = _ce_payload(rows, facets or {})
        _save_board(source, payload)
        return payload
    if source == "verizon":
        teams: set[str] = set()
        country_names: set[str] = set()
        cities: set[str] = set()
        page = 1
        total_pages = 1
        while page <= total_pages and page < 400:
            data = _verizon_page(page, "", "", "")
            total_pages = int(data.get("totalPages") or 1)
            jobs = data.get("jobs") or []
            if not jobs:
                break
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                for team_name in job.get("Teams") or []:
                    if _text(team_name):
                        teams.add(_text(team_name))
                for place in job.get("Locations") or []:
                    if not isinstance(place, dict):
                        continue
                    if _text(place.get("Country")):
                        country_names.add(_text(place.get("Country")))
                    if _text(place.get("Identifier")):
                        cities.add(_text(place.get("Identifier")))
            page += 1
            time.sleep(0.05)
        payload = {
            "countries": sorted(country_names, key=str.casefold),
            "categories": sorted(teams, key=str.casefold),
            "types": [],
            "locations": sorted(cities, key=str.casefold),
        }
        _save_board("verizon", payload)
        return payload
    if source == "tesla":
        payload = {"countries": [], "categories": [], "types": [], "locations": []}
        try:
            raw = _get(
                TESLA["state"],
                headers={"Accept": "application/json", "Referer": "https://www.tesla.com/careers/search/"},
            )
            data = json.loads(raw.decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
            _save_board("tesla", payload)
            return payload
        lookup = data.get("lookup") or {}
        for key, dest in (("regions", "countries"), ("departments", "categories"), ("locations", "locations")):
            table = lookup.get(key) or {}
            names = []
            if isinstance(table, dict):
                for value in table.values():
                    if isinstance(value, dict):
                        names.append(_text(value.get("name") or value.get("city") or value.get("label")))
                    else:
                        names.append(_text(value))
            payload[dest] = sorted({name for name in names if name}, key=str.casefold)
        _save_board("tesla", payload)
        return payload
    if source in WORKDAY:
        cfg = WORKDAY[source]
        live = _workday_facets(cfg)
        payload = {
            "countries": [name for name, _ident in live.get("locationCountry") or []],
            "categories": [name for name, _ident in live.get("jobFamilyGroup") or []],
            "types": [name for name, _ident in live.get("timeType") or []],
            "locations": [name for name, _ident in (live.get("locationRegionStateProvince") or [])],
            "category_ids": live.get("jobFamilyGroup") or [],
            "country_ids": live.get("locationCountry") or [],
        }
        _save_board(source, payload)
        return payload
    if source in RADANCY:
        payload = _radancy_catalog(RADANCY[source])
        _save_board(source, payload)
        return payload
    raise RuntimeError(f"Unknown company board: {source}")


def merge_company_filters(source: str, form: Any) -> dict[str, Any]:
    parsed = parse_company_link(source, (form.get(f"{source}_url") or "").strip())
    return {
        "countries": _unique(list(form.getlist(f"{source}_countries")) + list(parsed.get("countries") or [])),
        "categories": _unique(list(form.getlist(f"{source}_categories")) + list(parsed.get("categories") or [])),
        "locations": _unique(list(form.getlist(f"{source}_locations")) + list(parsed.get("locations") or [])),
        "modes": _unique(list(form.getlist(f"{source}_types")) + list(parsed.get("modes") or [])),
        "keyword": _text(parsed.get("keyword")),
    }
