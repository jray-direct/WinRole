"""Local web UI for Win Role. Run: python app.py

Sign-in uses the Cognito user pool. Set COGNITO_CLIENT_SECRET to the
Win Role app client secret before starting.
"""

from __future__ import annotations

import os
import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.flask_client import OAuth
from flask import Flask, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for
from markupsafe import Markup, escape
from werkzeug.utils import secure_filename

from db import (
    HEARD_BACK,
    JOB_TYPES,
    MATERIALS_DIR,
    PIPELINE_STATUSES,
    PROFILE_CLEARANCES,
    REMOTE_PREFS,
    STATUSES,
    STATUS_LABELS,
    add_location,
    add_material,
    clear_profile,
    delete_job,
    delete_jobs_for_keyword,
    delete_material,
    delete_run,
    ensure_user,
    format_pay,
    get_enabled_locations,
    get_grouped_locations,
    get_insights,
    get_job,
    get_latest_run,
    get_material,
    heat_scores_pending,
    get_profile,
    job_fit,
    init_db,
    list_keywords,
    list_materials,
    list_runs,
    query_jobs,
    get_user_resume_profile,
    resume_profile_for_material,
    save_profile,
    save_resume_extracted_text,
    save_resume_overrides,
    set_all_locations_enabled,
    set_default_material,
    save_filter_template,
    set_location_enabled_by_id,
    set_region_locations_enabled,
    templates_by_kind,
    tracker_summary,
    update_job_tracker,
)
from ai_client import (
    ai_settings,
    bullets_are_grounded,
    draft_cover,
    explain_job,
    follow_up_note,
    interview_questions,
    suggest_scan_keyword,
    suggest_unknown_phrases,
    tailor_bullets,
)
from resume import DEGREE_LABELS, experience_section, load_overrides, splice_experience, unknown_skill_phrases
from scrape import SearchFilters, run_scrape
from spacex import careers_filter_options, merge_board_filters
from textutil import (
    CERT_STACK,
    CLEARANCE_LABELS,
    CLEARANCE_ORDER,
    STANCE_LABELS,
    certs_mentioned,
    highlight_description,
    mention_stance,
)

app = Flask(__name__)
app.secret_key = os.environ.get("JOBSCRAPER_SECRET", "jobscraper-local")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

# Public client id and pool id. The client secret stays in COGNITO_CLIENT_SECRET.
COGNITO_POOL_ID = os.environ.get("COGNITO_POOL_ID", "us-east-2_cjKfpwQur")
COGNITO_CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "5f7sb7aiq5u3q3a0l5711lbuja")
COGNITO_REDIRECT_URI = os.environ.get("COGNITO_REDIRECT_URI", "http://127.0.0.1:5050/auth/callback")
COGNITO_LOGOUT_URI = os.environ.get("COGNITO_LOGOUT_URI", "http://127.0.0.1:5050/")

oauth = OAuth(app)
_cognito_ready = False
_PUBLIC_ENDPOINTS = {"login", "auth_callback", "logout", "static"}

ALLOWED_MATERIAL_EXT = {".pdf", ".doc", ".docx", ".txt", ".md", ".rtf"}

# Scans are queued and run one at a time by a single worker thread. Each user has
# their own log/result/error, and at most one scan (running or queued) at a time.
_scan_cv = threading.Condition()
_scan_queue: deque[tuple[int, SearchFilters]] = deque()
_scan_states: dict[int, dict[str, Any]] = {}
_scan_current: int | None = None
_scan_thread: threading.Thread | None = None


def _blank_scan_state() -> dict[str, Any]:
    return {"queued": False, "running": False, "log": [], "result": None, "error": None}


def _progress_for(uid: int):
    def _progress(msg: str) -> None:
        with _scan_cv:
            log = _scan_states.setdefault(uid, _blank_scan_state())["log"]
            log.append(msg)
            if len(log) > 400:
                del log[:100]

    return _progress


def _scan_snapshot(uid: int) -> dict[str, Any]:
    with _scan_cv:
        st = _scan_states.get(uid) or _blank_scan_state()
        position = 0
        for index, (queued_uid, _filters) in enumerate(_scan_queue, start=1):
            if queued_uid == uid:
                position = index
                break
        ahead = (position - 1 + (1 if _scan_current is not None else 0)) if position else 0
        return {
            "running": bool(st["running"] or st["queued"]),
            "queued": bool(st["queued"]),
            "ahead": ahead,
            "log": list(st["log"]),
            "result": st["result"],
            "error": st["error"],
        }


def _scan_loop() -> None:
    global _scan_current
    while True:
        with _scan_cv:
            while not _scan_queue:
                _scan_cv.wait()
            uid, filters = _scan_queue.popleft()
            _scan_current = uid
            st = _scan_states.setdefault(uid, _blank_scan_state())
            st["queued"] = False
            st["running"] = True
        try:
            result = run_scrape(filters, progress=_progress_for(uid), user_id=uid)
            with _scan_cv:
                st["result"] = {k: v for k, v in result.items() if k != "jobs"}
        except Exception as exc:  # noqa: BLE001
            with _scan_cv:
                st["error"] = str(exc)
            _progress_for(uid)(f"! {exc}")
        finally:
            with _scan_cv:
                st["running"] = False
                _scan_current = None


def _ensure_scan_thread() -> None:
    global _scan_thread
    with _scan_cv:
        if _scan_thread is None or not _scan_thread.is_alive():
            _scan_thread = threading.Thread(target=_scan_loop, name="scan-worker", daemon=True)
            _scan_thread.start()


def _enqueue_scan(uid: int, filters: SearchFilters) -> int:
    """Queue a scan for this user. Returns how many scans are ahead of it."""
    with _scan_cv:
        st = _scan_states.setdefault(uid, _blank_scan_state())
        if st["running"] or st["queued"]:
            raise ValueError("You already have a scan running or queued.")
        st.update(queued=True, running=False, result=None, error=None, log=[])
        _scan_queue.append((uid, filters))
        ahead = len(_scan_queue) - 1 + (1 if _scan_current is not None else 0)
        _scan_cv.notify()
    _ensure_scan_thread()
    if ahead:
        _progress_for(uid)(f"Queued: {ahead} scan(s) ahead of yours.")
    return ahead


def _clear_scan_log(uid: int) -> bool:
    with _scan_cv:
        st = _scan_states.setdefault(uid, _blank_scan_state())
        if st["running"] or st["queued"]:
            return False
        st.update(log=[], result=None, error=None)
        return True


@app.template_filter("usd")
def usd(value: Any) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return "—"


@app.template_filter("yn")
def yn(value: Any) -> str:
    return "Yes" if value else "No"


@app.template_filter("pay")
def pay(job: Any) -> str:
    if isinstance(job, dict):
        return format_pay(job)
    return "—"


def _cognito_host() -> str:
    region = COGNITO_POOL_ID.split("_", 1)[0]
    prefix = COGNITO_POOL_ID.replace("_", "").lower()
    return f"https://{prefix}.auth.{region}.amazoncognito.com"


def _client_secret() -> str:
    env = os.environ.get("COGNITO_CLIENT_SECRET", "").strip()
    if env:
        return env
    path = Path(__file__).resolve().parent / "cognito_secret.txt"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return ""


def _register_cognito() -> bool:
    """Register the app client once a secret is available. The secret is not kept in source."""
    global _cognito_ready
    if _cognito_ready:
        return True
    secret = _client_secret()
    if not secret:
        return False
    region = COGNITO_POOL_ID.split("_", 1)[0]
    oauth.register(
        name="oidc",
        client_id=COGNITO_CLIENT_ID,
        client_secret=secret,
        server_metadata_url=(
            f"https://cognito-idp.{region}.amazonaws.com/{COGNITO_POOL_ID}"
            "/.well-known/openid-configuration"
        ),
        client_kwargs={"scope": "openid email"},
    )
    _cognito_ready = True
    return True


def _missing_secret():
    path = Path(__file__).resolve().parent / "cognito_secret.txt"
    body = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Win Role sign-in</title></head>
<body style="font-family: sans-serif; max-width: 40rem; margin: 3rem auto; line-height: 1.45">
  <h1>Save the Cognito secret, then reload</h1>
  <p>The sign-in page stays closed until the app has the Win Role client secret.</p>
  <ol>
    <li>In Cognito, open the Win Role app client and copy the client secret.</li>
    <li>Create this file and paste only the secret into it:<br><code>{path}</code></li>
    <li>Reload this page. It will then send you to the Cognito sign-in page.</li>
  </ol>
</body>
</html>"""
    return body, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.before_request
def _prepare_request() -> None:
    init_db()
    if request.endpoint in _PUBLIC_ENDPOINTS:
        return
    user = session.get("user") or {}
    sub = str(user.get("sub") or "").strip()
    if not sub:
        if request.path == "/scrape/status":
            return jsonify({"error": "Sign in required."}), 401
        return redirect(url_for("login"))
    email = (user.get("email") or "").strip() or None
    g.user_id = ensure_user(sub, email)
    g.user_email = email or ""


def _uid() -> int:
    return int(g.user_id)


@app.context_processor
def _nav() -> dict[str, Any]:
    return {"endpoint": request.endpoint, "user_email": getattr(g, "user_email", "") or ""}


@app.route("/login")
def login():
    if session.get("user"):
        return redirect(url_for("insights"))
    if not _register_cognito():
        return _missing_secret()
    return oauth.oidc.authorize_redirect(COGNITO_REDIRECT_URI)


@app.route("/auth/callback")
def auth_callback():
    if not _register_cognito():
        return _missing_secret()
    try:
        token = oauth.oidc.authorize_access_token()
    except OAuthError as exc:
        reason = getattr(exc, "error", None) or "sign-in failed"
        return (f'Sign-in failed ({reason}). <a href="/login">Try again</a>', 400)
    info = token.get("userinfo") or {}
    sub = str(info.get("sub") or "").strip()
    if not sub:
        return ('Cognito did not return a user id. <a href="/login">Try again</a>', 400)
    email = (info.get("email") or "").strip() or None
    session["user"] = {"sub": sub, "email": email}
    ensure_user(sub, email)
    return redirect(url_for("insights"))


@app.route("/logout")
def logout():
    session.pop("user", None)
    query = urlencode({"client_id": COGNITO_CLIENT_ID, "logout_uri": COGNITO_LOGOUT_URI})
    return redirect(f"{_cognito_host()}/logout?{query}")


@app.template_filter("ago")
def ago(value: Any) -> str:
    """'2026-11-02 08:00:00' (UTC) -> '3d ago'."""
    if not value:
        return "—"
    text = str(value).strip().replace("T", " ")[:19]
    try:
        then = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            then = datetime.strptime(text[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return str(value)
    seconds = int((datetime.now(timezone.utc) - then).total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 90:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    days = hours // 24
    if days < 60:
        return f"{days}d ago"
    return f"{days // 30}mo ago"


@app.template_filter("pay_range")
def pay_range(stats: Any) -> str:
    """p25 – p75 for the Insights pay panels."""
    if not isinstance(stats, dict) or not stats.get("n"):
        return "—"
    return f"{usd(stats.get('p25'))} – {usd(stats.get('p75'))}"


@app.template_filter("clearance_label")
def clearance_label(value: Any) -> str:
    return CLEARANCE_LABELS.get(str(value or ""), "Not mentioned")


@app.template_filter("status_label")
def status_label(value: Any) -> str:
    return STATUS_LABELS.get(str(value or ""), str(value or "—"))


@app.template_filter("job_type_label")
def job_type_label(value: Any) -> str:
    labels = {
        "fulltime": "Full-time",
        "parttime": "Part-time",
        "contract": "Contract",
        "temporary": "Temporary",
        "internship": "Internship",
        "perdiem": "Per diem",
        "other": "Other",
        "volunteer": "Volunteer",
    }
    return labels.get(str(value or ""), str(value or ""))


def _posting_link(job: dict[str, Any]) -> tuple[str | None, str]:
    url = (job.get("job_url") or "").strip()
    site = (job.get("site") or "").strip().lower()
    blob = f"{site} {url}".lower()
    if "linkedin" in blob:
        label = "LinkedIn"
    elif "indeed" in blob:
        label = "Indeed"
    elif site == "spacex" or "greenhouse.io" in blob and "spacex" in blob:
        label = "SpaceX"
    elif site:
        label = site.replace("_", " ").title()
    else:
        label = "Posting"
    return (url or None), label


def _prepare_job_view(job: dict[str, Any], extra_keyword: str = "") -> dict[str, Any]:
    highlight_terms = list(job.get("keywords") or [])
    extra = (extra_keyword or "").strip()
    if extra and extra not in highlight_terms:
        highlight_terms = [extra, *highlight_terms]
    hay = f"{job.get('title') or ''}\n{job.get('description') or ''}"
    job["certs"] = certs_mentioned(hay)
    job["clearance_label"] = CLEARANCE_LABELS.get(
        job.get("clearance") or "unspecified", "Not mentioned"
    )
    stance_key = mention_stance(hay, extra or (highlight_terms[0] if highlight_terms else ""))
    job["stance"] = STANCE_LABELS.get(stance_key, stance_key)
    job["highlighted_description"] = highlight_description(
        job.get("description") or "",
        highlight_terms,
        company=job.get("company") or "",
    )
    job["posting_url"], job["posting_label"] = _posting_link(job)
    return job


def _attach_fit(job: dict[str, Any] | None, keyword: str) -> None:
    if not job:
        return
    job["fit"] = job_fit(_uid(), job, keyword)


_CHIP_ACTIONS = {
    "add_skill": ("add_skills", "remove_skills"),
    "remove_skill": ("remove_skills", "add_skills"),
    "add_language": ("add_languages", "remove_languages"),
    "remove_language": ("remove_languages", "add_languages"),
    "add_cert": ("add_certs", "remove_certs"),
    "remove_cert": ("remove_certs", "add_certs"),
    "add_field": ("add_degree_fields", "remove_degree_fields"),
    "remove_field": ("remove_degree_fields", "add_degree_fields"),
}


def _drop_term(items: list[str], value: str) -> list[str]:
    from resume import canonical_cert, canonical_field, canonical_skill

    keys = {value.casefold()}
    for canon in (canonical_skill(value), canonical_cert(value)):
        if canon:
            keys.add(canon.casefold())
    field_name = canonical_field(value)
    if field_name:
        keys.add(field_name.casefold())
    return [item for item in items if item.casefold() not in keys]


def _apply_readout_action(overrides: dict[str, Any], action: str, value: str, degree_level: str) -> dict[str, Any]:
    if action == "set_degree":
        choice = (degree_level or "").strip()
        if choice == "none":
            overrides["clear_degree"] = True
            overrides["degree_level"] = None
        elif choice == "detected" or choice == "":
            overrides["clear_degree"] = False
            overrides["degree_level"] = None
        else:
            overrides["clear_degree"] = False
            overrides["degree_level"] = choice
        return overrides
    pair = _CHIP_ACTIONS.get(action)
    text = (value or "").strip()
    if pair is None or not text:
        return overrides
    keep_key, drop_key = pair
    overrides[drop_key] = _drop_term(list(overrides.get(drop_key) or []), text)
    kept = _drop_term(list(overrides.get(keep_key) or []), text)
    kept.append(text)
    overrides[keep_key] = kept
    return overrides


@app.route("/")
def insights():
    last = get_latest_run(_uid())
    if "keyword" in request.args:
        keyword = (request.args.get("keyword") or "").strip()
    else:
        keyword = ((last or {}).get("keyword") or "").strip()
    data = get_insights(_uid(), keyword)
    return render_template(
        "insights.html",
        insights=data,
        keywords=list_keywords(_uid()),
        selected_keyword=keyword,
        status_labels=STATUS_LABELS,
    )


@app.route("/jobs")
def jobs():
    last = get_latest_run(_uid())
    if not request.args:
        keyword = ((last or {}).get("keyword") or "").strip()
        if keyword:
            return redirect(url_for("jobs", keyword=keyword))

    partial_rows = request.args.get("partial") == "rows"
    try:
        page = max(int(request.args.get("page", 1) or 1), 1)
    except ValueError:
        page = 1
    if not partial_rows:
        page = 1
    per_page = 40
    new_only = request.args.get("new") == "1"

    # Years, minimum pay, and remote come from the profile when the request doesn't
    # set them. `defaults=0` is "Turn off". A closed More filters panel leaves those
    # fields out of the submit, so searching does not wipe the profile filters.
    # Mirrored fields can appear twice; the last value is the one the user edited.
    profile = get_profile(_uid())
    suppress_profile = request.args.get("defaults") == "0"

    def last_arg(name: str) -> str | None:
        values = request.args.getlist(name)
        if not values:
            return None
        return values[-1].strip()

    def same_number(raw: Any, profile_value: Any) -> bool:
        if raw in (None, "") or profile_value in (None, ""):
            return False
        try:
            return float(raw) == float(profile_value)
        except (TypeError, ValueError):
            return False

    my_years_raw = last_arg("my_years")
    min_salary_raw = last_arg("min_salary")
    remote_values = request.args.getlist("remote")
    years_explicit = my_years_raw is not None
    salary_explicit = min_salary_raw is not None
    # remote_set marks an explicit choice, including "not remote", without a second
    # remote=0 value that would clash with remote=1 in the query string.
    remote_explicit = bool(remote_values) or request.args.get("remote_set") == "1"
    my_years = my_years_raw if years_explicit else None
    min_salary = min_salary_raw if salary_explicit else None
    remote_only = any(value == "1" for value in remote_values)
    if profile and not suppress_profile:
        if not years_explicit and profile.get("years") is not None:
            my_years = f"{profile['years']:g}"
        if not salary_explicit and profile.get("min_pay"):
            min_salary = f"{profile['min_pay']:.0f}"
        if not remote_explicit and profile.get("remote_pref") == "remote":
            remote_only = True
    profile_defaults: list[str] = []
    if profile and not suppress_profile:
        if same_number(my_years, profile.get("years")):
            profile_defaults.append(f"{float(profile['years']):g} yrs")
        if same_number(min_salary, profile.get("min_pay")):
            profile_defaults.append(f"${profile['min_pay']:,.0f}+ pay")
        if remote_only and profile.get("remote_pref") == "remote":
            profile_defaults.append("remote")
    status_arg = last_arg("status") or ""
    filters = {
        "keyword": request.args.get("keyword") or "",
        "q": request.args.get("q") or "",
        "remote_only": remote_only,
        "min_salary": min_salary or "",
        "my_years": my_years or "",
        "hide_nopay": last_arg("hide_nopay") == "1",
        "posted_within_days": last_arg("days") or "",
        "exclude_keywords": last_arg("exclude") or "",
        "title_include": last_arg("title_include") or "",
        "title_exclude": request.args.get("title_exclude") or "",
        "job_type": last_arg("job_type") or "",
        "status": status_arg,
        "clearance": last_arg("clearance") or "",
        "stance": (last_arg("stance") or "").lower(),
        "company": request.args.get("company") or "",
        "loc": request.args.get("loc") or "",
        "cert": request.args.get("cert") or "",
        "show_closed": last_arg("closed") == "1",
        "order": request.args.get("order") or "score",
        "seen_after": (last.get("started_at") if new_only and last else "") or "",
    }
    # Ignored and applied jobs are out of the way unless asked for (or filtered to by status).
    filters["hide_closed"] = not filters["show_closed"] and not status_arg
    rows, total = query_jobs(
        _uid(),
        filters,
        limit=per_page,
        offset=(page - 1) * per_page,
        profile=profile,
        defer_large_scores=True,
    )
    scores_pending = bool(getattr(rows, "scores_pending", False))
    if partial_rows:
        next_args = {
            k: v
            for k, v in request.args.to_dict().items()
            if k not in ("page", "job", "scan", "partial")
        }
        more = (page - 1) * per_page + len(rows) < total
        return jsonify(
            html=render_template(
                "_job_rows.html",
                jobs=rows,
                list_qs=next_args,
                selected_id=0,
                filters=filters,
            ),
            next=url_for("jobs", partial="rows", page=page + 1, **next_args) if more else None,
        )

    base_args = {k: v for k, v in request.args.to_dict().items() if k not in ("page", "job", "scan", "partial")}
    active_filters = []
    for key, label in (("company", "Company"), ("loc", "Location"), ("cert", "Cert")):
        if filters[key]:
            kept = {k: v for k, v in base_args.items() if k != key}
            kept["f"] = 1
            active_filters.append(
                {"label": f"{label}: {filters[key]}", "clear_url": url_for("jobs", **kept)}
            )
    defaults_off_url = None
    if profile_defaults and profile:
        off_args = {
            k: v
            for k, v in base_args.items()
            if k not in ("my_years", "min_salary", "remote", "defaults")
        }
        if not same_number(filters["my_years"], profile.get("years")) and filters["my_years"]:
            off_args["my_years"] = filters["my_years"]
        if not same_number(filters["min_salary"], profile.get("min_pay")) and filters["min_salary"]:
            off_args["min_salary"] = filters["min_salary"]
        if not (remote_only and profile.get("remote_pref") == "remote") and remote_explicit:
            off_args["remote"] = "1" if remote_only else "0"
        off_args["f"] = 1
        off_args["defaults"] = "0"
        defaults_off_url = url_for("jobs", **off_args)
    prof_remote = bool(profile and profile.get("remote_pref") == "remote")
    if suppress_profile:
        extra_count = sum(
            bool(value)
            for value in (
                filters["title_include"],
                filters["exclude_keywords"],
                filters["job_type"],
                filters["status"],
                filters["clearance"],
                filters["stance"],
                filters["posted_within_days"],
                filters["hide_nopay"],
                filters["show_closed"],
                filters["min_salary"],
                filters["my_years"],
                filters["remote_only"],
            )
        )
    else:
        extra_count = sum(
            bool(value)
            for value in (
                filters["title_include"],
                filters["exclude_keywords"],
                filters["job_type"],
                filters["status"],
                filters["clearance"],
                filters["stance"],
                filters["posted_within_days"],
                filters["hide_nopay"],
                filters["show_closed"],
            )
        )
        if filters["min_salary"] and not same_number(filters["min_salary"], (profile or {}).get("min_pay")):
            extra_count += 1
        if filters["my_years"] and not same_number(filters["my_years"], (profile or {}).get("years")):
            extra_count += 1
        if filters["remote_only"] and not prof_remote:
            extra_count += 1
        elif remote_explicit and not filters["remote_only"] and prof_remote:
            extra_count += 1
    try:
        selected_id = int(request.args.get("job") or 0)
    except ValueError:
        selected_id = 0
    if not selected_id and rows:
        selected_id = int(rows[0]["id"])
    selected = get_job(_uid(), selected_id) if selected_id else None
    if selected:
        _prepare_job_view(selected, filters["keyword"])
        _attach_fit(selected, filters["keyword"])
    state = _scan_snapshot(_uid())
    scan_open = request.args.get("scan") == "1" or bool(state["running"])
    return render_template(
        "jobs.html",
        jobs=rows,
        total=total,
        filters=filters,
        new_only=new_only,
        run_started=last.get("started_at") if last else None,
        keywords=list_keywords(_uid()),
        job_types=JOB_TYPES,
        statuses=STATUSES,
        status_labels=STATUS_LABELS,
        clearance_options=CLEARANCE_ORDER,
        selected_job=selected,
        selected_id=selected_id,
        heard_back=HEARD_BACK,
        resumes=list_materials(_uid(), "resume"),
        covers=list_materials(_uid(), "cover"),
        profile=profile,
        profile_defaults=profile_defaults,
        defaults_off_url=defaults_off_url,
        extra_count=extra_count,
        suppress_profile=suppress_profile,
        years_explicit=years_explicit,
        salary_explicit=salary_explicit,
        remote_explicit=remote_explicit,
        active_filters=active_filters,
        state=state,
        scan_open=scan_open,
        scores_pending=scores_pending,
        grouped=get_grouped_locations(_uid()),
        enabled_count=len(get_enabled_locations(_uid())),
        templates=templates_by_kind(_uid()),
        spacex_filters=careers_filter_options(),
        ai_ready=ai_settings()["ready"],
        sort_options=[
            ("score", "Heat"),
            ("date", "Date posted"),
            ("pay", "Pay"),
            ("company", "Company"),
            ("seen", "First seen"),
        ],
    )


@app.get("/jobs/heat-status")
def jobs_heat_status():
    return jsonify({"pending": heat_scores_pending(_uid())})


@app.route("/inbox")
def inbox():
    return redirect(url_for("jobs"))


@app.route("/jobs/<int:job_id>")
def job_detail(job_id: int):
    job = get_job(_uid(), job_id)
    if job is None:
        flash("Job not found.")
        return redirect(url_for("jobs"))
    extra = (request.args.get("keyword") or "").strip()
    _prepare_job_view(job, extra)
    _attach_fit(job, extra)
    return render_template(
        "job_detail.html",
        job=job,
        ai_ready=ai_settings()["ready"],
        statuses=STATUSES,
        status_labels=STATUS_LABELS,
        heard_back=HEARD_BACK,
        resumes=list_materials(_uid(), "resume"),
        covers=list_materials(_uid(), "cover"),
        back_keyword=extra,
    )


@app.get("/jobs/<int:job_id>/pane")
def job_pane(job_id: int):
    job = get_job(_uid(), job_id)
    if job is None:
        return ("Job not found.", 404)
    extra = (request.args.get("keyword") or "").strip()
    _prepare_job_view(job, extra)
    _attach_fit(job, extra)
    return_to = (request.args.get("next") or "").strip()
    if not return_to.startswith("/"):
        return_to = url_for("jobs", job=job_id, keyword=extra)
    return render_template(
        "job_pane.html",
        job=job,
        statuses=STATUSES,
        heard_back=HEARD_BACK,
        resumes=list_materials(_uid(), "resume"),
        covers=list_materials(_uid(), "cover"),
        return_to=return_to,
        ai_ready=ai_settings()["ready"],
    )


def _tracker_fields() -> dict[str, Any]:
    keys = ("status", "applied_on", "follow_up_on", "heard_back", "notes", "resume_id", "cover_id")
    return {key: request.form.get(key) for key in keys if key in request.form}


@app.post("/jobs/<int:job_id>/status")
def job_status(job_id: int):
    try:
        update_job_tracker(_uid(), job_id, _tracker_fields())
        tracker_url = url_for("tracker")
        flash(
            Markup(
                f'Tracker saved. <a href="{escape(tracker_url)}">View in Tracker</a>'
            ),
            "success",
        )
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(request.form.get("next") or request.referrer or url_for("job_detail", job_id=job_id))


@app.post("/jobs/<int:job_id>/delete")
def job_delete(job_id: int):
    title = (request.form.get("title") or "").strip()
    try:
        delete_job(_uid(), job_id)
        flash(f"Deleted {title or 'job'}.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(request.form.get("next") or url_for("jobs"))


def _scan_redirect():
    nxt = (request.form.get("next") or "").strip()
    if nxt.startswith("/"):
        return redirect(nxt)
    return redirect(url_for("jobs", scan=1))


def _cities_fetch() -> bool:
    return request.headers.get("X-Requested-With") == "fetch"


def _cities_panel():
    grouped = get_grouped_locations(_uid())
    return render_template(
        "_cities_panel.html",
        grouped=grouped,
        enabled_count=grouped["enabled_count"],
    )


@app.route("/cities")
def cities():
    return redirect(url_for("jobs", scan=1))


@app.post("/cities/<int:loc_id>/toggle")
def toggle_city(loc_id: int):
    enabled = request.form.get("enabled") == "1"
    try:
        set_location_enabled_by_id(_uid(), loc_id, enabled)
    except ValueError as exc:
        if _cities_fetch():
            return str(exc), 400
        flash(str(exc))
    if _cities_fetch():
        return _cities_panel()
    return _scan_redirect()


@app.post("/cities/add")
def add_city():
    name = (request.form.get("name") or "").strip()
    if not name:
        message = "Enter a location, e.g. Tulsa, OK"
        if _cities_fetch():
            return message, 400
        flash(message)
        return _scan_redirect()
    add_location(_uid(), name)
    if _cities_fetch():
        return _cities_panel()
    flash(f"Added {name}.")
    return _scan_redirect()


@app.post("/cities/bulk")
def bulk_cities():
    # Field must not be named "action" — that shadows HTMLFormElement.action in JS.
    action = request.form.get("bulk") or request.form.get("action")
    if action == "enable_all":
        set_all_locations_enabled(_uid(), True)
        message = "All locations enabled."
    elif action == "disable_all":
        set_all_locations_enabled(_uid(), False)
        message = "All locations disabled."
    elif action == "core_only":
        set_all_locations_enabled(_uid(), False, except_names=["United States", "Remote"])
        message = "Only United States + Remote enabled."
    else:
        message = ""
    if _cities_fetch():
        return _cities_panel()
    if message:
        flash(message)
    return _scan_redirect()


@app.post("/cities/region/<region_key>")
def toggle_region(region_key: str):
    enabled = request.form.get("enabled") == "1"
    try:
        set_region_locations_enabled(_uid(), region_key, enabled)
    except ValueError as exc:
        if _cities_fetch():
            return str(exc), 400
        flash(str(exc))
    if _cities_fetch():
        return _cities_panel()
    return _scan_redirect()


@app.route("/scrape", methods=["GET", "POST"])
def scrape_page():
    if request.method == "GET":
        return redirect(url_for("jobs", scan=1))

    if request.method == "POST":
        if _scan_snapshot(_uid())["running"]:
            flash("You already have a scan running or queued.", "info")
            return redirect(url_for("jobs", scan=1))

        salary_raw = (request.form.get("min_salary") or "").replace(",", "").strip()
        years_raw = (request.form.get("my_years") or "").strip()
        days_raw = (request.form.get("days") or "").strip()
        exclude_raw = (request.form.get("exclude") or "").strip()
        title_include_raw = (request.form.get("title_include") or "").strip()
        title_exclude_raw = (request.form.get("title_exclude") or "").strip()
        job_type = (request.form.get("job_type") or "").strip().lower()
        keyword = (request.form.get("keyword") or "").strip()
        source = (request.form.get("source") or "indeed").strip().lower()
        if source not in ("indeed", "spacex"):
            source = "indeed"
        board_url = (request.form.get("board_url") or "").strip()
        board_programs, board_locations, board_disciplines, url_keyword = merge_board_filters(
            request.form.getlist("sx_programs"),
            request.form.getlist("sx_locations"),
            request.form.getlist("sx_disciplines"),
            board_url,
        )
        if source == "spacex" and not keyword and url_keyword:
            keyword = url_keyword
        if not keyword:
            flash("Enter a keyword.", "error")
            return redirect(url_for("jobs", scan=1))

        try:
            my_years = float(years_raw) if years_raw else None
            min_salary = float(salary_raw) if salary_raw else None
            days = int(days_raw) if days_raw else None
        except ValueError:
            flash("Years, salary and days must be numbers.", "error")
            return redirect(url_for("jobs", scan=1))

        kw_match = (request.form.get("kw_match") or "any").strip().lower()
        if kw_match not in ("any", "title", "preferred", "required"):
            kw_match = "any"
        filters = SearchFilters(
            keyword=keyword,
            remote_only=request.form.get("remote") == "1",
            my_years=my_years,
            min_salary=min_salary,
            posted_within_days=days,
            exclude_keywords=[p.strip() for p in exclude_raw.split(",") if p.strip()],
            title_include=[p.strip() for p in title_include_raw.split(",") if p.strip()],
            title_exclude=[p.strip() for p in title_exclude_raw.split(",") if p.strip()],
            job_type=job_type if job_type in JOB_TYPES else None,
            kw_match=kw_match,
            fetch_description=request.form.get("full_text") == "1" or source == "spacex",
            source=source,
            board_programs=board_programs if source == "spacex" else [],
            board_locations=board_locations if source == "spacex" else [],
            board_disciplines=board_disciplines if source == "spacex" else [],
            board_url=board_url if source == "spacex" else "",
        )
        if source != "spacex":
            locations = get_enabled_locations(_uid())
            if not locations:
                flash("Enable at least one location first.", "error")
                return redirect(url_for("jobs", scan=1))

        try:
            ahead = _enqueue_scan(_uid(), filters)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("jobs", scan=1))
        where = " on the SpaceX careers page" if source == "spacex" else ""
        if ahead:
            flash(f"Scan for {keyword!r}{where} queued behind {ahead} other scan(s).", "info")
        else:
            flash(f"Scan started for {keyword!r}{where}.", "success")
        return redirect(url_for("jobs", scan=1))

    return redirect(url_for("jobs", scan=1))


@app.get("/scrape/status")
def scrape_status():
    snap = _scan_snapshot(_uid())
    return jsonify(
        {
            "running": snap["running"],
            "queued": snap["queued"],
            "ahead": snap["ahead"],
            "log": snap["log"][-80:],
            "result": snap["result"],
            "error": snap["error"],
        }
    )


@app.post("/scrape/clear-log")
def scrape_clear_log():
    if not _clear_scan_log(_uid()):
        flash("Wait for your scan to finish before clearing the log.")
        return redirect(url_for("jobs", scan=1))
    flash("Scan log cleared.")
    return redirect(url_for("jobs", scan=1))


@app.post("/hygiene/keyword")
def hygiene_delete_keyword():
    keyword = (request.form.get("keyword") or "").strip()
    if not keyword:
        flash("Pick a scan to delete.")
        return redirect(url_for("jobs"))

    def n_jobs(count: int) -> str:
        return "1 job" if count == 1 else f"{count} jobs"

    try:
        result = delete_jobs_for_keyword(_uid(), keyword)
        deleted = int(result["deleted"])
        kept = int(result.get("kept") or 0)
        if deleted and kept:
            flash(
                f"Removed {keyword!r}. Deleted {n_jobs(deleted)}. "
                f"Kept {n_jobs(kept)} that also matched another scan."
            )
        elif deleted:
            flash(f"Removed {keyword!r} and deleted {n_jobs(deleted)}.")
        elif kept:
            flash(f"Removed {keyword!r}. Kept {n_jobs(kept)} that also matched another scan.")
        else:
            flash(f"Removed {keyword!r}.")
    except ValueError as exc:
        flash(str(exc))
    return redirect(url_for("jobs"))


@app.post("/hygiene/run/<int:run_id>")
def hygiene_delete_run(run_id: int):
    try:
        delete_run(_uid(), run_id)
        flash(f"Removed run #{run_id} from history.")
    except ValueError as exc:
        flash(str(exc))
    return redirect(url_for("jobs", scan=1))


@app.route("/tracker")
def tracker():
    view = (request.args.get("view") or "pipeline").strip()
    status = (request.args.get("status") or "").strip()
    if status and status not in STATUSES:
        status = ""
    filters = {
        "keyword": request.args.get("keyword") or "",
        "q": request.args.get("q") or "",
        "status": status,
        "heard_back": request.args.get("heard_back") or "",
        "pipeline_only": view == "pipeline" and not status,
        "overdue": view == "overdue",
        "order": request.args.get("order") or "follow_up",
    }
    per_page = 200
    rows, total = query_jobs(_uid(), filters, limit=per_page, offset=0)
    summary = tracker_summary(_uid())
    return render_template(
        "tracker.html",
        jobs=rows,
        total=total,
        shown=len(rows),
        filters=filters,
        view=view,
        summary=summary,
        keywords=list_keywords(_uid()),
        statuses=STATUSES,
        status_labels=STATUS_LABELS,
        pipeline_statuses=PIPELINE_STATUSES,
        heard_back=HEARD_BACK,
        resumes=list_materials(_uid(), "resume"),
        covers=list_materials(_uid(), "cover"),
    )


@app.post("/templates")
def save_template():
    try:
        template_id = save_filter_template(
            _uid(),
            (request.form.get("kind") or "").strip(),
            (request.form.get("name") or "").strip(),
            (request.form.get("value") or "").strip(),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "id": template_id})


@app.route("/materials", methods=["GET", "POST"])
def materials():
    if request.method == "GET":
        return redirect(url_for("tracker") + "#materials")
    kind = (request.form.get("kind") or "").strip()
    name = (request.form.get("name") or "").strip()
    body = (request.form.get("body") or "").strip()
    uploaded = request.files.get("file")
    try:
        filename, stored = _store_upload(uploaded)
        if kind == "resume" and not filename and not body:
            raise ValueError("Choose a resume file to upload")
        if not name and filename:
            name = Path(filename).stem.replace("_", " ").strip()
        material_id = add_material(
            _uid(),
            kind,
            name,
            filename=filename,
            path=stored,
            body=body or None,
            is_default=request.form.get("is_default") == "1",
        )
        flash(f"Saved {kind} {name!r}.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("tracker") + "#materials")
    if kind == "resume":
        loaded = resume_profile_for_material(_uid(), material_id)
        note = (loaded[0].get("extract_note") if loaded else None) or ""
        if note:
            flash(note, "error")
        elif loaded and not (loaded[1].skills or loaded[1].languages or loaded[1].certifications):
            flash("Saved the file, but no skills, languages, or certs were detected. Check the readout and add anything it missed.", "info")
        return redirect(url_for("resume_readout", material_id=material_id))
    return redirect(url_for("tracker") + "#materials")


@app.route("/materials/<int:material_id>/readout", methods=["GET", "POST"])
def resume_readout(material_id: int):
    loaded = resume_profile_for_material(_uid(), material_id)
    if loaded is None:
        flash("Resume not found.", "error")
        return redirect(url_for("tracker") + "#materials")
    material, _parsed = loaded
    if request.method == "POST":
        edits = load_overrides(material.get("overrides"))
        edits = _apply_readout_action(
            edits,
            (request.form.get("action") or "").strip(),
            request.form.get("value") or "",
            request.form.get("degree_level") or "",
        )
        try:
            save_resume_overrides(_uid(), material_id, edits)
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("resume_readout", material_id=material_id))
    material, parsed = resume_profile_for_material(_uid(), material_id) or (material, _parsed)
    edits = load_overrides(material.get("overrides"))
    if edits.get("clear_degree"):
        degree_choice = "none"
    elif edits.get("degree_level") is not None:
        degree_choice = str(edits["degree_level"])
    else:
        degree_choice = "detected"
    skills = list(parsed.skills)
    for extra in parsed.extra_keywords:
        if extra not in skills:
            skills.append(extra)
    known = set(parsed.languages) | set(skills) | set(parsed.certifications)
    suggestions: list[dict[str, str]] = []
    try:
        phrases = unknown_skill_phrases(parsed.raw_text, known)
        suggestions = suggest_unknown_phrases(phrases)
    except Exception:
        suggestions = []
    held = {item.casefold() for item in skills}
    suggestions = [item for item in suggestions if item["label"].casefold() not in held]
    years = parsed.experience_years
    if years is None:
        years_label = "none detected"
    elif float(years).is_integer():
        years_label = str(int(years))
    else:
        years_label = f"{years:g}"
    return render_template(
        "resume_readout.html",
        material=material,
        parsed=parsed,
        skills=skills,
        suggestions=suggestions,
        degree_choice=degree_choice,
        degree_labels=sorted(DEGREE_LABELS.items()),
        years_label=years_label,
        clearance_label=CLEARANCE_LABELS.get(parsed.clearance or "none", parsed.clearance or "none"),
    )


def _job_for_ai(job_id: int) -> dict[str, Any] | None:
    job = get_job(_uid(), job_id)
    return job


def _resume_for_job(job: dict[str, Any]) -> tuple[dict[str, Any] | None, str, Any]:
    """The resume on this job, or the default resume. Experience text comes from headings."""
    material_id = job.get("resume_id")
    loaded = None
    if material_id:
        loaded = resume_profile_for_material(_uid(), int(material_id))
    if loaded is None:
        rows = list_materials(_uid(), "resume")
        if rows:
            loaded = resume_profile_for_material(_uid(), rows[0]["id"])
    if loaded is None:
        return None, "", None
    material, parsed = loaded
    return material, experience_section(parsed.raw_text), parsed


def _quiet(payload: dict[str, Any] | None = None):
    body = {"ok": False, "silent": True}
    if payload:
        body.update(payload)
        body["ok"] = False
        body["silent"] = True
    return jsonify(body)


@app.post("/jobs/<int:job_id>/explain")
def job_explain(job_id: int):
    return _ai_response(job_id, "explain")


@app.post("/jobs/<int:job_id>/cover-draft")
def job_cover_draft(job_id: int):
    return _ai_response(job_id, "cover")


@app.post("/jobs/<int:job_id>/bullets")
def job_bullets(job_id: int):
    return _ai_response(job_id, "bullets")


@app.post("/jobs/<int:job_id>/interview")
def job_interview(job_id: int):
    return _ai_response(job_id, "interview")


def _ai_response(job_id: int, kind: str):
    if not ai_settings()["ready"]:
        return jsonify({
            "ok": False,
            "message": "Set AI_API_KEY to explain jobs and draft cover letters.",
        })
    job = _job_for_ai(job_id)
    if job is None:
        return jsonify({"ok": False, "message": "Job not found."}), 404
    keyword = (request.args.get("keyword") or "").strip()
    fit = job_fit(_uid(), job, keyword)
    _material, experience, parsed = _resume_for_job(job)
    try:
        if kind == "explain":
            text = explain_job(job, fit)
            if not text:
                return _quiet()
            return jsonify({"ok": True, "kind": kind, "text": text})
        if kind == "cover":
            text = draft_cover(job, fit, experience)
            if not text:
                return _quiet()
            return jsonify({"ok": True, "kind": kind, "text": text})
        if kind == "bullets":
            text = tailor_bullets(experience, job.get("description") or "", job_id=job_id)
            if not text:
                return _quiet()
            return jsonify({"ok": True, "kind": kind, "text": text})
        if kind == "interview":
            held: list[str] = []
            if parsed is not None:
                for group in (parsed.skills, parsed.languages, parsed.certifications, parsed.extra_keywords):
                    for item in group:
                        if item not in held:
                            held.append(item)
            questions = interview_questions(
                held,
                fit.get("missing_required") or [],
                job.get("description") or "",
                job_id=job_id,
            )
            if not questions:
                return _quiet()
            return jsonify({"ok": True, "kind": kind, "questions": questions})
    except Exception:
        return _quiet()
    return _quiet()


@app.post("/jobs/<int:job_id>/bullets-save")
def job_bullets_save(job_id: int):
    job = get_job(_uid(), job_id)
    if job is None:
        return jsonify({"ok": False, "message": "Job not found."}), 404
    data = request.get_json(silent=True) or {}
    bullets = (data.get("body") or "").strip()
    material, experience, _parsed = _resume_for_job(job)
    if material is None or not bullets:
        return jsonify({"ok": False, "message": "Nothing to save."}), 400
    if not bullets_are_grounded(bullets, experience):
        return jsonify({
            "ok": False,
            "message": "That draft adds a tool or duty that is not in the experience section.",
        }), 400
    path_before = material.get("path")
    updated = splice_experience(_parsed.raw_text if _parsed is not None else "", bullets)
    try:
        save_resume_extracted_text(_uid(), int(material["id"]), updated)
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    stored = get_material(_uid(), int(material["id"]))
    if stored is None or stored.get("path") != path_before:
        return jsonify({"ok": False, "message": "The resume file was left unchanged."}), 400
    return jsonify({"ok": True})


@app.post("/jobs/<int:job_id>/follow-up-note")
def job_follow_up_note(job_id: int):
    job = get_job(_uid(), job_id)
    if job is None:
        return jsonify({"ok": False, "message": "Job not found."}), 404
    status_before = job.get("status")
    notes_before = job.get("notes") or ""
    note = follow_up_note(status_before or "", notes_before)
    stored = get_job(_uid(), job_id)
    if stored is None or stored.get("status") != status_before or (stored.get("notes") or "") != notes_before:
        return jsonify({"ok": False, "message": "Status was left unchanged."}), 400
    if not note:
        return _quiet()
    return jsonify({"ok": True, "notes": note})


@app.post("/scrape/suggest-keyword")
def suggest_keyword():
    resume = get_user_resume_profile(_uid())
    if resume is None:
        return _quiet()
    keyword = suggest_scan_keyword(
        [*resume.skills, *resume.extra_keywords],
        resume.languages,
        resume.certifications,
    )
    if not keyword:
        return _quiet()
    return jsonify({"ok": True, "keyword": keyword})


@app.post("/jobs/<int:job_id>/cover-save")
def job_cover_save(job_id: int):
    job = get_job(_uid(), job_id)
    if job is None:
        return jsonify({"ok": False, "message": "Job not found."}), 404
    data = request.get_json(silent=True) or {}
    body = (data.get("body") or "").strip()
    if not body:
        return jsonify({"ok": False, "message": "Nothing to save."}), 400
    title = (job.get("title") or "role").strip() or "role"
    name = (data.get("name") or "").strip() or f"Cover · {title[:48]}"
    try:
        material_id = add_material(_uid(), "cover", name, body=body)
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    return jsonify({"ok": True, "id": material_id, "name": name})


@app.post("/materials/<int:material_id>/default")
def materials_default(material_id: int):
    try:
        set_default_material(_uid(), material_id)
        flash("Default updated.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("tracker") + "#materials")


@app.post("/materials/<int:material_id>/delete")
def materials_delete(material_id: int):
    try:
        delete_material(_uid(), material_id)
        flash("Deleted from vault.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("tracker") + "#materials")


@app.get("/materials/<int:material_id>/file")
def materials_file(material_id: int):
    material = get_material(_uid(), material_id)
    if material is None or not material.get("path"):
        flash("No file attached.")
        return redirect(url_for("tracker") + "#materials")
    path = Path(material["path"])
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    if not path.is_file():
        flash("File is missing on disk.")
        return redirect(url_for("tracker") + "#materials")
    return send_file(
        path,
        as_attachment=True,
        download_name=material.get("filename") or path.name,
    )


def _store_upload(uploaded) -> tuple[str | None, str | None]:
    if uploaded is None or not uploaded.filename:
        return None, None
    filename = secure_filename(uploaded.filename)
    if not filename:
        raise ValueError("That filename is not usable")
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_MATERIAL_EXT:
        raise ValueError("Use PDF, Word, text, Markdown, or RTF")
    MATERIALS_DIR.mkdir(parents=True, exist_ok=True)
    stored = MATERIALS_DIR / f"{uuid.uuid4().hex}{ext}"
    uploaded.save(stored)
    return filename, str(stored.relative_to(Path(__file__).resolve().parent))


@app.route("/profile", methods=["GET", "POST"])
def profile_page():
    if request.method == "POST":
        if request.form.get("action") == "clear":
            clear_profile(_uid())
            flash(
                "Profile cleared. Jobs, Insights and the scan form no longer use it.",
                "info",
            )
            return redirect(url_for("profile_page"))
        try:
            save_profile(
                _uid(),
                years=request.form.get("years"),
                certs=request.form.getlist("certs"),
                clearance=request.form.get("clearance") or "none",
                min_pay=request.form.get("min_pay"),
                remote_pref=request.form.get("remote_pref") or "any",
            )
            jobs_url = url_for("jobs")
            flash(
                Markup(
                    f'Profile saved. These defaults prefill Jobs filters and new scans. '
                    f'<a href="{escape(jobs_url)}">Open Jobs</a>'
                ),
                "success",
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("profile_page"))
        nxt = (request.form.get("next") or "").strip()
        return redirect(nxt if nxt.startswith("/") else url_for("profile_page"))
    saved = get_profile(_uid())
    return render_template(
        "profile.html",
        profile=saved,
        form=saved or {"years": None, "certs": [], "clearance": "none", "min_pay": None, "remote_pref": "any"},
        cert_labels=[label for label, _term in CERT_STACK],
        clearances=[
            (key, "None" if key == "none" else CLEARANCE_LABELS[key]) for key in PROFILE_CLEARANCES
        ],
        remote_prefs=REMOTE_PREFS,
    )


def main() -> None:
    init_db()
    try:
        import pymupdf  # noqa: F401
    except ImportError:
        print("WARNING: pymupdf is not installed, so PDF resumes will not parse.")
        print("         From this folder: venv\\Scripts\\python.exe -m pip install -r requirements.txt")
    print("Win Role  ->  http://127.0.0.1:5050")
    app.run(host="127.0.0.1", port=5050, debug=False, threaded=True)


if __name__ == "__main__":
    main()
