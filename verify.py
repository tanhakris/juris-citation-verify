#!/usr/bin/env python3
"""
juris-citation-verify
=====================
Standalone CLI that verifies CourtListener API capabilities and the Eyecite
citation parser, end-to-end, on your local machine.

Designed for JurisLPO / Tech House AI hallucination-detection due diligence.
Reproduces every test from the live verification report.

One command, one report
-----------------------
The default flow runs system health checks, Eyecite-parses a memo, verifies
each FullCaseCitation against CourtListener, and renders everything in a
single console output and (optionally) a single HTML report.

Usage
-----
    python verify.py                                  # console output only
    python verify.py --html report.html               # console + unified HTML
    python verify.py --memo my_memo.txt --html r.html # custom memo
    python verify.py --no-net --memo sample_memo.txt  # eyecite-only (no CL)
    python verify.py --quick                          # skip practice-area stage
    CL_TOKEN=xxxxx python verify.py --html r.html     # auth = full opinion text

Author : Gopal | JurisConsultants Group
License: MIT
"""

from __future__ import annotations

import argparse
import json
import os
import random
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
    # verified_real | hallucinated | unparseable | skipped | api_error
    verdict: str = "verified_real"
    occurrences_in_memo: int = 1
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
class MemoAnalysis:
    memo_path: str
    auth_mode: str
    total_citations: int = 0       # unique by (vol, rep, pg)
    raw_citation_tokens: int = 0   # before dedup
    verified: int = 0
    hallucinated: int = 0
    unparseable: int = 0
    skipped: int = 0
    api_errors: int = 0
    cases: list[Case] = field(default_factory=list)


@dataclass
class Report:
    started_at: str
    finished_at: str = ""
    auth_mode: str = "anonymous"
    eyecite_version: str = ""
    results: list[TestResult] = field(default_factory=list)
    practice_area_results: list[dict] = field(default_factory=list)
    memo_analysis: MemoAnalysis | None = None

    def to_dict(self) -> dict:
        return asdict(self)


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


RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
RETRY_DELAYS = (1.0, 2.0, 4.0)   # seconds before each retry (jittered)


def http(
    method: str,
    path: str,
    *,
    verbose: bool = False,
    **kw,
) -> tuple[int, dict | str | None, float]:
    """Returns (status_code, parsed_body_or_text, total_latency_ms).

    Retries up to 3 times with exponential backoff (≈1s, 2s, 4s ± jitter)
    on HTTP 429 / 500 / 502 / 503 / 504. A transient blip used to mark a
    real case as hallucinated forever; the retry budget makes verdicts
    deterministic across runs.
    """
    url = path if path.startswith("http") else f"{CL_BASE}{path}"
    t_total = time.time()
    last_status: int = -1
    last_body: dict | str | None = None

    for attempt in range(len(RETRY_DELAYS) + 1):  # 1 initial + 3 retries
        try:
            r = requests.request(method, url, headers=_headers(), timeout=45, **kw)
            try:
                last_body = r.json()
            except ValueError:
                last_body = r.text
            last_status = r.status_code
        except requests.RequestException as exc:
            last_status = -1
            last_body = str(exc)

        if last_status not in RETRYABLE_STATUSES:
            return last_status, last_body, (time.time() - t_total) * 1000
        if attempt >= len(RETRY_DELAYS):
            return last_status, last_body, (time.time() - t_total) * 1000

        delay = RETRY_DELAYS[attempt] * (0.85 + random.random() * 0.3)
        if verbose:
            say(
                f"retry {attempt + 1}/{len(RETRY_DELAYS)}: HTTP {last_status} on "
                f"{method} {path} — sleeping {delay:.2f}s",
                "warn",
            )
        time.sleep(delay)

    # Unreachable; loop always returns.
    return last_status, last_body, (time.time() - t_total) * 1000


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


def _build_case_from_cluster(canonical: str, cluster: dict) -> Case:
    """Hydrate a Case from a /citation-lookup/ cluster object."""
    cluster_cites = [
        f"{c.get('volume', '')} {c.get('reporter', '')} {c.get('page', '')}".strip()
        for c in (cluster.get("citations") or [])
        if isinstance(c, dict)
    ]
    cluster_cites = [c for c in cluster_cites if c]
    return Case(
        queried_citation=canonical,
        verdict="verified_real",
        case_name=cluster.get("case_name") or cluster.get("case_name_full") or "",
        case_name_full=cluster.get("case_name_full") or "",
        citations=cluster_cites or [canonical],
        date_filed=cluster.get("date_filed") or "",
        date_argued=cluster.get("date_argued") or "",
        judges=cluster.get("judges") or "",
        syllabus=cluster.get("syllabus") or "",
        procedural_history=cluster.get("procedural_history") or "",
        posture=cluster.get("posture") or "",
        absolute_url=cluster.get("absolute_url") or "",
        cluster_id=cluster.get("id"),
        auth_mode="authenticated" if CL_TOKEN else "anonymous",
    )


def _hydrate_docket(case: Case, cluster: dict, verbose: bool = False) -> None:
    """Authenticated only: fetch the docket → court_name + docket_number."""
    docket_field = cluster.get("docket")
    if not (isinstance(docket_field, str) and docket_field.startswith("http")):
        return
    dcode, docket, _ = http("GET", docket_field, verbose=verbose)
    if dcode != 200 or not isinstance(docket, dict):
        return
    case.docket_number = docket.get("docket_number") or case.docket_number
    case.court_id = docket.get("court_id") or case.court_id
    court_field = docket.get("court")
    if isinstance(court_field, str) and court_field.startswith("http"):
        ccode, cbody, _ = http("GET", court_field, verbose=verbose)
        if ccode == 200 and isinstance(cbody, dict):
            case.court = cbody.get("full_name") or cbody.get("short_name") or case.court
            case.court_id = cbody.get("id") or case.court_id
    elif isinstance(court_field, dict):
        case.court = court_field.get("full_name") or court_field.get("short_name") or case.court
        case.court_id = court_field.get("id") or case.court_id


def _extract_via_citation_lookup(canonical: str, verbose: bool = False) -> Case:
    """Primary path: POST /citation-lookup/ (auth-only, deterministic).

    Returns a Case whose verdict is one of:
      - verified_real (cluster matched + populated)
      - hallucinated  (per-citation status 404 / clusters empty)
      - api_error     (HTTP non-200 after retries, or anomalous response shape)
    """
    code, body, ms = http(
        "POST",
        "/citation-lookup/",
        data={"text": canonical},
        verbose=verbose,
    )
    if verbose:
        say(f"  POST /citation-lookup/ ← '{canonical}' → HTTP {code} ({ms:.0f} ms)", "info")
        if isinstance(body, (dict, list)):
            preview = json.dumps(body, default=str)[:500]
            console.print(f"     [dim]{preview}{'…' if len(preview) >= 500 else ''}[/dim]") if RICH else print(f"     {preview}")

    if code != 200:
        case = Case(queried_citation=canonical, verdict="api_error")
        case.notes.append(f"POST /citation-lookup/ returned HTTP {code} after retries.")
        if isinstance(body, dict):
            err = body.get("detail") or body.get("error") or ""
            if err:
                case.notes.append(str(err))
        return case

    if not isinstance(body, list) or not body:
        case = Case(queried_citation=canonical, verdict="api_error")
        case.notes.append("HTTP 200 but the citation-lookup body was empty / wrong shape.")
        return case

    entry = body[0] if isinstance(body[0], dict) else {}
    per_status = entry.get("status", 200)
    clusters = entry.get("clusters") or []

    # Per-citation 404 (or 200 with no clusters) = the citation is real-looking
    # but resolves to nothing in CL's index. That's a hallucination.
    if per_status == 404 or (per_status == 200 and not clusters):
        return Case(queried_citation=canonical, verdict="hallucinated")

    # Per-citation status that is not 200 and not 404 is an API hiccup,
    # not a hallucination. Surface it distinctly.
    if per_status != 200:
        case = Case(queried_citation=canonical, verdict="api_error")
        err = entry.get("error_message") or f"per-citation status {per_status}"
        case.notes.append(f"citation-lookup per-citation status {per_status}: {err}")
        return case

    if not isinstance(clusters[0], dict):
        case = Case(queried_citation=canonical, verdict="api_error")
        case.notes.append("citation-lookup returned a cluster entry of unexpected shape.")
        return case

    cluster = clusters[0]
    case = _build_case_from_cluster(canonical, cluster)

    # Authenticated enrichment: docket → court_name; _refine_from_cluster
    # pulls sub_opinions for full text.
    _hydrate_docket(case, cluster, verbose=verbose)
    _refine_from_cluster(case)
    return case


def _extract_via_search(canonical: str, verbose: bool = False) -> Case:
    """Fallback path for anonymous mode (no CL_TOKEN). /citation-lookup/
    requires auth; /search/ does not. Less precise but still useful for
    case-name + parallel-citation metadata."""
    code, body, ms = http(
        "GET",
        "/search/",
        params={
            "q": f'"{canonical}"',
            "type": "o",
            "order_by": "dateFiled asc",
            "page_size": 50,
        },
        verbose=verbose,
    )
    if verbose:
        say(f"  GET /search/ ← '{canonical}' → HTTP {code} ({ms:.0f} ms)", "info")

    if code != 200:
        case = Case(queried_citation=canonical, verdict="api_error")
        case.notes.append(f"GET /search/ returned HTTP {code} after retries.")
        return case
    if not isinstance(body, dict):
        return Case(queried_citation=canonical, verdict="hallucinated")
    match = _pick_cluster_from_search(canonical, body.get("results") or [])
    if match is None:
        return Case(queried_citation=canonical, verdict="hallucinated")

    case = Case(
        queried_citation=canonical,
        verdict="verified_real",
        case_name=match.get("caseName") or match.get("caseNameFull") or "",
        case_name_full=match.get("caseNameFull") or "",
        citations=[c for c in (match.get("citation") or []) if c],
        court=match.get("court") or "",
        court_id=match.get("court_id") or "",
        date_filed=match.get("dateFiled") or "",
        date_argued=match.get("dateArgued") or "",
        judges=match.get("judge") or "",
        docket_number=match.get("docketNumber") or "",
        absolute_url=match.get("absolute_url") or "",
        cluster_id=match.get("cluster_id"),
        auth_mode="anonymous",
    )
    case.fields_missing_due_to_auth = ["full opinion text", "concurrences", "dissents"]
    case.notes.append(
        "Anonymous mode: lookup via /search/ (less precise than /citation-lookup/). "
        "Set CL_TOKEN for full opinion text and the deterministic citation-lookup path."
    )
    return case


def extract_case(
    citation_str: str,
    eyecite_match: FullCaseCitation | None = None,
    verbose: bool = False,
) -> Case:
    """Resolve a citation to a Case. Always returns a Case with `.verdict` set.

    Primary path: POST /citation-lookup/ (when CL_TOKEN is present).
    Fallback for anonymous use: GET /search/ + `_pick_cluster_from_search`.
    """
    if eyecite_match is None:
        cleaned = clean_text(citation_str, ["html", "inline_whitespace"])
        parsed = [c for c in get_citations(cleaned) if isinstance(c, FullCaseCitation)]
        if not parsed:
            return Case(queried_citation=citation_str, verdict="unparseable")
        eyecite_match = parsed[0]

    vol = eyecite_match.groups.get("volume", "") or ""
    rep = eyecite_match.groups.get("reporter", "") or ""
    pg = eyecite_match.groups.get("page", "") or ""
    canonical = f"{vol} {rep} {pg}".strip()
    if not (vol and rep and pg):
        return Case(queried_citation=citation_str, verdict="unparseable")

    if CL_TOKEN:
        return _extract_via_citation_lookup(canonical, verbose=verbose)
    return _extract_via_search(canonical, verbose=verbose)


def _citation_key(fc: FullCaseCitation) -> tuple[str, str, str]:
    """Canonical dedup key. Lower-cased, whitespace-collapsed."""
    return (
        " ".join((fc.groups.get("volume", "") or "").split()).lower(),
        " ".join((fc.groups.get("reporter", "") or "").split()).lower(),
        " ".join((fc.groups.get("page", "") or "").split()).lower(),
    )


def stage_memo_analysis(
    report: Report,
    memo_path: Path | None,
    do_network: bool,
    verbose: bool = False,
) -> None:
    """Parse the memo, extract every full citation, and tag a verdict per citation.

    Replaces the older stage_eyecite_local + stage_hybrid_detector duo.
    Dedupes by (volume, reporter, page) — a legal memo typically lists each
    case twice (Table of Authorities + body), and we don't want to double-bill
    the API or risk inconsistent verdicts across the duplicates.
    """
    banner("Stage 4 · Memo analysis (Eyecite + CourtListener)")
    text = memo_path.read_text() if memo_path and memo_path.exists() else DEFAULT_TEST_MEMO
    cleaned = clean_text(text, ["html", "inline_whitespace"])

    t0 = time.time()
    citations = get_citations(cleaned)
    full_cites = [c for c in citations if isinstance(c, FullCaseCitation)]
    parse_ms = (time.time() - t0) * 1000

    # ----- Dedup pass -----
    occurrence_counts: dict[tuple[str, str, str], int] = {}
    deduped: list[FullCaseCitation] = []
    for fc in full_cites:
        key = _citation_key(fc)
        if key in occurrence_counts:
            occurrence_counts[key] += 1
        else:
            occurrence_counts[key] = 1
            deduped.append(fc)

    say(
        f"Parsed {len(citations)} citation tokens "
        f"({len(full_cites)} full cites, {len(deduped)} unique by vol/rep/page) "
        f"in {parse_ms:.1f} ms ({len(text)} chars input)",
        "pass",
    )

    analysis = MemoAnalysis(
        memo_path=str(memo_path) if memo_path else "(default memo)",
        auth_mode=report.auth_mode,
        total_citations=len(deduped),
        raw_citation_tokens=len(full_cites),
    )

    if not do_network:
        say("--no-net set: skipping CourtListener lookups.", "warn")
        for fc in deduped:
            vol = fc.groups.get("volume", "") or ""
            rep = fc.groups.get("reporter", "") or ""
            pg = fc.groups.get("page", "") or ""
            canonical = f"{vol} {rep} {pg}".strip() or fc.matched_text()
            analysis.cases.append(
                Case(
                    queried_citation=canonical,
                    verdict="skipped",
                    occurrences_in_memo=occurrence_counts[_citation_key(fc)],
                )
            )
        analysis.skipped = len(deduped)
        report.memo_analysis = analysis
        report.results.append(
            TestResult("memo_analysis", "pass",
                       f"{len(deduped)} unique citations (skipped lookups)")
        )
        return

    if RICH:
        progress = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold cyan")
        progress.add_column("#", width=3)
        progress.add_column("Citation", width=22)
        progress.add_column("Occ.", justify="right", width=5)
        progress.add_column("Verdict", width=18)
        progress.add_column("Case name", width=42, overflow="ellipsis")

    total = len(deduped)
    for i, fc in enumerate(deduped, 1):
        key = _citation_key(fc)
        occurrences = occurrence_counts[key]
        t_call = time.time()
        case = extract_case(fc.matched_text(), eyecite_match=fc, verbose=verbose)
        case.occurrences_in_memo = occurrences
        call_ms = (time.time() - t_call) * 1000
        analysis.cases.append(case)

        if case.verdict == "verified_real":
            analysis.verified += 1
        elif case.verdict == "hallucinated":
            analysis.hallucinated += 1
        elif case.verdict == "api_error":
            analysis.api_errors += 1
        else:
            analysis.unparseable += 1

        verdict_label = VERDICT_LABEL.get(case.verdict, case.verdict)
        if verbose:
            occ = f" [×{occurrences}]" if occurrences > 1 else ""
            say(
                f"[{i}/{total}] {case.queried_citation}{occ} → {verdict_label} "
                f"· {case.case_name or '—'} · {call_ms:.0f}ms",
                "info",
            )
        if RICH:
            progress.add_row(
                str(i),
                case.queried_citation,
                str(occurrences) if occurrences > 1 else "",
                verdict_label,
                case.case_name or "—",
            )
        else:
            print(
                f"  [{i}] {case.queried_citation:22s} "
                f"×{occurrences:<2d} {verdict_label:18s} {case.case_name or '—'}"
            )
        time.sleep(0.2)

    if RICH:
        console.print(progress)

    report.memo_analysis = analysis
    overall = (
        "pass"
        if (analysis.hallucinated == 0
            and analysis.unparseable == 0
            and analysis.api_errors == 0)
        else "warn"
    )
    detail = (
        f"{analysis.verified}/{analysis.total_citations} verified, "
        f"{analysis.hallucinated} hallucinated, {analysis.unparseable} unparseable, "
        f"{analysis.api_errors} api_errors"
    )
    report.results.append(TestResult("memo_analysis", overall, detail))


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Citation Verification Report</title>
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
          border-radius: 8px; box-shadow: 0 2px 24px rgba(0,0,0,0.06);
          overflow: hidden; }}
  .hero {{ background: linear-gradient(135deg, var(--navy) 0%, var(--blue) 100%);
          color: #fff; padding: 36px 56px 28px; }}
  .hero h1 {{ margin: 0 0 6px 0; font-size: 1.85em; }}
  .hero .meta {{ color: #cfe0f4; font-size: 13px; margin-bottom: 24px; }}
  .stats {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }}
  .stat {{ background: rgba(255,255,255,0.08); border-radius: 6px;
          padding: 18px 22px; }}
  .stat .num {{ font-size: 2.2em; font-weight: 700; line-height: 1.1; }}
  .stat .label {{ color: #cfe0f4; font-size: 13px; margin-top: 4px;
                 letter-spacing: 0.02em; text-transform: uppercase; }}
  .stat.good .num {{ color: #b6e3b6; }}
  .stat.bad  .num {{ color: #ffb4b4; }}
  .body-pad {{ padding: 32px 56px 8px 56px; }}
  h2 {{ color: var(--blue); margin-top: 32px; font-size: 1.25em;
       border-bottom: 1px solid #d8dde4; padding-bottom: 6px; }}
  h3 {{ color: var(--navy); margin-top: 22px; font-size: 1.05em; }}
  .meta {{ color: var(--gray); font-size: 13px; margin-bottom: 18px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0 22px;
          font-size: 14px; }}
  th {{ background: var(--navy); color: #fff; text-align: left;
       padding: 10px 12px; font-weight: 600; }}
  td {{ padding: 9px 12px; border-bottom: 1px solid #e3e6eb; vertical-align: top; }}
  tr:nth-child(even) td {{ background: #fafbfc; }}
  .pill {{ display: inline-block; padding: 2px 10px; border-radius: 12px;
          font-size: 12px; font-weight: 600; }}
  .pill.pass, .pill.verified_real {{ background: #DBEFD9; color: var(--pass); }}
  .pill.fail, .pill.hallucinated   {{ background: #FDE3E3; color: var(--fail); }}
  .pill.warn, .pill.unparseable, .pill.skipped,
  .pill.api_error {{ background: #FBF1D5; color: var(--warn); }}
  .pill.info {{ background: #DBE7F0; color: var(--blue); }}
  .callout {{ background: #FCF0C8; border-left: 4px solid var(--warn);
             padding: 14px 18px; margin: 18px 0; font-style: italic;
             color: #5C4A0A; }}
  code {{ background: #eef0f4; padding: 1px 5px; border-radius: 3px;
         font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 13px; }}
  .case-card {{ margin: 28px 0; border: 1px solid #e3e6eb; border-radius: 8px;
               overflow: hidden; }}
  .case-card .case-hero {{ background: linear-gradient(135deg, var(--navy) 0%, var(--blue) 100%);
                          color: #fff; padding: 22px 28px; }}
  .case-card .case-hero h3 {{ color: #fff; margin: 0 0 6px 0; font-size: 1.4em;
                             border: none; padding: 0; }}
  .case-card .case-hero .citations {{ font-family: "SF Mono", Menlo, Consolas, monospace;
                                     font-size: 13px; opacity: 0.92; }}
  .case-card .case-hero .citations .sep {{ opacity: 0.5; padding: 0 6px; }}
  .case-card .case-hero .court-line {{ font-size: 13px; opacity: 0.92; margin-top: 8px; }}
  .case-card .case-body {{ padding: 18px 28px 4px; background: #fff;
                          font: 16px/1.65 Georgia, "Times New Roman", serif; }}
  .case-card table.info {{ font-size: 14px;
                          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif; }}
  .case-card table.info td {{ padding: 7px 10px; }}
  .case-card table.info td.k {{ color: var(--gray); width: 200px; }}
  .syllabus {{ background: #f6f8fb; border-left: 4px solid var(--blue);
              padding: 14px 18px; margin: 14px 0; font-size: 0.97em;
              white-space: pre-wrap; }}
  .procedural {{ background: #f6f8fb; border-left: 4px solid var(--gray);
                padding: 14px 18px; margin: 14px 0; font-size: 0.97em;
                white-space: pre-wrap; }}
  .opinion {{ margin-top: 24px; border-left: 4px solid var(--blue);
             padding: 12px 18px 16px 22px; background: #fafbfd; }}
  .opinion h4 {{ margin: 0 0 8px 0; font-size: 1em; color: var(--navy);
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif; }}
  .opinion .by {{ color: var(--gray); font-weight: 400; font-style: italic;
                font-size: 0.92em; margin-left: 6px; }}
  .opinion .text-html p {{ margin: 0 0 1em; }}
  .opinion pre.text-plain {{ font: 16px/1.7 Georgia, "Times New Roman", serif;
                            white-space: pre-wrap; word-wrap: break-word;
                            margin: 0; background: transparent; }}
  .footer {{ margin-top: 24px; padding: 16px 56px; border-top: 1px solid #e3e6eb;
            color: var(--gray); font-size: 12px; }}
  a {{ color: var(--blue); }}
  .hero a {{ color: #cfe0f4; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="hero">
    <h1>Citation Verification Report</h1>
    <div class="meta">Memo: <code>{memo_path}</code> &nbsp;·&nbsp;
        Auth: <code>{auth_mode}</code> &nbsp;·&nbsp;
        Eyecite: <code>{eyecite_version}</code> &nbsp;·&nbsp;
        Generated {generated_at}</div>
    <div class="stats">{stat_boxes}</div>
  </div>

  <div class="body-pad">
    <h2>System health</h2>
    <table>
      <thead><tr><th>Check</th><th>Status</th><th>Detail</th><th>Latency</th></tr></thead>
      <tbody>{summary_rows}</tbody>
    </table>

    {practice_section}

    <h2>Memo analysis</h2>
    {memo_summary_meta}
    <table>
      <thead><tr><th>#</th><th>Citation</th><th>Verdict</th>
                 <th>Case name</th><th>Detail</th></tr></thead>
      <tbody>{memo_rows}</tbody>
    </table>

    {cases_section}
  </div>

  <div class="footer">
    juris-citation-verify · MIT licensed · auth: <code>{auth_mode}</code> ·
    eyecite: <code>{eyecite_version}</code> · {generated_at}
  </div>
</div>
</body>
</html>
"""


def _row(cells: list[str]) -> str:
    return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


def _pill(status: str, label: str | None = None) -> str:
    return f'<span class="pill {status}">{html_escape(label or status)}</span>'


def _opinion_label(type_code: str) -> str:
    return OPINION_TYPE_NAMES.get(type_code, "Opinion")


def _format_op_heading(op: Opinion) -> str:
    label = _opinion_label(op.type)
    return f"{label} — {op.author_str}" if op.author_str else label


VERDICT_LABEL = {
    "verified_real": "✅ verified",
    "hallucinated":  "🚨 hallucinated",
    "unparseable":   "⚠️ unparseable",
    "skipped":       "⏸ skipped",
    "api_error":     "⚠️ api error",
}


def _case_anchor(idx: int) -> str:
    return f"case-{idx}"


def _render_case_card_html(case: Case, anchor: str) -> str:
    """Inner HTML for a single verified case (no <html><head>)."""
    cluster_url = (
        f"https://www.courtlistener.com{case.absolute_url}"
        if case.absolute_url
        else (
            f"https://www.courtlistener.com/opinion/{case.cluster_id}/"
            if case.cluster_id else "https://www.courtlistener.com/"
        )
    )

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
    info_pairs.append((
        "Citations",
        ", ".join(f"<code>{html_escape(c)}</code>"
                  for c in (case.citations or [case.queried_citation])),
    ))
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
            "courtlistener.com/help/api/rest</a>, "
            "<code>export CL_TOKEN=&lt;token&gt;</code>, then re-run.</div>"
        )

    syllabus_block = ""
    if case.syllabus:
        syllabus_block = (
            "<h4>Syllabus</h4>\n"
            f'<div class="syllabus">{case.syllabus}</div>'
        )

    procedural_block = ""
    if case.procedural_history:
        procedural_block = (
            "<h4>Procedural history</h4>\n"
            f'<div class="procedural">{case.procedural_history}</div>'
        )

    if case.opinions:
        op_parts: list[str] = []
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
                    '<div class="callout">No body text returned for this opinion.</div>'
                )
            op_parts.append(
                '<div class="opinion">\n'
                f"  <h4>{heading}{by}</h4>\n"
                f"  {body}\n"
                "</div>"
            )
        opinions_block = "\n".join(op_parts)
    else:
        opinions_block = ""  # already noted in auth_callout

    return (
        f'<div class="case-card" id="{html_escape(anchor)}">\n'
        '  <div class="case-hero">\n'
        f'    <h3>{html_escape(case.case_name or case.queried_citation)}</h3>\n'
        f'    <div class="citations">{citations_inline}</div>\n'
        f'    <div class="court-line">{court_line}</div>\n'
        "  </div>\n"
        '  <div class="case-body">\n'
        f"    <table class=\"info\">{info_rows}</table>\n"
        f"    {auth_callout}\n"
        f"    {syllabus_block}\n"
        f"    {procedural_block}\n"
        f"    {opinions_block}\n"
        "  </div>\n"
        "</div>"
    )


def render_html(report: Report, out_path: Path) -> None:
    """Unified single-file HTML report: system health + memo analysis + case cards."""
    analysis = report.memo_analysis or MemoAnalysis(
        memo_path="(none)", auth_mode=report.auth_mode,
    )

    # ---- Hero stat boxes ----
    hallucinated_class = "stat bad" if analysis.hallucinated > 0 else "stat good"
    stat_blocks: list[str] = [
        f'<div class="stat"><div class="num">{analysis.total_citations}</div>'
        '<div class="label">unique citations</div></div>',
        f'<div class="stat good"><div class="num">{analysis.verified}</div>'
        '<div class="label">verified</div></div>',
        f'<div class="{hallucinated_class}"><div class="num">{analysis.hallucinated}</div>'
        '<div class="label">hallucinated</div></div>',
    ]
    if analysis.api_errors:
        stat_blocks.append(
            f'<div class="stat"><div class="num">{analysis.api_errors}</div>'
            '<div class="label">api errors</div></div>'
        )
    stat_boxes = "\n".join(stat_blocks)

    # ---- System health table ----
    summary_rows = "\n".join(
        _row([
            html_escape(r.name),
            _pill(r.status),
            html_escape(r.detail or ""),
            f"{r.latency_ms:.0f} ms" if r.latency_ms else "",
        ])
        for r in report.results
    ) or _row(["—", "—", "—", "—"])

    # ---- Practice-area section ----
    if report.practice_area_results:
        coverage_rows = "\n".join(
            _row([
                html_escape(e["area"]),
                f"<code>{html_escape(e['query'])}</code>",
                str(e["hits"]),
                html_escape(e.get("top", "")),
                f"{e['latency_ms']:.0f} ms",
            ])
            for e in report.practice_area_results
        )
        practice_section = (
            "<h3>Practice-area coverage</h3>\n"
            "<table>\n"
            "<thead><tr><th>Area</th><th>Query</th><th>Hits</th>"
            "<th>Top result</th><th>Latency</th></tr></thead>\n"
            f"<tbody>{coverage_rows}</tbody>\n</table>"
        )
    else:
        practice_section = ""

    # ---- Memo analysis table + per-case cards ----
    memo_rows_parts: list[str] = []
    case_cards: list[str] = []
    for i, case in enumerate(analysis.cases, 1):
        verdict_label = VERDICT_LABEL.get(case.verdict, case.verdict)
        if case.verdict == "verified_real":
            anchor = _case_anchor(i)
            detail_html = f'<a href="#{html_escape(anchor)}">view full case</a>'
            case_cards.append(_render_case_card_html(case, anchor))
        elif case.verdict == "hallucinated":
            detail_html = "no CourtListener match"
        elif case.verdict == "skipped":
            detail_html = "no network"
        elif case.verdict == "api_error":
            reason = (case.notes[0] if case.notes else "API error")[:90]
            detail_html = html_escape(reason)
        else:
            detail_html = "—"
        if case.occurrences_in_memo > 1:
            detail_html += (
                f' <span class="meta">(cited ×{case.occurrences_in_memo} in memo)</span>'
            )
        memo_rows_parts.append(
            _row([
                str(i),
                f"<code>{html_escape(case.queried_citation)}</code>",
                _pill(case.verdict, verdict_label),
                html_escape(case.case_name or "—"),
                detail_html,
            ])
        )
    memo_rows = "\n".join(memo_rows_parts) or _row(["—", "—", "—", "—", "—"])

    memo_summary_meta = (
        f'<div class="meta">Memo: <code>{html_escape(analysis.memo_path)}</code> · '
        f"{analysis.raw_citation_tokens or analysis.total_citations} raw tokens · "
        f"{analysis.total_citations} unique · "
        f"{analysis.verified} verified · {analysis.hallucinated} hallucinated · "
        f"{analysis.unparseable} unparseable"
        + (f" · {analysis.api_errors} api errors" if analysis.api_errors else "")
        + (f" · {analysis.skipped} skipped" if analysis.skipped else "")
        + "</div>"
    )

    if case_cards:
        cases_section = "<h2>Verified cases</h2>\n" + "\n".join(case_cards)
    else:
        cases_section = ""

    html = HTML_TEMPLATE.format(
        memo_path=html_escape(analysis.memo_path),
        auth_mode=html_escape(report.auth_mode),
        eyecite_version=html_escape(report.eyecite_version),
        generated_at=html_escape(report.finished_at or ""),
        stat_boxes=stat_boxes,
        summary_rows=summary_rows,
        practice_section=practice_section,
        memo_summary_meta=memo_summary_meta,
        memo_rows=memo_rows,
        cases_section=cases_section,
    )
    out_path.write_text(html, encoding="utf-8")


def print_memo_analysis_to_console(analysis: MemoAnalysis | None) -> None:
    """Console summary: per-citation verdict table, then per-verified-case detail."""
    EXCERPT_LIMIT = 3000
    if analysis is None:
        return

    banner("Memo analysis · summary")
    if RICH:
        t = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold cyan")
        t.add_column("#", width=3)
        t.add_column("Citation", width=18)
        t.add_column("Verdict", width=18)
        t.add_column("Case name", width=46, overflow="ellipsis")
        for i, case in enumerate(analysis.cases, 1):
            verdict_label = VERDICT_LABEL.get(case.verdict, case.verdict)
            colour = {
                "verified_real": "green",
                "hallucinated": "red",
                "unparseable": "yellow",
                "skipped": "yellow",
                "api_error": "yellow",
            }.get(case.verdict, "white")
            t.add_row(
                str(i),
                case.queried_citation,
                f"[{colour}]{verdict_label}[/]",
                case.case_name or "—",
            )
        console.print(t)
    else:
        for i, case in enumerate(analysis.cases, 1):
            verdict_label = VERDICT_LABEL.get(case.verdict, case.verdict)
            print(f"  [{i}] {case.queried_citation:18s} {verdict_label:18s} "
                  f"{case.case_name or '—'}")

    summary = (
        f"{analysis.verified}/{analysis.total_citations} verified · "
        f"{analysis.hallucinated} hallucinated · "
        f"{analysis.unparseable} unparseable"
    )
    if analysis.api_errors:
        summary += f" · {analysis.api_errors} api_errors"
    if analysis.skipped:
        summary += f" · {analysis.skipped} skipped"
    overall = (
        "pass"
        if (analysis.hallucinated == 0
            and analysis.unparseable == 0
            and analysis.api_errors == 0)
        else "warn"
    )
    say(summary, overall)

    verified_cases = [c for c in analysis.cases if c.verdict == "verified_real"]
    if not verified_cases:
        return

    for i, case in enumerate(verified_cases, 1):
        banner(f"Case {i} · {case.case_name or case.queried_citation}")
        if RICH:
            info = Table(box=box.SIMPLE_HEAVY, show_header=False)
            info.add_column("k", style="dim", width=20)
            info.add_column("v")
            info.add_row("Citations", ", ".join(case.citations or [case.queried_citation]))
            if case.court:
                info.add_row("Court", case.court)
            if case.date_filed:
                info.add_row("Decided", case.date_filed)
            if case.judges:
                info.add_row("Judges", case.judges)
            if case.docket_number:
                info.add_row("Docket", case.docket_number)
            if case.absolute_url:
                info.add_row(
                    "CourtListener",
                    f"https://www.courtlistener.com{case.absolute_url}",
                )
            console.print(info)
        else:
            print(f"  Citations: {', '.join(case.citations or [case.queried_citation])}")
            print(f"  Court: {case.court}    Decided: {case.date_filed}")

        if case.auth_mode != "authenticated":
            for note in case.notes:
                say(note, "warn")

        if case.opinions:
            for j, op in enumerate(case.opinions, 1):
                say(f"Opinion {j}: {_format_op_heading(op)}", "info")
                body = op.text or _strip_html(op.html)
                if not body:
                    continue
                excerpt = body[:EXCERPT_LIMIT]
                if RICH:
                    console.print(excerpt)
                    if len(body) > EXCERPT_LIMIT:
                        console.print(
                            f"[dim]… (+{len(body) - EXCERPT_LIMIT:,} chars truncated; "
                            "full text in --html export)[/dim]"
                        )
                else:
                    print(excerpt)
                    if len(body) > EXCERPT_LIMIT:
                        print(f"... (+{len(body) - EXCERPT_LIMIT} chars truncated)")
        elif case.syllabus:
            say("Syllabus excerpt:", "info")
            text = _strip_html(case.syllabus)
            excerpt = text[:EXCERPT_LIMIT]
            if RICH:
                console.print(excerpt)
            else:
                print(excerpt)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify CourtListener API + Eyecite parser, then analyse a memo "
        "(parse citations + verify each one against CourtListener).",
    )
    parser.add_argument("--html", type=Path, help="Path to write the unified HTML report")
    parser.add_argument("--json", type=Path, help="Path to write raw JSON results")
    parser.add_argument("--memo", type=Path, help="Path to a memo (.txt). Defaults to sample_memo.txt")
    parser.add_argument("--no-net", action="store_true", help="Skip all network calls")
    parser.add_argument("--quick", action="store_true", help="Skip slow practice-area stage")
    parser.add_argument(
        "--verbose", action="store_true",
        help="Per-citation diagnostics: endpoint, HTTP status, retries, latency, verdict",
    )
    args = parser.parse_args()

    import eyecite as _eyecite

    report = Report(
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        auth_mode="authenticated" if CL_TOKEN else "anonymous",
        eyecite_version=getattr(_eyecite, "__version__", "unknown"),
    )

    if RICH:
        console.print(
            Panel(
                "[bold]juris-citation-verify[/bold]\n"
                "System health + Eyecite + per-citation CourtListener verification\n"
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

    # System health stages
    if not args.no_net:
        stage_reachability(report)
        stage_anon_audit(report)
        if not args.quick:
            stage_practice_areas(report)

    # Memo analysis (Eyecite parse + per-citation verdict + extraction)
    memo_path = args.memo if args.memo else Path("sample_memo.txt")
    stage_memo_analysis(
        report, memo_path,
        do_network=not args.no_net,
        verbose=args.verbose,
    )

    report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Console: detailed memo analysis output (verdict table + per-case detail)
    print_memo_analysis_to_console(report.memo_analysis)

    # Final tally
    banner("Final tally")
    passes = sum(1 for r in report.results if r.status == "pass")
    fails = sum(1 for r in report.results if r.status == "fail")
    warns = sum(1 for r in report.results if r.status == "warn")
    analysis = report.memo_analysis
    if analysis and analysis.hallucinated > 0:
        say(
            f"🚨 {analysis.hallucinated} hallucinated citation"
            f"{'s' if analysis.hallucinated != 1 else ''} found in memo",
            "fail",
        )
    if analysis and analysis.api_errors > 0:
        say(
            f"⚠️  {analysis.api_errors} citation"
            f"{'s' if analysis.api_errors != 1 else ''} hit an API error "
            "(distinct from hallucinations; re-run to confirm)",
            "warn",
        )
    say(f"{passes} passed · {warns} warnings · {fails} failed",
        "pass" if fails == 0 else "fail")

    if args.json:
        args.json.write_text(
            json.dumps(report.to_dict(), indent=2, default=str),
            encoding="utf-8",
        )
        say(f"Wrote JSON: {args.json}", "pass")

    if args.html:
        render_html(report, args.html)
        say(f"Wrote HTML: {args.html}  (open in browser)", "pass")

    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
