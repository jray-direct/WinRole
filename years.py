"""Pull "years of experience" out of a job description.

Three numbers come out of a posting:

* ``years_min``  - the required minimum ("3+ years", "minimum of 3 years").
* ``years_max``  - an optional ceiling ("1-5 years", "up to 2 years").
* ``years_pref`` - a preferred minimum ("5+ years preferred", anything under a
  "Preferred / Nice to have" heading).

A posting that never states a number gets three NULLs. A mention only counts
when it sits in an experience context, so "401k vests after 3 years", "valid for
3 years" and "30 years in business" do not turn into requirements. Months,
"a decade", "10-plus", "a year of", and a parenthetical "(3-5 years)" in a title
count. "No less than N" is a floor. "Not required" is not a floor.

Postings that state several numbers are the hard case. Each mention is
classified on its own (required / preferred / max) from the words right around
it, then from the section heading above it. Mentions in the same sentence that
are alternatives ("BS + 5 years, or MS + 3 years") collapse to the lowest.
Otherwise the strictest required number wins, and its own upper bound goes with
it. ``parse_years(..., evidence=True)`` returns every mention for review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
}
_NUM = r"(?:\d{1,2}(?:\.5)?|" + "|".join(_WORDS) + r")"
_PAREN = r"(?:\s*\(\s*\d{1,2}\s*\))?"
_YEARS = r"(?:years?|yrs?\b\.?|yoe\b)(?![A-Za-z])"
_START = r"(?<![\w.,$/#%-])"

_RANGE = re.compile(
    rf"{_START}(?P<lo>{_NUM}){_PAREN}\s*(?P<lo_plus>\+)?\s*"
    rf"(?:-|to|or|and)\s*(?P<hi>{_NUM}){_PAREN}\s*(?P<hi_plus>\+)?\s*[- ]?\s*{_YEARS}",
    re.I,
)
_SINGLE = re.compile(
    rf"{_START}(?P<n>{_NUM}){_PAREN}\s*(?P<plus>\+|plus\b|or\s+(?:more|greater|higher)\b)?\s*[- ]?\s*{_YEARS}",
    re.I,
)
# "Experience: 5+" / "Years: 5+" / "YOE: 5" states a number and never repeats "years".
_BARE_EXP = re.compile(
    rf"\b(?:experience|exp|years?|yrs?|yoe)\b\s*[:\-]\s*(?P<n>{_NUM})\s*(?P<plus>\+)?(?!\s*{_YEARS})",
    re.I,
)
_MONTH_NUM = r"(?:\d{1,3}|" + "|".join(_WORDS) + r")"
_MONTH_RANGE = re.compile(
    rf"{_START}(?P<lo>{_MONTH_NUM})\s*(?:-|to)\s*(?P<hi>{_MONTH_NUM})\s*(?P<plus>\+)?\s*months?\b",
    re.I,
)
_MONTH_SINGLE = re.compile(
    rf"{_START}(?P<n>{_MONTH_NUM})\s*(?P<plus>\+|plus\b|or\s+(?:more|greater|higher)\b)?\s*[- ]?\s*months?\b",
    re.I,
)
# "52 weeks of specialized experience" is the federal way of writing one year.
_WEEKS = re.compile(
    rf"{_START}(?P<n>\d{{1,3}})\s*(?P<plus>\+)?\s*weeks?\s+of\s+(?:specialized\s+)?(?:progressive\s+)?experience\b",
    re.I,
)

_EXPERIENCE = re.compile(
    r"experienc|\bexp\b|\byoe\b|background|\bwork(?:ing|ed)?\b|hands[- ]on|expertise|"
    r"proven|track record|professional|proficien|tenure|practical|in the field|"
    r"(?:related|relevant) (?:field|role|position|industry)|"
    r"\b(?:candidat\w*|canidat\w*)\b",
    re.I,
)
# "5 years supporting Windows", "3+ years in a lab", "2 years with Splunk"
_EXPERIENCE_RIGHT_AFTER = re.compile(
    r"^\W*(?:of\s+\w|in\s+(?:an?\s+|the\s+)?\w|as\s+an?\s|with\s+\w|using\b|"
    r"(?:supporting|managing|administering|building|designing|developing|"
    r"implementing|engineering|maintaining|operating|deploying|configuring|"
    r"troubleshooting|programming|leading|performing|creating|writing)\b)",
    re.I,
)
_QUAL_WORDS = re.compile(
    r"\b(?:require[ds]?|requirements?|qualif\w*|minimum|must|preferred|ideal(?:ly)?|"
    r"candidate|applicants?|you|your|need(?:s|ed)?|"
    r"we(?:'re| are) (?:looking|seeking)|seeking)\b",
    re.I,
)
_NOT_EXPERIENCE_AFTER = re.compile(
    r"^\W*(?:old\b|period\b|of age|ago\b|from now|"
    r"(?:degree|diploma|college\b|university\b|bachelor|associate'?s?\s+degree|accredited|institution)|"
    r"of service(?=\s*(?:[.,;)!]|$|award|anniversary|bonus|and\b|for\b|with\b|at\b))|"
    r"of (?:age|history|operation|u\.?s\.?\s+resid|residen|tenure|vesting|membership|existence)|"
    r"in business|in operation|warrant|guarantee|lease|term\b|contract\b|"
    r"retention|maturity|apart|later\b|after\b|since\b)",
    re.I,
)
_NOT_EXPERIENCE_BEFORE = re.compile(
    r"(?:(?:last|past|previous|prior|next|every|each|after|within|during|valid for|"
    r"expires? (?:in|after)|renew\w* (?:every|in)|lived|resid\w+|for the (?:last|past))\s*)$",
    re.I,
)
_COMPANY_HISTORY = re.compile(
    r"\b(?:we(?:'ve| have)|we are|our (?:company|firm|team|history|story|mission)|"
    r"the company|founded|established|since (?:19|20)\d\d|"
    r"leader in|leading provider|family[- ]owned)\b",
    re.I,
)
# "... is a veteran-owned small business with more than 25 years of experience ..."
_COMPANY_LOOSE = re.compile(
    r"\b(?:business|corporation|firm|integrator|provider|trusted|partners?|company|"
    r"organization|agency|inc|llc|ltd|group|leader|has been|have been|history|legacy)\b",
    re.I,
)
# "we build" / "brings over 20 years" — company voice, only together with "over" / "more than".
_COMPANY_VOICE = re.compile(r"\b(?:we|our)\b|\bbrings?\b|\bcelebrat\w*", re.I)
_HISTORY_LEAD = re.compile(
    r"(?:\bover|\bmore than|\bnearly|\balmost|\bspanning|\bboasting|\bhaving|^with|^for)\s*$", re.I
)
# "Required 6 Years", "minimum 3 years": a marker straight before the number.
_MARKER_BEFORE = re.compile(r"\b(?:required?|minimum|min\.?|at least|must have)\s*[:(]?\s*$", re.I)

_PREF_WORDS = re.compile(
    r"\b(?:preferred|prefer(?:red)?|a plus|nice[- ]to[- ]have|desired|desirable|"
    r"ideal(?:ly)?|bonus|advantage|beneficial|favou?rable|helpful|extra credit|"
    r"good to have|not required but)",
    re.I,
)
_REQ_WORDS = re.compile(
    r"\b(?:required?|requires?|must|minimum|min\.?|at least|mandatory|need(?:ed|s)?|"
    r"necessary|essential|basic qualification)\b",
    re.I,
)
_MAX_BEFORE = re.compile(
    r"(?:up to|no more than|not more than|at most|maximum(?: of)?|max\.?(?: of)?|"
    r"less than|fewer than|under|not exceed(?:ing)?|capped at)\s*$",
    re.I,
)
# "no less than 3" contains "less than", but it is a floor, not a ceiling.
_MIN_FLOOR = re.compile(r"(?:no|not)\s+(?:less|fewer)\s+than\s*$", re.I)
_MAX_AFTER = re.compile(r"^\W*(?:or\s+(?:less|fewer)|and\s+under|maximum|max\b)", re.I)

_PREF_HEAD = re.compile(
    r"\b(?:preferred|nice[- ]to[- ]have|desired|desirable|bonus|good to have|a plus|"
    r"plus(?:es)?|additional (?:skills|qualifications)|ideal (?:candidate|qualifications)|"
    r"extra credit|set you apart|stand out|bonus points|advantages?)\b",
    re.I,
)
_REQ_HEAD = re.compile(
    r"\b(?:required|requirements?|minimum|basic qualifications?|qualifications?|"
    r"must[- ]haves?|what you(?:'ll|’ll| will)? (?:need|bring|have)|"
    r"what we(?:'re|’re| are) looking for|about you|who you are|skills|"
    r"your (?:background|experience|profile)|you have|you bring|education|experience)\b",
    re.I,
)
_SKIP_HEAD = re.compile(
    r"\b(?:about (?:us|the company|our company)|who we are|our (?:story|mission|history|company)|"
    r"company overview|benefits|perks|compensation|equal opportunity|eeo)\b",
    re.I,
)
_DEGREE = re.compile(
    r"\b(?:bachelor|master|ph\.?d|doctorate|associate|b\.?s\.?|m\.?s\.?|degree|diploma|"
    r"high school|ged)\b",
    re.I,
)
_SUBSTITUTE = re.compile(
    r"\bor\s+(?:an?\s+)?equivalent\b|\bequivalent\s+(?:work\s+|related\s+)?experience\b|"
    r"\bin\s+lieu\s+of\b|\bwithout\s+(?:an?\s+)?(?:bachelor|degree|college)|\bno\s+degree\b|"
    r"\bif\s+(?:you\s+)?(?:have|hold|possess)?\s*(?:an?\s+)?(?:associate|high school|ged|no\s+degree)",
    re.I,
)
# "An additional four (4) years of experience may be considered in place of the degree"
_CONSIDERED_AFTER = re.compile(
    r"^[^.;]{0,60}\bmay\s+(?:be\s+)?(?:considered|substituted|accepted)\b|"
    r"^[^.;]{0,60}\bcan\s+(?:be\s+)?substitut", re.I
)
# "Degree or foreign equivalent required. Will also consider three years of ..."
_CONSIDER_BEFORE = re.compile(r"\b(?:will\s+)?(?:also\s+)?consider(?:\s+an?)?\s*$|\bin\s+place\s+of\s*$", re.I)
# "Bachelor's degree, or 4 years of experience": the years replace the degree.
_SUBSTITUTE_BEFORE = re.compile(
    r"\b(?:degree|diploma|bachelor'?s?|master'?s?|ph\.?d|associate'?s?|ged)\b[^.;]{0,30}"
    r"\bor\s+(?:an?\s+)?(?:additional\s+)?(?:minimum\s+of\s+|at\s+least\s+)?$",
    re.I,
)
_DEGREE_LEVEL = re.compile(
    r"\b(?:bachelor'?s?|master'?s?|ph\.?d\.?|doctorate|associate'?s?|high school|ged|"
    r"b\.?s\.?|m\.?s\.?|b\.?a\.?|m\.?a\.?)(?![\w])",
    re.I,
)
_NOT_REQUIRED = re.compile(
    r"\bnot\s+required\b|\bnot\s+a\s+requirement\b|"
    r"\b(?:does|do)\s+not\s+require\b|\b(?:doesn't|don't|isn’t|isn't)\s+require\b|"
    r"\bis\s+not\s+required\b",
    re.I,
)
# A title parenthetical that is only the years: "Software Engineer (3-5 years)".
_PAREN_REJECT = re.compile(
    r"\b(?:warrant\w*|contracts?|vest\w*|leases?|renew\w*|valid|expires?|anniversary|"
    r"sabbatical|pto|vacation|tuition|residen\w*|sponsor\w*)\b",
    re.I,
)

_ALT_BEFORE = re.compile(
    r"\b(?:or|alternatively|in lieu of)\s+(?:an?\s+|the\s+)?"
    r"(?:(?:[\w'.]+\s+){0,3}(?:degree|diploma|bachelor\S*|master\S*|ph\.?d\.?|associate\S*|"
    r"b\.?s\.?|m\.?s\.?|ged)\S*\s+)?"
    r"(?:with\s+|and\s+|plus\s+|\+\s*|,\s*)?(?:an?\s+|additional\s+|minimum(?:\s+of)?\s+|at\s+least\s+)*$",
    re.I,
)

_MAX_REASONABLE = 30


@dataclass
class Mention:
    kind: str  # required | preferred | max
    lo: float | None
    hi: float | None
    unit: int
    text: str
    start: int = 0
    # "degree or equivalent (12 years)": experience that stands in for a degree.
    substitute: bool = False


@dataclass
class YearsInfo:
    years_min: float | None = None
    years_max: float | None = None
    years_pref: float | None = None
    mentions: list[Mention] = field(default_factory=list)

    def as_row(self) -> dict[str, float | None]:
        return {
            "years_min": self.years_min,
            "years_max": self.years_max,
            "years_pref": self.years_pref,
        }


def _num(raw: str) -> float:
    raw = raw.lower()
    return float(_WORDS[raw]) if raw in _WORDS else float(raw)


def _normalise(text: str) -> str:
    text = (
        text.replace("\u00a0", " ")
        .replace("\u2011", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2019", "'")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    # An en dash that lost its encoding ("3\ufffd5 years") is still a range.
    text = re.sub(r"(?<=\d)\s*\ufffd\s*(?=\d)", "-", text)
    text = re.sub(r"\\([+\-.()&!#*_])", r"\1", text)
    text = re.sub(r"[*_`]+", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    # "3+\nyears" (PDF-style wrapping) is one phrase.
    text = re.sub(r"(\d\+?)[ \t]*\n[ \t]*(?=(?:years?|yrs?)\b)", r"\1 ", text, flags=re.I)
    # "y.o.e." / "Y.O.E" and "10-plus years" / "five-plus years".
    text = re.sub(r"\by\s*\.\s*o\s*\.\s*e\s*\.?(?!\w)", "YOE", text, flags=re.I)
    text = re.sub(rf"\b({_NUM})\s*-\s*plus\b", r"\1+", text, flags=re.I)
    text = re.sub(r"\bhalf\s+a\s+decade\b", "5 years", text, flags=re.I)
    text = re.sub(
        r"\b((?:over|more than|nearly|almost)\s+)(?:a|one)\s+decade\b",
        r"\1 10 years",
        text,
        flags=re.I,
    )
    text = re.sub(r"\b(?:a|one)\s+decade\b", "10 years", text, flags=re.I)
    text = re.sub(r"\btwenty[\s-]+five\b", "25", text, flags=re.I)
    text = re.sub(r"(\d)\s*(?:½|1/2)\b", r"\1.5", text)
    text = re.sub(
        rf"\b({_NUM})\s+and\s+(?:a|one)\s+half\b",
        lambda m: str(_num(m.group(1)) + 0.5),
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\ba\s+year\b(?=\s+of\s+(?!age\b|service\b))",
        "1 year",
        text,
        flags=re.I,
    )
    text = re.sub(r"(?<=\d)\s*[~～]\s*(?=\d)", "-", text)
    text = re.sub(r"(?<=\d)\s*-\s*to\s*-\s*(?=\d)", "-", text, flags=re.I)
    return text


_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")


def _units(text: str) -> list[tuple[str, str | None]]:
    """(unit_text, section) pairs: one per bullet/sentence, tagged with the heading above."""
    out: list[tuple[str, str | None]] = []
    section: str | None = None
    pending_or = False
    for raw in text.split("\n"):
        line = raw.strip(" -•·▪◦\t")
        if not line:
            continue
        label, rest = _split_label(line)
        if label is not None:
            mode = _heading_mode(label)
            if re.search(r"\bnot\s+required\b", label, re.I):
                # "not required: 5+ years" is a heading the required-word list would misread.
                mode = "neutral"
                rest = f"not required {rest}".strip()
            if not rest:
                section = None if mode == "neutral" else mode
                continue
            # "Preferred: 5 years ..." tags just this line; keep the section for the next.
            out.extend((piece, mode) for piece in _SENTENCE_BREAK.split(rest) if piece.strip())
            continue
        words = line.split()
        if (
            len(words) <= 8
            and not line.endswith((".", ",", ";"))
            and not re.search(r"\d|\b(?:years?|yrs?)\b", line, re.I)
        ):
            mode = _heading_mode(line)
            if mode != "?":
                section = mode if mode != "neutral" else None
                continue
        if re.match(r"(?i)^or\b", line) and out:
            prev, prev_section = out[-1]
            out[-1] = (f"{prev} {line}", prev_section)
            continue
        if pending_or and out:
            prev, prev_section = out[-1]
            out[-1] = (f"{prev} {line}", prev_section)
            pending_or = False
            continue
        pending_or = bool(re.search(r"(?i)\bor\s*$", line))
        for piece in _SENTENCE_BREAK.split(line):
            if piece.strip():
                out.append((piece, section))
    return out


_LABEL = re.compile(r"^([A-Za-z][A-Za-z '’/&,()-]{2,60}?)\s*:\s*(.*)$")


def _split_label(line: str) -> tuple[str | None, str]:
    m = _LABEL.match(line)
    if not m:
        return None, line
    label, rest = m.group(1), m.group(2).strip()
    if len(label.split()) > 6 or _heading_mode(label) == "?":
        return None, line
    return label, rest


def _heading_mode(label: str) -> str:
    if _SKIP_HEAD.search(label):
        return "skip"
    if _PREF_HEAD.search(label):
        return "preferred"
    if _REQ_HEAD.search(label):
        return "required"
    if re.search(
        r"(?i)\b(?:responsibilit|duties|what you(?:'ll| will) do|the role|overview|"
        r"summary|description|how to apply)",
        label,
    ):
        return "neutral"
    return "?"


def _classify(
    unit: str, start: int, end: int, section: str | None, prev_end: int = 0, next_start: int | None = None
) -> str | None:
    """required | preferred | None (not a candidate requirement).

    A marker only speaks for this mention when no other year mention sits between
    them: in "3+ years required, 5+ years preferred" the word "required" belongs to the 3.
    """
    left = unit[max(0, start - 70, prev_end) : start]
    right_stop = end + 70 if next_start is None else min(end + 70, next_start)
    right = unit[end:right_stop]
    # ...and only within the same sentence/clause.
    cut = max(left.rfind(ch) for ch in ".!?;")
    left = left[cut + 1 :]
    stops = [i for i in (right.find(ch) for ch in ".!?;") if i != -1]
    right = right[: min(stops)] if stops else right
    best: tuple[int, str] | None = None
    for rx, kind in ((_PREF_WORDS, "preferred"), (_REQ_WORDS, "required")):
        for m in rx.finditer(left):
            dist = len(left) - m.end()
            if best is None or dist < best[0] or (dist == best[0] and kind == "required"):
                best = (dist, kind)
        for m in rx.finditer(right):
            dist = m.start()
            if best is None or dist < best[0] or (dist == best[0] and kind == "required"):
                best = (dist, kind)
    if best is not None:
        return best[1]
    # "skip" (About us / Benefits) headings are sticky and a posting can be nothing
    # but an About-us block, so they only add doubt; company-history lines are
    # already rejected on their own wording.
    if section == "preferred":
        return "preferred"
    return "required"


def _find_mentions(unit: str, unit_index: int, section: str | None) -> list[Mention]:
    found: list[Mention] = []
    taken: list[tuple[int, int]] = []
    candidates: list[tuple[int, int, float, float | None, bool]] = []
    for m in _RANGE.finditer(unit):
        lo, hi = _num(m.group("lo")), _num(m.group("hi"))
        if hi < lo:
            continue
        # "3 or 4 years" / "3 and 5 years" are ranges; "2 and 3 years of X" too.
        candidates.append((m.start(), m.end(), lo, hi, bool(m.group("hi_plus") or m.group("lo_plus"))))
    for start, end, *_ in candidates:
        taken.append((start, end))
    for m in _SINGLE.finditer(unit):
        if any(s <= m.start() < e for s, e in taken):
            continue
        candidates.append((m.start(), m.end(), _num(m.group("n")), None, bool(m.group("plus"))))
        taken.append((m.start(), m.end()))
    for m in _BARE_EXP.finditer(unit):
        if any(s <= m.start() < e for s, e in taken):
            continue
        candidates.append((m.start("n"), m.end(), _num(m.group("n")), None, bool(m.group("plus"))))
    # "Experience: 5+" is split into a heading and a line that is only the number.
    if section in ("required", "preferred"):
        bare = re.fullmatch(rf"\s*(?P<n>{_NUM})\s*(?P<plus>\+)\s*", unit)
        if bare and not any(s <= bare.start("n") < e for s, e in taken):
            candidates.append(
                (bare.start("n"), bare.end(), _num(bare.group("n")), None, bool(bare.group("plus")))
            )

    def _taken(start: int) -> bool:
        return any(s <= start < e for s, e in taken)

    def _as_years(months: float) -> float:
        return round(months / 12.0, 1)

    for m in _MONTH_RANGE.finditer(unit):
        if _taken(m.start()):
            continue
        lo_m, hi_m = _num(m.group("lo")), _num(m.group("hi"))
        if hi_m < lo_m or hi_m > 240:
            continue
        candidates.append((m.start(), m.end(), _as_years(lo_m), _as_years(hi_m), bool(m.group("plus"))))
        taken.append((m.start(), m.end()))
    for m in _MONTH_SINGLE.finditer(unit):
        if _taken(m.start()):
            continue
        months = _num(m.group("n"))
        if months < 1 or months > 240:
            continue
        candidates.append((m.start(), m.end(), _as_years(months), None, bool(m.group("plus"))))
        taken.append((m.start(), m.end()))
    for m in _WEEKS.finditer(unit):
        if _taken(m.start()):
            continue
        weeks = float(m.group("n"))
        if weeks < 4 or weeks > 52 * 30:
            continue
        candidates.append((m.start(), m.end(), round(weeks / 52.0, 1), None, bool(m.group("plus"))))
        taken.append((m.start(), m.end()))
    candidates.sort()

    for idx, (start, end, lo, hi, plus) in enumerate(candidates):
        prev_end = candidates[idx - 1][1] if idx else 0
        next_start = candidates[idx + 1][0] if idx + 1 < len(candidates) else None
        if lo > _MAX_REASONABLE or (hi is not None and hi > _MAX_REASONABLE):
            continue
        before = unit[max(0, start - 40) : start]
        after = unit[end : end + 60]
        if _NOT_EXPERIENCE_AFTER.search(after) or _NOT_EXPERIENCE_BEFORE.search(before):
            continue
        near = unit[max(0, start - 90) : end + 90]
        in_requirement_block = section in ("required", "preferred")
        small = lo <= 15 and (hi is None or hi <= 15)
        wrapped = before.rstrip().endswith("(") and after.lstrip().startswith(")")
        if wrapped and _PAREN_REJECT.search(before[-50:]):
            wrapped = False
        if not (
            _EXPERIENCE.search(near)
            or _EXPERIENCE_RIGHT_AFTER.search(unit[end : end + 40])
            or in_requirement_block
            # a bullet that opens with the number, "N+ years", or "Required N years"
            or (small and (start <= 3 or plus or _MARKER_BEFORE.search(before)))
            # "Software Engineer (3-5 years)" — the parenthetical is the requirement.
            or wrapped
        ):
            continue
        if _NOT_REQUIRED.search(unit[max(0, start - 50) : end + 50]) and not _PREF_WORDS.search(
            unit[max(0, start - 50) : end + 50]
        ):
            continue
        if not _QUAL_WORDS.search(unit):
            if _COMPANY_HISTORY.search(unit) or re.search(r"\bcelebrat", unit, re.I):
                continue
            # "over 10 years of experience" is a requirement. The same lead is the
            # company's age when the sentence is about us ("we build", "brings").
            if _HISTORY_LEAD.search(before) and (
                _COMPANY_LOOSE.search(unit) or _COMPANY_VOICE.search(unit)
            ):
                continue
        text = unit[max(0, start - 30) : end + 40].strip()
        if not _MIN_FLOOR.search(before) and (_MAX_BEFORE.search(before) or _MAX_AFTER.search(after)):
            found.append(Mention("max", None, hi if hi is not None else lo, unit_index, text, start))
            continue
        kind = _classify(unit, start, end, section, prev_end, next_start)
        if kind is None:
            continue
        window = unit[max(0, start - 130) : end + 60]
        substitute = bool(
            (_SUBSTITUTE.search(window) and _DEGREE.search(window))
            or _SUBSTITUTE_BEFORE.search(before)
            or _CONSIDER_BEFORE.search(before)
            or _CONSIDERED_AFTER.search(after)
        )
        found.append(Mention(kind, lo, hi, unit_index, text, start, substitute))
    return found


def _collapse_alternatives(mentions: list[Mention], units: list[tuple[str, str | None]]) -> list[Mention]:
    """Mentions of one sentence that are either/or choices count once, at the lowest value."""
    out: list[Mention] = []
    by_unit: dict[int, list[Mention]] = {}
    for m in mentions:
        by_unit.setdefault(m.unit, []).append(m)
    for idx, group in by_unit.items():
        req = [m for m in group if m.kind == "required"]
        rest = [m for m in group if m.kind != "required"]
        out.extend(rest)
        if len(req) >= 2:
            unit = units[idx][0]
            lows = {m.lo for m in req}
            # "5 years of X, or 3 years of Y" / "BS with 5 years, or MS with 3 years":
            # the "or" must sit right in front of the next number, not just somewhere
            # in a list of skills ("... networking or DevOps, with at least 2 years").
            gap_has_or = any(
                _ALT_BEFORE.search(unit[max(a.start, b.start - 70) : b.start])
                for a, b in zip(req, req[1:])
            )
            levels = {d.lower().rstrip("'s") for d in _DEGREE_LEVEL.findall(unit)}
            if len(lows) > 1 and (gap_has_or or len(levels) >= 2):
                out.append(min(req, key=lambda m: m.lo or 0))
                continue
        out.extend(req)
    return out


def parse_years(text: str | None, *, evidence: bool = False) -> YearsInfo:
    info = YearsInfo()
    if not text or not str(text).strip():
        return info
    units = _units(_normalise(str(text)))
    mentions: list[Mention] = []
    for i, (unit, section) in enumerate(units):
        mentions.extend(_find_mentions(unit, i, section))
    mentions = _collapse_alternatives(mentions, units)

    required = [m for m in mentions if m.kind == "required" and not m.substitute]
    preferred = [m for m in mentions if m.kind == "preferred" and not m.substitute]
    maxes = [m.hi for m in mentions if m.kind == "max" and m.hi is not None]

    if required:
        top = max(required, key=lambda m: (m.lo or 0, m.hi or 0))
        info.years_min = top.lo
        info.years_max = top.hi
    if maxes:
        ceiling = min(maxes)
        if info.years_min is None:
            info.years_max = ceiling
        elif info.years_max is None and ceiling >= info.years_min:
            info.years_max = ceiling
    if preferred:
        info.years_pref = max(m.lo or 0 for m in preferred)
    if info.years_max is not None and info.years_min is not None and info.years_max <= info.years_min:
        info.years_max = None
    if evidence:
        info.mentions = mentions
    return info


def _fmt(value: float | None) -> str:
    if value is None:
        return ""
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def years_chips(
    years_min: Any = None, years_max: Any = None, years_pref: Any = None
) -> list[dict[str, str]]:
    """Chip text for a job row: '3+ req', '1–5', '5+ preferred'."""

    def val(x: Any) -> float | None:
        try:
            return None if x is None or x == "" else float(x)
        except (TypeError, ValueError):
            return None

    lo, hi, pref = val(years_min), val(years_max), val(years_pref)
    chips: list[dict[str, str]] = []
    if lo is not None and hi is not None and hi > lo:
        chips.append({"text": f"{_fmt(lo)}–{_fmt(hi)}", "kind": "range",
                      "title": f"Required {_fmt(lo)} to {_fmt(hi)} years of experience"})
    elif lo is not None and lo > 0:
        chips.append({"text": f"{_fmt(lo)}+ req", "kind": "req",
                      "title": f"Requires at least {_fmt(lo)} years of experience"})
    elif lo is None and hi is not None:
        chips.append({"text": f"≤{_fmt(hi)}", "kind": "range",
                      "title": f"Mentions up to {_fmt(hi)} years of experience"})
    if pref is not None and pref > 0:
        chips.append({"text": f"{_fmt(pref)}+ preferred", "kind": "pref",
                      "title": f"Prefers {_fmt(pref)}+ years of experience"})
    return chips
