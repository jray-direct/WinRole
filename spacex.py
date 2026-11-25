"""SpaceX careers page (https://www.spacex.com/careers/jobs).

The page is an Angular app. It does not search Indeed. It loads two public files:

- https://content.spacex.com/cms-assets/job_posts_new.json
  every opening the jobs page lists (title, location, programs, discipline,
  category, employment type, greenhouse id, board url)
- https://content.spacex.com/cms-assets/job_metadata_new.json
  the program, location, and discipline filters

Each row links to a Greenhouse posting. Three boards back the page:

- boards.greenhouse.io/spacex
- job-boards.greenhouse.io/spacexglobal
- job-boards.greenhouse.io/xai   (shown on the SpaceX page as SpaceXSI)

The page filters in the browser, then sends you to Greenhouse for the writeup:

- programs: opening belongs to any selected program
- locations: exact site, or one piece of a "City; City" location.
  Selecting the location named Remote also matches "Remote, United States"
  and "Remote, International"
- disciplines: exact discipline. A category checkbox selects every discipline
  in that group
- keyword box on the page: substring of the title or location only

This module applies those board filters, then downloads the Greenhouse
posting text so the app keyword can match the full writeup.
"""

from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from textutil import has_any_phrase, keyword_pattern

POSTS_URL = "https://content.spacex.com/cms-assets/job_posts_new.json"
META_URL = "https://content.spacex.com/cms-assets/job_metadata_new.json"
CAREERS_URL = "https://www.spacex.com/careers/jobs"
GH_BOARD = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
USER_AGENT = "WinRole job search (public SpaceX careers page)"

_ROOT = Path(__file__).resolve().parent
_SNAPSHOT = _ROOT / "spacex_filters.json"
_CACHE = _ROOT / "output" / "spacex_filters.json"

ProgressFn = Callable[[str], None]

_JOB_TYPES = {
    "regular": "fulltime",
    "intern": "internship",
    "temporary": "temporary",
    "contract": "contract",
    "international": "fulltime",
}


def _get_json(url: str, timeout: int = 120) -> Any:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def board_key(post: dict[str, Any]) -> str:
    """Which Greenhouse board hosts this opening.

    When the jobs page already stored a full URL, that URL wins. Employment
    type International only picks the global board for rows that have no URL.
    """
    board = str(post.get("board") or "")
    lowered = board.lower()
    if "/xai" in lowered or lowered == "xai":
        return "xai"
    if "spacexglobal" in lowered:
        return "spacexglobal"
    if board.startswith("http"):
        return "spacex"
    if str(post.get("employementType") or "") == "International":
        return "spacexglobal"
    return "spacex"


def job_link(post: dict[str, Any]) -> str:
    board = post.get("board")
    if isinstance(board, str) and board.startswith("http"):
        return board
    gid = post.get("greenhouseId")
    key = board_key(post)
    if key == "xai":
        return f"https://job-boards.greenhouse.io/xai/jobs/{gid}"
    if key == "spacexglobal":
        return f"https://job-boards.greenhouse.io/spacexglobal/jobs/{gid}"
    return f"https://boards.greenhouse.io/spacex/jobs/{gid}"


def location_matches(job_location: str, selected: str) -> bool:
    """Same location rule as the SpaceX jobs page."""
    place = (job_location or "").strip()
    wanted = (selected or "").strip()
    if not place or not wanted:
        return False
    if wanted.lower() == "remote":
        return "remote" in place.lower()
    if place == wanted:
        return True
    return any(part.strip() == wanted for part in place.split(";"))


def greenhouse_text(raw: str | None) -> str:
    """Greenhouse stores the posting as escaped HTML."""
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


def parse_careers_link(url: str) -> dict[str, Any]:
    """Read programs, locations, disciplines, and keyword from a jobs-page URL.

    The page writes them as pipe-separated query params, for example
    /careers/jobs?programs=Starlink|Starship&locations=Hawthorne, CA&disciplines=Network
    """
    raw = (url or "").strip()
    if not raw:
        return {}
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    if host and "spacex.com" not in host:
        return {}
    if path and "career" not in path and "job" not in path:
        return {}
    qs = parse_qs(parsed.query, keep_blank_values=False)

    def pieces(key: str) -> list[str]:
        found: list[str] = []
        for item in qs.get(key) or []:
            for bit in str(item).split("|"):
                bit = bit.strip()
                if bit and bit not in found:
                    found.append(bit)
        return found

    keyword = ""
    for item in qs.get("keyword") or []:
        keyword = str(item).strip()
        if keyword:
            break
    return {
        "keyword": keyword,
        "programs": pieces("programs"),
        "locations": pieces("locations"),
        "disciplines": pieces("disciplines"),
    }


def _unique(items: list[str]) -> list[str]:
    found: list[str] = []
    for item in items:
        text = (item or "").strip()
        if text and text not in found:
            found.append(text)
    return found


def merge_board_filters(
    programs: list[str],
    locations: list[str],
    disciplines: list[str],
    board_url: str,
) -> tuple[list[str], list[str], list[str], str]:
    """Form selections plus whatever a pasted SpaceX jobs link already had."""
    parsed = parse_careers_link(board_url)
    return (
        _unique(list(programs) + list(parsed.get("programs") or [])),
        _unique(list(locations) + list(parsed.get("locations") or [])),
        _unique(list(disciplines) + list(parsed.get("disciplines") or [])),
        str(parsed.get("keyword") or "").strip(),
    )


def normalize_meta(meta: dict[str, Any] | None) -> dict[str, Any]:
    meta = meta or {}
    categories: list[dict[str, Any]] = []
    raw = meta.get("disciplines") or {}
    if isinstance(raw, dict):
        for name, node in raw.items():
            kids: list[str] = []
            if isinstance(node, dict):
                kids = [str(item) for item in (node.get("disciplines") or []) if str(item).strip()]
            elif isinstance(node, list):
                kids = [str(item) for item in node if str(item).strip()]
            categories.append({"name": str(name), "disciplines": kids})
    return {
        "programs": [str(item) for item in (meta.get("programs") or []) if str(item).strip()],
        "locations": [str(item) for item in (meta.get("locations") or []) if str(item).strip()],
        "categories": categories,
    }


def _read_meta_file(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def careers_filter_options() -> dict[str, Any]:
    """Program, site, and discipline lists for the scan form.

    A scan refreshes output/spacex_filters.json. Until then, the snapshot
    saved with the app is used.
    """
    for path in (_CACHE, _SNAPSHOT):
        data = _read_meta_file(path)
        if data:
            return normalize_meta(data)
    return normalize_meta({})


def _save_meta(meta: dict[str, Any]) -> None:
    try:
        _CACHE.parent.mkdir(exist_ok=True)
        _CACHE.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except OSError:
        return


def expand_disciplines(selected: list[str], meta: dict[str, Any] | None) -> list[str]:
    """Turn a category pick into every discipline the jobs page would check."""
    tree = normalize_meta(meta)
    by_category = {cat["name"]: cat["disciplines"] for cat in tree["categories"]}
    found: list[str] = []
    for item in selected:
        text = (item or "").strip()
        if not text:
            continue
        if text.startswith("category:"):
            for kid in by_category.get(text.split(":", 1)[1], []):
                if kid not in found:
                    found.append(kid)
            continue
        if text not in found:
            found.append(text)
    return found


def filter_posts(
    posts: list[dict[str, Any]],
    *,
    programs: list[str] | None = None,
    locations: list[str] | None = None,
    disciplines: list[str] | None = None,
) -> list[dict[str, Any]]:
    wanted_programs = {item.strip() for item in (programs or []) if item and item.strip()}
    wanted_locations = [item.strip() for item in (locations or []) if item and item.strip()]
    wanted_disciplines = {item.strip() for item in (disciplines or []) if item and item.strip()}
    kept: list[dict[str, Any]] = []
    for post in posts:
        if wanted_programs:
            have = {str(item) for item in (post.get("programs") or [])}
            if not have & wanted_programs:
                continue
        if wanted_locations:
            place = str(post.get("location") or "")
            if not any(location_matches(place, item) for item in wanted_locations):
                continue
        if wanted_disciplines and str(post.get("discipline") or "") not in wanted_disciplines:
            continue
        kept.append(post)
    return kept


def _as_posts(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("jobs", "posts", "data"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [item for item in rows if isinstance(item, dict)]
    raise ValueError("SpaceX job list was not a list of openings")


def _company_name(key: str, gh: dict[str, Any] | None) -> str:
    raw = str((gh or {}).get("company_name") or "").strip()
    if key == "xai":
        return raw or "SpaceXSI"
    if raw.lower() in {"", "spacex", "spacex_global", "spacex global"}:
        return "SpaceX"
    return raw


def _job_type(post: dict[str, Any]) -> str | None:
    label = str(post.get("employementType") or "").strip().lower()
    return _JOB_TYPES.get(label)


def _posted_day(gh: dict[str, Any] | None) -> str | None:
    if not gh:
        return None
    raw = gh.get("first_published") or gh.get("updated_at")
    if not raw:
        return None
    text = str(raw)
    return text[:10] if len(text) >= 10 else None


def opening_to_record(post: dict[str, Any], gh: dict[str, Any] | None = None) -> dict[str, Any]:
    """Shape one careers-page row like the other scrapers' job dicts."""
    gh = gh or {}
    key = board_key(post)
    gid = post.get("greenhouseId") or gh.get("id")
    title = str(post.get("title") or gh.get("title") or "").strip()
    location = str(post.get("location") or "")
    if not location and isinstance(gh.get("location"), dict):
        location = str(gh["location"].get("name") or "")
    programs = [str(item) for item in (post.get("programs") or []) if str(item).strip()]
    discipline = str(post.get("discipline") or "").strip()
    category = str(post.get("category") or "").strip()
    employment = str(post.get("employementType") or "").strip()
    header: list[str] = []
    if programs:
        header.append("Program: " + ", ".join(programs))
    if discipline:
        header.append("Discipline: " + discipline)
    if category:
        header.append("Category: " + category)
    if employment:
        header.append("Employment type: " + employment)
    body = greenhouse_text(gh.get("content"))
    description = "\n".join(header)
    if body:
        description = f"{description}\n\n{body}" if description else body
    industry = " · ".join(part for part in (category, discipline) if part)
    remote = "remote" in location.lower()
    return {
        "id": str(gid) if gid is not None else None,
        "title": title,
        "company": _company_name(key, gh),
        "location": location.strip(),
        "job_url": str(gh.get("absolute_url") or job_link(post)),
        "job_url_direct": job_link(post),
        "site": "spacex",
        "description": description,
        "date_posted": _posted_day(gh),
        "job_type": _job_type(post),
        "is_remote": remote,
        "company_industry": industry or None,
        "company_url": CAREERS_URL,
        "company_url_direct": CAREERS_URL,
    }


def _fetch_one(board: str, gid: int) -> dict[str, Any] | None:
    try:
        data = _get_json(f"{GH_BOARD.format(board=board)}/{gid}", timeout=40)
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _descriptions_for(posts: list[dict[str, Any]], log: ProgressFn) -> dict[int, dict[str, Any]]:
    needed: dict[str, list[int]] = {}
    for post in posts:
        try:
            gid = int(post.get("greenhouseId"))
        except (TypeError, ValueError):
            continue
        needed.setdefault(board_key(post), []).append(gid)

    found: dict[int, dict[str, Any]] = {}
    for board, ids in needed.items():
        wanted = set(ids)
        if len(wanted) <= 40:
            log(f"Reading {len(wanted)} {board} posting{'s' if len(wanted) != 1 else ''}")
            with ThreadPoolExecutor(max_workers=6) as pool:
                for job in pool.map(lambda gid, board=board: _fetch_one(board, gid), wanted):
                    if job and job.get("id") is not None:
                        found[int(job["id"])] = job
            continue
        url = GH_BOARD.format(board=board) + "?content=true"
        log(f"Downloading {board} posting text ({len(wanted)} openings)")
        try:
            payload = _get_json(url)
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            log(f"! Could not download {board} postings: {exc}")
            continue
        for job in (payload or {}).get("jobs") or []:
            if not isinstance(job, dict) or job.get("id") is None:
                continue
            jid = int(job["id"])
            if jid in wanted:
                found[jid] = job
    return found


def fetch_openings(
    *,
    programs: list[str] | None = None,
    locations: list[str] | None = None,
    disciplines: list[str] | None = None,
    title_include: list[str] | None = None,
    title_exclude: list[str] | None = None,
    keyword: str = "",
    kw_match: str = "any",
    progress: ProgressFn | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Openings on the SpaceX jobs page that pass the page's own filters.

    Descriptions come from Greenhouse. Keyword matching stays with the caller
    so it uses the same rules as an Indeed scan. Title-only scans skip
    postings whose title already fails, before the download.
    """
    log = progress or (lambda _msg: None)
    errors: list[str] = []
    log("Loading the SpaceX careers jobs page")
    try:
        meta = _get_json(META_URL, timeout=30)
        posts_payload = _get_json(POSTS_URL, timeout=60)
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not load the SpaceX jobs page: {exc}") from exc
    if isinstance(meta, dict):
        _save_meta(meta)
    posts = _as_posts(posts_payload)
    log(f"SpaceX lists {len(posts)} openings")

    chosen_disciplines = expand_disciplines(list(disciplines or []), meta if isinstance(meta, dict) else None)
    narrowed = filter_posts(
        posts,
        programs=programs,
        locations=locations,
        disciplines=chosen_disciplines,
    )
    if title_include:
        narrowed = [post for post in narrowed if has_any_phrase(str(post.get("title") or ""), title_include)]
    if title_exclude:
        narrowed = [
            post
            for post in narrowed
            if not has_any_phrase(str(post.get("title") or ""), title_exclude)
        ]
    if (kw_match or "any") == "title" and keyword.strip():
        pattern = keyword_pattern(keyword)
        narrowed = [post for post in narrowed if pattern.search(str(post.get("title") or ""))]

    bits = []
    if programs:
        bits.append("programs " + ", ".join(programs))
    if locations:
        bits.append("sites " + ", ".join(locations))
    if chosen_disciplines:
        bits.append("disciplines " + ", ".join(chosen_disciplines))
    if bits:
        log(f"Board filters ({'; '.join(bits)}) leave {len(narrowed)}")
    else:
        log(f"No board filters, so all {len(narrowed)} stay in play")

    if not narrowed:
        return [], {"raw_fetched": len(posts), "described": 0, "errors": errors}

    described = _descriptions_for(narrowed, log)
    missing = 0
    records: list[dict[str, Any]] = []
    for post in narrowed:
        try:
            gid = int(post.get("greenhouseId"))
        except (TypeError, ValueError):
            gid = None
        gh = described.get(gid) if gid is not None else None
        if gh is None:
            missing += 1
        records.append(opening_to_record(post, gh))
    if missing:
        errors.append(f"{missing} openings had no Greenhouse writeup")
        log(f"{missing} openings had no Greenhouse writeup; the title is all we have for those")
    else:
        log(f"Got the writeup for all {len(records)} openings")
    return records, {"raw_fetched": len(posts), "described": len(records) - missing, "errors": errors}
