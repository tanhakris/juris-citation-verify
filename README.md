# juris-citation-verify

A small standalone CLI that verifies **CourtListener's API capabilities** and
the **Eyecite citation parser** end-to-end on your local machine. It reproduces
every test from the [JurisLPO Live Verification Report](../CourtListener_Live_Verification_Report_V1_0.docx) and produces a console summary plus an optional HTML report.

> **Why this exists.** Citation hallucinations have two distinct failure modes —
> *structural* (an unknown reporter, a malformed cite) and *existence*
> (a syntactically valid cite that points to no real case). This tool exercises
> both and produces verifiable evidence we can show clients, investors, and
> partners.

---

## What it tests

| Stage | What it checks |
|------|----------------|
| 1 · Reachability        | Is the CourtListener REST API live and serving JSON? |
| 2 · Anonymous audit      | Which endpoints work without an API token; which require auth |
| 3 · Practice-area cover. | One landmark SCOTUS case from each of our six curriculum tracks |
| 4 · Eyecite local       | Pure-local citation extraction from a sample memo (no network) |
| 5 · Hybrid detector      | Eyecite + CourtListener Search → pass/fail on real and fake citations |

The tool runs anonymously by default. Set the `CL_TOKEN` environment variable
to switch to authenticated mode (faster, more reliable, higher rate limits).

---

## Quick start

> **Python version note.** This project works on Python 3.9 and above. If you
> have Python 3.10+ you can bump `eyecite` to `>=2.6` in `requirements.txt`
> for slightly better short-form / supra-citation resolution. macOS ships with
> a system Python that may be old — `python3 --version` to check; install
> a newer one via `brew install python@3.12` if needed.

```bash
# 1. Clone or unzip into your projects folder
cd juris-citation-verify

# 2. Create a virtualenv (one-time)
python3 -m venv .venv
source .venv/bin/activate            # macOS/Linux
# .venv\Scripts\activate             # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run
python verify.py                     # console-only
python verify.py --html report.html  # also generate HTML report
open report.html                     # macOS — opens in browser
```

---

## Two modes

`verify.py` has two distinct modes.

| | **Verification** (default) | **Case extraction** (`--extract`) |
|---|---|---|
| What it does | Runs all five verification stages (reachability, anonymous audit, practice-area coverage, Eyecite local, hybrid hallucination detector) | Looks up *one* case by citation and dumps its full detail |
| Trigger | No `--extract` flag | `--extract "<citation>"` |
| Network | Optional (`--no-net` skips it) | Required (`--no-net` rejected) |
| Anon CourtListener | All five stages run; the audit reports which endpoints are gated | Returns case name, citations, court, dates, judge, docket, status, URL, syllabus |
| Authenticated CourtListener | Same five stages, faster + more endpoints accessible | Adds full majority opinion + concurrences + dissents (typically tens of thousands of words) |
| Outputs | `--html` audit report, `--json` raw results | `--html` case page, `--text` plain-text dump, `--json` machine-readable case |
| Exit codes | `0` success, `1` failure | `0` success, `2` parse error, `3` case not found |

### Examples — case extraction

```bash
# Anonymous (metadata + syllabus only)
python verify.py --extract "576 U.S. 644"                    # Obergefell v. Hodges
python verify.py --extract "347 U.S. 483"                    # Brown v. Board of Education
python verify.py --extract "457 U.S. 202"                    # Plyler v. Doe

# Three exports in one call (HTML, plain text, JSON)
python verify.py --extract "576 U.S. 644" \
    --html /tmp/obergefell.html \
    --text /tmp/obergefell.txt \
    --json /tmp/obergefell.json

# Authenticated — same command, full opinion text
export CL_TOKEN=your-token-here
python verify.py --extract "576 U.S. 644" --html /tmp/obergefell_full.html
```

Get a free CourtListener token at <https://www.courtlistener.com/help/api/rest/>.
The HTML output is a single self-contained file (embedded CSS, no JS) and
opens in any browser — share it with non-technical stakeholders.

---

## CLI flags

```
python verify.py [OPTIONS]

  --extract CITATION   Case-extraction mode. Look up one case by citation
                       and dump full case detail (skips verification stages).
  --html PATH          Write standalone HTML (audit report or case page)
  --json PATH          Write raw JSON (audit results or case data)
  --text PATH          (--extract only) Write plain-text case dump (90 cols)
  --memo PATH          Use a custom .txt memo for Eyecite extraction
  --no-net             Skip all network tests (Eyecite-only run)
  --quick              Skip the slow practice-area coverage test
  -h, --help           Show help and exit
```

### Authenticated mode

```bash
export CL_TOKEN=your-token-here     # macOS/Linux
$env:CL_TOKEN="your-token-here"     # Windows PowerShell
python verify.py
```

Get a free CourtListener token at <https://www.courtlistener.com/help/api/rest/>.

---

## Output

**Console.** Colourised tables (via `rich`) showing each stage's results, latencies, and final pass/fail tally.

**HTML report (`--html`).** Single-file standalone HTML with embedded styles —
no external CSS, no JS, opens in any browser. Designed for sharing with non-technical stakeholders.

**JSON (`--json`).** Raw machine-readable results for further automation.

---

## Architecture

```
verify.py            single-file CLI (≈400 lines)
sample_memo.txt      mixed-citation test memo (real + fake + typo'd)
requirements.txt     three deps: requests, eyecite, rich
docs/                VSCode setup + Claude Code prompt
```

The script is intentionally one file — easy to read, easy to extend, easy to email.

---

## Extending

Common extensions:

- Replace `sample_memo.txt` with a real client memo to spot-check before delivery
- Add to `KNOWN_REAL` / `KNOWN_FAKE` in `verify.py` to grow the regression set
- Pipe `--json` output into your CI to track latency / pass-rate over time
- Drop authenticated calls into the workflow agent's verification step

To regenerate this entire project from scratch in Claude Code, see
[`prompts/CLAUDE_CODE_PROMPT.md`](prompts/CLAUDE_CODE_PROMPT.md).

---

## License

MIT. Built for JurisConsultants Group / JurisLPO.
