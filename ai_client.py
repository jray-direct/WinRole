"""Light and medium model calls for Win Role.

AI_MODEL is the fallback. AI_LIGHT_MODEL and AI_MEDIUM_MODEL select the
tier. Answers are cached by task, model, and input hash. A missing key or a
failed call returns nothing. Text that contains a banned phrase is rejected.
A payload that tries to set years, a degree level, a certification, a
required-skill hit, or a point value is dropped.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Iterable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

Transport = Callable[[dict[str, Any]], str]

# Tests set this to avoid the network. Production leaves it empty.
_transport: Transport | None = None

_PARSER_KEYS = frozenset({
    "years",
    "experience_years",
    "years_min",
    "years_max",
    "years_pref",
    "degree_level",
    "certification",
    "certifications",
    "cert",
    "certs",
    "required_hit",
    "required_skill",
    "required_skills",
    "point",
    "points",
    "point_value",
    "score",
    "heat_score",
})

_SECTION_NAMES = frozenset({"experience", "skills", "education", "projects"})
_STANCES = frozenset({"required", "preferred", "mentioned"})

_SCORE_CLAIM = re.compile(
    r"\b(?:heat\s*)?score\s*(?:of|:|is|=)?\s*\d{1,3}\b"
    r"|\b\d{1,3}\s*/\s*100\b"
    r"|\b\d{1,3}\s+out\s+of\s+100\b",
    re.I,
)
_YEAR_TOKEN = re.compile(r"\b(?:19|20)\d{2}\b")
_YEARS_PHRASE = re.compile(r"\b(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b", re.I)
_DEGREE_WORD = re.compile(
    r"\b(?:associate(?:'s)?|bachelor(?:'s)?|master(?:'s)?|doctorate|ph\.?\s*d\.?|mba|b\.s\.|m\.s\.|b\.a\.|m\.a\.)\b",
    re.I,
)
_EMPLOYER = re.compile(
    r"\b([A-Z][A-Za-z0-9&'.-]+(?:\s+[A-Z][A-Za-z0-9&'.-]+){0,3})"
    r"\s+(?:Inc\.?|LLC|L\.L\.C\.|Corp\.?|Corporation|Ltd\.?|Company)\b"
)
_WORKED_AT = re.compile(
    r"\b(?:at|for|with)\s+([A-Z][A-Za-z0-9&'.-]+(?:\s+[A-Z][A-Za-z0-9&'.-]+){0,2})"
)
_EMPLOYER_SKIP = frozenset({
    "hiring manager",
    "hiring team",
    "dear hiring",
    "best regards",
    "thank you",
    "united states",
    "the role",
    "this role",
    "the team",
    "the company",
})
_GAP_CLAIM = re.compile(
    r"you(?:'ve| have| had)?\s+(?:already\s+)?(?:used|built|worked with|know|known|mastered)"
    r"|your\s+(?:experience|background|expertise|skill)\w*\s+(?:with|in)"
    r"|you\s+already\s+(?:have|know)"
    r"|as\s+someone\s+who\s+(?:knows|uses|has)",
    re.I,
)
_GAP_DENY = re.compile(
    r"you\s+(?:do\s+not|don't|dont)\s+have|not\s+on\s+your|you\s+have\s+not|which\s+you\s+lack|you\s+lack",
    re.I,
)


def ai_settings() -> dict[str, Any]:
    key = (os.environ.get("AI_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    base = (os.environ.get("AI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    fallback = (os.environ.get("AI_MODEL") or "gpt-4o-mini").strip() or "gpt-4o-mini"
    light = (os.environ.get("AI_LIGHT_MODEL") or fallback).strip() or fallback
    medium = (os.environ.get("AI_MEDIUM_MODEL") or fallback).strip() or fallback
    return {
        "key": key,
        "base": base,
        "model": fallback,
        "light": light,
        "medium": medium,
        "ready": bool(key),
    }


def model_name(tier: str) -> str:
    cfg = ai_settings()
    if tier == "medium":
        return cfg["medium"]
    if tier == "light":
        return cfg["light"]
    return cfg["model"]


def contains_banned_phrase(text: str, phrases: Iterable[str]) -> bool:
    """True when text contains any banned phrase, ignoring case."""
    folded = (text or "").casefold()
    for phrase in phrases:
        needle = (phrase or "").strip().casefold()
        if needle and needle in folded:
            return True
    return False


def reject_banned(text: str | None, phrases: Iterable[str]) -> str | None:
    """Return the text, or nothing when it contains a banned phrase."""
    if text is None:
        return None
    if contains_banned_phrase(text, phrases):
        return None
    return text


def sets_parser_fields(payload: Any) -> bool:
    """True when the model tried to set years, degree, a cert, a required hit, or points."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).casefold() in _PARSER_KEYS:
                return True
            if sets_parser_fields(value):
                return True
        return False
    if isinstance(payload, list):
        return any(sets_parser_fields(item) for item in payload)
    return False


def _iter_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)


def _strings_banned(value: Any, phrases: Iterable[str]) -> bool:
    banned = [phrase for phrase in phrases if (phrase or "").strip()]
    if not banned:
        return False
    return any(contains_banned_phrase(text, banned) for text in _iter_strings(value))


def _parse_json_object(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _input_hash(*parts: str) -> str:
    blob = "\n---\n".join(parts)
    return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()


def _http_complete(model: str, system: str, user: str, timeout: float) -> str:
    cfg = ai_settings()
    if not cfg["ready"]:
        raise RuntimeError("Set AI_API_KEY to explain jobs and draft cover letters.")
    payload = {
        "model": model,
        "temperature": 0.2 if model == cfg["light"] else 0.4,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    req = Request(
        cfg["base"] + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {cfg['key']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"AI request failed ({exc.code}). {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"AI request failed. {exc.reason}") from exc
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("AI response did not include a message.") from exc
    return str(text).strip()


def _payload(model: str, system: str, user: str) -> dict[str, Any]:
    cfg = ai_settings()
    return {
        "model": model,
        "temperature": 0.2 if model == cfg["light"] else 0.4,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }


def ask_model(
    task: str,
    tier: str,
    user: str,
    *,
    system: str,
    banned: Iterable[str] = (),
    cache_extra: str = "",
    db_path: Any = None,
    transport: Transport | None = None,
    timeout: float | None = None,
    accept: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any] | None:
    """JSON answer, or nothing if there is no key, the call fails, or the text is rejected.

    The cache key is the task, the model name, and a hash of the input.
    Rejected text is not cached. A failed call does not change stored rows.
    """
    sender = transport or _transport
    cfg = ai_settings()
    if sender is None and not cfg["ready"]:
        return None
    model = model_name(tier)
    digest = _input_hash(system, user, cache_extra)
    banned_list = list(banned)

    def _ok(data: dict[str, Any] | None) -> dict[str, Any] | None:
        if not data or sets_parser_fields(data) or _strings_banned(data, banned_list):
            return None
        if accept is not None and not accept(data):
            return None
        return data

    from db import ai_cache_get, ai_cache_put

    cached = ai_cache_get(task, model, digest, db_path)
    if cached:
        hit = _ok(_parse_json_object(cached))
        if hit is not None:
            return hit
    try:
        if sender is not None:
            raw = sender(_payload(model, system, user))
        else:
            raw = _http_complete(model, system, user, timeout if timeout is not None else (45 if tier == "medium" else 20))
    except Exception:
        return None
    data = _ok(_parse_json_object(raw))
    if data is None:
        return None
    try:
        ai_cache_put(task, model, digest, json.dumps(data, ensure_ascii=False), db_path)
    except Exception:
        return data
    return data


def chat(system: str, user: str, timeout: float = 45) -> str:
    """Plain chat on the fallback model. Raises when the key is missing or the call fails."""
    return _http_complete(ai_settings()["model"], system, user, timeout)


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part.strip()]


def names_phrase(text: str, phrase: str) -> bool:
    if not text or not phrase:
        return False
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, re.I) is not None


def names_unlisted_skill(text: str, allowed: Iterable[str]) -> bool:
    """True when text names a catalog skill that is not in the allowed list."""
    from resume import CERTIFICATIONS, PROGRAMMING_LANGUAGES, TECHNICAL_SKILLS

    allowed_cf = {(item or "").casefold() for item in allowed if item}
    for label, pat in (*PROGRAMMING_LANGUAGES, *TECHNICAL_SKILLS, *CERTIFICATIONS):
        if not pat.search(text or ""):
            continue
        key = label.casefold()
        if key in allowed_cf:
            continue
        if any(key in item or item in key for item in allowed_cf):
            continue
        return True
    return False


def _fit_prompt(matched: list[str], missing: list[str], degree: str, years: str) -> str:
    return (
        f"Matched: {', '.join(matched) if matched else 'none'}\n"
        f"Missing required: {', '.join(missing) if missing else 'none'}\n"
        f"Degree: {degree or 'not compared'}\n"
        f"Years: {years or 'not compared'}"
    )


def fit_sentence(
    matched: list[str],
    missing: list[str],
    degree: str,
    years: str,
    *,
    job_id: Any = "",
    resume_sig: str = "",
    db_path: Any = None,
    timeout: float | None = None,
) -> str | None:
    """Two or three sentences from the four fit fields. Nothing if it names another skill."""
    prompt = _fit_prompt(list(matched or []), list(missing or []), degree or "", years or "")
    allowed = [*(matched or []), *(missing or [])]

    def _accept(payload: dict[str, Any]) -> bool:
        text = str(payload.get("text") or "").strip()
        sentences = _sentences(text)
        if not text or not 2 <= len(sentences) <= 3:
            return False
        return not names_unlisted_skill(text, allowed)

    data = ask_model(
        "fit_sentence",
        "light",
        prompt,
        system=(
            "Write two or three sentences using only the matched list, the missing-required list, "
            "the degree line, and the years line. Return JSON {\"text\":\"...\"}. "
            "Do not name any other skill. Do not give a score."
        ),
        cache_extra=f"{job_id}\n{resume_sig}",
        db_path=db_path,
        timeout=timeout,
        banned=["heat score", "/100", "out of 100"],
        accept=_accept,
    )
    if not data:
        return None
    text = str(data.get("text") or "").strip()
    if not _accept(data):
        return None
    return text


def section_labels(lines: list[str], *, db_path: Any = None) -> dict[int, str] | None:
    """Label existing lines. A rewrite, or a parser field, drops the whole answer."""
    if not lines:
        return None
    numbered = "\n".join(f"{index}|{line}" for index, line in enumerate(lines))
    data = ask_model(
        "section_labels",
        "light",
        numbered,
        system=(
            "Label each existing line as experience, skills, education, or projects. "
            "Return JSON {\"lines\":[{\"index\":0,\"section\":\"experience\"}]}. "
            "Do not rewrite a line. Do not set years, a degree, a certification, or a point value."
        ),
        db_path=db_path,
        timeout=20,
    )
    if not data:
        return None
    rows = data.get("lines")
    if not isinstance(rows, list):
        return None
    labels: dict[int, str] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if index < 0 or index >= len(lines):
            continue
        section = str(item.get("section") or "").strip().lower()
        if section not in _SECTION_NAMES:
            continue
        rewritten = item.get("text", item.get("line"))
        if rewritten is not None and str(rewritten).strip() != lines[index].strip():
            return None
        labels[index] = section
    return labels or None


def suggest_catalog_label(
    phrase: str,
    candidates: list[str],
    *,
    db_path: Any = None,
) -> str | None:
    """One catalog label from the candidate list, or nothing."""
    choices = [label for label in candidates if label]
    if not phrase or not choices:
        return None
    data = ask_model(
        "unknown_phrase",
        "light",
        "Phrase: " + phrase + "\nCatalog labels:\n" + "\n".join(choices),
        system=(
            "Choose one catalog label for the phrase. "
            'Return JSON {"label":"..."} using a label from the list exactly. '
            "Do not add the skill and do not assign points."
        ),
        db_path=db_path,
    )
    if not data:
        return None
    label = str(data.get("label") or "").strip()
    for candidate in choices:
        if candidate.casefold() == label.casefold():
            return candidate
    return None


def _neighbor_labels(phrases: list[str]) -> dict[str, list[str]] | None:
    from embeddings import cosine
    from resume import PROGRAMMING_LANGUAGES, TECHNICAL_SKILLS

    from db import cached_vectors

    labels: list[str] = []
    seen: set[str] = set()
    for label, _pat in (*PROGRAMMING_LANGUAGES, *TECHNICAL_SKILLS):
        if label in seen:
            continue
        seen.add(label)
        labels.append(label)
    vectors = cached_vectors("catalog:skills", labels)
    vectors.update(cached_vectors("resume:unknown", phrases))
    if not any(phrase in vectors for phrase in phrases):
        return None
    neighbors: dict[str, list[str]] = {}
    for phrase in phrases:
        vec = vectors.get(phrase)
        if not vec:
            continue
        scored: list[tuple[float, str]] = []
        for label in labels:
            other = vectors.get(label)
            if not other:
                continue
            scored.append((cosine(vec, other), label))
        scored.sort(key=lambda item: item[0], reverse=True)
        top = [label for _sim, label in scored[:5] if _sim > 0]
        if top:
            neighbors[phrase] = top
    return neighbors or None


def suggest_unknown_phrases(
    phrases: list[str],
    *,
    neighbors: dict[str, list[str]] | None = None,
    db_path: Any = None,
) -> list[dict[str, str]]:
    """Catalog suggestions after embeddings. Empty when embeddings or the model return nothing."""
    cleaned = [phrase for phrase in phrases if phrase][:5]
    if not cleaned:
        return []
    found = neighbors if neighbors is not None else _neighbor_labels(cleaned)
    if not found:
        return []
    suggestions: list[dict[str, str]] = []
    for phrase in cleaned:
        label = suggest_catalog_label(phrase, found.get(phrase) or [], db_path=db_path)
        if label:
            suggestions.append({"phrase": phrase, "label": label})
    return suggestions


def requirement_stance(
    title: Any,
    description: Any,
    keyword: str,
    *,
    db_path: Any = None,
) -> str:
    """Regex wins for required, preferred, equivalent, and absent. The model may only fill mentioned."""
    from textutil import keyword_pattern, mention_stance

    regex = mention_stance(
        f"{title or ''}\n{description or ''}",
        keyword,
        pat=keyword_pattern(keyword),
    )
    if regex != "mentioned":
        return regex
    description_text = description if isinstance(description, str) else ""
    data = ask_model(
        "requirement_stance",
        "light",
        f"Keyword: {keyword}\n\nDescription:\n{description_text[:6000]}",
        system=(
            "How is the keyword framed in this posting? "
            'Return JSON {"stance":"required"|"preferred"|"mentioned"}. '
            "Do not include a point value."
        ),
        cache_extra=keyword,
        db_path=db_path,
        timeout=12,
        banned=["point value", "points", "heat score", "/100"],
    )
    if not data:
        return "mentioned"
    stance = str(data.get("stance") or "").strip().lower()
    if stance not in _STANCES:
        return "mentioned"
    return stance


def suggest_scan_keyword(
    skills: Iterable[str],
    languages: Iterable[str],
    certs: Iterable[str],
    *,
    db_path: Any = None,
) -> str | None:
    """A keyword drawn from the resume. Nothing starts a scrape."""
    skill_list = [str(item).strip() for item in skills if str(item).strip()]
    language_list = [str(item).strip() for item in languages if str(item).strip()]
    cert_list = [str(item).strip() for item in certs if str(item).strip()]
    held = skill_list + language_list + cert_list
    if not held:
        return None
    data = ask_model(
        "scan_keywords",
        "light",
        "Skills: " + ", ".join(skill_list)
        + "\nLanguages: " + ", ".join(language_list)
        + "\nCertifications: " + ", ".join(cert_list),
        system=(
            'Return JSON {"keyword":"..."} using only these resume skills, languages, and certifications. '
            "Do not start a search."
        ),
        db_path=db_path,
    )
    if not data:
        return None
    keyword = str(data.get("keyword") or "").strip()
    if not keyword or names_unlisted_skill(keyword, held):
        return None
    blob = " ".join(held).casefold()
    for word in re.findall(r"[A-Za-z0-9+#.]{4,}", keyword):
        if word.casefold() not in blob:
            return None
    return keyword


def follow_up_note(status: str, notes: str, *, db_path: Any = None) -> str | None:
    """A short note from the status and notes already saved. Does not change the status."""
    data = ask_model(
        "follow_up_note",
        "light",
        f"Status: {status or 'new'}\nNotes: {notes or 'none'}",
        system=(
            'Return JSON {"note":"..."} with a short follow-up note from the status and notes. '
            "Do not change the status."
        ),
        db_path=db_path,
    )
    if not data:
        return None
    note = str(data.get("note") or "").strip()
    return note or None


def _posting_block(job: dict[str, Any]) -> str:
    description = (job.get("description") or "").strip()[:4000]
    return (
        f"Title: {job.get('title') or ''}\n"
        f"Company: {job.get('company') or ''}\n"
        f"Location: {job.get('location') or ''}\n\n"
        f"Description:\n{description}"
    )


def explain_job(job: dict[str, Any], fit: dict[str, Any] | None = None, *, db_path: Any = None) -> str | None:
    """What the posting asks, including travel, clearance, and on-call. A 0–100 score is rejected."""
    del fit  # The score and the fit lists stay off this prompt so the reply cannot echo a number.
    data = ask_model(
        "explain_job",
        "medium",
        _posting_block(job),
        system=(
            "Explain this posting in one short paragraph of plain sentences. "
            "Describe travel, clearance, and on-call when the posting mentions them. "
            'Return JSON {"text":"..."}. Do not state a score from 0 to 100.'
        ),
        cache_extra=str(job.get("id") or ""),
        db_path=db_path,
        banned=["heat score", "out of 100", "/100"],
        accept=lambda payload: not _SCORE_CLAIM.search(str(payload.get("text") or "")),
    )
    if not data:
        return None
    text = str(data.get("text") or "").strip()
    if not text or _SCORE_CLAIM.search(text):
        return None
    return text


def _supplied_cover(
    job: dict[str, Any],
    fit: dict[str, Any],
    experience: str,
) -> str:
    return "\n".join([
        experience or "",
        str(job.get("company") or ""),
        str(job.get("title") or ""),
        str(fit.get("degree") or ""),
        str(fit.get("years") or ""),
        ", ".join(fit.get("matched") or []),
    ])


def draft_is_grounded(text: str, supplied: str, missing: Iterable[str]) -> bool:
    """False when the draft names a missing skill or an employer, degree, or year that was not supplied."""
    if not text:
        return False
    for skill in missing:
        if names_phrase(text, str(skill)):
            return False
    supplied_cf = (supplied or "").casefold()
    for year in _YEAR_TOKEN.findall(text):
        if year not in supplied:
            return False
    for number in _YEARS_PHRASE.findall(text):
        if number not in supplied:
            return False
    for match in _DEGREE_WORD.finditer(text):
        if match.group(0).casefold() not in supplied_cf:
            return False
    for pattern in (_EMPLOYER, _WORKED_AT):
        for match in pattern.finditer(text):
            name = match.group(1).strip()
            if name.casefold() in _EMPLOYER_SKIP:
                continue
            if name.casefold() not in supplied_cf:
                return False
    return True


def draft_cover(
    job: dict[str, Any],
    fit: dict[str, Any],
    experience: str = "",
    *,
    db_path: Any = None,
) -> str | None:
    """A letter from experience bullets and the matched list. The user still edits and saves it."""
    matched = list(fit.get("matched") or [])
    missing = [str(item) for item in (fit.get("missing_required") or []) if item]
    supplied = _supplied_cover(job, fit, experience or "")
    prompt = (
        f"Experience:\n{experience or 'none'}\n\n"
        f"Matched: {', '.join(matched) if matched else 'none'}\n"
        f"Missing required: {', '.join(missing) if missing else 'none'}\n"
        f"Company: {job.get('company') or ''}\n"
        f"Role: {job.get('title') or ''}\n"
        f"Degree: {fit.get('degree') or 'not supplied'}\n"
        f"Years: {fit.get('years') or 'not supplied'}"
    )
    data = ask_model(
        "cover_letter",
        "medium",
        prompt,
        system=(
            "Draft a cover letter under 250 words. Use the experience section and the matched list. "
            "Do not claim a missing-required skill. Do not invent an employer, degree, or year. "
            'Return JSON {"text":"..."}. Write the letter only.'
        ),
        cache_extra=str(job.get("id") or ""),
        db_path=db_path,
        accept=lambda payload: draft_is_grounded(str(payload.get("text") or ""), supplied, missing),
    )
    if not data:
        return None
    text = str(data.get("text") or "").strip()
    if not draft_is_grounded(text, supplied, missing):
        return None
    return text


def _content_words(text: str) -> set[str]:
    stop = {
        "about", "after", "across", "their", "there", "which", "while", "with", "from", "that",
        "this", "have", "been", "were", "your", "role", "team", "work", "using", "used", "into",
        "during", "other", "these", "those", "would", "could", "should", "where", "when",
    }
    words: set[str] = set()
    for word in re.findall(r"[A-Za-z][A-Za-z0-9+#./-]{3,}", text or ""):
        token = word.casefold().strip(".#")
        if token and token not in stop:
            words.add(token)
    return words


def bullets_are_grounded(bullets: str, experience: str) -> bool:
    """False when a bullet adds a catalog tool, or a duty with no words from the experience section."""
    from resume import CERTIFICATIONS, PROGRAMMING_LANGUAGES, ROLE_PATTERNS, TECHNICAL_SKILLS

    text = (bullets or "").strip()
    source = experience or ""
    if not text or not source:
        return False
    for _label, pat in (*TECHNICAL_SKILLS, *PROGRAMMING_LANGUAGES, *CERTIFICATIONS, *ROLE_PATTERNS):
        if pat.search(text) and not pat.search(source):
            return False
    experience_words = _content_words(source)
    for line in text.splitlines():
        words = _content_words(line)
        if words and experience_words and not (words & experience_words):
            return False
    return True


def tailor_bullets(experience: str, posting: str, *, job_id: Any = "", db_path: Any = None) -> str | None:
    """Rephrased experience bullets. A new tool or duty is rejected. Nothing is saved here."""
    if not (experience or "").strip():
        return None
    data = ask_model(
        "bullet_tailoring",
        "medium",
        f"Experience:\n{experience}\n\nPosting:\n{(posting or '')[:4000]}",
        system=(
            "Rephrase the experience bullets toward the posting. Keep every tool and duty that you mention "
            "inside the experience section. Return JSON {\"text\":\"...\"}. Do not add a new tool or duty."
        ),
        cache_extra=str(job_id),
        db_path=db_path,
        accept=lambda payload: bullets_are_grounded(str(payload.get("text") or ""), experience),
    )
    if not data:
        return None
    text = str(data.get("text") or "").strip()
    if not bullets_are_grounded(text, experience):
        return None
    return text


def claims_gap_as_held(text: str, missing: Iterable[str]) -> bool:
    """True when the reply treats a missing-required skill as one the user already has."""
    for skill in missing:
        if not skill or not names_phrase(text, str(skill)):
            continue
        for match in re.finditer(rf"(?<!\w){re.escape(str(skill))}(?!\w)", text, re.I):
            window = text[max(0, match.start() - 90) : match.end() + 90]
            if _GAP_DENY.search(window):
                continue
            if _GAP_CLAIM.search(window):
                return True
    return False


def interview_questions(
    held: Iterable[str],
    missing: Iterable[str],
    posting: str,
    *,
    job_id: Any = "",
    db_path: Any = None,
) -> list[str] | None:
    """Questions from skills the resume already holds. A filled-in gap is rejected."""
    held_list = [str(item).strip() for item in held if str(item).strip()]
    missing_list = [str(item).strip() for item in missing if str(item).strip()]
    if not held_list:
        return None
    data = ask_model(
        "interview_set",
        "medium",
        "Skills the applicant has:\n"
        + "\n".join(held_list)
        + "\n\nMissing required, which they do not have:\n"
        + ("\n".join(missing_list) if missing_list else "none")
        + "\n\nPosting:\n"
        + (posting or "")[:3000],
        system=(
            "Write five interview questions from skills the applicant already has. "
            'Return JSON {"questions":["..."]}. '
            "Do not treat a missing-required skill as if they already had it."
        ),
        cache_extra=str(job_id),
        db_path=db_path,
        accept=lambda payload: _questions_ok(payload.get("questions"), held_list, missing_list),
    )
    if not data:
        return None
    questions = _clean_questions(data.get("questions"))
    if not _questions_ok(questions, held_list, missing_list):
        return None
    return questions


def _clean_questions(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _questions_ok(value: Any, held: list[str], missing: list[str]) -> bool:
    questions = value if isinstance(value, list) else _clean_questions(value)
    if not questions:
        return False
    blob = "\n".join(str(item) for item in questions)
    if claims_gap_as_held(blob, missing):
        return False
    return any(names_phrase(blob, skill) for skill in held)
