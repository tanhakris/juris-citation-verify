#!/usr/bin/env python3
"""
juris-citation-verify
=====================
Standalone CLI that verifies CourtListener API capabilities and the Eyecite
citation parser, end-to-end, on your local machine.

Designed for JurisLPO / Tech House AI hallucination-detection due diligence.
Reproduces every test from the live verification report.

Usage
-----
    python verify.py                        # console output only
    python verify.py --html report.html     # also generate HTML report
    python verify.py --no-net               # skip all network tests (eyecite only)
    python verify.py --quick                # skip slow practice-area test
    CL_TOKEN=xxxxx python verify.py         # use authenticated CourtListener mode

Author : Gopal | JurisConsultants Group
License: MIT
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
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
    parser.add_argument("--html", type=Path, help="Path to write HTML report")
    parser.add_argument("--json", type=Path, help="Path to write raw JSON results")
    parser.add_argument("--memo", type=Path, help="Path to a custom test memo (.txt)")
    parser.add_argument("--no-net", action="store_true", help="Skip all network calls")
    parser.add_argument("--quick", action="store_true", help="Skip slow practice-area test")
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
