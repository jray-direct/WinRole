"""Keyword matching, negation-aware excludes, snippets, and a simple fit score."""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from markupsafe import Markup


_CITE_TAG = re.compile(r"[ \t]*<Cite\b[^>]*/>", re.I)
_BR_TAG = re.compile(r"<br\s*/?>", re.I)
_BLOCK_TAG = re.compile(
    r"</?(?:div|p|span|b|i|u|strong|em|ul|ol|li|font|section|article|table|tr|td|th)\b[^>]*>",
    re.I,
)
_ENTITY = re.compile(r"&(?:#\d{1,6}|#x[0-9a-fA-F]{1,5}|[A-Za-z]{2,8});")


def clean_description(text: str | None) -> str | None:
    """Drop leftover HTML/LLM-citation fragments from a stored description.

    Only known markup is touched, so literal angle-bracket text such as
    ``<Job Summary>`` headings or ``<name@example.com>`` stays as written.
    """
    if not text:
        return text
    out = _CITE_TAG.sub("", text)
    out = _BR_TAG.sub("\n", out)
    out = _BLOCK_TAG.sub("", out)
    if _ENTITY.search(out):
        out = _ENTITY.sub(lambda m: html.unescape(m.group(0)), out)
    out = out.replace("\u00a0", " ")
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def keyword_pattern(keyword: str, *, whole_word: bool = False) -> re.Pattern[str]:
    """Match a scan keyword, cert, or title phrase, including the usual spellings.

    A leading letter boundary is always on, so "Security+" does not match inside
    "Cybersecurity+". A trailing boundary is added only when whole_word is set,
    so "Architect" still matches "Architects".
    """
    raw = keyword.strip()
    key = raw.lower().rstrip(".")
    if key in {"sr", "senior"}:
        body = r"(?:senior|sr\.?)"
    elif raw.upper() == "CCNA":
        body = r"(?:CCNA(?!\s+Security)|cisco\s+certified\s+network\s+associate)"
    elif key in {"security+", "comptia security+"}:
        body = r"(?:(?:comptia\s+)?security(?:\s*\+|[\s-]*plus\b)|(?<![A-Za-z])sec\+)"
    elif key in {"network+", "comptia network+"}:
        body = r"(?:(?:comptia\s+)?network(?:\s*\+|[\s-]*plus\b)|(?<![A-Za-z])(?:net|n)\+)"
    elif raw.endswith("+") and len(raw) > 1:
        stem = re.escape(raw[:-1].rstrip())
        body = rf"(?:comptia\s+)?{stem}(?:\s*\+|[\s-]*plus\b)"
    else:
        parts = []
        for ch in raw:
            if ch == "+":
                parts.append(r"\+")
            elif ch == "-" or ch in "\u2010\u2011\u2013\u2014":
                parts.append(r"[\s\u2010\u2011\u2013\u2014-]+")
            elif ch.isspace():
                parts.append(r"[\s\u2010\u2011\u2013\u2014-]+")
            else:
                parts.append(re.escape(ch))
        body = "".join(parts) or r"$^"
    if whole_word:
        body = rf"(?<![A-Za-z0-9])(?:{body})(?![A-Za-z0-9])"
    else:
        body = rf"(?<![A-Za-z])(?:{body})"
    return re.compile(body, re.IGNORECASE)


def _sentence_at(text: str, start: int, end: int) -> str:
    left = max(text.rfind(".", 0, start), text.rfind("!", 0, start), text.rfind("?", 0, start), text.rfind("\n", 0, start))
    right_candidates = [i for i in (text.find(".", end), text.find("!", end), text.find("?", end), text.find("\n", end)) if i != -1]
    right = min(right_candidates) if right_candidates else len(text)
    return text[left + 1 : right + 1].strip()


@lru_cache(maxsize=512)
def _negation_patterns(term: str) -> tuple[re.Pattern[str], ...]:
    checks = (
        rf"\b(no|not|without|never|neither)\b.{{0,48}}{term}",
        rf"\b(does\s+not|doesn't|doesn’t|do\s+not|don't|don’t|is\s+not|isn't|isn’t|are\s+not|aren't|aren’t)\b.{{0,48}}{term}",
        rf"{term}.{{0,40}}\b(not\s+(required|needed|necessary|a\s+requirement)|is\s+not\s+(required|needed)|are\s+not\s+(required|needed))",
        rf"\b(not\s+(require[ds]?|need(?:ed)?|necessary))\b.{{0,32}}{term}",
        rf"\b(unrelated\s+to|except(\s+for)?|other\s+than)\b.{{0,24}}{term}",
    )
    return tuple(re.compile(pat, re.IGNORECASE) for pat in checks)


def _mention_is_negated(sentence: str, match: re.Match[str]) -> bool:
    """True when the hit is 'no X required' / 'X not needed', not a real requirement."""
    term = re.escape(match.group(0))
    return any(pat.search(sentence) for pat in _negation_patterns(term))


def has_affirmative_mention(text: str, term: str) -> bool:
    """True if `term` appears as a real requirement, not a negated phrase."""
    if not text or not term.strip():
        return False
    pat = keyword_pattern(term, whole_word=True)
    for match in pat.finditer(text):
        sentence = _sentence_at(text, match.start(), match.end())
        if not _mention_is_negated(sentence, match):
            return True
    return False


def should_exclude(text: str, terms: list[str]) -> bool:
    return any(has_affirmative_mention(text, term) for term in terms if term.strip())


def parse_phrases(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [p.strip() for p in raw.split(",") if p.strip()]
    return [str(p).strip() for p in raw if p and str(p).strip()]


def has_any_phrase(text: str, terms: list[str], *, whole_word: bool = True) -> bool:
    blob = text or ""
    return any(
        keyword_pattern(term, whole_word=whole_word).search(blob)
        for term in terms
        if term.strip()
    )


def has_all_phrases(text: str, terms: list[str], *, whole_word: bool = True) -> bool:
    wanted = [term for term in terms if term.strip()]
    if not wanted:
        return True
    blob = text or ""
    return all(keyword_pattern(term, whole_word=whole_word).search(blob) for term in wanted)


def highlight_text(text: str, keyword: str) -> Markup:
    if not text:
        return Markup("")
    pat = keyword_pattern(keyword)
    parts: list[str] = []
    last = 0
    for match in pat.finditer(text):
        parts.append(html.escape(text[last : match.start()]))
        parts.append("<mark>")
        parts.append(html.escape(match.group(0)))
        parts.append("</mark>")
        last = match.end()
    parts.append(html.escape(text[last:]))
    return Markup("".join(parts))


def make_snippet(text: str, keyword: str, width: int = 180) -> Markup | None:
    if not text or not keyword:
        return None
    pat = keyword_pattern(keyword)
    match = pat.search(text)
    if not match:
        return None
    start = max(0, match.start() - width // 2)
    end = min(len(text), match.end() + width // 2)
    while start > 0 and text[start] not in " \n\t":
        start -= 1
    while end < len(text) and text[end] not in " \n\t":
        end += 1
    piece = re.sub(r"\s+", " ", text[start:end]).strip()
    marked = highlight_text(piece, keyword)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return Markup(f"{prefix}{marked}{suffix}")


def highlight_description(
    text: str,
    keywords: list[str] | None = None,
    *,
    company: str = "",
) -> Markup:
    """Turn a job description into readable HTML with keyword highlights."""
    if not text or not str(text).strip():
        return Markup("<p class='muted'>No description stored.</p>")
    kws = [k for k in (keywords or []) if k]
    parts: list[str] = []
    for kind, payload in _description_blocks(str(text), company=company):
        if kind == "h":
            parts.append(f"<h3>{_render_inline(payload, kws)}</h3>")
        elif kind == "ul":
            items = "".join(f"<li>{_render_inline(item, kws)}</li>" for item in payload)
            parts.append(f"<ul>{items}</ul>")
        elif kind == "kv":
            rows = "".join(
                "<div>"
                f"<dt>{_render_inline(label, kws)}</dt>"
                f"<dd>{_render_inline(value, kws)}</dd>"
                "</div>"
                for label, value in payload
            )
            parts.append(f"<dl class='job-fields'>{rows}</dl>")
        else:
            parts.append(f"<p>{_render_inline(payload, kws)}</p>")
    return Markup("".join(parts) or "<p class='muted'>No description stored.</p>")


_FIELD_LABELS = (
    "Priority Review Date (Note - Posting may close at any time)",
    "Priority Review Date",
    "Anticipated Salary Range",
    "Standard Hours per Week",
    "Full Time or Part Time",
    "Patient Sensitive Job Code",
    "Career Progression Track",
    "Type of Recruitment",
    "Work Schedule Summary",
    "Requisition Number",
    "Employment Type",
    "Security Clearance",
    "Level of Experience",
    "Required Travel",
    "Pay Rate Range",
    "Working Title",
    "Track Level",
    "FLSA Code",
    "Open Date",
    "Close Date",
    "Job Title",
    "Job no",
    "VP Area",
    "Department",
    "Location",
    "Company",
    "Shift",
    "Date",
    "City",
)

_SECTION_LABELS = (
    "What You\u2019ll Do",
    "What You'll Do",
    "What You Will Do",
    "What You\u2019ll Bring",
    "What You'll Bring",
    "Basic Qualifications",
    "Minimum Qualifications",
    "Required Qualifications",
    "Preferred Qualifications",
    "Nice to Have",
    "Nice-to-Have",
    "Duties and Responsibilities",
    "Salary and Benefit Information",
    "Our Identity Verification Process",
    "Equal Opportunity Employer",
    "Eligibility Requirements",
    "Eligibility Requirement",
    "Minimum Requirements",
    "Classification Title",
    "Security clearance level",
    "Key Skills For Success",
    "About the Position",
    "About the Company",
    "About the Team",
    "About the Role",
    "About the Job",
    "About Our Work",
    "Work Requirements",
    "Years of Experience",
    "Travel Required",
    "How to Apply",
    "Job Description",
    "Job Overview",
    "Job Summary",
    "Responsibilities",
    "Qualifications",
    "Requirements",
    "Certifications",
    "Compensation",
    "Your Impact",
    "Education",
    "Experience",
    "Benefits",
    "Details",
)

_ALL_LABELS = tuple(
    sorted(set(_FIELD_LABELS + _SECTION_LABELS), key=len, reverse=True)
)
_FIELD_SET = {label.lower() for label in _FIELD_LABELS}
_SECTION_SET = {label.lower() for label in _SECTION_LABELS}

_SHOUT_TOKEN = r"(?:A(?![A-Za-z])|[A-Z]{2,}(?:[\u2019'][A-Z]+)*)"
_SHOUT_HEAD = re.compile(
    rf"(?<![A-Za-z])((?:{_SHOUT_TOKEN})(?:\s+{_SHOUT_TOKEN}){{2,}})"
)
_MD_WRAP = re.compile(r"^\s*(\*{1,3}|_{1,2})(.+?)\1\s*$")
_MD_INLINE = re.compile(r"(\*\*(.+?)\*\*|__(.+?)__|(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*))")
_BULLET_SPLIT = re.compile(r"\s*[•·▪◦]\s*")
_ATS_SPLIT = re.compile(
    r"(?<![A-Za-z0-9\n])("
    + "|".join(re.escape(label) for label in _ALL_LABELS)
    + r")(?=\s*[:?]|\s+\S)"
)
_NARRATIVE_START = re.compile(
    r"^(This |The |We |Come |Join |Our |As an? |You |Are you |HII |Mission )"
)


def _description_blocks(text: str, company: str = "") -> list[tuple[str, Any]]:
    text = _normalize_description(text, company)
    if "\n" in text:
        return _blocks_from_multiline(text)
    return _blocks_from_flat(text)


def _normalize_description(text: str, company: str = "") -> str:
    text = (
        text.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\u00a0", " ")
        .replace("\u2011", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )
    company = (company or "").strip()
    if company:
        esc = re.escape(company)
        text = re.sub(
            rf"(About\s+{esc})\s+{esc}\b",
            rf"\1. {company}",
            text,
            count=1,
            flags=re.I,
        )
    if text.count("\n") < 3 and "**" not in text:
        text = _ATS_SPLIT.sub(r"\n\1", text)
    lines = []
    for raw in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", raw).strip()
        if line:
            lines.append(line)
        elif lines and lines[-1] != "":
            lines.append("")
    return "\n".join(lines).strip()


def _flush_list(items: list[str], out: list[tuple[str, Any]]) -> None:
    if items:
        out.append(("ul", items[:]))
        items.clear()


def _flush_para(lines: list[str], out: list[tuple[str, Any]]) -> None:
    if lines:
        out.append(("p", " ".join(lines).strip()))
        lines.clear()


def _short_field_value(value: str) -> tuple[str, str]:
    if not value or len(value) < 40:
        return value, ""
    words = value.split()
    for n in (1, 2, 3, 4):
        if n >= len(words):
            break
        head = " ".join(words[:n])
        tail = " ".join(words[n:])
        if len(head) <= 48 and _NARRATIVE_START.match(tail):
            return head, tail
    return value, ""


def _flush_fields(fields: list[tuple[str, str]], out: list[tuple[str, Any]]) -> None:
    if fields:
        out.append(("kv", fields[:]))
        fields.clear()


def _match_known_label(line: str) -> tuple[str, str, bool] | None:
    lower = line.lower()
    for label in _ALL_LABELS:
        if not lower.startswith(label.lower()):
            continue
        rest = line[len(label) :]
        if rest and rest[0] not in " :?\t":
            continue
        value = rest.lstrip(" :?\t")
        return label, value, label.lower() in _FIELD_SET
    return None


def _blocks_from_multiline(text: str) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    para: list[str] = []
    items: list[str] = []
    fields: list[tuple[str, str]] = []
    pending: str | None = None

    def take_pending(value: str = "") -> None:
        nonlocal pending
        if pending:
            if value:
                fields.append((pending, value))
            pending = None

    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            _flush_list(items, out)
            _flush_para(para, out)
            continue
        heading = re.match(r"^#{1,3}\s+(.+)", stripped)
        if heading:
            take_pending()
            _flush_list(items, out)
            _flush_para(para, out)
            _flush_fields(fields, out)
            out.append(("h", heading.group(1).strip().strip("*_")))
            continue
        md_lead = re.match(r"^(\*{1,3}|_{1,2})(.+?)\1\s*(.*)$", stripped)
        if md_lead:
            inner = md_lead.group(2).strip().rstrip(":").strip()
            rest = md_lead.group(3).strip()
            if _match_known_label(inner):
                stripped = f"{inner} {rest}".strip()
            elif not rest and 2 <= len(inner) <= 70:
                take_pending()
                _flush_list(items, out)
                _flush_para(para, out)
                _flush_fields(fields, out)
                out.append(("h", inner))
                continue
        bullet = re.match(r"^(?:[-*•]|\d+[.)])\s+(.+)", stripped)
        if bullet:
            take_pending()
            _flush_fields(fields, out)
            _flush_para(para, out)
            items.append(bullet.group(1).strip())
            continue
        known = _match_known_label(stripped)
        if known:
            label, value, is_field = known
            _flush_list(items, out)
            _flush_para(para, out)
            if is_field:
                take_pending()
                value, leftover = _short_field_value(value)
                if value:
                    fields.append((label, value))
                else:
                    pending = label
                if leftover:
                    _flush_fields(fields, out)
                    para.append(leftover)
            else:
                take_pending()
                _flush_fields(fields, out)
                out.append(("h", label))
                if value:
                    para.append(value)
            continue
        if pending:
            value, leftover = _short_field_value(stripped)
            fields.append((pending, value))
            pending = None
            if leftover:
                _flush_fields(fields, out)
                para.append(leftover)
            continue
        _flush_list(items, out)
        _flush_fields(fields, out)
        para.append(stripped)
    take_pending()
    _flush_list(items, out)
    _flush_para(para, out)
    _flush_fields(fields, out)
    return out


def _header_spans(text: str) -> list[tuple[int, int, str]]:
    found: list[tuple[int, int, str, int]] = []
    for match in _ATS_SPLIT.finditer(text):
        label = match.group(1)
        rank = 0 if label.lower() in _SECTION_SET or label.lower() in _FIELD_SET else 2
        found.append((match.start(1), match.end(), label, rank))
    for match in _SHOUT_HEAD.finditer(text):
        label = re.sub(r"\s+", " ", match.group(1)).strip()
        if 8 <= len(label) <= 80:
            found.append((match.start(1), match.end(1), label, 3))
    found.sort(key=lambda t: (t[0], t[3], -(t[1] - t[0])))
    merged: list[tuple[int, int, str]] = []
    for start, end, label, _rank in found:
        if merged and start < merged[-1][1]:
            continue
        merged.append((start, end, label))
    return merged


def _maybe_list(text: str) -> list[str] | None:
    if _BULLET_SPLIT.search(text):
        parts = [p.strip(" \t-–—") for p in _BULLET_SPLIT.split(text) if p.strip()]
        if len(parts) >= 2:
            return parts
    return None


def _blocks_from_flat(text: str) -> list[tuple[str, Any]]:
    spans = _header_spans(text)
    out: list[tuple[str, Any]] = []
    fields: list[tuple[str, str]] = []
    cursor = 0
    for i, (start, end, label) in enumerate(spans):
        lead = text[cursor:start].strip(" \t:.-–—")
        if lead:
            _flush_fields(fields, out)
            items = _maybe_list(lead)
            out.append(("ul", items) if items else ("p", lead))
        heading = label.rstrip(":").strip()
        known = _match_known_label(heading) if heading else None
        next_start = spans[i + 1][0] if i + 1 < len(spans) else len(text)
        value = text[end:next_start].strip(" \t:.-–—")
        if known and known[2]:
            value, leftover = _short_field_value(value)
            fields.append((known[0], value))
            if leftover:
                _flush_fields(fields, out)
                items = _maybe_list(leftover)
                out.append(("ul", items) if items else ("p", leftover))
            cursor = next_start
            continue
        _flush_fields(fields, out)
        if heading:
            out.append(("h", known[0] if known else heading))
        cursor = end
    tail = text[cursor:].strip(" \t:.-–—")
    if tail:
        _flush_fields(fields, out)
        items = _maybe_list(tail)
        out.append(("ul", items) if items else ("p", tail))
    _flush_fields(fields, out)
    if not out:
        out.append(("p", text))
    return out


def _highlight_many(text: str, keywords: list[str]) -> str:
    spans: list[tuple[int, int]] = []
    for keyword in keywords:
        for match in keyword_pattern(keyword).finditer(text):
            spans.append((match.start(), match.end()))
    if not spans:
        return html.escape(text)
    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    parts: list[str] = []
    last = 0
    for start, end in merged:
        parts.append(html.escape(text[last:start]))
        parts.append("<mark>")
        parts.append(html.escape(text[start:end]))
        parts.append("</mark>")
        last = end
    parts.append(html.escape(text[last:]))
    return "".join(parts)


def _render_inline(text: str, keywords: list[str]) -> str:
    if not text:
        return ""
    parts: list[str] = []
    last = 0
    for match in _MD_INLINE.finditer(text):
        parts.append(_highlight_many(text[last:match.start()], keywords))
        inner = match.group(2) or match.group(3) or match.group(4) or ""
        tag = "strong" if match.group(2) or match.group(3) else "em"
        parts.append(f"<{tag}>{_highlight_many(inner, keywords)}</{tag}>")
        last = match.end()
    parts.append(_highlight_many(text[last:], keywords))
    return "".join(parts)


CERT_STACK = [
    ("Security+", "Security+"),
    ("Network+", "Network+"),
    ("A+", "A+"),
    ("CCNA", "CCNA"),
    ("CySA+", "CySA+"),
    ("CISSP", "CISSP"),
    ("Splunk", "Splunk"),
    ("ITIL", "ITIL"),
]
CERT_PATTERNS = [
    (label, keyword_pattern(term, whole_word=True)) for label, term in CERT_STACK
]

CLEARANCE_LABELS = {
    "sci": "TS/SCI",
    "ts": "Top Secret",
    "secret": "Secret",
    "public_trust": "Public Trust",
    "confidential": "Confidential",
    "none": "None required",
    "unspecified": "Not mentioned",
}

CLEARANCE_ORDER = (
    "sci",
    "ts",
    "secret",
    "public_trust",
    "confidential",
    "none",
    "unspecified",
)

_CLEARANCE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("sci", re.compile(r"ts/?\s*sci|top\s*secret/?\s*sci|\bsci\s+clearance|sensitive\s+compartmented", re.I)),
    # T5 is a Top Secret investigation. DOE Q, and an L/Q pair, sit at that tier.
    # "Tier 5" is a help-desk level as often as a clearance, so only the T5 code counts.
    ("ts", re.compile(
        r"top\s*secret|\bts\s+clearance|\bT5\b|"
        r"\b(?:doe\s+)?q(?:\s*[/&-]\s*l)?\s*clearance\b|"
        r"\b[lq]\s*/\s*q\b|\bq\s*/\s*l\b",
        re.I,
    )),
    # T3 is a Secret investigation. DOE L alone is the lower of the L/Q pair.
    ("secret", re.compile(
        r"secret\s+(security\s+)?clearance|(active|current)\s+secret\b|"
        r"clearance.{0,40}\bsecret\b|\bsecret\b.{0,40}clearance|"
        r"\bT3\b|\b(?:doe\s+)?l\s*clearance\b",
        re.I,
    )),
    ("public_trust", re.compile(r"public\s+trust|\bnaclc\b|\bT[24]\b", re.I)),
    ("confidential", re.compile(r"confidential\s+clearance", re.I)),
]


def classify_clearance(text: str) -> str:
    if not text or not str(text).strip():
        return "unspecified"
    blob = str(text)
    for level, pat in _CLEARANCE_PATTERNS:
        for match in pat.finditer(blob):
            sentence = _sentence_at(blob, match.start(), match.end())
            if not _mention_is_negated(sentence, match):
                return level
    if has_affirmative_mention(blob, "clearance"):
        return "unspecified"
    if keyword_pattern("clearance", whole_word=True).search(blob):
        return "none"
    return "unspecified"


def certs_mentioned(text: str) -> list[str]:
    """Certs the posting actually asks about; "no Security+ required" does not count."""
    return list(cert_stances(text))


_STANCE_REQ = re.compile(
    r"\b(required|must\s+(have|possess|hold|obtain)|mandatory|min(?:imum)?\s+qualif)",
    re.I,
)
_STANCE_PREF = re.compile(
    r"\b(preferred|a\s+plus|nice\s+to\s+have|desired|bonus|ideally|progress\s+toward)\b",
    re.I,
)
_STANCE_EQUIV = re.compile(r"\b(or\s+equivalent|or\s+similar|or\s+related)\b", re.I)


STANCE_RANK = {"required": 4, "preferred": 3, "equivalent": 2, "mentioned": 1}


def _stance_of_match(text: str, match: re.Match[str]) -> str:
    left = max(0, match.start() - 100)
    window = text[left : match.end() + 100]
    kw_at = match.start() - left

    def nearest(rx: re.Pattern[str]) -> int | None:
        best: int | None = None
        for hit in rx.finditer(window):
            dist = min(abs(hit.start() - kw_at), abs(hit.end() - kw_at))
            if best is None or dist < best:
                best = dist
        return best

    scored = []
    for key, rx in (
        ("required", _STANCE_REQ),
        ("preferred", _STANCE_PREF),
        ("equivalent", _STANCE_EQUIV),
    ):
        dist = nearest(rx)
        if dist is not None:
            scored.append((dist, -STANCE_RANK[key], key))
    scored.sort()
    return scored[0][2] if scored else "mentioned"


def mention_stance(text: str, keyword: str, pat: re.Pattern[str] | None = None) -> str:
    """required | preferred | equivalent | mentioned | absent

    A negated hit ("no Network+ required") is never the strongest reading; if
    every hit is negated the posting only "mentions" the term.
    """
    if not text or not keyword:
        return "absent"
    pat = pat or keyword_pattern(keyword)
    strongest: str | None = None
    saw_any = False
    for match in pat.finditer(text):
        saw_any = True
        if _mention_is_negated(_sentence_at(text, match.start(), match.end()), match):
            continue
        stance = _stance_of_match(text, match)
        if strongest is None or STANCE_RANK[stance] > STANCE_RANK[strongest]:
            strongest = stance
    if strongest is not None:
        return strongest
    return "mentioned" if saw_any else "absent"


def affirmative_stance(text: str, pat: re.Pattern[str]) -> str | None:
    """Strongest stance among hits that are not negated; None if there is no real hit."""
    strongest: str | None = None
    for match in pat.finditer(text):
        if _mention_is_negated(_sentence_at(text, match.start(), match.end()), match):
            continue
        stance = _stance_of_match(text, match)
        if strongest is None or STANCE_RANK[stance] > STANCE_RANK[strongest]:
            strongest = stance
    return strongest


def cert_stances(text: str) -> dict[str, str]:
    """{cert label: required|preferred|equivalent|mentioned} for certs asked about in `text`.

    Uses the same negation check as the exclude box, so "no Security+ required"
    is not counted.
    """
    if not text:
        return {}
    out: dict[str, str] = {}
    for label, pat in CERT_PATTERNS:
        stance = affirmative_stance(text, pat)
        if stance:
            out[label] = stance
    return out


STANCE_LABELS = {
    "required": "Required",
    "preferred": "Preferred / a plus",
    "equivalent": "Or equivalent",
    "mentioned": "Mentioned only",
    "absent": "Not in description",
}

# --- fit score -------------------------------------------------------------

CLEARANCE_RANK = {
    "unspecified": 0,
    "none": 0,
    "public_trust": 1,
    "confidential": 2,
    "secret": 3,
    "ts": 4,
    "sci": 5,
}

_KEYWORD_STANCE_POINTS = {"required": 10, "preferred": 4, "equivalent": 2, "mentioned": 0}


def _fmt_pts(value: float) -> str:
    text = f"{value:+.1f}"
    return text[:-2] if text.endswith(".0") else text


def _fmt_years(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def score_job(
    job: dict[str, Any],
    keyword: str = "",
    profile: dict[str, Any] | None = None,
) -> tuple[float, list[tuple[str, float]]]:
    """Fit score and the points behind it.

    Everything that does not depend on you (remote, pay, freshness, title match)
    is always scored. Years, clearance, and certs only count once a profile is
    saved, and "required" beats "preferred" wherever the posting makes the call.
    """
    parts: list[tuple[str, float]] = []

    def add(label: str, points: float) -> None:
        if points:
            parts.append((label, round(points, 1)))

    pref = (profile or {}).get("remote_pref") or ""
    if job.get("is_remote"):
        add("Remote", {"remote": 25, "any": 10, "onsite": 0}.get(pref, 25))
    elif pref == "onsite":
        add("On-site", 10)

    pay = job.get("salary_annual_min")
    if pay:
        try:
            points = min(float(pay) / 5000.0, 40)
            if (job.get("salary_source") or "") == "estimate":
                points *= 0.6
                add("Pay (Indeed estimate, 60%)", points)
            else:
                add("Pay", points)
        except (TypeError, ValueError):
            pass

    posted = job.get("date_posted")
    if posted:
        try:
            posted_date = datetime.fromisoformat(str(posted)[:10]).date()
            days = (datetime.now(timezone.utc).date() - posted_date).days
            add(f"Posted {max(days, 0)}d ago", max(0, 30 - days))
        except (TypeError, ValueError):
            pass

    title = str(job.get("title") or "")
    if keyword and keyword_pattern(keyword).search(title):
        add("Keyword in title", 20)
    stance = job.get("kw_stance")
    if keyword and stance:
        add(f"{keyword} {stance}", _KEYWORD_STANCE_POINTS.get(stance, 0))
    if (job.get("status") or "") == "new":
        add("New", 5)

    if profile:
        have = profile.get("years")
        need = job.get("years_min")
        if have is not None and need is not None:
            try:
                have_f, need_f = float(have), float(need)
            except (TypeError, ValueError):
                have_f = need_f = None
            if have_f is not None:
                if need_f <= have_f:
                    add(f"Needs {_fmt_years(need_f)} yrs, you have {_fmt_years(have_f)}", 8)
                else:
                    gap = need_f - have_f
                    add(
                        f"Needs {_fmt_years(need_f)} yrs, you have {_fmt_years(have_f)}",
                        -min(gap * 6, 30),
                    )
        pref_years = job.get("years_pref")
        if have is not None and pref_years is not None and need is None:
            try:
                if float(have) >= float(pref_years):
                    add(f"Prefers {_fmt_years(pref_years)} yrs (you meet it)", 2)
            except (TypeError, ValueError):
                pass

        mine = CLEARANCE_RANK.get(profile.get("clearance") or "none", 0)
        theirs = CLEARANCE_RANK.get(job.get("clearance") or "unspecified", 0)
        label = CLEARANCE_LABELS.get(job.get("clearance") or "", "")
        if theirs > mine:
            add(f"{label} clearance you lack", -min(10 * (theirs - mine), 30))
        elif theirs > 0:
            add(f"{label} clearance you hold", 5)

        held = {c.lower() for c in profile.get("certs") or []}
        stances = job.get("cert_stances") or {}
        if isinstance(stances, str):
            try:
                import json

                stances = json.loads(stances) if stances else {}
            except ValueError:
                stances = {}
        cert_points = 0.0
        notes: list[str] = []
        for cert, how in stances.items():
            if keyword and cert.lower() == keyword.lower():
                continue
            if cert.lower() in held:
                gain = {"required": 4, "preferred": 2}.get(how, 1)
                cert_points += gain
                notes.append(f"{cert} {how}")
            elif how == "required":
                cert_points -= 4
                notes.append(f"{cert} required, you lack it")
        if cert_points:
            add("Certs: " + ", ".join(notes), max(min(cert_points, 10), -12))

    return round(sum(points for _label, points in parts), 1), parts


def score_tooltip(parts: list[tuple[str, float]]) -> str:
    if not parts:
        return "No score factors apply"
    return "\n".join(f"{_fmt_pts(points)}  {label}" for label, points in parts)


def job_score(job: dict[str, Any], keyword: str = "", profile: dict[str, Any] | None = None) -> float:
    return score_job(job, keyword, profile)[0]


def heat_tier(score: float) -> str:
    if score >= 75:
        return "hot"
    if score >= 50:
        return "warm"
    if score >= 25:
        return "mild"
    return "cool"
