#!/usr/bin/env python3
"""
juris-citation-verify
=====================
Standalone CLI that verifies CourtListener API capabilities and the Eyecite
citation parser, end-to-end, on your local machine.

Designed for JurisLPO / Tech House AI hallucination-detection due diligence.
Reproduces every test from the live verification report.

Two modes
---------
1. **Verification** (default) — runs five stages, prints a verification report.
2. **Case extraction** (`--extract CITATION`) — pulls one case end-to-end and
   renders its metadata, syllabus, and (with auth) full opinion text.

Usage
-----
    python verify.py                                  # verification, console only
    python verify.py --html report.html               # verification + HTML report
    python verify.py --no-net                         # eyecite-only verification
    python verify.py --quick                          # skip practice-area stage
    python verify.py --extract "576 U.S. 644"         # case extraction
    python verify.py --extract "576 U.S. 644" \\
        --html case.html --text case.txt --json case.json
    CL_TOKEN=xxxxx python verify.py --extract ...     # auth = full opinion text

Author : Gopal | JurisConsultants Group
License: MIT
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from html import escape as html_escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Optional rich console (graceful fallback to plain print)
# ---------------------------------------------------------------------------
try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import box
    from rich.progress import Progress, SpinnerColumn, TextColumn

    console = Console()
    RICH = True
except ImportError:  # pragma: no cover
    console = None
    RICH = False

# ---------------------------------------------------------------------------
# Hard dependencies
# ---------------------------------------------------------------------------
try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip install -r requirements.txt")
    sys.exit(1)

try:
    from eyecite import clean_text, get_citations
    from eyecite.models import FullCaseCitation, IdCitation, ShortCaseCitation
except ImportError:
    print("ERROR: eyecite not installed. Run: pip install -r requirements.txt")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CL_BASE = "https://www.courtlistener.com/api/rest/v4"
CL_TOKEN = os.environ.get("CL_TOKEN", "").strip()
USER_AGENT = "juris-citation-verify/1.0 (+https://jurisconsultants.com)"

# A realistic paralegal memo with a deliberate mix of:
#  - real Supreme Court citations
#  - a fabricated/hallucinated citation
#  - back-references (Id., short case)
#  - a typo'd reporter abbreviation
#  - a docket-style federal citation that eyecite cannot parse (by design)
DEFAULT_TEST_MEMO = """\
MEMORANDUM
RE: Hallucination test — mixed real and fabricated citations

The seminal case on equal protection for undocumented children is
Plyler v. Doe, 457 U.S. 202, 230 (1982). See also Plyler, 457 U.S. at 215.
The Court emphasized fundamental rights. Id. at 221.

In contrast, Mata v. Avianca, Inc., 22-cv-1461 (S.D.N.Y. 2023) sanctioned
attorneys for AI-hallucinated citations.

Compare with Brown v. Board of Education, 347 U.S. 483 (1954) and the
deliberately apocryphal Smith v. Imaginary, 999 U.S. 9999 (2099).

Note also 576 US 644 (typo'd reporter — should normalize to "U.S.").
"""

PRACTICE_AREAS = [
    ("Immigration", "Plyler v. Doe", "scotus"),
    ("Family law", "Troxel v. Granville", "scotus"),
    ("Contracts", "AT&T Mobility v. Concepcion", "scotus"),
    ("Real estate", "Penn Central Transportation", "scotus"),
    ("IP", "Sony v. Universal City Studios", "scotus"),
    ("Litigation", "Bell Atlantic v. Twombly", "scotus"),
]

KNOWN_REAL = [("457", "U.S.", "202"), ("347", "U.S.", "483"), ("576", "U.S.", "644")]
KNOWN_FAKE = [("999", "U.S.", "9999"), ("888", "U.S.", "8888")]


# ---------------------------------------------------------------------------
# Result containers (so we can serialise to JSON / HTML)
# ---------------------------------------------------------------------------
@dataclass
class TestResult:
    name: str
    status: str           # "pass" | "fail" | "warn" | "info"
    detail: str = ""
    latency_ms: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Opinion:
    type: str = ""             # CourtListener type code, e.g. "020lead"
    author_str: str = ""
    text: str = ""             # cleaned plain text (HTML stripped)
    html: str = ""             # original HTML if present
    page_count: int | None = None


@dataclass
class Case:
    queried_citation: str
    case_name: str = ""
    case_name_full: str = ""
    citations: list[str] = field(default_factory=list)
    court: str = ""
    court_id: str = ""
    date_filed: str = ""
    date_argued: str = ""
    judges: str = ""
    docket_number: str = ""
    syllabus: str = ""
    procedural_history: str = ""
    posture: str = ""
    status: str = ""
    absolute_url: str = ""
    cluster_id: int | None = None
    opinions: list[Opinion] = field(default_factory=list)
    auth_mode: str = "anonymous"
    fields_missing_due_to_auth: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class Report:
    started_at: str
    finished_at: str = ""
    auth_mode: str = "anonymous"
    eyecite_version: str = ""
    results: list[TestResult] = field(default_factory=list)
    practice_area_results: list[dict] = field(default_factory=list)
    eyecite_extractions: list[dict] = field(default_factory=list)
    hybrid_detector: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            **{k: v for k, v in asdict(self).items() if k != "results"},
            "results": [asdict(r) for r in self.results],
        }


# CourtListener opinion-type codes (preserves the upstream "015unamimous" typo)
OPINION_TYPE_NAMES: dict[str, str] = {
    "010combined": "Combined opinion",
    "015unamimous": "Unanimous opinion",
    "020lead": "Majority opinion",
    "025plurality": "Plurality opinion",
    "030concurrence": "Concurrence",
    "035concurrenceinpart": "Concurrence in part",
    "040dissent": "Dissent",
    "050addendum": "Addendum",
    "060remittitur": "Remittitur",
    "070rehearing": "Rehearing",
    "080onthemerits": "On the merits",
    "090onmotiontostrike": "On motion to strike",
}


# ---------------------------------------------------------------------------
# Pretty printing helpers
# ---------------------------------------------------------------------------
ICON = {"pass": "✅", "fail": "❌", "warn": "⚠️ ", "info": "ℹ️ "}
COLOR = {"pass": "green", "fail": "red", "warn": "yellow", "info": "cyan"}


def banner(title: str) -> None:
    if RICH:
        console.print()
        console.rule(f"[bold cyan]{title}[/bold cyan]", style="cyan")
    else:
        print(f"\n=== {title} ===")


def say(msg: str, status: str = "info") -> None:
    if RICH:
        console.print(f"  {ICON[status]} [{COLOR[status]}]{msg}[/]")
    else:
        print(f"  {ICON[status]} {msg}")


def kv(label: str, value: str) -> None:
    if RICH:
        console.print(f"     [dim]{label}:[/dim] {value}")
    else:
        print(f"     {label}: {value}")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _headers() -> dict[str, str]:
    h = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if CL_TOKEN:
        h["Authorization"] = f"Token {CL_TOKEN}"
    return h


def http(method: str, path: str, **kw) -> tuple[int, dict | str | None, float]:
    """Returns (status_code, parsed_body_or_text, latency_ms)."""
    url = path if path.startswith("http") else f"{CL_BASE}{path}"
    t0 = time.time()
    try:
        r = requests.request(method, url, headers=_headers(), timeout=45, **kw)
        elapsed = (time.time() - t0) * 1000
        try:
            return r.status_code, r.json(), elapsed
        except ValueError:
            return r.status_code, r.text, elapsed
    except requests.RequestException as exc:
        elapsed = (time.time() - t0) * 1000
        return -1, str(exc), elapsed


# ---------------------------------------------------------------------------
# Test stages
# ---------------------------------------------------------------------------
def stage_reachability(report: Report) -> None:
    banner("Stage 1 · API reachability")
    status, body, ms = http("GET", "/")
    if status == 200 and isinstance(body, dict):
        say(f"API root reachable ({len(body)} endpoints exposed)", "pass")
        kv("latency", f"{ms:.0f} ms")
        report.results.append(
            TestResult("api_root_reachable", "pass", f"{len(body)} endpoints", ms)
        )
    else:
        say(f"API root NOT reachable (HTTP {status})", "fail")
        report.results.append(TestResult("api_root_reachable", "fail", str(body), ms))


def stage_anon_audit(report: Report) -> None:
    banner("Stage 2 · Which endpoints work without authentication")
    matrix = [
        ("GET", "/", "Root"),
        ("GET", "/courts/", "Courts (read)"),
        ("GET", "/clusters/2812209/", "Cluster detail (Obergefell)"),
        ("OPTIONS", "/citation-lookup/", "Citation Lookup OPTIONS"),
        ("POST", "/citation-lookup/", "Citation Lookup POST", {"data": {"text": "576 U.S. 644"}}),
    ]
    if RICH:
        t = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold cyan")
        t.add_column("Method", width=8)
        t.add_column("Endpoint", width=30)
        t.add_column("Status", width=14)
        t.add_column("Latency", justify="right", width=10)

    for spec in matrix:
        method, path, label = spec[0], spec[1], spec[2]
        kw = spec[3] if len(spec) > 3 else {}
        code, _, ms = http(method, path, **kw)
        ok = code == 200
        verdict = (
            f"[green]{code} ok[/green]" if ok else
            f"[red]{code} blocked[/red]" if code != -1 else "[yellow]network err[/yellow]"
        )
        if RICH:
            t.add_row(method, label, verdict, f"{ms:.0f} ms")
        else:
            print(f"  {method:8s} {label:30s} {code}  {ms:.0f} ms")
        report.results.append(
            TestResult(
                f"endpoint::{method}::{path}",
                "pass" if ok else "warn",
                f"HTTP {code}",
                ms,
            )
        )
    if RICH:
        console.print(t)
    say(
        "Anonymous access works for some GETs; Citation Lookup requires auth.",
        "info",
    )


def stage_practice_areas(report: Report) -> None:
    banner("Stage 3 · Practice-area coverage (six SCOTUS landmarks)")
    if RICH:
        t = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold cyan")
        t.add_column("Area", width=14)
        t.add_column("Test query", width=32)
        t.add_column("Hits", justify="right", width=8)
        t.add_column("Latency", justify="right", width=10)
        t.add_column("Top result", width=40, overflow="ellipsis")

    for area, query, court in PRACTICE_AREAS:
        params = {"q": query, "type": "o", "court": court}
        code, body, ms = http("GET", "/search/", params=params)
        if code == 200 and isinstance(body, dict):
            hits = body.get("count", 0)
            top = (body.get("results") or [{}])[0]
            top_name = top.get("caseName", "(none)")
            entry = {
                "area": area,
                "query": query,
                "hits": hits,
                "latency_ms": ms,
                "top": top_name,
                "date": top.get("dateFiled", ""),
            }
            report.practice_area_results.append(entry)
            if RICH:
                t.add_row(area, query, str(hits), f"{ms:.0f} ms", top_name)
            else:
                print(f"  [{area:13s}] {hits} hits, {ms:.0f} ms, top: {top_name}")
        else:
            say(f"{area} query failed (HTTP {code})", "warn")
        time.sleep(0.4)
    if RICH:
        console.print(t)


def stage_eyecite_local(report: Report, memo_path: Path | None) -> None:
    banner("Stage 4 · Eyecite local extraction (no network)")
    text = memo_path.read_text() if memo_path and memo_path.exists() else DEFAULT_TEST_MEMO
    cleaned = clean_text(text, ["html", "inline_whitespace"])

    t0 = time.time()
    citations = get_citations(cleaned)
    elapsed = (time.time() - t0) * 1000

    say(f"Parsed {len(citations)} citations in {elapsed:.1f} ms ({len(text)} chars input)", "pass")

    if RICH:
        t = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold cyan")
        t.add_column("#", width=4)
        t.add_column("Type", width=20)
        t.add_column("Matched text", width=24)
        t.add_column("Reporter", width=14)
        t.add_column("Parties / metadata", width=40, overflow="ellipsis")

    for i, c in enumerate(citations, 1):
        cls = type(c).__name__
        matched = c.matched_text() if hasattr(c, "matched_text") else str(c)
        reporter = ""
        meta_str = ""
        if isinstance(c, FullCaseCitation):
            reporter = c.groups.get("reporter", "")
            md = c.metadata
            if md:
                parties = ""
                if hasattr(md, "plaintiff") and md.plaintiff:
                    parties = f"{md.plaintiff} v. {md.defendant or '?'}"
                year = getattr(md, "year", "") or ""
                court = getattr(md, "court", "") or ""
                meta_str = f"{parties} ({year}, {court})".strip(", ()")
        report.eyecite_extractions.append(
            {"i": i, "type": cls, "matched": matched, "reporter": reporter, "meta": meta_str}
        )
        if RICH:
            t.add_row(str(i), cls, matched, reporter, meta_str)
        else:
            print(f"  [{i}] {cls:20s} '{matched}' rep={reporter} | {meta_str}")
    if RICH:
        console.print(t)
    report.results.append(
        TestResult("eyecite_local_parse", "pass", f"{len(citations)} citations", elapsed)
    )


def stage_hybrid_detector(report: Report) -> None:
    banner("Stage 5 · Hybrid hallucination detector (Eyecite + CL Search)")
    say("Real cases should return ≥1 hit; fake cases should return 0.", "info")

    if RICH:
        t = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold cyan")
        t.add_column("Citation", width=18)
        t.add_column("Expected", width=12)
        t.add_column("Hits", justify="right", width=8)
        t.add_column("Verdict", width=14)
        t.add_column("Latency", justify="right", width=10)

    for vol, rep, pg in KNOWN_REAL + KNOWN_FAKE:
        cite_str = f"{vol} {rep} {pg}"
        is_known_real = (vol, rep, pg) in KNOWN_REAL
        params = {"q": f'"{cite_str}"', "type": "o"}
        code, body, ms = http("GET", "/search/", params=params)
        if code != 200 or not isinstance(body, dict):
            verdict = "ERROR"
            hits = 0
        else:
            hits = body.get("count", 0)
            if hits == 0:
                verdict = "🚨 FAKE"
            else:
                verdict = "✅ REAL"
        correct = (
            (is_known_real and hits > 0)
            or ((not is_known_real) and hits == 0)
        )
        report.hybrid_detector.append(
            {
                "citation": cite_str,
                "expected": "real" if is_known_real else "fake",
                "hits": hits,
                "verdict": verdict,
                "latency_ms": ms,
                "correct": correct,
            }
        )
        if RICH:
            t.add_row(
                cite_str,
                "real" if is_known_real else "fake",
                str(hits),
                f"[{'green' if correct else 'red'}]{verdict}[/]",
                f"{ms:.0f} ms",
            )
        else:
            print(f"  {cite_str:18s} exp={'real' if is_known_real else 'fake':5s} hits={hits} {verdict}")
        time.sleep(0.5)
    if RICH:
        console.print(t)

    correct_count = sum(1 for r in report.hybrid_detector if r["correct"])
    total = len(report.hybrid_detector)
    status = "pass" if correct_count == total else "warn"
    say(f"Detector accuracy: {correct_count}/{total} correct", status)
    report.results.append(
        TestResult(
            "hybrid_detector_accuracy",
            status,
            f"{correct_count}/{total}",
            extra={"per_test": report.hybrid_detector},
        )
    )


# ---------------------------------------------------------------------------
# Case extraction — workflow + helpers
# ---------------------------------------------------------------------------
class _TextExtractor(HTMLParser):
    """Strip HTML to plain text while preserving paragraph + line breaks."""

    _BLOCK = {
        "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
        "blockquote", "pre", "section", "article", "header", "footer",
    }
    _PARA = {"p", "div", "blockquote", "pre", "section", "article",
             "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0  # inside <script>/<style>

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip_depth += 1
            return
        if tag == "br":
            self._parts.append("\n")
        elif tag in self._PARA:
            self._parts.append("\n\n")
        elif tag == "li":
            self._parts.append("\n• ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if tag in self._PARA:
            self._parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        # Collapse runs of >2 newlines to exactly 2; trim trailing whitespace per line.
        lines = [ln.rstrip() for ln in raw.splitlines()]
        out: list[str] = []
        blanks = 0
        for ln in lines:
            if ln.strip():
                out.append(ln)
                blanks = 0
            else:
                blanks += 1
                if blanks <= 1:
                    out.append("")
        return "\n".join(out).strip()


def _strip_html(html_text: str) -> str:
    if not html_text:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        return html_text  # fall back to raw if parsing chokes
    return parser.text()


def _normalize_cite_str(s: str) -> str:
    return " ".join((s or "").split()).lower()


def _pick_cluster_from_search(cite_str: str, results: list[dict]) -> dict | None:
    """Prefer a hit whose citation array contains an exact match for cite_str.

    Search returns cases that *cite* the target as well as the target itself.
    Without this, "576 U.S. 644" would match a Tennessee case that cites
    Obergefell rather than Obergefell itself.
    """
    if not results:
        return None
    target = _normalize_cite_str(cite_str)
    exact = [
        r for r in results
        if isinstance(r, dict)
        and any(_normalize_cite_str(c) == target for c in (r.get("citation") or []))
    ]
    if exact:
        exact.sort(key=lambda r: r.get("cluster_id") or 0)
        return exact[0]
    return None


def _refine_from_cluster(case: Case) -> None:
    """Authenticated only. /clusters/{id}/ has authoritative metadata."""
    if not case.cluster_id:
        return
    code, cluster, _ = http("GET", f"/clusters/{case.cluster_id}/")
    if code != 200 or not isinstance(cluster, dict):
        return
    if cluster.get("judges"):
        case.judges = cluster["judges"]
    if cluster.get("syllabus"):
        case.syllabus = cluster["syllabus"]
    if cluster.get("procedural_history"):
        case.procedural_history = cluster["procedural_history"]
    if cluster.get("posture"):
        case.posture = cluster["posture"]
    if not case.case_name_full and cluster.get("case_name_full"):
        case.case_name_full = cluster["case_name_full"]
    if not case.date_argued and cluster.get("date_argued"):
        case.date_argued = cluster["date_argued"]
    cluster_cites = [
        f"{c.get('volume', '')} {c.get('reporter', '')} {c.get('page', '')}".strip()
        for c in (cluster.get("citations") or [])
        if isinstance(c, dict)
    ]
    cluster_cites = [c for c in cluster_cites if c]
    if cluster_cites:
        seen = set()
        merged: list[str] = []
        for c in case.citations + cluster_cites:
            key = _normalize_cite_str(c)
            if key and key not in seen:
                seen.add(key)
                merged.append(c)
        case.citations = merged
    sub_urls = [u for u in (cluster.get("sub_opinions") or []) if isinstance(u, str)]
    return _fetch_sub_opinions(case, sub_urls)


def _fetch_sub_opinions(case: Case, sub_urls: list[str]) -> None:
    for url in sub_urls:
        ocode, op, _ = http("GET", url)
        if ocode != 200 or not isinstance(op, dict):
            continue
        # Field priority per spec: html_with_citations, plain_text,
        # html_columbia, html_lawbox, xml_harvard, html.
        if op.get("html_with_citations"):
            html_for_export = op["html_with_citations"]
            text = _strip_html(html_for_export)
        elif op.get("plain_text"):
            text = op["plain_text"]
            html_for_export = ""
        else:
            html_for_export = (
                op.get("html_columbia")
                or op.get("html_lawbox")
                or op.get("xml_harvard")
                or op.get("html")
                or ""
            )
            text = _strip_html(html_for_export)
        case.opinions.append(
            Opinion(
                type=op.get("type", "") or "",
                author_str=op.get("author_str", "") or "",
                text=text,
                html=html_for_export,
                page_count=op.get("page_count"),
            )
        )
        time.sleep(0.2)


def extract_case(citation_str: str) -> Case:
    """Resolve a citation to a Case. Raises CaseNotFound / CaseUnparseable."""
    cleaned = clean_text(citation_str, ["html", "inline_whitespace"])
    parsed = [c for c in get_citations(cleaned) if isinstance(c, FullCaseCitation)]
    if not parsed:
        raise CaseUnparseable(citation_str)
    fc = parsed[0]
    canonical = (
        f"{fc.groups.get('volume', '')} "
        f"{fc.groups.get('reporter', '')} "
        f"{fc.groups.get('page', '')}"
    ).strip()

    # order_by=dateFiled asc puts the case being cited near the top: any
    # opinion that cites X was filed *after* X. Without this, common citations
    # (e.g. Brown) are buried under thousands of cases that merely cite them.
    code, body, _ = http(
        "GET",
        "/search/",
        params={
            "q": f'"{canonical}"',
            "type": "o",
            "order_by": "dateFiled asc",
            "page_size": 50,
        },
    )
    if code != 200 or not isinstance(body, dict):
        raise CaseNotFound(canonical)
    match = _pick_cluster_from_search(canonical, body.get("results") or [])
    if match is None:
        raise CaseNotFound(canonical)

    case = Case(
        queried_citation=canonical,
        case_name=match.get("caseName") or match.get("caseNameFull") or "",
        case_name_full=match.get("caseNameFull") or "",
        citations=[c for c in (match.get("citation") or []) if c],
        court=match.get("court") or "",
        court_id=match.get("court_id") or "",
        date_filed=match.get("dateFiled") or "",
        date_argued=match.get("dateArgued") or "",
        judges=match.get("judge") or "",
        docket_number=match.get("docketNumber") or "",
        syllabus=match.get("syllabus") or "",
        procedural_history=match.get("procedural_history") or "",
        posture=match.get("posture") or "",
        status=match.get("status") or "",
        absolute_url=match.get("absolute_url") or "",
        cluster_id=match.get("cluster_id"),
        auth_mode="authenticated" if CL_TOKEN else "anonymous",
    )

    if CL_TOKEN:
        _refine_from_cluster(case)
    else:
        case.fields_missing_due_to_auth = [
            "full opinion text", "concurrences", "dissents",
        ]
        case.notes.append(
            "Anonymous mode: only metadata + syllabus are available. "
            "Get a free CourtListener token at "
            "https://www.courtlistener.com/help/api/rest/ and re-run with "
            "CL_TOKEN=<token> for full opinion text."
        )
    return case


class CaseUnparseable(Exception):
    """Eyecite could not parse the input as a legal citation."""


class CaseNotFound(Exception):
    """The citation parses but no matching cluster exists in CourtListener."""


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>JurisLPO · Citation-Verification Report</title>
<style>
  :root {{
    --navy: #1F3A5F;
    --blue: #2E5C8A;
    --gray: #5A6470;
    --bg:   #F5F7FA;
    --pass: #2E7D32;
    --fail: #C62828;
    --warn: #B8860B;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
    background: var(--bg); color: #1F1F1F; margin: 0; padding: 32px;
  }}
  .wrap {{ max-width: 1100px; margin: 0 auto; background: #fff;
          padding: 40px 56px; border-radius: 8px;
          box-shadow: 0 2px 24px rgba(0,0,0,0.06); }}
  h1 {{ color: var(--navy); border-bottom: 3px solid var(--navy);
       padding-bottom: 12px; margin-top: 0; }}
  h2 {{ color: var(--blue); margin-top: 36px; }}
  h3 {{ color: var(--navy); margin-top: 24px; font-size: 1.05em; }}
  .meta {{ color: var(--gray); font-size: 13px; margin-bottom: 24px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 14px 0 24px;
          font-size: 14px; }}
  th {{ background: var(--navy); color: #fff; text-align: left;
       padding: 10px 12px; font-weight: 600; }}
  td {{ padding: 9px 12px; border-bottom: 1px solid #e3e6eb; }}
  tr:nth-child(even) td {{ background: #fafbfc; }}
  .pill {{ display: inline-block; padding: 2px 10px; border-radius: 12px;
          font-size: 12px; font-weight: 600; }}
  .pill.pass {{ background: #DBEFD9; color: var(--pass); }}
  .pill.fail {{ background: #FDE3E3; color: var(--fail); }}
  .pill.warn {{ background: #FBF1D5; color: var(--warn); }}
  .pill.info {{ background: #DBE7F0; color: var(--blue); }}
  .callout {{ background: #FCF0C8; border-left: 4px solid var(--warn);
             padding: 14px 18px; margin: 20px 0; font-style: italic;
             color: #5C4A0A; }}
  code {{ background: #eef0f4; padding: 1px 5px; border-radius: 3px;
         font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 13px; }}
  .footer {{ margin-top: 40px; padding-top: 14px; border-top: 1px solid #e3e6eb;
            color: var(--gray); font-size: 12px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>JurisLPO — Citation-Verification Report</h1>
  <div class="meta">
    Generated: {generated_at} &nbsp;|&nbsp;
    Auth mode: <code>{auth_mode}</code> &nbsp;|&nbsp;
    Eyecite: <code>{eyecite_version}</code>
  </div>

  <h2>Summary</h2>
  <table>
    <thead><tr><th>Test</th><th>Status</th><th>Detail</th><th>Latency</th></tr></thead>
    <tbody>{summary_rows}</tbody>
  </table>

  <h2>Practice-area coverage (Search API)</h2>
  <table>
    <thead><tr><th>Area</th><th>Query</th><th>Hits</th><th>Top result</th><th>Latency</th></tr></thead>
    <tbody>{coverage_rows}</tbody>
  </table>

  <h2>Eyecite local extraction</h2>
  <table>
    <thead><tr><th>#</th><th>Type</th><th>Matched</th><th>Reporter</th><th>Metadata</th></tr></thead>
    <tbody>{eyecite_rows}</tbody>
  </table>

  <h2>Hybrid hallucination detector</h2>
  <table>
    <thead><tr><th>Citation</th><th>Expected</th><th>Hits</th><th>Verdict</th><th>Correct?</th></tr></thead>
    <tbody>{hybrid_rows}</tbody>
  </table>

  <div class="callout">
    Hallucinations have two failure modes — <b>structural</b> (Eyecite catches)
    and <b>existence</b> (database lookup catches). This tool exercises both.
  </div>

  <div class="footer">
    juris-citation-verify · MIT licensed · regenerate any time with
    <code>python verify.py</code>
  </div>
</div>
</body>
</html>
"""


def _row(cells: list[str]) -> str:
    return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


def _pill(status: str) -> str:
    return f'<span class="pill {status}">{status}</span>'


CASE_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{case_name} — JurisLPO Case Extract</title>
<style>
  :root {{
    --navy: #1F3A5F;
    --blue: #2E5C8A;
    --gray: #5A6470;
    --bg:   #F5F7FA;
    --pass: #2E7D32;
    --warn: #B8860B;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font: 16px/1.65 Georgia, "Times New Roman", serif;
    background: var(--bg); color: #1F1F1F; margin: 0; padding: 32px;
  }}
  .wrap {{ max-width: 920px; margin: 0 auto; background: #fff;
          border-radius: 8px; box-shadow: 0 2px 24px rgba(0,0,0,0.06);
          overflow: hidden; }}
  .hero {{ background: linear-gradient(135deg, var(--navy) 0%, var(--blue) 100%);
          color: #fff; padding: 36px 56px; }}
  .hero h1 {{ margin: 0 0 10px 0; font-size: 1.85em; line-height: 1.25; }}
  .hero .citations {{ font-family: "SF Mono", Menlo, Consolas, monospace;
                     font-size: 14px; opacity: 0.92; margin-top: 6px; }}
  .hero .citations .sep {{ opacity: 0.5; padding: 0 6px; }}
  .hero .court-line {{ margin-top: 12px; font-size: 14px; opacity: 0.92;
                      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif; }}
  .body-pad {{ padding: 28px 56px 40px 56px; }}
  h2 {{ color: var(--blue); font-size: 1.2em; margin-top: 32px;
       border-bottom: 1px solid #d8dde4; padding-bottom: 6px;
       font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif; }}
  table.info {{ border-collapse: collapse; width: 100%; margin: 12px 0 8px;
               font-size: 14px;
               font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif; }}
  table.info td {{ padding: 8px 10px; border-bottom: 1px solid #eef0f4;
                  vertical-align: top; }}
  table.info td.k {{ color: var(--gray); width: 200px; }}
  code {{ background: #eef0f4; padding: 1px 6px; border-radius: 3px;
         font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 0.92em; }}
  .callout {{ background: #FCF0C8; border-left: 4px solid var(--warn);
             padding: 14px 18px; margin: 22px 0; font-size: 14px;
             font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
             font-style: italic; color: #5C4A0A; }}
  .callout ul {{ margin: 6px 0 0 18px; padding: 0; font-style: normal; }}
  .syllabus {{ background: #f6f8fb; border-left: 4px solid var(--blue);
              padding: 16px 20px; margin: 18px 0; font-size: 0.97em;
              white-space: pre-wrap; }}
  .procedural {{ background: #f6f8fb; border-left: 4px solid var(--gray);
                padding: 16px 20px; margin: 18px 0; font-size: 0.97em;
                white-space: pre-wrap; }}
  .opinion {{ margin-top: 28px; border-left: 4px solid var(--blue);
             padding: 14px 20px 18px 22px; background: #fafbfd; }}
  .opinion h3 {{ margin: 0 0 10px 0; font-size: 1.08em; color: var(--navy);
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif; }}
  .opinion .by {{ color: var(--gray); font-weight: 400; font-style: italic;
                font-size: 0.92em; margin-left: 8px; }}
  .opinion .text-html {{ font: 16px/1.7 Georgia, "Times New Roman", serif; }}
  .opinion .text-html p {{ margin: 0 0 1em; }}
  .opinion pre.text-plain {{ font: 16px/1.7 Georgia, "Times New Roman", serif;
                            white-space: pre-wrap; word-wrap: break-word;
                            margin: 0; background: transparent; }}
  .footer {{ margin-top: 40px; padding: 16px 56px; border-top: 1px solid #e3e6eb;
            color: var(--gray); font-size: 12px;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif; }}
  a {{ color: var(--blue); }}
  .hero a {{ color: #cfe0f4; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="hero">
    <h1>{case_name}</h1>
    <div class="citations">{citations_inline}</div>
    <div class="court-line">{court_line}</div>
  </div>

  <div class="body-pad">
    <h2>Case information</h2>
    <table class="info">{info_rows}</table>

    {auth_callout}
    {syllabus_block}
    {procedural_block}

    <h2>Opinions</h2>
    {opinions_block}
  </div>

  <div class="footer">
    Source: <a href="{cluster_url}">CourtListener cluster {cluster_id}</a> ·
    Fetched {fetched_at} ·
    Mode: <code>{auth_mode}</code> ·
    Generated by <code>juris-citation-verify</code>
  </div>
</div>
</body>
</html>
"""


def _opinion_label(type_code: str) -> str:
    return OPINION_TYPE_NAMES.get(type_code, "Opinion")


def _format_op_heading(op: Opinion) -> str:
    label = _opinion_label(op.type)
    return f"{label} — {op.author_str}" if op.author_str else label


def render_case_html(case: Case, path: Path) -> None:
    """Single-file standalone HTML for a Case."""
    cluster_url = (
        f"https://www.courtlistener.com{case.absolute_url}"
        if case.absolute_url
        else (
            f"https://www.courtlistener.com/opinion/{case.cluster_id}/"
            if case.cluster_id else "https://www.courtlistener.com/"
        )
    )

    # Hero — show the queried citation plus other citations as parallel.
    cite_chunks = [html_escape(case.queried_citation)]
    for c in case.citations:
        if _normalize_cite_str(c) != _normalize_cite_str(case.queried_citation):
            cite_chunks.append(html_escape(c))
    citations_inline = '<span class="sep">·</span>'.join(cite_chunks)

    court_bits: list[str] = []
    if case.court:
        court_bits.append(html_escape(case.court))
    if case.date_filed:
        court_bits.append(f"Decided {html_escape(case.date_filed)}")
    if case.date_argued:
        court_bits.append(f"Argued {html_escape(case.date_argued)}")
    court_line = " · ".join(court_bits) if court_bits else "&nbsp;"

    info_pairs: list[tuple[str, str]] = []
    if case.case_name_full and case.case_name_full != case.case_name:
        info_pairs.append(("Full caption", html_escape(case.case_name_full)))
    info_pairs.append(
        ("Citations",
         ", ".join(f"<code>{html_escape(c)}</code>"
                   for c in (case.citations or [case.queried_citation])))
    )
    if case.court:
        info_pairs.append(("Court", html_escape(case.court)))
    if case.court_id:
        info_pairs.append(("Court ID", f"<code>{html_escape(case.court_id)}</code>"))
    if case.date_filed:
        info_pairs.append(("Decided", html_escape(case.date_filed)))
    if case.date_argued:
        info_pairs.append(("Argued", html_escape(case.date_argued)))
    if case.judges:
        info_pairs.append(("Judges", html_escape(case.judges)))
    if case.docket_number:
        info_pairs.append(("Docket number", f"<code>{html_escape(case.docket_number)}</code>"))
    if case.posture:
        info_pairs.append(("Posture", html_escape(case.posture)))
    if case.status:
        info_pairs.append(("Status", html_escape(case.status)))
    info_pairs.append((
        "CourtListener",
        f'<a href="{html_escape(cluster_url)}">{html_escape(cluster_url)}</a>',
    ))
    info_rows = "\n".join(
        f'<tr><td class="k">{k}</td><td>{v}</td></tr>' for k, v in info_pairs
    )

    auth_callout = ""
    if case.auth_mode != "authenticated":
        missing = case.fields_missing_due_to_auth or [
            "full opinion text", "concurrences", "dissents",
        ]
        items = "".join(f"<li>{html_escape(m)}</li>" for m in missing)
        auth_callout = (
            '<div class="callout"><b>Anonymous mode.</b> '
            "CourtListener returns metadata + syllabus over the public Search API, "
            "but full opinion bodies require authentication. Missing here:"
            f"<ul>{items}</ul>"
            "Get a free token at "
            '<a href="https://www.courtlistener.com/help/api/rest/">'
            "courtlistener.com/help/api/rest</a>, then "
            "<code>export CL_TOKEN=&lt;token&gt;</code> and re-run.</div>"
        )

    syllabus_block = ""
    if case.syllabus:
        # Syllabus often comes as HTML from CL — render as-is, but inside a
        # styled wrapper. Whitespace-pre-wrap handles plain-text fallback.
        syllabus_block = (
            "<h2>Syllabus</h2>\n"
            f'<div class="syllabus">{case.syllabus}</div>'
        )

    procedural_block = ""
    if case.procedural_history:
        procedural_block = (
            "<h2>Procedural history</h2>\n"
            f'<div class="procedural">{case.procedural_history}</div>'
        )

    if not case.opinions:
        opinions_block = (
            '<div class="callout">No opinion bodies were returned for this case.</div>'
        )
    else:
        parts: list[str] = []
        for op in case.opinions:
            heading = html_escape(_opinion_label(op.type))
            by = (
                f' <span class="by">by {html_escape(op.author_str)}</span>'
                if op.author_str else ""
            )
            if op.html:
                body = f'<div class="text-html">{op.html}</div>'
            elif op.text:
                body = f'<pre class="text-plain">{html_escape(op.text)}</pre>'
            else:
                body = (
                    '<div class="callout">No body text returned for this opinion '
                    "(authenticated mode required).</div>"
                )
            parts.append(
                '<div class="opinion">\n'
                f"  <h3>{heading}{by}</h3>\n"
                f"  {body}\n"
                "</div>"
            )
        opinions_block = "\n".join(parts)

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = CASE_HTML_TEMPLATE.format(
        case_name=html_escape(case.case_name or case.queried_citation),
        citations_inline=citations_inline,
        court_line=court_line,
        info_rows=info_rows,
        auth_callout=auth_callout,
        syllabus_block=syllabus_block,
        procedural_block=procedural_block,
        opinions_block=opinions_block,
        cluster_url=html_escape(cluster_url),
        cluster_id=case.cluster_id or "",
        fetched_at=html_escape(fetched_at),
        auth_mode=html_escape(case.auth_mode),
    )
    path.write_text(out, encoding="utf-8")


def render_case_text(case: Case, path: Path) -> None:
    """Plain-text export wrapped at 90 columns. Case name leads the metadata block."""
    width = 90

    def wrap(label: str, value: str) -> list[str]:
        if not value:
            return []
        lead = f"{label}: "
        body = textwrap.fill(
            value,
            width=width,
            initial_indent=lead,
            subsequent_indent=" " * len(lead),
        )
        return [body]

    lines: list[str] = []
    lines.append(case.case_name or case.queried_citation)
    lines.append("=" * min(width, len(lines[0])))
    lines.append("")

    if case.case_name_full and case.case_name_full != case.case_name:
        lines.extend(wrap("Full caption", case.case_name_full))
    lines.extend(wrap("Citations", ", ".join(case.citations or [case.queried_citation])))
    lines.extend(wrap("Court", case.court))
    lines.extend(wrap("Court ID", case.court_id))
    lines.extend(wrap("Decided", case.date_filed))
    lines.extend(wrap("Argued", case.date_argued))
    lines.extend(wrap("Judges", case.judges))
    lines.extend(wrap("Docket number", case.docket_number))
    lines.extend(wrap("Posture", case.posture))
    lines.extend(wrap("Status", case.status))
    if case.absolute_url:
        lines.extend(wrap("CourtListener", f"https://www.courtlistener.com{case.absolute_url}"))
    lines.extend(wrap("Auth mode", case.auth_mode))

    if case.auth_mode != "authenticated":
        lines.append("")
        lines.append("-" * width)
        for note in case.notes:
            lines.append(textwrap.fill(note, width=width))

    if case.syllabus:
        lines.append("")
        lines.append("SYLLABUS")
        lines.append("-" * width)
        lines.append(textwrap.fill(_strip_html(case.syllabus), width=width))

    if case.procedural_history:
        lines.append("")
        lines.append("PROCEDURAL HISTORY")
        lines.append("-" * width)
        lines.append(textwrap.fill(_strip_html(case.procedural_history), width=width))

    if case.opinions:
        for op in case.opinions:
            lines.append("")
            lines.append(_format_op_heading(op).upper())
            lines.append("-" * width)
            text = op.text or _strip_html(op.html)
            if text:
                for paragraph in text.split("\n\n"):
                    paragraph = paragraph.strip()
                    if not paragraph:
                        continue
                    lines.append(textwrap.fill(paragraph, width=width))
                    lines.append("")
            else:
                lines.append("(no body text returned)")
    else:
        lines.append("")
        lines.append("(no opinion bodies returned)")

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def print_case_to_console(case: Case) -> None:
    """Rich panels with truncated opinion excerpts (max 3000 chars per opinion)."""
    EXCERPT_LIMIT = 3000

    if RICH:
        header_lines = [f"[bold white]{case.case_name or case.queried_citation}[/bold white]"]
        cite_str = " · ".join(case.citations or [case.queried_citation])
        header_lines.append(f"[white]{cite_str}[/white]")
        court_bits: list[str] = []
        if case.court:
            court_bits.append(case.court)
        if case.date_filed:
            court_bits.append(f"Decided {case.date_filed}")
        if case.date_argued:
            court_bits.append(f"Argued {case.date_argued}")
        if court_bits:
            header_lines.append(f"[white]{' · '.join(court_bits)}[/white]")
        console.print(
            Panel(
                "\n".join(header_lines),
                border_style="cyan",
                box=box.DOUBLE,
            )
        )

        info = Table(box=box.SIMPLE_HEAVY, show_header=False)
        info.add_column("k", style="dim", width=20)
        info.add_column("v")
        if case.case_name_full and case.case_name_full != case.case_name:
            info.add_row("Full caption", case.case_name_full)
        info.add_row("Citations", ", ".join(case.citations or [case.queried_citation]))
        if case.court:
            info.add_row("Court", case.court)
        if case.court_id:
            info.add_row("Court ID", case.court_id)
        if case.date_filed:
            info.add_row("Decided", case.date_filed)
        if case.date_argued:
            info.add_row("Argued", case.date_argued)
        if case.judges:
            info.add_row("Judges", case.judges)
        if case.docket_number:
            info.add_row("Docket", case.docket_number)
        if case.posture:
            info.add_row("Posture", case.posture)
        if case.status:
            info.add_row("Status", case.status)
        if case.absolute_url:
            info.add_row(
                "CourtListener",
                f"https://www.courtlistener.com{case.absolute_url}",
            )
        info.add_row("Auth mode", case.auth_mode)
        console.print(info)
    else:
        print(f"\n=== {case.case_name or case.queried_citation} ===")
        print(f"  Citations: {', '.join(case.citations or [case.queried_citation])}")
        print(f"  Court: {case.court}    Decided: {case.date_filed}")
        if case.judges:
            print(f"  Judges: {case.judges}")
        if case.docket_number:
            print(f"  Docket: {case.docket_number}")
        print(f"  Auth mode: {case.auth_mode}")

    if case.auth_mode != "authenticated":
        for note in case.notes:
            say(note, "warn")

    if case.syllabus:
        banner("Syllabus")
        text = _strip_html(case.syllabus)
        if RICH:
            console.print(text[:EXCERPT_LIMIT])
            if len(text) > EXCERPT_LIMIT:
                console.print(f"[dim]… (+{len(text) - EXCERPT_LIMIT:,} chars truncated)[/dim]")
        else:
            print(text[:EXCERPT_LIMIT])
            if len(text) > EXCERPT_LIMIT:
                print(f"... (+{len(text) - EXCERPT_LIMIT} chars truncated)")

    if case.procedural_history:
        banner("Procedural history")
        text = _strip_html(case.procedural_history)
        if RICH:
            console.print(text[:EXCERPT_LIMIT])
        else:
            print(text[:EXCERPT_LIMIT])

    if case.opinions:
        for i, op in enumerate(case.opinions, 1):
            banner(f"Opinion {i} · {_format_op_heading(op)}")
            body = op.text or _strip_html(op.html)
            if not body:
                say("(no body text — set CL_TOKEN for full opinions)", "warn")
                continue
            excerpt = body[:EXCERPT_LIMIT]
            if RICH:
                console.print(excerpt)
                if len(body) > EXCERPT_LIMIT:
                    console.print(
                        f"[dim]… (+{len(body) - EXCERPT_LIMIT:,} chars truncated; "
                        "full text in --html / --text exports)[/dim]"
                    )
            else:
                print(excerpt)
                if len(body) > EXCERPT_LIMIT:
                    print(f"... (+{len(body) - EXCERPT_LIMIT} chars truncated)")
    else:
        say("(no opinion bodies returned)", "warn")


def render_html(report: Report, out_path: Path) -> None:
    summary_rows = "\n".join(
        _row([
            r.name,
            _pill(r.status),
            r.detail or "",
            f"{r.latency_ms:.0f} ms" if r.latency_ms else "",
        ])
        for r in report.results
    )
    coverage_rows = "\n".join(
        _row([
            e["area"],
            e["query"],
            str(e["hits"]),
            e.get("top", ""),
            f"{e['latency_ms']:.0f} ms",
        ])
        for e in report.practice_area_results
    )
    eyecite_rows = "\n".join(
        _row([str(e["i"]), e["type"], f"<code>{e['matched']}</code>", e["reporter"], e["meta"]])
        for e in report.eyecite_extractions
    )
    hybrid_rows = "\n".join(
        _row([
            f"<code>{e['citation']}</code>",
            e["expected"],
            str(e["hits"]),
            e["verdict"],
            "✅" if e["correct"] else "❌",
        ])
        for e in report.hybrid_detector
    )

    html = HTML_TEMPLATE.format(
        generated_at=report.finished_at,
        auth_mode=report.auth_mode,
        eyecite_version=report.eyecite_version,
        summary_rows=summary_rows or _row(["—", "—", "—", "—"]),
        coverage_rows=coverage_rows or _row(["—", "—", "—", "—", "—"]),
        eyecite_rows=eyecite_rows or _row(["—", "—", "—", "—", "—"]),
        hybrid_rows=hybrid_rows or _row(["—", "—", "—", "—", "—"]),
    )
    out_path.write_text(html, encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify CourtListener API + Eyecite parser hands-on."
    )
    parser.add_argument("--html", type=Path, help="Path to write HTML report (verification) or HTML case page (--extract)")
    parser.add_argument("--json", type=Path, help="Path to write raw JSON results / case data")
    parser.add_argument("--text", type=Path, help="(--extract only) Path to write plain-text case dump")
    parser.add_argument("--memo", type=Path, help="Path to a custom test memo (.txt)")
    parser.add_argument("--no-net", action="store_true", help="Skip all network calls")
    parser.add_argument("--quick", action="store_true", help="Skip slow practice-area test")
    parser.add_argument(
        "--extract",
        metavar="CITATION",
        help='Switch to case-extraction mode. Look up a single case by citation '
        '(e.g. "576 U.S. 644") and dump full case detail. Skips verification stages.',
    )
    args = parser.parse_args()

    import eyecite as _eyecite

    auth_mode = "authenticated" if CL_TOKEN else "anonymous"
    eyecite_version = getattr(_eyecite, "__version__", "unknown")

    # ----- Case-extraction mode (bypasses verification stages) -----
    if args.extract:
        if args.no_net:
            print("ERROR: --extract requires network access (cannot combine with --no-net).",
                  file=sys.stderr)
            return 2
        if RICH:
            console.print(
                Panel(
                    "[bold]juris-citation-verify · CASE EXTRACTION[/bold]\n"
                    f"Citation: [cyan]{args.extract}[/cyan]\n"
                    f"Auth mode: [cyan]{auth_mode}[/cyan]   "
                    f"Eyecite: [cyan]{eyecite_version}[/cyan]",
                    border_style="cyan",
                    box=box.DOUBLE,
                )
            )
        else:
            print("=" * 60)
            print(" juris-citation-verify · CASE EXTRACTION")
            print(f" Citation: {args.extract}  |  Auth: {auth_mode}")
            print("=" * 60)

        try:
            case = extract_case(args.extract)
        except CaseUnparseable:
            print(f"ERROR: Could not parse '{args.extract}' as a legal citation.",
                  file=sys.stderr)
            return 2
        except CaseNotFound:
            print("ERROR: Case not found in CourtListener.", file=sys.stderr)
            return 3

        print_case_to_console(case)

        if args.html:
            render_case_html(case, args.html)
            say(f"Wrote HTML: {args.html}", "pass")
        if args.text:
            render_case_text(case, args.text)
            say(f"Wrote text: {args.text}", "pass")
        if args.json:
            args.json.write_text(
                json.dumps(asdict(case), indent=2, default=str),
                encoding="utf-8",
            )
            say(f"Wrote JSON: {args.json}", "pass")
        return 0

    # ----- Verification mode -----
    if args.text:
        say("--text is only meaningful with --extract (ignored).", "warn")

    report = Report(
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        auth_mode=auth_mode,
        eyecite_version=eyecite_version,
    )

    if RICH:
        console.print(
            Panel(
                "[bold]juris-citation-verify[/bold]\n"
                "Hands-on verification of CourtListener API + Eyecite\n"
                f"Auth mode: [cyan]{report.auth_mode}[/cyan]   "
                f"Eyecite: [cyan]{report.eyecite_version}[/cyan]",
                border_style="cyan",
                box=box.DOUBLE,
            )
        )
    else:
        print("=" * 60)
        print(" juris-citation-verify ")
        print(f" Auth: {report.auth_mode}  |  Eyecite: {report.eyecite_version}")
        print("=" * 60)

    # Run stages
    if not args.no_net:
        stage_reachability(report)
        stage_anon_audit(report)
        if not args.quick:
            stage_practice_areas(report)

    stage_eyecite_local(report, args.memo)

    if not args.no_net:
        stage_hybrid_detector(report)

    report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Final tally
    banner("Final tally")
    passes = sum(1 for r in report.results if r.status == "pass")
    fails = sum(1 for r in report.results if r.status == "fail")
    warns = sum(1 for r in report.results if r.status == "warn")
    say(f"{passes} passed · {warns} warnings · {fails} failed", "pass" if fails == 0 else "fail")

    if args.json:
        args.json.write_text(json.dumps(report.to_dict(), indent=2, default=str))
        say(f"Wrote JSON: {args.json}", "pass")

    if args.html:
        render_html(report, args.html)
        say(f"Wrote HTML: {args.html}  (open in browser)", "pass")

    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
