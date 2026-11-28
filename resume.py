"""Resume text extraction, structured parsing, and resume-based Heat Score."""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date
from functools import cached_property
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from textutil import (
    CLEARANCE_LABELS,
    CLEARANCE_RANK,
    aws_solutions_architect_cert_body,
    classify_clearance,
    keyword_pattern,
    mention_stance,
)
from years import parse_years


# ---------------------------------------------------------------------------
# File text extraction
# ---------------------------------------------------------------------------

def extract_resume_text(path: Path | str) -> str:
    """Extract plain text from a resume file (.pdf, .docx, .txt, .md, .rtf, .doc)."""
    text, _problem = extract_resume_detail(path)
    return text


def extract_resume_detail(path: Path | str) -> tuple[str, str | None]:
    """Return (text, problem). problem is a user-facing reason when text is empty."""
    p = Path(path)
    if not p.is_file():
        return "", "The uploaded file is missing on disk."
    ext = p.suffix.lower()
    try:
        if ext in {".txt", ".md"}:
            text = p.read_text(encoding="utf-8", errors="replace").strip()
            return text, None if text else "That text file is empty."
        if ext == ".pdf":
            return _extract_pdf(p)
        if ext == ".docx":
            text = _extract_docx(p)
            return text, None if text else "Couldn't read text from this Word file."
        if ext == ".rtf":
            text = _extract_rtf(p.read_text(encoding="utf-8", errors="replace"))
            return text, None if text else "Couldn't read text from this RTF file."
        if ext == ".doc":
            text = _extract_doc_fallback(p)
            return text, None if text else "Couldn't read text from this .doc file. Save it as PDF or .docx and upload that."
    except Exception as exc:
        return "", f"Couldn't read this file ({exc})."
    return "", "Use PDF, Word, text, Markdown, or RTF."


def _extract_pdf(path: Path) -> tuple[str, str | None]:
    """Read a text layer with PyMuPDF, then pypdf. Say why when both come up empty."""
    problems: list[str] = []
    try:
        import pymupdf
    except ImportError:
        pymupdf = None
        problems.append("PyMuPDF is not installed")
    if pymupdf is not None:
        try:
            text, image_only, locked = _pymupdf_text(pymupdf, path)
        except Exception as exc:
            text, image_only, locked = "", False, False
            problems.append(f"PyMuPDF could not open it ({exc})")
        else:
            if locked:
                return "", "This PDF is password protected. Remove the password and upload it again."
            if text:
                return text, None
            if image_only:
                return "", "This PDF has no selectable text (it looks scanned). Upload a text PDF, a Word file, or paste the resume."
    try:
        from pypdf import PdfReader
    except ImportError:
        PdfReader = None
        problems.append("pypdf is not installed")
    if PdfReader is not None:
        try:
            reader = PdfReader(str(path))
            if getattr(reader, "is_encrypted", False):
                return "", "This PDF is password protected. Remove the password and upload it again."
            text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
        except Exception as exc:
            text = ""
            problems.append(f"pypdf could not open it ({exc})")
        if text:
            return text, None
    if problems:
        return "", "Couldn't read this PDF (" + "; ".join(problems) + "). Install pymupdf in the app virtualenv and restart."
    return "", "Couldn't find any text in this PDF."


def _pymupdf_text(pymupdf: Any, path: Path) -> tuple[str, bool, bool]:
    """Return (text, image_only, locked). sort=True keeps multi-column resumes in reading order."""
    parts: list[str] = []
    image_pages = 0
    with pymupdf.open(str(path)) as doc:
        if doc.is_encrypted and not doc.authenticate(""):
            return "", False, True
        for page in doc:
            text = (page.get_text("text", sort=True) or "").strip()
            if len(text) < 40:
                blocks = page.get_text("blocks", sort=True) or []
                block_text = "\n".join(
                    block[4].strip()
                    for block in blocks
                    if len(block) > 4 and isinstance(block[4], str) and block[4].strip()
                )
                if len(block_text) > len(text):
                    text = block_text
            if text:
                parts.append(text)
            elif page.get_images():
                image_pages += 1
        page_count = doc.page_count
    joined = "\n\n".join(parts).strip()
    image_only = not joined and page_count > 0 and image_pages == page_count
    return joined, image_only, False


def _extract_docx(path: Path) -> str:
    """Read paragraphs and table cells from word/document.xml using stdlib only."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n in {"word/document.xml", "word/header1.xml", "word/header2.xml"}]
            if "word/document.xml" not in names:
                return ""
            lines: list[str] = []
            for name in names:
                xml_bytes = zf.read(name)
                root = ElementTree.fromstring(xml_bytes)
                for para in root.iter():
                    if para.tag.endswith("}p"):
                        chunks = [
                            node.text
                            for node in para.iter()
                            if node.tag.endswith("}t") and node.text
                        ]
                        line = "".join(chunks).strip()
                        if line:
                            lines.append(line)
            return "\n".join(lines).strip()
    except Exception:
        return ""


def _extract_rtf(raw: str) -> str:
    # Remove RTF destinations like {\fonttbl ...}
    text = re.sub(r"\{\\\*?[a-zA-Z]+[^}]*\}", " ", raw)
    text = re.sub(r"\\par\b|\\line\b", "\n", text)
    text = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes.fromhex(m.group(1)).decode("latin1", errors="ignore"), text)
    text = re.sub(r"\\[a-zA-Z]+-?\d*\s?", " ", text)
    text = text.replace("{", " ").replace("}", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_doc_fallback(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    runs = re.findall(rb"[ -~\r\n\t]{5,}", raw)
    return "\n".join(r.decode("ascii", errors="ignore").strip() for r in runs).strip()


# ---------------------------------------------------------------------------
# Catalogs: Degrees, Programming Languages, Certifications, Skills, Roles
# ---------------------------------------------------------------------------

DEGREE_LABELS = {
    0: "High School / GED",
    1: "Associate's",
    2: "Bachelor's",
    3: "Master's",
    4: "Doctorate",
}

_DEGREE_PATTERNS: tuple[tuple[int, re.Pattern[str]], ...] = (
    (4, re.compile(
        r"\b(?:ph\.?\s*d\.?|doctorate|doctoral(?:\s+degree)?|doctor\s+of\s+(?:philosophy|science|engineering))\b",
        re.I,
    )),
    (3, re.compile(
        r"\b(?:master'?s?(?:\s+degree)?|master\s+of\s+(?:science|arts|engineering|business|information|cybersecurity|technology)|"
        r"m\.s\.|m\.sc\.?|m\.a\.|m\.eng\.?|m\.c\.s\.|mba)\b|"
        r"(?<![A-Za-z])M\.?S\.?(?=\s+(?:in\b|of\b|degree\b|computer|information|cyber|data|software|electrical|network|engineering))|"
        r"(?<![A-Za-z])M\.?S\.?(?=\s*[/,]\s*(?:Ph\.?D|B\.?S|Computer|Information|Cyber|Data|Software|Engineering))",
        re.I,
    )),
    (2, re.compile(
        r"\b(?:bachelor'?s?(?:\s+degree)?|bachelor\s+of\s+(?:science|arts|engineering|technology|applied|business)|"
        r"b\.s\.|b\.sc\.?|b\.a\.|b\.eng\.?|b\.tech\.?|b\.s\.e\.?|4[- ]year\s+degree|undergraduate\s+degree)\b|"
        r"(?<![A-Za-z])B\.?[SA]\.?(?=\s+(?:in\b|of\b|degree\b|or\b|computer|information|cyber|data|software|electrical|network|engineering|math|business|science))|"
        r"(?<![A-Za-z])B\.?[SA]\.?(?=\s*[/,]\s*(?:M\.?S|B\.?[SA]|Computer|Information|Cyber|Data|Software|Engineering))",
        re.I,
    )),
    (1, re.compile(
        r"\b(?:associate'?s?(?:\s+degree)?|associate\s+of\s+(?:science|arts|applied)|a\.a\.s\.?|a\.s\.|2[- ]year\s+degree)\b",
        re.I,
    )),
    (0, re.compile(r"\b(?:high\s+school\s+diploma|g\.?e\.?d\.?)\b", re.I)),
)

DEGREE_FIELDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Computer Science", re.compile(r"\bcomputer\s+science\b", re.I)),
    ("Cybersecurity", re.compile(r"\b(?:cyber\s*security|cyber\s+defense)\b", re.I)),
    ("Information Security", re.compile(r"\binformation\s+security\b", re.I)),
    ("Information Technology", re.compile(r"\binformation\s+technology\b", re.I)),
    ("Information Systems", re.compile(r"\b(?:management\s+)?information\s+systems\b|\bMIS\b")),
    ("Software Engineering", re.compile(r"\bsoftware\s+engineering\b", re.I)),
    ("Computer Engineering", re.compile(r"\bcomputer\s+engineering\b", re.I)),
    ("Electrical Engineering", re.compile(r"\belectrical\s+engineering\b", re.I)),
    ("Network Engineering", re.compile(r"\bnetwork(?:ing)?\s+engineering\b", re.I)),
    ("Data Science", re.compile(r"\bdata\s+science\b", re.I)),
    ("Data Analytics", re.compile(r"\bdata\s+analytics\b", re.I)),
    ("Artificial Intelligence", re.compile(r"\bartificial\s+intelligence\b", re.I)),
    ("Cloud Computing", re.compile(r"\bcloud\s+computing\b", re.I)),
    ("Mathematics", re.compile(r"\bmathematics\b|\bapplied\s+math\b", re.I)),
    ("Statistics", re.compile(r"\bstatistics\b", re.I)),
    ("Physics", re.compile(r"\bphysics\b", re.I)),
    ("Business Administration", re.compile(r"\bbusiness\s+administration\b", re.I)),
    ("Finance", re.compile(r"\bfinance\b", re.I)),
    ("Economics", re.compile(r"\beconomics\b", re.I)),
    ("Accounting", re.compile(r"\baccounting\b", re.I)),
    ("Engineering", re.compile(r"\b(?:stem|engineering)\s+(?:degree|field|discipline)\b", re.I)),
)

# Programming & scripting languages: (canonical label, compiled pattern)
_PROGRAMMING_LANGUAGE_SPECS: tuple[tuple[str, str, int], ...] = (
    ("Python", r"\bpython(?:\s*3)?\b", re.I),
    ("JavaScript", r"\bjavascript\b|\bECMAScript\b|\bJS\b(?=\s*[,/)&+]|(?:\s+(?:framework|engineer|developer|code)))", re.I),
    ("TypeScript", r"\btypescript\b", re.I),
    ("Java", r"(?<![A-Za-z])java(?!\s*script)(?![A-Za-z0-9])", re.I),
    ("C++", r"(?<![A-Za-z0-9])c\+\+(?![A-Za-z0-9])", re.I),
    ("C#", r"(?<![A-Za-z0-9])(?:c#|c\s+sharp)(?![A-Za-z0-9])", re.I),
    ("C", r"(?<![A-Za-z0-9])C(?=\s*/\s*C\+\+|\s+(?:programming|language|code|compiler))|(?:(?<=,\s)|(?<=\(\s)|(?<=:\s))C(?=\s*[,/)])", 0),
    ("Go", r"\bgolang\b|(?:^|[,/(\s])Go(?=\s*(?:[,/)]|\s+(?:programming|language|lang|developer|engineer)))", 0),
    ("Rust", r"\brust(?:\s+(?:programming|language|lang|developer|engineer))?\b", re.I),
    ("SQL", r"\b(?:sql|t-sql|tsql|pl/sql|plsql)\b", re.I),
    ("Bash", r"\bbash\b", re.I),
    ("PowerShell", r"\bpowershell\b", re.I),
    ("Shell Scripting", r"\bshell\s+script(?:ing|s)?\b|\b(?:sh|zsh|ksh)\s+script", re.I),
    ("Ruby", r"\bruby\b", re.I),
    ("PHP", r"\bphp\b", re.I),
    ("Swift", r"\bswift\b", re.I),
    ("Kotlin", r"\bkotlin\b", re.I),
    ("Scala", r"\bscala\b", re.I),
    ("R", r"\bRStudio\b|\bR\s+(?:programming|language)\b|(?:(?<=,\s)|(?<=/)|(?<=:\s))R(?=\s*[,/)])", 0),
    ("Perl", r"\bperl\b", re.I),
    ("Lua", r"\blua\b", re.I),
    ("HTML", r"\bhtml5?\b", re.I),
    ("CSS", r"\bcss3?\b|\bsass\b|\bscss\b", re.I),
    ("Assembly", r"\bassembly\b|\bx86\s+asm\b|\barm\s+assembly\b", re.I),
    ("MATLAB", r"\bmatlab\b", re.I),
    ("VHDL", r"\bvhdl\b", re.I),
    ("Verilog", r"\bverilog\b|\bsystemverilog\b", re.I),
    ("Groovy", r"\bgroovy\b", re.I),
    ("Dart", r"\bdart\b", re.I),
    ("Elixir", r"\belixir\b", re.I),
    ("Haskell", r"\bhaskell\b", re.I),
    ("Objective-C", r"\bobjective[\s-]+c\b", re.I),
    ("VBA", r"\bvba\b|\bvisual\s+basic\b", re.I),
    ("GraphQL", r"\bgraphql\b", re.I),
    ("HCL", r"\bhcl\b", re.I),
    ("Solidity", r"\bsolidity\b", re.I),
    ("Julia", r"\bjulia\b(?=\s*[,/)]|\s+(?:programming|language))", re.I),
    ("COBOL", r"\bcobol\b", re.I),
    ("Apex", r"\bapex\b(?=\s*[,/)]|\s+(?:code|programming|triggers|salesforce))", re.I),
)

PROGRAMMING_LANGUAGES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pat, flags)) for label, pat, flags in _PROGRAMMING_LANGUAGE_SPECS
)
_LANG_BY_NAME: dict[str, re.Pattern[str]] = dict(PROGRAMMING_LANGUAGES)

# Certifications catalog: (canonical label, compiled pattern)
_CERT_SPECS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # CompTIA. Exam IDs (SY0-701, N10-008, …) are the cert even when the name is absent.
    ("A+", re.compile(keyword_pattern("A+", whole_word=True).pattern + r"|\b220-1[0-2]\d{2}\b", re.I)),
    ("Network+", re.compile(keyword_pattern("Network+", whole_word=True).pattern + r"|\bn10-\d{3}\b", re.I)),
    ("Security+", re.compile(keyword_pattern("Security+", whole_word=True).pattern + r"|\bsy0-\d{3}\b", re.I)),
    ("Linux+", keyword_pattern("Linux+", whole_word=True)),
    ("Server+", keyword_pattern("Server+", whole_word=True)),
    ("Cloud+", keyword_pattern("Cloud+", whole_word=True)),
    ("CySA+", re.compile(
        r"(?<![A-Za-z0-9])(?:comptia\s+)?cysa(?:\s*\+|[\s-]*plus\b)?(?![A-Za-z0-9])|\bcs0-\d{3}\b",
        re.I,
    )),
    ("PenTest+", re.compile(keyword_pattern("PenTest+", whole_word=True).pattern + r"|\bpt0-\d{3}\b", re.I)),
    ("CASP+", re.compile(
        r"(?<![A-Za-z])(?:comptia\s+)?(?:casp(?:\s*\+|[\s-]*plus\b)|securityx)\b"
        r"|\b(?:comptia\s+)?advanced\s+security\s+practitioner\b"
        r"|\bcas-00\d\b",
        re.I,
    )),
    ("Data+", keyword_pattern("Data+", whole_word=True)),
    ("DataSys+", re.compile(r"(?<![A-Za-z])(?:comptia\s+)?datasys(?:\s*\+|[\s-]*plus\b)", re.I)),
    ("Project+", keyword_pattern("Project+", whole_word=True)),
    ("ITF+", re.compile(
        r"(?<![A-Za-z])(?:comptia\s+)?itf(?:\s*\+|[\s-]*plus\b)|\bcomptia\s+it\s+fundamentals\b",
        re.I,
    )),
    # Cisco
    ("CCNA", keyword_pattern("CCNA", whole_word=True)),
    ("CCNP", re.compile(r"\bccnp\b|\bcisco\s+certified\s+network\s+professional\b", re.I)),
    ("CCIE", re.compile(r"\bccie\b", re.I)),
    ("CCST", re.compile(r"\bccst\b", re.I)),
    ("CyberOps", re.compile(r"\bcyberops\b", re.I)),
    # ISC2 & ISACA. Bare "CISA" is also the federal agency, so the agency's own words don't count.
    ("CISSP", re.compile(
        r"\bcissp\b|\bcertified\s+information\s+systems\s+security\s+professional\b",
        re.I,
    )),
    ("SSCP", re.compile(r"\bsscp\b", re.I)),
    ("CCSP", re.compile(r"\bccsp\b", re.I)),
    ("CISA", re.compile(
        r"\bcertified\s+information\s+systems\s+auditor\b"
        r"|\bcisa\b(?!(?:'s)?\s*(?:\(|[-–])?\s*(?:directive|guidelines?|alerts?|agency|"
        r"cybersecurity\s+and\s+infrastructure|region|binding|operational|issued|published))",
        re.I,
    )),
    ("CISM", re.compile(r"\bcism\b|\bcertified\s+information\s+security\s+manager\b", re.I)),
    ("CRISC", re.compile(r"\bcrisc\b", re.I)),
    # Security & GIAC
    ("OSCP", re.compile(r"\boscp\b|\boffensive\s+security\s+certified\s+professional\b", re.I)),
    ("CEH", re.compile(r"\bceh\b|\bcertified\s+ethical\s+hacker\b", re.I)),
    ("GSEC", re.compile(r"\bgsec\b|\bgiac\s+security\s+essentials\b", re.I)),
    ("GCIH", re.compile(r"\bgcih\b|\bgiac\s+certified\s+incident\s+handler\b", re.I)),
    ("GCIA", re.compile(r"\bgcia\b", re.I)),
    ("GCFA", re.compile(r"\bgcfa\b", re.I)),
    ("GPEN", re.compile(r"\bgpen\b", re.I)),
    ("GIAC", re.compile(r"\bgiac\b", re.I)),
    ("eJPT", re.compile(r"\bejpt\b", re.I)),
    ("PNPT", re.compile(r"\bpnpt\b", re.I)),
    ("BTL1", re.compile(r"\bbtl1\b", re.I)),
    # AWS
    ("AWS Solutions Architect", re.compile(aws_solutions_architect_cert_body(), re.I)),
    ("AWS Cloud Practitioner", re.compile(r"\baws(?:\s+certified)?\s+cloud\s+practitioner\b|\bclf-c\d\d\b", re.I)),
    ("AWS SysOps", re.compile(r"\baws(?:\s+certified)?\s+sysops\b|\bsoa-c\d\d\b", re.I)),
    ("AWS Developer", re.compile(
        r"\bdva-c\d\d\b|\baws\s+certified\s+developer\b|\baws\s+developer\s+(?:associate|certification)\b",
        re.I,
    )),
    ("AWS Security Specialty", re.compile(r"\baws(?:\s+certified)?\s+security\s*[-–]?\s*specialty\b|\bscs-c\d\d\b", re.I)),
    # Azure / Microsoft
    ("AZ-900", re.compile(r"\baz-?900\b|\bazure\s+fundamentals\b", re.I)),
    ("AZ-104", re.compile(
        r"\baz-?104\b|\bazure\s+administrator\s+associate\b|\bcertified\s+azure\s+administrator\b"
        r"|\bazure\s+administrator\b(?=[^.\n]{0,30}\b(?:certification|certified|cert)\b)",
        re.I,
    )),
    ("AZ-305", re.compile(
        r"\baz-?305\b|\bazure\s+solutions?\s+architect\s+(?:expert|certification)\b"
        r"|\bcertified\s+azure\s+solutions?\s+architect\b",
        re.I,
    )),
    ("AZ-500", re.compile(
        r"\baz-?500\b|\bazure\s+security\s+engineer\s+associate\b|\bcertified\s+azure\s+security\s+engineer\b",
        re.I,
    )),
    ("SC-900", re.compile(r"\bsc-?900\b", re.I)),
    ("SC-200", re.compile(r"\bsc-?200\b", re.I)),
    ("SC-300", re.compile(r"\bsc-?300\b", re.I)),
    ("MD-102", re.compile(r"\bmd-?102\b", re.I)),
    ("MS-102", re.compile(r"\bms-?102\b", re.I)),
    ("MCSA", re.compile(r"\bmcsa\b", re.I)),
    ("MCSE", re.compile(r"\bmcse\b", re.I)),
    # Cloud Native & Vendors
    ("Google Cloud Certified", re.compile(r"\bgoogle\s+cloud\s+certified\b|\bgcp\s+(?:associate|professional)\b", re.I)),
    ("CKA", re.compile(r"\bcka\b|\bcertified\s+kubernetes\s+administrator\b", re.I)),
    ("CKAD", re.compile(r"\bckad\b", re.I)),
    ("CKS", re.compile(r"\bcks\b|\bcertified\s+kubernetes\s+security\b", re.I)),
    ("Terraform Associate", re.compile(r"\bterraform\s+associate\b", re.I)),
    ("RHCSA", re.compile(
        r"\brhcsa\b|\bred\s+hat\s+certified\s+(?:system|systems)\s+administrator\b",
        re.I,
    )),
    ("RHCE", re.compile(r"\brhce\b", re.I)),
    ("LPIC", re.compile(r"\blpic-?[123]?\b", re.I)),
    ("VCP", re.compile(r"\bvcp(?:-dcv|-nv)?\b", re.I)),
    ("JNCIA", re.compile(r"\bjncia\b", re.I)),
    ("PCNSA", re.compile(r"\bpcnsa\b", re.I)),
    ("PCNSE", re.compile(r"\bpcnse\b", re.I)),
    ("Splunk", keyword_pattern("Splunk", whole_word=True)),
    ("ITIL", keyword_pattern("ITIL", whole_word=True)),
    ("PMP", re.compile(r"\bpmp\b|\bproject\s+management\s+professional\b", re.I)),
    ("CAPM", re.compile(r"\bcapm\b", re.I)),
    ("CSM", re.compile(
        r"\bcertified\s+scrum\s*master\b"
        r"|\bcsm\b(?=[^.\n]{0,40}\b(?:certification|certified|scrum)\b)"
        r"|\b(?:scrum|certification|certified)\b[^.\n]{0,48}\bcsm\b",
        re.I,
    )),
    ("DoD 8570 / 8140", re.compile(r"\b(?:dod\s+)?(?:8570|8140)\b|\biat\s+(?:level\s+)?(?:i{1,3}|[123])\b|\biam\s+(?:level\s+)?(?:i{1,3}|[123])\b", re.I)),
)

CERTIFICATIONS: tuple[tuple[str, re.Pattern[str]], ...] = _CERT_SPECS
_CERT_BY_NAME: dict[str, re.Pattern[str]] = dict(CERTIFICATIONS)
_CERT_CANON_LOWER: dict[str, str] = {label.lower(): label for label, _ in CERTIFICATIONS}

# Technical skills, tools, platforms, frameworks & concepts
_SKILL_ITEMS: tuple[tuple[str, str], ...] = (
    # Cloud & DevOps
    ("AWS", r"\baws\b|\bamazon\s+web\s+services\b"),
    ("Azure", r"\bazure\b"),
    ("GCP", r"\bgcp\b|\bgoogle\s+cloud(?:\s+platform)?\b"),
    ("Docker", r"\bdocker\b"),
    ("Kubernetes", r"\bkubernetes\b|\bk8s\b"),
    ("Helm", r"\bhelm\b"),
    ("Terraform", r"\bterraform\b"),
    ("Ansible", r"\bansible\b"),
    ("Puppet", r"\bpuppet\b"),
    ("Chef", r"\bchef\b(?=\s+(?:infra|automation|recipes|cookbooks)|[,/)])"),
    ("Jenkins", r"\bjenkins\b"),
    ("GitHub Actions", r"\bgithub\s+actions\b"),
    ("GitLab CI", r"\bgitlab\s+ci(?:/cd)?\b"),
    ("CI/CD", r"\bci\s*/\s*cd\b|\bcontinuous\s+integration\b"),
    ("Git", r"\bgit\b(?!hub|lab)"),
    ("GitHub", r"\bgithub\b"),
    ("GitLab", r"\bgitlab\b"),
    ("Bitbucket", r"\bbitbucket\b"),
    ("ArgoCD", r"\bargocd\b|\bargo\s+cd\b"),
    ("Prometheus", r"\bprometheus\b"),
    ("Grafana", r"\bgrafana\b"),
    ("Datadog", r"\bdatadog\b"),
    ("Nagios", r"\bnagios\b"),
    ("Zabbix", r"\bzabbix\b"),
    ("SolarWinds", r"\bsolarwinds\b"),
    ("ELK Stack", r"\belk(?:\s+stack)?\b|\belasticsearch\b"),
    ("Kibana", r"\bkibana\b"),
    ("Logstash", r"\blogstash\b"),
    ("CloudFormation", r"\bcloudformation\b"),
    ("AWS Lambda", r"\b(?:aws\s+)?lambda\b"),
    ("EC2", r"\bec2\b"),
    ("S3", r"\b(?:aws\s+)?s3\b"),
    ("VPC", r"\bvpc\b"),
    ("CloudWatch", r"\bcloudwatch\b"),
    ("ServiceNow", r"\bservicenow\b"),
    ("Jira", r"\bjira\b"),
    ("Confluence", r"\bconfluence\b"),
    ("Zendesk", r"\bzendesk\b"),
    # OS & Enterprise IT
    ("Linux", r"\blinux\b|\bubuntu\b|\brhel\b|\bred\s+hat\b|\bcentos\b|\bdebian\b"),
    ("Windows Server", r"\bwindows\s+server\b"),
    ("Windows", r"\bwindows\s+(?:10|11|os|desktop|administration|systems?)\b"),
    ("macOS", r"\bmacos\b|\bmac\s+os\b"),
    ("Active Directory", r"\bactive\s+directory\b"),
    ("Group Policy", r"\bgroup\s+policy\b|\bgpo\b"),
    ("Microsoft 365", r"\bmicrosoft\s+365\b|\boffice\s+365\b|\bm365\b|\bo365\b"),
    ("Exchange", r"\b(?:microsoft\s+)?exchange(?:\s+online|\s+server)?\b"),
    ("Intune", r"\bintune\b"),
    ("SCCM / MECM", r"\bsccm\b|\bmecm\b|\bendpoint\s+manager\b"),
    ("Entra ID / Azure AD", r"\bentra\s+id\b|\bazure\s+ad\b|\bazure\s+active\s+directory\b"),
    ("VMware", r"\bvmware\b|\bvsphere\b|\besxi\b|\bvcenter\b"),
    ("Hyper-V", r"\bhyper-?v\b"),
    ("Proxmox", r"\bproxmox\b"),
    ("Citrix", r"\bcitrix\b"),
    ("Jamf", r"\bjamf\b"),
    ("MDM", r"\bmdm\b|\bmobile\s+device\s+management\b"),
    # Networking
    ("TCP/IP", r"\btcp\s*/\s*ip\b|\btcpip\b"),
    ("DNS", r"\bdns\b"),
    ("DHCP", r"\bdhcp\b"),
    ("VLAN", r"\bvlans?\b"),
    ("VPN", r"\bvpns?\b|\bipsec\b"),
    ("BGP", r"\bbgp\b"),
    ("OSPF", r"\bospf\b"),
    ("MPLS", r"\bmpls\b"),
    ("SNMP", r"\bsnmp\b"),
    ("SSH", r"\bssh\b"),
    ("SSL/TLS", r"\bssl\s*/\s*tls\b|\btls\b|\bssl\b"),
    ("LDAP", r"\bldap\b"),
    ("SAML", r"\bsaml\b"),
    ("OAuth / OIDC", r"\boauth2?\b|\boidc\b|\bopenid\s+connect\b"),
    ("Kerberos", r"\bkerberos\b"),
    ("RADIUS", r"\bradius\b"),
    ("TACACS+", r"\btacacs\+?\b"),
    ("SD-WAN", r"\bsd-?wan\b"),
    ("Firewalls", r"\bfirewalls?\b"),
    ("Palo Alto", r"\bpalo\s+alto\b"),
    ("Fortinet", r"\bfortinet\b|\bfortigate\b"),
    ("Cisco", r"\bcisco\b"),
    ("Meraki", r"\bmeraki\b"),
    ("Juniper", r"\bjuniper\b"),
    ("Aruba", r"\baruba\b"),
    ("F5", r"\bf5\b|\bbig-?ip\b"),
    ("Wireshark", r"\bwireshark\b"),
    ("Nmap", r"\bnmap\b"),
    ("VoIP", r"\bvoip\b|\bsip\b"),
    ("Load Balancing", r"\bload\s+balanc(?:ing|ers?)\b"),
    ("Routing & Switching", r"\brouting\b|\bswitching\b"),
    ("Subnetting", r"\bsubnetting\b|\bcidr\b"),
    # Security
    ("SIEM", r"\bsiem\b"),
    ("SOAR", r"\bsoar\b"),
    ("EDR / XDR", r"\bedr\b|\bxdr\b"),
    ("IDS / IPS", r"\bids\b|\bips\b|\bintrusion\s+detection\b|\bintrusion\s+prevention\b"),
    ("SOC", r"\bsoc\b|\bsecurity\s+operations\s+center\b"),
    ("Incident Response", r"\bincident\s+response\b"),
    ("Threat Hunting", r"\bthreat\s+hunting\b|\bthreat\s+intelligence\b"),
    ("Vulnerability Management", r"\bvulnerability\s+(?:management|scanning|assessment)\b"),
    ("Penetration Testing", r"\bpenetration\s+testing\b|\bpen\s*testing\b"),
    ("NIST", r"\bnist\b"),
    ("ISO 27001", r"\biso\s*27001\b"),
    ("HIPAA", r"\bhipaa\b"),
    ("PCI DSS", r"\bpci[\s-]*dss\b"),
    ("SOC 2", r"\bsoc\s*2\b"),
    ("FedRAMP", r"\bfedramp\b"),
    ("RMF", r"\brmf\b|\brisk\s+management\s+framework\b"),
    ("STIG", r"\bstigs?\b|\bdisa\s+stig\b"),
    ("FISMA", r"\bfisma\b"),
    ("Zero Trust", r"\bzero\s+trust\b"),
    ("IAM", r"\biam\b|\bidentity\s+(?:and|&)\s+access\s+management\b"),
    ("PAM", r"\bpam\b|\bprivileged\s+access\b"),
    ("MFA / SSO", r"\bmfa\b|\bsso\b|\bsingle\s+sign-?on\b|\bmulti-?factor\b"),
    ("PKI", r"\bpki\b|\bpublic\s+key\s+infrastructure\b"),
    ("QRadar", r"\bqradar\b"),
    ("Microsoft Sentinel", r"\b(?:microsoft|azure)\s+sentinel\b"),
    ("CrowdStrike", r"\bcrowdstrike\b|\bfalcon\b"),
    ("SentinelOne", r"\bsentinelone\b"),
    ("Microsoft Defender", r"\bdefender\s+for\s+(?:endpoint|cloud|identity|office)\b|\bwindows\s+defender\b"),
    ("Nessus / Tenable", r"\bnessus\b|\btenable\b"),
    ("Qualys", r"\bqualys\b"),
    ("Rapid7", r"\brapid7\b|\bnexpose\b|\binsightvm\b"),
    ("Burp Suite", r"\bburp\s*suite\b"),
    ("Metasploit", r"\bmetasploit\b"),
    ("Snort / Suricata", r"\bsnort\b|\bsuricata\b"),
    ("Zeek", r"\bzeek\b"),
    ("Sysmon", r"\bsysmon\b"),
    ("MITRE ATT&CK", r"\bmitre\s+att&?ck\b"),
    ("OWASP", r"\bowasp\b"),
    ("Digital Forensics", r"\b(?:digital\s+)?forensics\b"),
    ("Malware Analysis", r"\bmalware\s+analysis\b"),
    ("DLP", r"\bdlp\b|\bdata\s+loss\s+prevention\b"),
    ("WAF", r"\bwaf\b|\bweb\s+application\s+firewall\b"),
    ("Okta", r"\bokta\b"),
    ("CyberArk", r"\bcyberark\b"),
    # Software, Web & Data
    ("React", r"\breact(?:\.?js)?\b"),
    ("Next.js", r"\bnext\.?js\b"),
    ("Angular", r"\bangular\b"),
    ("Vue.js", r"\bvue(?:\.?js)?\b"),
    ("Node.js", r"\bnode\.?js\b"),
    ("Express.js", r"\bexpress(?:\.?js)\b"),
    ("Django", r"\bdjango\b"),
    ("Flask", r"\bflask\b"),
    ("FastAPI", r"\bfastapi\b"),
    ("Spring Boot", r"\bspring\s*boot\b|\bspring\s+framework\b"),
    (".NET", r"(?<![A-Za-z0-9])\.net\b|\basp\.net\b"),
    ("Ruby on Rails", r"\bruby\s+on\s+rails\b|\brails\b"),
    ("Tailwind CSS", r"\btailwind\b"),
    ("Bootstrap", r"\bbootstrap\b"),
    ("REST API", r"\brest(?:ful)?\s+apis?\b|\bapi\s+integration\b"),
    ("gRPC", r"\bgrpc\b"),
    ("Microservices", r"\bmicroservices?\b"),
    ("PostgreSQL", r"\bpostgresql\b|\bpostgres\b"),
    ("MySQL", r"\bmysql\b|\bmariadb\b"),
    ("SQL Server", r"\bsql\s+server\b|\bmssql\b"),
    ("Oracle DB", r"\boracle\s+(?:db|database|sql)\b"),
    ("SQLite", r"\bsqlite\b"),
    ("MongoDB", r"\bmongodb\b|\bmongo\b"),
    ("Redis", r"\bredis\b"),
    ("DynamoDB", r"\bdynamodb\b"),
    ("Snowflake", r"\bsnowflake\b"),
    ("BigQuery", r"\bbigquery\b"),
    ("Kafka", r"\bkafka\b"),
    ("RabbitMQ", r"\brabbitmq\b"),
    ("Apache Spark", r"\b(?:apache\s+)?spark\b|\bpyspark\b"),
    ("Hadoop", r"\bhadoop\b"),
    ("Airflow", r"\bairflow\b"),
    ("dbt", r"\bdbt\b"),
    ("Pandas", r"\bpandas\b"),
    ("NumPy", r"\bnumpy\b"),
    ("PyTorch", r"\bpytorch\b"),
    ("TensorFlow", r"\btensorflow\b"),
    ("Scikit-learn", r"\bscikit-?learn\b|\bsklearn\b"),
    ("Machine Learning", r"\bmachine\s+learning\b"),
    ("Deep Learning", r"\bdeep\s+learning\b"),
    ("NLP", r"\bnlp\b|\bnatural\s+language\s+processing\b"),
    ("LLM / GenAI", r"\bllms?\b|\bgenerative\s+ai\b|\bgenai\b|\blangchain\b"),
    ("Tableau", r"\btableau\b"),
    ("Power BI", r"\bpower\s*bi\b"),
    ("Excel", r"\b(?:microsoft\s+)?excel\b"),
    ("ETL", r"\betl\b"),
    ("Data Warehousing", r"\bdata\s+warehous(?:e|ing)\b"),
    ("Agile / Scrum", r"\bagile\b|\bscrum\b"),
    ("SDLC", r"\bsdlc\b"),
    ("Unit Testing", r"\bunit\s+testing\b|\bpytest\b|\bjest\b|\bjunit\b"),
    ("Test Automation", r"\bselenium\b|\bplaywright\b|\bcypress\b|\btest\s+automation\b"),
    ("Networking", r"\bnetwork\s+(?:administration|engineering|security|infrastructure|troubleshooting)\b"),
    ("System Administration", r"\bsystem(?:s)?\s+administration\b|\bsysadmin\b"),
    ("Help Desk / Support", r"\bhelp\s*desk\b|\bservice\s*desk\b|\bdesktop\s+support\b|\btechnical\s+support\b"),
    ("Troubleshooting", r"\btroubleshooting\b"),
    ("DevOps", r"\bdevops\b|\bdevsecops\b"),
    ("Site Reliability", r"\bsite\s+reliability\b|\bsre\b"),
)

TECHNICAL_SKILLS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pat, re.I)) for label, pat in _SKILL_ITEMS
)
_SKILL_BY_NAME: dict[str, re.Pattern[str]] = dict(TECHNICAL_SKILLS)

# Extra spellings that should fold to a catalog label on both the resume and the job.
# Catalog regexes already catch many of these; the map is what typed chips and
# leftover keywords use, and what fills gaps like "psql" or bare "js".
SKILL_ALIASES: dict[str, tuple[str, ...]] = {
    "JavaScript": ("js", "ecmascript"),
    "PowerShell": ("pwsh", "posh"),
    "Go": ("golang",),
    "Kubernetes": ("k8s", "kube"),
    "PostgreSQL": ("postgres", "postgresql", "psql"),
    "MySQL": ("mariadb",),
    "MongoDB": ("mongo",),
    "Node.js": ("nodejs", "node js"),
    "React": ("reactjs", "react js"),
    "Vue.js": ("vuejs", "vue js"),
    "Next.js": ("nextjs",),
    "AWS": ("amazon web services",),
    "GCP": ("google cloud platform", "google cloud"),
    "SQL Server": ("mssql", "ms sql", "sqlserver"),
    "C#": ("csharp", "c sharp"),
    "C++": ("cpp", "c plus plus"),
    ".NET": ("dotnet",),
    "GitHub Actions": ("gha",),
    "CI/CD": ("cicd",),
    "Entra ID / Azure AD": ("azure ad", "entra id"),
    "REST API": ("restful",),
    "Power BI": ("powerbi",),
    "ELK Stack": ("elasticsearch", "elastic stack"),
    "Microsoft 365": ("m365", "o365", "office 365"),
    "Apache Spark": ("pyspark",),
    "Scikit-learn": ("sklearn", "scikit-learn"),
    "Ruby on Rails": ("rails",),
    "Spring Boot": ("springboot",),
    "Hyper-V": ("hyperv",),
    "TCP/IP": ("tcpip",),
    "DynamoDB": ("dynamo",),
}

_LANG_NAMES: frozenset[str] = frozenset(label for label, _ in PROGRAMMING_LANGUAGES)
_SKILL_NAMES: frozenset[str] = frozenset(label for label, _ in TECHNICAL_SKILLS)
_FIELD_BY_LOWER: dict[str, str] = {label.casefold(): label for label, _ in DEGREE_FIELDS}


def _alias_key(term: str) -> str:
    return re.sub(r"\s+", " ", (term or "").strip().casefold())


def _build_alias_lookup() -> dict[str, str]:
    lookup: dict[str, str] = {}
    for label in (*_LANG_NAMES, *_SKILL_NAMES):
        lookup[_alias_key(label)] = label
    for label, phrases in SKILL_ALIASES.items():
        if label not in _LANG_NAMES and label not in _SKILL_NAMES:
            continue
        lookup[_alias_key(label)] = label
        for phrase in phrases:
            key = _alias_key(phrase)
            if key:
                lookup[key] = label
    return lookup


_ALIAS_TO_CANON: dict[str, str] = _build_alias_lookup()


def _compile_alias_patterns() -> tuple[tuple[str, re.Pattern[str]], ...]:
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for label, phrases in SKILL_ALIASES.items():
        if label not in _LANG_NAMES and label not in _SKILL_NAMES:
            continue
        parts = [
            keyword_pattern(phrase, whole_word=True).pattern
            for phrase in phrases
            if _alias_key(phrase) and _alias_key(phrase) != _alias_key(label)
        ]
        if not parts:
            continue
        compiled.append((label, re.compile("|".join(f"(?:{part})" for part in parts), re.I)))
    return tuple(compiled)


ALIAS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = _compile_alias_patterns()

# Bump when the Heat Score formula changes. Stored beside the resume signature
# so cached rows from the previous formula are not reused.
SCORER_VERSION = "7"
SIMILAR_THRESHOLD = 0.78


def canonical_skill(term: str) -> str | None:
    """Catalog label for a skill or language spelling, or None if we don't know it."""
    key = _alias_key(term)
    if not key:
        return None
    return _ALIAS_TO_CANON.get(key)


def canonical_cert(term: str) -> str | None:
    key = _alias_key(term)
    if not key:
        return None
    return _CERT_CANON_LOWER.get(key)


def canonical_field(term: str) -> str:
    text = (term or "").strip()
    if not text:
        return ""
    return _FIELD_BY_LOWER.get(text.casefold(), text)


def required_skill_penalty(missing: int) -> float:
    """Points removed after normalization for each absent required skill, capped at 24."""
    if missing <= 0:
        return 0.0
    return float(min(8 * missing, 24))

# Common roles / job functions for experience-title alignment
_ROLE_ITEMS: tuple[tuple[str, str], ...] = (
    ("Software Engineer", r"\bsoftware\s+(?:engineer|developer)\b"),
    ("Full Stack Developer", r"\bfull[\s-]?stack\s+(?:engineer|developer)\b"),
    ("Backend Engineer", r"\bback[\s-]?end\s+(?:engineer|developer)\b"),
    ("Frontend Engineer", r"\bfront[\s-]?end\s+(?:engineer|developer)\b"),
    ("Web Developer", r"\bweb\s+(?:developer|engineer)\b"),
    ("Mobile Developer", r"\b(?:mobile|ios|android)\s+(?:developer|engineer)\b"),
    ("Systems Administrator", r"\bsystems?\s+admin(?:istrator)?\b|\bsysadmin\b"),
    ("Systems Engineer", r"\bsystems?\s+engineer\b"),
    ("Network Engineer", r"\bnetwork\s+engineer\b"),
    ("Network Administrator", r"\bnetwork\s+admin(?:istrator)?\b"),
    ("Network Technician", r"\bnetwork\s+technician\b|\bnoc\s+(?:technician|analyst|engineer)\b"),
    ("SOC Analyst", r"\bsoc\s+analyst\b|\bsecurity\s+operations\s+analyst\b"),
    ("Cybersecurity Analyst", r"\b(?:cyber\s*security|information\s+security|infosec)\s+analyst\b"),
    ("Security Engineer", r"\b(?:cyber\s*security|information\s+security|security)\s+engineer\b"),
    ("Security Architect", r"\bsecurity\s+architect\b"),
    ("Penetration Tester", r"\bpenetration\s+tester\b|\bred\s+team(?:er)?\b"),
    ("Vulnerability Analyst", r"\bvulnerability\s+(?:analyst|engineer)\b"),
    ("GRC Analyst", r"\bgrc\s+(?:analyst|specialist|engineer)\b|\bcompliance\s+analyst\b"),
    ("IAM Engineer", r"\biam\s+(?:engineer|analyst|administrator)\b"),
    ("Cloud Engineer", r"\bcloud\s+(?:engineer|administrator|architect)\b"),
    ("DevOps Engineer", r"\b(?:devops|devsecops|platform)\s+engineer\b"),
    ("Site Reliability Engineer", r"\bsite\s+reliability\s+engineer\b|\bsre\b"),
    ("Infrastructure Engineer", r"\binfrastructure\s+engineer\b"),
    ("Help Desk Technician", r"\b(?:help\s*desk|service\s*desk|desktop\s+support|it\s+support)\s*(?:technician|analyst|specialist|engineer)?\b"),
    ("IT Specialist", r"\bit\s+(?:specialist|technician|administrator|analyst)\b"),
    ("Field Technician", r"\bfield\s+(?:service\s+)?technician\b"),
    ("Application Support", r"\bapplication\s+support\b"),
    ("Database Administrator", r"\bdatabase\s+admin(?:istrator)?\b|\bdba\b"),
    ("Data Analyst", r"\bdata\s+analyst\b|\bbusiness\s+intelligence\s+analyst\b"),
    ("Data Engineer", r"\bdata\s+engineer\b"),
    ("Data Scientist", r"\bdata\s+scientist\b"),
    ("Machine Learning Engineer", r"\b(?:machine\s+learning|ml|ai)\s+engineer\b"),
    ("QA Engineer", r"\b(?:qa|quality\s+assurance|test|automation)\s+engineer\b"),
    ("Solutions Architect", r"\bsolutions?\s+architect\b"),
    ("Project Manager", r"\b(?:it\s+)?project\s+manager\b"),
    ("Product Manager", r"\bproduct\s+manager\b"),
    ("Business Analyst", r"\bbusiness\s+analyst\b"),
    ("Embedded Engineer", r"\b(?:embedded|firmware)\s+(?:software\s+)?engineer\b"),
)

ROLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pat, re.I)) for label, pat in _ROLE_ITEMS
)
_ROLE_BY_NAME: dict[str, re.Pattern[str]] = dict(ROLE_PATTERNS)


# ---------------------------------------------------------------------------
# Extra / custom keyword extraction from resume
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset({
    "a", "about", "above", "across", "active", "additional", "after", "all", "also", "am", "an", "and",
    "any", "application", "applications", "applied", "april", "are", "area", "areas", "as", "assisted",
    "at", "august", "available", "award", "awards", "based", "basic", "be", "been", "before", "being",
    "best", "between", "both", "build", "building", "built", "business", "but", "by", "can", "career",
    "certification", "certifications", "certified", "city", "client", "clients", "code", "collaborated",
    "college", "com", "communication", "company", "completed", "complex", "computer", "conducted",
    "configured", "contact", "coordinated", "core", "coursework", "create", "created", "cross", "current",
    "currently", "customer", "customers", "daily", "data", "date", "dates", "dec", "december", "degree",
    "delivered", "delivery", "department", "deployed", "design", "designed", "detail", "detailed",
    "develop", "developed", "developing", "development", "different", "directly", "do", "documented",
    "documentation", "does", "done", "driven", "during", "duties", "each", "education", "effective",
    "efficient", "email", "employee", "employees", "end", "engineer", "engineering", "ensured", "ensuring",
    "enterprise", "environment", "environments", "equipment", "etc", "evaluated", "excellent", "executed",
    "experience", "experienced", "expertise", "fast", "feb", "february", "field", "first", "focus",
    "focused", "for", "from", "full", "functional", "general", "github", "gmail", "goal", "goals",
    "good", "gpa", "graduated", "graduation", "great", "group", "had", "hands", "has", "have", "he",
    "help", "helped", "her", "here", "high", "his", "home", "honors", "hours", "how", "http", "https",
    "i", "identified", "if", "implemented", "improved", "improving", "in", "inc", "include", "included",
    "including", "increased", "independent", "industry", "information", "infrastructure", "initiatives",
    "installed", "internal", "into", "is", "issue", "issues", "it", "its", "jan", "january", "job",
    "july", "jun", "june", "key", "knowledge", "language", "languages", "large", "lead", "leader",
    "leadership", "leading", "learned", "led", "level", "linkedin", "list", "llc", "local", "location",
    "maintained", "maintaining", "maintenance", "major", "make", "manage", "managed", "management",
    "manager", "managing", "many", "mar", "march", "may", "member", "members", "mentored", "methods",
    "metrics", "minor", "monitored", "monitoring", "month", "months", "more", "most", "multiple", "must",
    "my", "name", "national", "need", "needed", "needs", "new", "no", "non", "not", "nov", "november",
    "number", "objective", "oct", "october", "of", "off", "office", "on", "one", "ongoing", "online",
    "only", "open", "operated", "operating", "operation", "operational", "operations", "optimized", "or",
    "order", "org", "organization", "organizational", "other", "our", "out", "over", "overall", "oversaw",
    "part", "participated", "partnered", "per", "performed", "performance", "personal", "phone", "plan",
    "planned", "planning", "platform", "platforms", "policies", "policy", "position", "practices",
    "prepared", "present", "presented", "previous", "primary", "prior", "problem", "problems", "procedures",
    "process", "processes", "product", "production", "products", "professional", "proficiency", "proficient",
    "program", "programming", "programs", "project", "projects", "provide", "provided", "providing",
    "public", "quality", "quick", "range", "received", "reduced", "references", "related", "relevant",
    "reliable", "remote", "report", "reported", "reporting", "reports", "request", "requests", "required",
    "requirements", "research", "resolution", "resolved", "resolving", "resource", "resources", "response",
    "responsibilities", "responsible", "results", "resume", "reviewed", "role", "roles", "run", "running",
    "same", "school", "science", "scripts", "section", "secure", "security", "self", "senior", "sep",
    "sept", "september", "server", "servers", "service", "services", "set", "several", "she", "site",
    "skill", "skills", "small", "software", "solution", "solutions", "solving", "some", "special",
    "specialist", "specific", "staff", "standard", "standards", "state", "states", "status", "strategic",
    "streamlined", "strong", "student", "studies", "study", "successfully", "such", "summary", "support",
    "supported", "supporting", "system", "systems", "tasks", "team", "teams", "tech", "technical",
    "technician", "technologies", "technology", "tested", "testing", "than", "that", "the", "their",
    "them", "then", "there", "these", "they", "this", "through", "time", "title", "to", "tool", "tools",
    "top", "total", "tracked", "tracking", "trained", "training", "troubleshooting", "two", "type", "under",
    "united", "university", "up", "updated", "updates", "upgraded", "upon", "us", "usa", "use", "used",
    "user", "users", "using", "utilized", "various", "verbal", "very", "via", "was", "we", "web", "weekly",
    "well", "were", "what", "when", "where", "which", "while", "who", "wide", "will", "with", "within",
    "without", "work", "worked", "working", "written", "wrote", "www", "year", "years", "you", "your",
})

# Heading label only, after bullets, numbers, and a trailing rule are stripped.
# Projects are matched before experience so "Project Experience" is not treated as a job.
_SECTION_LABELS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("education", re.compile(
        r"^(?:education|academic(?:\s+background)?|academics)"
        r"(?:\s*(?:&|and|/)\s*(?:training|certifications?|coursework))?$",
        re.I,
    )),
    ("projects", re.compile(
        r"^(?:(?:personal|academic|selected|key|relevant|school|technical|capstone|side|course)\s+)?"
        r"projects?(?:\s+(?:experience|highlights?|portfolio))?"
        r"(?:\s*(?:&|and|/)\s*contributions)?$",
        re.I,
    )),
    ("experience", re.compile(
        r"^(?:professional|work)$|"
        r"^(?:(?:work|professional|relevant|related|selected|additional|industry|career)\s+)?"
        r"(?:experience|employment(?:\s+history)?|work\s+history|career\s+history|professional\s+background)$",
        re.I,
    )),
    ("skills", re.compile(
        r"^(?:(?:technical|core|key|professional)\s+)?"
        r"(?:skills?(?:\s*(?:&|and|/)\s*(?:tools|technologies|abilities|competenc(?:y|ies)|expertise))?|"
        r"skill\s*set|technologies|tech(?:nical)?\s+stack|tools(?:\s*(?:&|and|/)\s*technologies)?|"
        r"competenc(?:y|ies)|proficiencies|areas?\s+of\s+expertise|expertise|"
        r"programming\s+languages?|languages?|certifications?|licenses?(?:\s*(?:&|and)\s*certifications?)?|clearance)$",
        re.I,
    )),
    ("summary", re.compile(
        r"^(?:summary|professional\s+summary|executive\s+summary|career\s+summary|objective|profile|about(?:\s+me)?)$",
        re.I,
    )),
    ("other", re.compile(
        r"^(?:awards?(?:\s*(?:&|and)\s*honors?)?|honors?|publications?|volunteer(?:ing)?(?:\s+experience)?|"
        r"activities|references?|military(?:\s+service)?|interests?|hobbies)$",
        re.I,
    )),
)
_ANY_SECTION_HEAD = re.compile(
    r"^\s*(?:experience|work\s+experience|professional\s+experience|employment(?:\s+history)?|"
    r"education|projects?|certifications?|licenses?|skills?|technical\s+skills?|technologies|"
    r"summary|professional\s+summary|objective|profile|awards?|honors?|publications?|"
    r"volunteer|activities|references?|languages?|clearance|military)\s*:?\s*$",
    re.I,
)
_EDU_SECTION_HEAD = re.compile(r"^\s*education(?:\s*(?:&|and)\s*training)?\s*:?\s*$", re.I)
_EXP_SECTION_HEAD = re.compile(
    r"^\s*(?:work\s+|professional\s+|relevant\s+|related\s+)?(?:experience|employment(?:\s+history)?|career\s+history)\s*:?\s*$",
    re.I,
)

# Date range parsing for resume work experience
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
    "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MONTH_RE = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
_DATE_POINT = rf"(?:(?P<m>{_MONTH_RE})\s+(?P<y>(?:19|20)\d\d)|(?P<num_m>0?[1-9]|1[0-2])/(?P<num_y>(?:19|20)\d\d)|(?P<only_y>(?:19|20)\d\d))"
_PRESENT = r"(?:present|current|now|ongoing|to\s+date)"
_DATE_RANGE_RE = re.compile(
    rf"(?P<s_m>{_MONTH_RE})?\s*(?P<s_num_m>(?:0?[1-9]|1[0-2])/)?(?P<s_y>(?:19|20)\d\d)"
    rf"\s*(?:[-–—]|to)\s*"
    rf"(?:(?P<present>{_PRESENT})|(?:(?P<e_m>{_MONTH_RE})?\s*(?P<e_num_m>(?:0?[1-9]|1[0-2])/)?(?P<e_y>(?:19|20)\d\d)))",
    re.I,
)
_EXPLICIT_RESUME_YEARS_RE = re.compile(
    r"\b(?:over\s+|more\s+than\s+|nearly\s+)?(\d{1,2}(?:\.\d)?)\s*\+?\s*(?:years?|yrs?)\s+"
    r"(?:of\s+)?(?:[\w-]+\s+){0,3}(?:experience|background|expertise)\b",
    re.I,
)
_ACADEMIC_LINE = re.compile(
    r"\b(?:university|college|bachelor|master|b\.s\.|m\.s\.|ph\.?d|associate'?s?\s+degree|high\s+school)\b",
    re.I,
)
_CREDENTIAL_LEAD = re.compile(
    r"^(?:ph\.?\s*d|doctor|master|bachelor|associate|m\.?\s*s|b\.?\s*s|m\.?\s*a|b\.?\s*a|mba|a\.?\s*s)\b",
    re.I,
)
_CREDENTIAL_CONTEXT = re.compile(r"\b(?:degree|university|college|major|graduat)\b", re.I)
_HEADING_MARK = re.compile(r"^(?:[#>*\-]+\s*)+")
_HEADING_BULLET = re.compile(r"^[•·▪◦●○■▸►‣⁃]+\s*")
_INVISIBLE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff\u00ad\u202a-\u202e\u2066-\u2069]")
_HEADING_INDEX = re.compile(r"^(?:\d{1,2}|[IVX]{1,4})[\).:\-]\s+", re.I)
_HEADING_RULE = re.compile(r"[\s|–—_=.*·•]{2,}$")
_SKILL_LIST_SPLIT = re.compile(r"[,|•·▪◦;]+")


def _month_index(year: int, month: int) -> int:
    return year * 12 + (month - 1)


def _parse_month(name: str | None, slash_num: str | None, default: int) -> int:
    if name:
        return _MONTHS.get(name.strip(".").lower(), default)
    if slash_num:
        try:
            return max(1, min(12, int(slash_num.strip("/"))))
        except ValueError:
            return default
    return default


def _explicit_year_values(text: str) -> list[float]:
    found: list[float] = []
    for m in _EXPLICIT_RESUME_YEARS_RE.finditer(text):
        try:
            val = float(m.group(1))
        except ValueError:
            continue
        if 0 < val <= 45:
            found.append(val)
    return found


def _date_intervals(text: str, today: date | None = None) -> list[tuple[int, int]]:
    """Employment date ranges, ignoring degree lines. One pass, no heading logic."""
    today = today or date.today()
    now_idx = _month_index(today.year, today.month)
    intervals: list[tuple[int, int]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or _ACADEMIC_LINE.search(line):
            continue
        for m in _DATE_RANGE_RE.finditer(line):
            sy = int(m.group("s_y"))
            if sy < 1975 or sy > today.year:
                continue
            sm = _parse_month(m.group("s_m"), m.group("s_num_m"), 1)
            start_i = _month_index(sy, sm)
            if m.group("present"):
                end_i = now_idx
            else:
                ey = int(m.group("e_y"))
                if ey < sy or ey > today.year + 1:
                    continue
                em = _parse_month(m.group("e_m"), m.group("e_num_m"), 12)
                end_i = min(_month_index(ey, em) + 1, now_idx + 1)
            if end_i > start_i and (end_i - start_i) <= 45 * 12:
                intervals.append((start_i, end_i))
    return intervals


def _combine_years(explicit: list[float], intervals: list[tuple[int, int]]) -> float | None:
    date_years: float | None = None
    if intervals:
        intervals.sort()
        merged: list[tuple[int, int]] = []
        for start, end in intervals:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        total_months = sum(end - start for start, end in merged)
        date_years = round((total_months / 12.0) * 2) / 2.0
        if date_years <= 0:
            date_years = None
    candidates = [v for v in (max(explicit) if explicit else None, date_years) if v is not None]
    if not candidates:
        return None
    return min(max(candidates), 45.0)


def estimate_resume_years(text: str) -> float | None:
    """Estimate total professional years from text that has not been sectioned."""
    if not text or not text.strip():
        return None
    # Education dates are not jobs. Everything else still counts for an unsectioned resume.
    kept: list[str] = []
    section = "top"
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if len(line) <= 45:
            if _EDU_SECTION_HEAD.match(line):
                section = "education"
                continue
            if _EXP_SECTION_HEAD.match(line):
                section = "experience"
                continue
            if _ANY_SECTION_HEAD.match(line):
                section = "other"
                continue
        if section != "education":
            kept.append(line)
    body = "\n".join(kept)
    return _combine_years(_explicit_year_values(text), _date_intervals(body))


def extract_degrees(text: str) -> tuple[int, str, list[str]]:
    """Return (highest_level, label, fields) detected in text. Level -1 if none."""
    if not text:
        return -1, "", []
    highest = -1
    for level, pat in _DEGREE_PATTERNS:
        if pat.search(text):
            highest = max(highest, level)
            break  # patterns are ordered highest -> lowest
    fields: list[str] = []
    if highest >= 0 or re.search(r"\b(?:degree|major|bs|ba|ms|ma|b\.s\.|m\.s\.|university|college)\b", text, re.I):
        for label, pat in DEGREE_FIELDS:
            if pat.search(text):
                fields.append(label)
    return highest, DEGREE_LABELS.get(highest, ""), fields


def extract_job_degree_requirement(text: str) -> tuple[int, list[str], bool]:
    """Return (min_required_level, fields, is_preferred_only) from a job posting.

    If a job says "Bachelor's or Master's degree", min_required_level is 2 (Bachelor's).
    If no degree is mentioned, min_required_level is -1.
    """
    if not text:
        return -1, [], False
    found_levels: list[int] = []
    for level, pat in _DEGREE_PATTERNS:
        for m in pat.finditer(text):
            # Ignore negated degree requirements ("no degree required")
            left = text[max(0, m.start() - 35) : m.start()]
            right = text[m.end() : m.end() + 35]
            if re.search(r"\b(?:no|not|without)\s*$", left, re.I) or re.search(r"^\s*(?:not\s+required|unnecessary)", right, re.I):
                continue
            found_levels.append(level)
            break
    if not found_levels:
        return -1, [], False
    # Lowest mentioned level is the minimum acceptable (e.g., Bachelor's in "BS or MS")
    min_level = min(found_levels)
    fields = [label for label, pat in DEGREE_FIELDS if pat.search(text)]
    # Check if the posting treats a degree purely as preferred / or equivalent
    pref_only = bool(
        re.search(
            r"\b(?:degree\s+(?:preferred|a\s+plus|desired)|or\s+(?:equivalent|comparable)\s+(?:work\s+)?experience)\b",
            text,
            re.I,
        )
    )
    return min_level, fields, pref_only


def _extract_custom_keywords(
    text: str,
    already_known: set[str],
    limit: int = 35,
) -> list[str]:
    """Pull distinctive domain/technical keywords from the resume not in curated lists."""
    if not text:
        return []
    known_lower = {k.lower() for k in already_known}
    # Also ignore individual words of already-known multi-word items so we don't duplicate
    known_words: set[str] = set(known_lower)
    for item in known_lower:
        for tok in re.split(r"[\s/&,-]+", item):
            if tok:
                known_words.add(tok)

    scores: dict[str, int] = {}
    display: dict[str, str] = {}

    def record(raw_term: str, weight: int) -> None:
        term = raw_term.strip(" .,;:|•·▪◦()[]{}\"'")
        if len(term) < 2 or len(term) > 36:
            return
        if re.fullmatch(r"[\d\W]+", term):
            return
        # Skip emails, urls, phone-like strings, pure numbers/years
        if "@" in term or "http" in term.lower() or re.search(r"\d{3}[-.)]\d{3}", term):
            return
        low = term.lower()
        if low in _STOPWORDS or low in known_lower or low in known_words:
            return
        words = low.split()
        if len(words) > 3:
            return
        if all(w in _STOPWORDS for w in words):
            return
        if len(words) == 1:
            # Single token: require either >= 3 chars or uppercase acronym/symbol (e.g. "QA", "BI", "F5")
            if len(term) == 2 and not term.isupper():
                return
            if low.endswith(("ing", "ed", "ly", "tion", "ment")) and weight < 3:
                return
        scores[low] = scores.get(low, 0) + weight
        if low not in display or (term[0].isupper() and not display[low][0].isupper()):
            display[low] = term

    section = "top"
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if len(line) <= 180:
            kind, rest = _classify_heading(line)
            if kind == "skills":
                section = "skills"
                for piece in _SKILL_LIST_SPLIT.split(rest):
                    chunk = piece.strip()
                    if chunk and len(chunk.split()) <= 3:
                        record(chunk, 4)
                continue
            if kind:
                section = "other"
                continue

        # In skills lines (or lines like "Skills: X, Y, Z"), split comma/pipe/bullet lists
        if section == "skills" or re.match(r"^(?:skills?|technologies|tools|languages|certifications?|software)\s*:", line, re.I):
            cleaned = re.sub(r"^[A-Za-z &/]{2,28}:\s*", "", line)
            for piece in _SKILL_LIST_SPLIT.split(cleaned):
                chunk = piece.strip()
                if chunk and len(chunk.split()) <= 3:
                    record(chunk, 4)

        # Capture uppercase acronyms (2..7 chars) like SCADA, SIEM, FHIR, HL7, SAP, ERP
        for m in re.finditer(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9+#/-]{1,6}(?![A-Za-z0-9])", line):
            tok = m.group(0).strip("-/")
            if len(tok) >= 2 and not re.fullmatch(r"(?:I|II|III|IV|US|USA|UK|CA|TX|NY|FL|IL|PA|OH|GA|NC|MI|NJ|VA|WA|AZ|MA|TN|IN|MO|MD|WI|CO|MN|SC|AL|LA|KY|OR|OK|CT|UT|IA|NV|AR|MS|KS|NM|NE|ID|WV|HI|NH|ME|MT|RI|DE|SD|ND|AK|VT|WY|DC|AM|PM|GPA|PDF|URL|ST|RD|AVE|DR|LN|BLVD)", tok):
                record(tok, 3)

        # Capture CamelCase or dotted tech terms (e.g. FastAPI, GraphQL, PowerBI, Node.js)
        for m in re.finditer(r"\b[A-Za-z]+(?:[A-Z][a-z]+|\.[A-Za-z]+)\b", line):
            record(m.group(0), 3)

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [display[k] for k, _ in ranked[:limit]]


# ---------------------------------------------------------------------------
# ResumeProfile & Fast Job Matching
# ---------------------------------------------------------------------------

@dataclass
class ResumeProfile:
    signature: str
    raw_text: str
    degree_level: int = -1
    degree_label: str = ""
    degree_fields: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    certifications: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    experience_years: float | None = None
    role_keywords: list[str] = field(default_factory=list)
    extra_keywords: list[str] = field(default_factory=list)
    clearance: str = "none"
    # Precompiled patterns for extra keywords (excluded from repr)
    _extra_patterns: list[tuple[str, re.Pattern[str]]] = field(default_factory=list, repr=False)

    def summary_dict(self) -> dict[str, Any]:
        return {
            "degree_level": self.degree_level,
            "degree_label": self.degree_label,
            "degree_fields": list(self.degree_fields),
            "languages": list(self.languages),
            "certifications": list(self.certifications),
            "skills": list(self.skills),
            "experience_years": self.experience_years,
            "role_keywords": list(self.role_keywords),
            "extra_keywords": list(self.extra_keywords),
            "clearance": self.clearance,
            "total_keywords": (
                len(self.languages)
                + len(self.certifications)
                + len(self.skills)
                + len(self.role_keywords)
                + len(self.extra_keywords)
                + (1 if self.degree_level >= 0 else 0)
            ),
        }


def empty_overrides() -> dict[str, Any]:
    return {
        "add_skills": [],
        "remove_skills": [],
        "add_languages": [],
        "remove_languages": [],
        "add_certs": [],
        "remove_certs": [],
        "degree_level": None,
        "clear_degree": False,
        "add_degree_fields": [],
        "remove_degree_fields": [],
    }


def _norm_terms(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item).strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def normalize_overrides(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    degree = raw.get("degree_level")
    degree_level: int | None
    if degree is None or degree == "":
        degree_level = None
    else:
        try:
            degree_level = int(degree)
        except (TypeError, ValueError):
            degree_level = None
        if degree_level not in DEGREE_LABELS:
            degree_level = None
    base = empty_overrides()
    base.update(
        {
            "add_skills": _norm_terms(raw.get("add_skills")),
            "remove_skills": _norm_terms(raw.get("remove_skills")),
            "add_languages": _norm_terms(raw.get("add_languages")),
            "remove_languages": _norm_terms(raw.get("remove_languages")),
            "add_certs": _norm_terms(raw.get("add_certs")),
            "remove_certs": _norm_terms(raw.get("remove_certs")),
            "degree_level": degree_level,
            "clear_degree": bool(raw.get("clear_degree")),
            "add_degree_fields": _norm_terms(raw.get("add_degree_fields")),
            "remove_degree_fields": _norm_terms(raw.get("remove_degree_fields")),
        }
    )
    return base


def load_overrides(raw: str | None) -> dict[str, Any]:
    if not raw:
        return empty_overrides()
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return empty_overrides()
    return normalize_overrides(data if isinstance(data, dict) else None)


def _term_keys(term: str) -> set[str]:
    keys = {term.casefold()}
    for canon in (canonical_skill(term), canonical_cert(term)):
        if canon:
            keys.add(canon.casefold())
    field = canonical_field(term)
    if field:
        keys.add(field.casefold())
    return keys


def _without(items: list[str], removed: list[str]) -> list[str]:
    banned: set[str] = set()
    for term in removed:
        banned |= _term_keys(term)
    kept: list[str] = []
    for item in items:
        if _term_keys(item) & banned:
            continue
        kept.append(item)
    return kept


def _include_one(items: list[str], label: str) -> list[str]:
    label = (label or "").strip()
    if not label:
        return items
    if any(_term_keys(label) & _term_keys(item) for item in items):
        return items
    return [*items, label]


def _removed(term: str, removed: list[str]) -> bool:
    keys = _term_keys(term)
    return any(keys & _term_keys(item) for item in removed)


def _place_skill(label: str, languages: list[str], skills: list[str]) -> tuple[list[str], list[str]]:
    if label in _LANG_NAMES:
        return _include_one(languages, label), skills
    if label in _SKILL_NAMES:
        return languages, _include_one(skills, label)
    return languages, skills


@dataclass
class ResumeSections:
    """Heading buckets. `found` is empty when the resume has no recognizable headings."""

    full: str
    found: frozenset[str]
    parts: dict[str, str]

    def _join(self, *keys: str) -> str:
        chunks = [self.parts[key] for key in keys if self.parts.get(key)]
        return "\n".join(chunks).strip()

    @cached_property
    def work_text(self) -> str:
        """Preamble, summary, experience, and skills. Projects and education stay out."""
        if not self.found:
            return self.full
        return self._join("preamble", "summary", "experience", "skills")

    @cached_property
    def degree_text(self) -> str:
        """Education, or credential lines elsewhere. Never a project write-up."""
        if not self.found:
            return self.full
        chunks: list[str] = []
        if "education" in self.found:
            education = self.parts.get("education", "")
            if education:
                chunks.append(education)
            header_keys = ("preamble", "summary")
        else:
            header_keys = ("preamble", "summary", "experience", "skills")
        for key in header_keys:
            for line in self.parts.get(key, "").splitlines():
                if _credential_line(line):
                    chunks.append(line)
        return "\n".join(chunks).strip()

    @cached_property
    def cert_text(self) -> str:
        """Work text plus education, where licenses often sit beside the degree."""
        if not self.found:
            return self.full
        education = self.parts.get("education", "")
        if not education:
            return self.work_text
        return self.work_text + "\n" + education

    @cached_property
    def years_text(self) -> str:
        """Job dates only. A cert course or a school project is not employment."""
        if not self.found:
            return self.full
        if self.parts.get("experience"):
            return self.parts["experience"]
        return self._join("preamble", "summary")

    @cached_property
    def years_claim_text(self) -> str:
        """Where an explicit 'N years of experience' claim is trusted."""
        if not self.found:
            return self.full
        return self._join("preamble", "summary", "experience")


def _credential_line(line: str) -> bool:
    """A degree line, not a sentence that happens to mention a doctorate."""
    text = line.strip()
    if not text or len(text) > 120 or len(text.split()) > 14:
        return False
    if not any(pat.search(text) for _, pat in _DEGREE_PATTERNS):
        return False
    return bool(_CREDENTIAL_LEAD.match(text) or _CREDENTIAL_CONTEXT.search(text))


def _plain_line(raw: str) -> str:
    """Drop zero-width characters PDF extractors leave on headings."""
    text = _INVISIBLE.sub("", raw or "")
    return text.replace("\u00a0", " ").replace("\u202f", " ").replace("\u2009", " ").strip()


def _normalize_heading_label(raw: str) -> str:
    """Turn '1. E X P E R I E N C E ----' into 'EXPERIENCE'."""
    text = _plain_line(raw)
    text = _HEADING_BULLET.sub("", _HEADING_MARK.sub("", text))
    text = _HEADING_INDEX.sub("", text)
    text = _HEADING_RULE.sub("", text).strip(" -–—_:|./")
    tokens = text.split()
    if len(tokens) >= 6 and all(len(tok) == 1 and tok.isalpha() for tok in tokens):
        text = "".join(tokens)
    return " ".join(text.split()).strip(" .")


def _section_kind(label: str) -> str | None:
    cleaned = _normalize_heading_label(label)
    if not cleaned or len(cleaned) > 60 or len(cleaned.split()) > 6:
        return None
    for kind, pat in _SECTION_LABELS:
        if pat.match(cleaned):
            return kind
    return None


def _looks_like_list(rest: str) -> bool:
    """'Python, SQL' is a heading payload. 'Led a team for two years.' is a bullet."""
    text = rest.strip()
    if not text or len(text) > 160 or text.endswith("."):
        return False
    words = text.split()
    if len(words) > 14:
        return False
    if any(mark in text for mark in (",", "|", "•", "·", ";")):
        return True
    return len(words) <= 4


def _classify_heading(line: str) -> tuple[str | None, str]:
    """Return (section, remainder). Remainder is the list after 'Skills: Python, SQL'."""
    if len(line) > 180:
        return None, ""
    stripped = _plain_line(line)
    stripped = _HEADING_BULLET.sub("", _HEADING_MARK.sub("", stripped)).strip()
    if not stripped:
        return None, ""
    head, sep, rest = stripped.partition(":")
    if sep:
        kind = _section_kind(head)
        if kind and _looks_like_list(rest):
            return kind, rest.strip()
    if len(stripped) > 80:
        return None, ""
    kind = _section_kind(stripped)
    if kind:
        return kind, ""
    return None, ""


_USE_MODEL = object()
_LABEL_SECTIONS = frozenset({"experience", "skills", "education", "projects"})


def _empty_buckets() -> dict[str, list[str]]:
    return {
        "preamble": [],
        "summary": [],
        "experience": [],
        "skills": [],
        "education": [],
        "projects": [],
        "other": [],
    }


def _model_section_labels(lines: list[str]) -> dict[int, str] | None:
    from ai_client import section_labels

    try:
        return section_labels(lines)
    except Exception:
        return None


def _normalize_labels(raw: Any, count: int) -> dict[int, str]:
    if not isinstance(raw, dict):
        return {}
    labels: dict[int, str] = {}
    for key, value in raw.items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        if index < 0 or index >= count:
            continue
        section = str(value or "").strip().lower()
        if section in _LABEL_SECTIONS:
            labels[index] = section
    return labels


def split_resume_sections(text: str, labeler: Any = _USE_MODEL) -> ResumeSections:
    """Split resume text on headings. A model may label lines only when no experience heading exists."""
    buckets = _empty_buckets()
    current = "preamble"
    found: set[str] = set()
    placements: list[tuple[str, str]] = []
    for raw_line in (text or "").splitlines():
        line = _plain_line(raw_line)
        if not line:
            continue
        kind, rest = _classify_heading(line)
        if kind:
            found.add(kind)
            current = kind if kind in buckets else "other"
            if rest:
                buckets[current].append(rest)
                placements.append((rest, current))
            continue
        buckets[current].append(line)
        placements.append((line, current))

    def _finish(parts_buckets: dict[str, list[str]], parts_found: set[str]) -> ResumeSections:
        parts = {key: "\n".join(lines).strip() for key, lines in parts_buckets.items()}
        return ResumeSections(full=(text or "").strip(), found=frozenset(parts_found), parts=parts)

    # An experience heading is enough. The model is not called.
    if "experience" in found or not placements:
        return _finish(buckets, found)

    caller = _model_section_labels if labeler is _USE_MODEL else labeler
    raw_labels: Any = None
    if caller is not None:
        try:
            raw_labels = caller([line for line, _section in placements])
        except Exception:
            raw_labels = None
    labels = _normalize_labels(raw_labels, len(placements))
    if not labels:
        return _finish(buckets, found)

    relabeled = _empty_buckets()
    relabeled_found: set[str] = set()
    for index, (line, previous) in enumerate(placements):
        section = labels.get(index, previous)
        if section not in relabeled:
            section = "preamble"
        relabeled[section].append(line)
        if section not in ("preamble", "other"):
            relabeled_found.add(section)
    return _finish(relabeled, relabeled_found)


def experience_section(text: str) -> str:
    """Experience lines only. Heading detection runs; the model is not asked again here."""
    sections = split_resume_sections(text, labeler=None)
    return sections.parts.get("experience") or ""


def splice_experience(full: str, bullets: str) -> str:
    """Replace the experience block in memory. The caller decides when to store it."""
    sections = split_resume_sections(full, labeler=None)
    old = sections.parts.get("experience") or ""
    new = (bullets or "").strip()
    if old and old in (full or ""):
        return full.replace(old, new, 1)
    if not new:
        return full or ""
    return (full or "").rstrip() + "\n\nExperience\n" + new + "\n"


def parse_resume(
    text: str,
    profile: dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
    *,
    section_labeler: Any = _USE_MODEL,
) -> ResumeProfile:
    """Parse a user's resume text (plus optional profile supplements and readout edits)."""
    raw = (text or "").strip()
    profile = profile or {}
    edits = normalize_overrides(overrides)

    sections = split_resume_sections(raw, labeler=section_labeler)
    work = sections.work_text
    if sections.found:
        resume_years_hint = _combine_years(
            _explicit_year_values(sections.years_claim_text),
            _date_intervals(sections.years_text),
        )
    else:
        resume_years_hint = estimate_resume_years(raw)
    degree_level, degree_label, degree_fields = extract_degrees(sections.degree_text)
    languages = [label for label, pat in PROGRAMMING_LANGUAGES if pat.search(work)]

    certs_found: list[str] = [label for label, pat in CERTIFICATIONS if pat.search(sections.cert_text)]
    certs_seen = {c.lower() for c in certs_found}
    for cert in profile.get("certs") or []:
        canon = _CERT_CANON_LOWER.get(str(cert).strip().lower()) or str(cert).strip()
        if canon and canon.lower() not in certs_seen:
            certs_found.append(canon)
            certs_seen.add(canon.lower())

    skills = [label for label, pat in TECHNICAL_SKILLS if pat.search(work)]
    for label, pat in ALIAS_PATTERNS:
        if pat.search(work):
            languages, skills = _place_skill(label, languages, skills)
    roles = [label for label, pat in ROLE_PATTERNS if pat.search(work)]

    resume_years = resume_years_hint
    prof_years = profile.get("years")
    if resume_years is not None:
        years_val = resume_years
    elif prof_years is not None:
        try:
            years_val = float(prof_years)
        except (TypeError, ValueError):
            years_val = None
    else:
        years_val = None

    resume_clr = classify_clearance(work)
    prof_clr = profile.get("clearance") or "none"
    clearance = (
        resume_clr
        if CLEARANCE_RANK.get(resume_clr, 0) >= CLEARANCE_RANK.get(prof_clr, 0)
        else prof_clr
    )
    if clearance == "unspecified":
        clearance = "none"

    already_known = set(languages) | set(certs_found) | set(skills) | set(roles) | set(degree_fields)
    extra_keywords = _extract_custom_keywords(work, already_known)
    folded_extra: list[str] = []
    for kw in extra_keywords:
        canon = canonical_skill(kw)
        if canon in _LANG_NAMES or canon in _SKILL_NAMES:
            if canon and not _removed(canon, edits["remove_languages"] + edits["remove_skills"]):
                languages, skills = _place_skill(canon, languages, skills)
            continue
        if not _removed(kw, edits["remove_skills"]):
            folded_extra.append(kw)
    extra_keywords = folded_extra

    languages = _without(languages, edits["remove_languages"])
    certs_found = _without(certs_found, edits["remove_certs"])
    skills = _without(skills, edits["remove_skills"])
    extra_keywords = _without(extra_keywords, edits["remove_skills"])

    for raw_add in edits["add_languages"]:
        if _removed(raw_add, edits["remove_languages"]):
            continue
        label = canonical_skill(raw_add) or raw_add.strip()
        if label in _SKILL_NAMES and label not in _LANG_NAMES:
            skills = _include_one(skills, label)
        else:
            languages = _include_one(languages, label)
    for raw_add in edits["add_skills"]:
        if _removed(raw_add, edits["remove_skills"]):
            continue
        canon = canonical_skill(raw_add)
        if canon in _LANG_NAMES:
            languages = _include_one(languages, canon)
        elif canon in _SKILL_NAMES:
            skills = _include_one(skills, canon)
        else:
            extra_keywords = _include_one(extra_keywords, raw_add.strip())
    for raw_add in edits["add_certs"]:
        if _removed(raw_add, edits["remove_certs"]):
            continue
        certs_found = _include_one(certs_found, canonical_cert(raw_add) or raw_add.strip())

    if edits["clear_degree"]:
        degree_level, degree_label, degree_fields = -1, "", []
    elif edits["degree_level"] is not None:
        degree_level = int(edits["degree_level"])
        degree_label = DEGREE_LABELS.get(degree_level, "")
    if not edits["clear_degree"]:
        degree_fields = _without(degree_fields, edits["remove_degree_fields"])
        for raw_field in edits["add_degree_fields"]:
            if _removed(raw_field, edits["remove_degree_fields"]):
                continue
            degree_fields = _include_one(degree_fields, canonical_field(raw_field))

    extra_patterns = [
        (kw, keyword_pattern(kw, whole_word=True)) for kw in extra_keywords
    ]

    sig_payload = "|".join([
        "v6",
        raw,
        str(profile.get("years")),
        ",".join(sorted(str(c) for c in (profile.get("certs") or []))),
        str(profile.get("clearance") or "none"),
        json.dumps(edits, sort_keys=True, separators=(",", ":")),
    ])
    sig = hashlib.sha256(sig_payload.encode("utf-8", errors="replace")).hexdigest()[:24]

    return ResumeProfile(
        signature=sig,
        raw_text=raw,
        degree_level=degree_level,
        degree_label=degree_label,
        degree_fields=degree_fields,
        languages=languages,
        certifications=certs_found,
        skills=skills,
        experience_years=years_val,
        role_keywords=roles,
        extra_keywords=extra_keywords,
        clearance=clearance,
        _extra_patterns=extra_patterns,
    )


# ---------------------------------------------------------------------------
# Resume-based Heat Score (0 - 100)
# ---------------------------------------------------------------------------

def heat_tier(score: float) -> str:
    if score >= 75:
        return "hot"
    if score >= 50:
        return "warm"
    if score >= 25:
        return "mild"
    return "cool"


def _is_affirmative_in(text: str, pat: re.Pattern[str]) -> bool:
    for m in pat.finditer(text):
        left = text[max(0, m.start() - 48) : m.start()]
        right = text[m.end() : m.end() + 40]
        if re.search(r"\b(?:no|not|without|never|neither|does\s+not|doesn't|do\s+not|don't)\b[^.!?\n]{0,36}$", left, re.I):
            continue
        if re.search(r"^[^.!?\n]{0,28}\bnot\s+(?:required|needed|necessary)\b", right, re.I):
            continue
        return True
    return False


def _fmt_years(val: float) -> str:
    return str(int(val)) if float(val).is_integer() else f"{val:g}"


def heat_cache_sig(resume: ResumeProfile) -> str:
    """Cache key: resume content plus the scorer version."""
    return f"{resume.signature}:{SCORER_VERSION}"


def unknown_skill_phrases(text: str, known: set[str] | None = None) -> list[str]:
    """Skill-like phrases that do not fold to a catalog label. These are the only embedding inputs."""
    phrases = _extract_custom_keywords(text or "", known or set())
    unknown: list[str] = []
    seen: set[str] = set()
    for phrase in phrases:
        if canonical_skill(phrase) is not None:
            continue
        key = phrase.casefold()
        if key in seen:
            continue
        seen.add(key)
        unknown.append(phrase)
    return unknown


def _folded_names(*groups: list[str]) -> set[str]:
    names: set[str] = set()
    for group in groups:
        for item in group:
            names.add(canonical_skill(item) or item)
    return names


def _clean_similar_pairs(pairs: list[Any] | None) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for item in pairs or []:
        if isinstance(item, dict):
            job_phrase = item.get("job")
            resume_phrase = item.get("resume")
            sim = item.get("score", 1)
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            job_phrase, resume_phrase = item[0], item[1]
            sim = item[2] if len(item) > 2 else 1
        else:
            continue
        if not job_phrase or not resume_phrase:
            continue
        try:
            sim_f = float(sim)
        except (TypeError, ValueError):
            continue
        if sim_f < SIMILAR_THRESHOLD:
            continue
        cleaned.append({
            "job": str(job_phrase),
            "resume": str(resume_phrase),
            "score": round(sim_f, 3),
        })
        if len(cleaned) >= 8:
            break
    return cleaned


def score_job_resume(
    job: dict[str, Any],
    resume: ResumeProfile,
    keyword: str = "",
    similar_pairs: list[Any] | None = None,
    similar_lookup: Any = None,
) -> dict[str, Any]:
    """Compute a 0-100 Heat Score based on how well a job aligns with the user's resume.

    Evaluates:
      - Programming languages listed on resume vs job
      - Certifications listed on resume vs job
      - Technical skills & all resume keywords vs job
      - Degree / education level & field of study
      - Experience (years + role/title alignment)
      - Security clearance alignment (penalty if job requires higher than user holds)
    """
    title = str(job.get("title") or "")
    desc = str(job.get("description") or "")
    hay = f"{title}\n{desc}".strip()

    earned = 0.0
    max_possible = 0.0
    breakdown: list[str] = []

    # Catalog hits first, then alias spellings, so language and skill scores see the same labels.
    job_langs: list[str] = []
    job_lang_req: list[str] = []
    for label, pat in PROGRAMMING_LANGUAGES:
        if _is_affirmative_in(hay, pat):
            job_langs.append(label)
            if mention_stance(hay, label, pat=pat) == "required":
                job_lang_req.append(label)
    job_skills: list[str] = []
    job_skill_req: list[str] = []
    for label, pat in TECHNICAL_SKILLS:
        if _is_affirmative_in(hay, pat):
            job_skills.append(label)
            if mention_stance(hay, label, pat=pat) == "required":
                job_skill_req.append(label)
    for label, pat in ALIAS_PATTERNS:
        if label in job_langs or label in job_skills:
            continue
        if not _is_affirmative_in(hay, pat):
            continue
        required = mention_stance(hay, label, pat=pat) == "required"
        if label in _LANG_NAMES:
            job_langs.append(label)
            if required:
                job_lang_req.append(label)
        elif label in _SKILL_NAMES:
            job_skills.append(label)
            if required:
                job_skill_req.append(label)
    held = _folded_names(list(resume.languages), list(resume.skills), list(resume.extra_keywords))
    matched_langs = [lang for lang in job_langs if lang in held]
    missing_langs = [lang for lang in job_langs if lang not in held]

    # 1) Programming Languages (weight 22 when the job mentions any)
    if job_langs:
        w = 22.0
        max_possible += w
        coverage = len(matched_langs) / len(job_langs)
        depth = min(len(matched_langs) / min(len(job_langs), 3), 1.0)
        title_bonus = 0.1 if any(_LANG_BY_NAME[l].search(title) for l in matched_langs) else 0.0
        ratio = min(1.0, 0.7 * coverage + 0.3 * depth + title_bonus)
        pts = w * ratio
        earned += pts
        if matched_langs:
            breakdown.append(f"Languages ({len(matched_langs)}/{len(job_langs)}): {', '.join(matched_langs[:6])}")
        else:
            breakdown.append(f"Languages (0/{len(job_langs)}): job asks for {', '.join(missing_langs[:4])}")

    # 2) Certifications (weight 22 when job mentions certs)
    job_certs: list[str] = []
    job_cert_req: list[str] = []
    for label, pat in CERTIFICATIONS:
        if _is_affirmative_in(hay, pat):
            job_certs.append(label)
            if mention_stance(hay, label, pat=pat) == "required":
                job_cert_req.append(label)
    resume_cert_set = {c.lower() for c in resume.certifications}
    matched_certs = [c for c in job_certs if c.lower() in resume_cert_set]
    missing_req_certs = [c for c in job_cert_req if c.lower() not in resume_cert_set]
    if job_certs:
        w = 22.0
        max_possible += w
        if matched_certs:
            coverage = len(matched_certs) / len(job_certs)
            # Holding at least one asked-for cert is strong; missing a required cert pulls it down
            req_hit = sum(1 for c in job_cert_req if c.lower() in resume_cert_set)
            base = 0.55 + 0.45 * coverage if req_hit else (0.35 + 0.55 * coverage)
            if missing_req_certs and not req_hit:
                base *= 0.55
            pts = w * min(1.0, base)
        else:
            pts = 0.0
        earned += pts
        if matched_certs:
            note = f"Certifications: {', '.join(matched_certs[:5])}"
            if missing_req_certs:
                note += f" (missing req: {', '.join(missing_req_certs[:3])})"
            breakdown.append(note)
        else:
            breakdown.append(f"Certifications (0/{len(job_certs)}): asks for {', '.join(job_certs[:4])}")

    # 3) Technical Skills & Resume Keywords (always active, weight 28)
    matched_skills = [s for s in job_skills if s in held]
    matched_extra = [
        kw for kw, pat in resume._extra_patterns
        if canonical_skill(kw) is None and _is_affirmative_in(hay, pat)
    ]
    all_matched_skills = matched_skills + [kw for kw in matched_extra if kw not in matched_skills]

    known_for_unknown = set(job_langs) | set(job_skills) | set(job_certs) | set(matched_extra)
    job_unknown = unknown_skill_phrases(hay, known_for_unknown)
    resume_unknown = [kw for kw in resume.extra_keywords if canonical_skill(kw) is None]
    if similar_pairs is None and similar_lookup is not None and job_unknown and resume_unknown:
        try:
            similar_pairs = similar_lookup(job_unknown, resume_unknown)
        except Exception:
            similar_pairs = []
    similar = _clean_similar_pairs(similar_pairs)
    # A similar phrase never satisfies a required skill. Exact or alias match does.
    missing_required_skills: list[str] = []
    seen_req: set[str] = set()
    for label in [*job_lang_req, *job_skill_req]:
        if label in seen_req or label in held:
            continue
        seen_req.add(label)
        missing_required_skills.append(label)

    w_skills = 28.0
    max_possible += w_skills
    if job_skills or all_matched_skills or similar:
        cov = (len(matched_skills) / len(job_skills)) if job_skills else 0.0
        # Saturate depth smoothly around 5-6 distinct matching resume skills/keywords.
        # Each unknown-skill embedding match counts as half a skill.
        depth_target = max(3, min(len(job_skills) if job_skills else 5, 6))
        depth = min((len(all_matched_skills) + 0.5 * len(similar)) / depth_target, 1.0)
        title_hit = any(
            (_SKILL_BY_NAME[s].search(title) if s in _SKILL_BY_NAME else keyword_pattern(s, whole_word=True).search(title))
            for s in all_matched_skills[:12]
        )
        if job_skills:
            ratio = min(1.0, 0.55 * cov + 0.45 * depth + (0.08 if title_hit else 0.0))
        else:
            ratio = min(1.0, 0.85 * depth + (0.15 if title_hit else 0.0))
        earned += w_skills * ratio
    if all_matched_skills:
        shown = ", ".join(all_matched_skills[:7])
        extra_n = len(all_matched_skills) - 7
        if extra_n > 0:
            shown += f" +{extra_n} more"
        breakdown.append(f"Skills & keywords ({len(all_matched_skills)}): {shown}")
    elif not similar:
        breakdown.append("Skills & keywords: no resume keywords matched")
    if similar:
        shown_sim = ", ".join(f"{item['job']} ~ {item['resume']}" for item in similar[:6])
        breakdown.append(f"Similar: {shown_sim}")

    # 4) Experience: Years + Role/Title alignment (always active, weight 18)
    w_exp = 18.0
    max_possible += w_exp
    years_min = job.get("years_min")
    years_pref = job.get("years_pref")
    if years_min is None and years_pref is None and desc:
        parsed_y = parse_years(hay)
        years_min = parsed_y.years_min
        years_pref = parsed_y.years_pref

    exp_years_score = 0.55  # neutral when neither states a number
    exp_years_note = ""
    have_yrs = resume.experience_years
    if have_yrs is not None and years_min is not None:
        try:
            need = float(years_min)
            if need <= have_yrs:
                exp_years_score = 1.0
                exp_years_note = f"{_fmt_years(have_yrs)} yrs meets {_fmt_years(need)}+ req"
            else:
                gap = need - have_yrs
                exp_years_score = max(0.0, 1.0 - gap * 0.28)
                exp_years_note = f"needs {_fmt_years(need)}+ yrs, resume ~{_fmt_years(have_yrs)} yrs"
        except (TypeError, ValueError):
            pass
    elif have_yrs is not None and years_pref is not None:
        try:
            pref = float(years_pref)
            if have_yrs >= pref:
                exp_years_score = 1.0
                exp_years_note = f"{_fmt_years(have_yrs)} yrs meets {_fmt_years(pref)}+ preferred"
            else:
                exp_years_score = 0.75
                exp_years_note = f"prefers {_fmt_years(pref)}+ yrs, resume ~{_fmt_years(have_yrs)} yrs"
        except (TypeError, ValueError):
            pass
    elif have_yrs is not None:
        exp_years_score = 0.7
        exp_years_note = f"resume ~{_fmt_years(have_yrs)} yrs (job states no minimum)"

    job_roles = [label for label, pat in ROLE_PATTERNS if pat.search(hay)]
    resume_role_set = set(resume.role_keywords)
    matched_roles = [r for r in job_roles if r in resume_role_set]
    title_role_match = any(_ROLE_BY_NAME[r].search(title) for r in matched_roles)

    if matched_roles:
        role_score = 1.0 if title_role_match else 0.75
    elif job_roles and resume_role_set:
        # Check word overlap between resume role titles and job title (e.g. "Network" / "Security" / "Analyst")
        role_words = {
            w.lower()
            for r in resume_role_set
            for w in r.split()
            if len(w) > 3 and w.lower() not in {"engineer", "analyst", "specialist", "technician", "manager", "developer", "administrator"}
        }
        title_words = set(re.findall(r"[a-z]{4,}", title.lower()))
        role_score = 0.55 if (role_words & title_words) else 0.15
    else:
        role_score = 0.45 if have_yrs is not None else 0.25

    exp_ratio = 0.55 * exp_years_score + 0.45 * role_score
    earned += w_exp * exp_ratio
    exp_bits = [b for b in (exp_years_note, (", ".join(matched_roles[:3]) if matched_roles else "")) if b]
    if exp_bits:
        breakdown.append("Experience: " + " · ".join(exp_bits))

    # 5) Degree / Education (weight 10 when job mentions a degree)
    job_deg_lvl, job_deg_fields, deg_pref_only = extract_job_degree_requirement(hay)
    matched_deg_fields = [f for f in job_deg_fields if f in set(resume.degree_fields)]
    degree_status = ""
    if job_deg_lvl >= 0:
        w_deg = 10.0
        max_possible += w_deg
        if resume.degree_level >= job_deg_lvl:
            deg_ratio = 1.0 if (not job_deg_fields or matched_deg_fields) else 0.85
            degree_status = f"{resume.degree_label} meets {DEGREE_LABELS[job_deg_lvl]}"
            if matched_deg_fields:
                degree_status += f" ({', '.join(matched_deg_fields[:2])})"
        elif resume.degree_level >= 0:
            gap = job_deg_lvl - resume.degree_level
            deg_ratio = 0.55 if deg_pref_only else max(0.2, 0.65 - 0.3 * gap)
            degree_status = f"{resume.degree_label} vs {DEGREE_LABELS[job_deg_lvl]} asked"
        else:
            deg_ratio = 0.4 if deg_pref_only else 0.0
            degree_status = f"asks for {DEGREE_LABELS[job_deg_lvl]} (none detected on resume)"
        earned += w_deg * deg_ratio
        breakdown.append(f"Degree: {degree_status}")

    # Normalize to 0..100
    raw_score = (100.0 * earned / max_possible) if max_possible > 0 else 0.0

    # Mild damping if posting has almost no description text
    if len(desc.strip()) < 60 and not (matched_langs or matched_certs or all_matched_skills):
        raw_score *= 0.65

    # Clearance adjustment (if job requires a clearance above what user holds)
    job_clr = job.get("clearance") or classify_clearance(hay)
    mine_rank = CLEARANCE_RANK.get(resume.clearance or "none", 0)
    theirs_rank = CLEARANCE_RANK.get(job_clr or "unspecified", 0)
    clr_note = ""
    if theirs_rank > mine_rank:
        penalty = min(10.0 * (theirs_rank - mine_rank), 25.0)
        raw_score -= penalty
        clr_note = f"Clearance: requires {CLEARANCE_LABELS.get(job_clr, job_clr)} (-{int(penalty)})"
        breakdown.append(clr_note)
    elif theirs_rank > 0:
        raw_score += 5.0
        clr_note = f"Clearance: {CLEARANCE_LABELS.get(job_clr, job_clr)} match (+5)"
        breakdown.append(clr_note)

    # Required skills are penalized after normalization so coverage math cannot hide a miss.
    req_penalty = required_skill_penalty(len(missing_required_skills))
    if missing_required_skills:
        raw_score -= req_penalty
        breakdown.append(
            "Missing required: " + ", ".join(missing_required_skills[:8])
            + (f" (-{int(req_penalty)})" if req_penalty else "")
        )

    # Small bonus if scan keyword itself is on resume AND in title
    if keyword and keyword_pattern(keyword).search(resume.raw_text) and keyword_pattern(keyword).search(title):
        raw_score += 3.0

    score = round(max(0.0, min(100.0, raw_score)), 1)
    tier = heat_tier(score)
    total_matched = (
        len(matched_langs)
        + len(matched_certs)
        + len(all_matched_skills)
        + len(matched_roles)
        + len(matched_deg_fields)
    )
    header = f"Heat Score: {int(round(score))}/100 ({total_matched} resume keyword{'s' if total_matched != 1 else ''} matched)"
    tooltip = "\n".join([header, *breakdown])

    details = {
        "score": score,
        "tier": tier,
        "total_matched": total_matched,
        "languages": matched_langs,
        "missing_languages": missing_langs[:6],
        "certifications": matched_certs,
        "missing_required_certs": missing_req_certs[:5],
        "skills": all_matched_skills[:16],
        "missing_required_skills": missing_required_skills[:12],
        "required_penalty": req_penalty,
        "similar": similar,
        "roles": matched_roles[:5],
        "experience_note": exp_years_note,
        "degree_status": degree_status,
        "degree_fields": matched_deg_fields,
        "clearance_note": clr_note,
    }
    return {
        "score": score,
        "tier": tier,
        "tooltip": tooltip,
        "details": details,
    }
