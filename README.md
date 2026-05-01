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

## What it does

One command runs the entire pipeline end-to-end:

| Stage | What it does |
|------|----------------|
| 1 · Reachability         | Is the CourtListener REST API live and serving JSON? |
| 2 · Anonymous audit      | Which endpoints work without an API token; which require auth |
| 3 · Practice-area cover. | One landmark SCOTUS case from each of our six curriculum tracks (skip with `--quick`) |
| 4 · Memo analysis        | Eyecite parses the memo. For each full citation, search CourtListener and tag a verdict: ✅ verified, 🚨 hallucinated, or ⚠️ unparseable. With `CL_TOKEN`, additionally pull the full majority + concurrences + dissents. |

`--html report.html` produces a single self-contained file containing
system health + the per-citation verdict table + a per-case detail card
for every verified citation. Open it in a browser; it has embedded CSS,
no JS, no external resources.

The tool runs anonymously by default. Set the `CL_TOKEN` environment variable
to switch to authenticated mode (full opinion text + faster lookups + higher
rate limits).

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

# 4. Run — one command, one report
python verify.py --html report.html --memo sample_memo.txt
open report.html                     # macOS — opens in browser
```

---

## What you get

The HTML report has four sections, in order:

1. **Hero header** — three big stat boxes: total citations parsed, ✅ verified,
   🚨 hallucinated. Hallucinated count goes red when > 0.
2. **System health** — reachability, anonymous-vs-authenticated endpoint matrix,
   practice-area coverage tables.
3. **Memo analysis** — every full citation Eyecite found, with a verdict pill
   and a link to its detail card.
4. **Verified cases** — for each verified citation, a card with case name,
   parallel citations, court, decided date, judges, docket, status,
   syllabus, and (with `CL_TOKEN`) full majority + concurrences + dissents.

### Examples

```bash
# Default flow on the bundled memo
python verify.py --html /tmp/r.html

# Skip the slow practice-area stage
python verify.py --quick --html /tmp/r.html

# Eyecite only, no CourtListener (each citation tagged "skipped")
python verify.py --no-net --memo sample_memo.txt

# Custom memo
python verify.py --html /tmp/r.html --memo /path/to/my_memo.txt

# Authenticated — full majority opinion + dissents per verified case
export CL_TOKEN=your-token-here
python verify.py --html /tmp/r.html
```

Get a free CourtListener token at <https://www.courtlistener.com/help/api/rest/>.

---

## CLI flags

```
python verify.py [OPTIONS]

  --html PATH          Write the unified HTML report
  --json PATH          Write raw JSON (full report including memo analysis)
  --memo PATH          Use a custom .txt memo (default: sample_memo.txt)
  --no-net             Skip all network calls (Eyecite-only, citations tagged "skipped")
  --quick              Skip the slow practice-area coverage stage
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
