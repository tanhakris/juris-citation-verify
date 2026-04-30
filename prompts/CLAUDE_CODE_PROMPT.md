# Claude Code Prompt — `juris-citation-verify`

This single prompt recreates the entire project in a fresh directory.
Drop it into Claude Code and let it work autonomously.

> **How to use**
> 1. Open a terminal in an empty folder
> 2. Run `claude` (the Claude Code CLI)
> 3. Paste the prompt block below
> 4. Approve file writes when Claude Code asks

---

## The prompt — copy from here ⤵

````
You are building `juris-citation-verify` — a single-file Python CLI that
verifies CourtListener's REST API and the Eyecite citation parser end-to-end.
This tool is for JurisConsultants Group / JurisLPO (a legal-tech company).

### Goals

Build a small, polished, single-file CLI that:

1. Tests CourtListener API reachability (anonymous + optional authenticated
   via `CL_TOKEN` env var)
2. Audits which endpoints work without authentication
3. Verifies practice-area coverage by querying the Search API for one
   landmark Supreme Court case from each of six curriculum tracks:
   Immigration, Family law, Contracts, Real estate, IP, Litigation
4. Runs Eyecite locally on a sample memo containing a deliberate mix of:
   real citations, fabricated citations, short-form references, an `Id.`,
   and a typo'd reporter abbreviation
5. Builds a hybrid hallucination detector: Eyecite extracts citations,
   then anonymous CourtListener Search verifies whether each citation
   actually appears in any case opinion (0 hits = fake, ≥1 hit = real)

### Tech stack

- Python 3.10+
- `requests` for HTTP
- `eyecite` for citation parsing (BSD-licensed, pip install eyecite)
- `rich` for coloured console output (graceful fallback to plain print
  if not installed)

### Project structure

```
juris-citation-verify/
├── verify.py              # single-file CLI, ~400 lines
├── requirements.txt       # requests, eyecite, rich
├── README.md              # usage + architecture
├── sample_memo.txt        # mixed real/fake/typo'd citations
├── .env.example           # CL_TOKEN= placeholder
├── .gitignore
└── docs/
    └── VSCODE_SETUP.md    # editor config
```

### CLI design

```
python verify.py [OPTIONS]

  --html PATH    Standalone HTML report (single file, embedded CSS, no JS)
  --json PATH    Raw JSON results
  --memo PATH    Use a custom .txt memo for Eyecite extraction
  --no-net       Skip all network tests (Eyecite-only)
  --quick        Skip the slow practice-area coverage test
```

Set `CL_TOKEN` env var to switch from anonymous to authenticated mode.

### Console output

Use `rich` to render: a banner panel, sectioned stages with `rule()` titles,
tables for matrix results, coloured status icons (✅ ❌ ⚠️  ℹ️), and a final
pass/warn/fail tally. The output should look like a CI test runner.

### HTML report

Single-file HTML with embedded CSS (no external stylesheets, no JavaScript).
Use a navy/blue palette: `--navy: #1F3A5F`, `--blue: #2E5C8A`, with pill-style
status badges (pass=green, fail=red, warn=amber, info=blue). The report has
four tables: summary, practice-area coverage, eyecite extractions, hybrid
detector results — plus a callout box explaining the two-failure-mode model
(structural vs existence). Should look professional enough to share with
non-technical stakeholders.

### Test data

The sample memo must include all of:

- `Plyler v. Doe, 457 U.S. 202, 230 (1982)` — real, full case
- `457 U.S. at 215` — short-form back-reference
- `Id. at 221` — id citation
- `Mata v. Avianca, Inc., 22-cv-1461 (S.D.N.Y. 2023)` — docket-style,
  Eyecite cannot parse this (intentional)
- `Brown v. Board of Education, 347 U.S. 483 (1954)` — real
- `Smith v. Imaginary, 999 U.S. 9999 (2099)` — fabricated
- `576 US 644` — typo'd reporter (should normalise to U.S.)

### Hybrid detector test cases

Real (expect ≥1 search hit):
- 457 U.S. 202 (Plyler)
- 347 U.S. 483 (Brown v. Board)
- 576 U.S. 644 (Obergefell)

Fake (expect 0 hits):
- 999 U.S. 9999
- 888 U.S. 8888

Mark the result correct when the verdict matches expectation. Print accuracy
(e.g., 5/5 correct).

### CourtListener endpoints

- Base: `https://www.courtlistener.com/api/rest/v4`
- Reachability: `GET /` (returns 47 endpoints)
- Anonymous audit:
  - `GET /` (200)
  - `GET /courts/` (200)
  - `GET /clusters/2812209/` (401 — Obergefell, requires auth)
  - `OPTIONS /citation-lookup/` (401)
  - `POST /citation-lookup/` with `text=...` (401 anonymously, 200 with token)
- Search: `GET /search/?q=<query>&type=o[&court=scotus]`

### Code quality

- Use dataclasses for result records (`TestResult`, `Report`)
- Type hints throughout
- Graceful fallback when `rich` is not installed
- Single-file CLI — keep all logic in `verify.py`, no extra modules
- Exit code 0 on full pass, 1 on any failure
- Polite to the API: include a `User-Agent` header, sleep ~0.4–0.5s between calls

### README

Explain what each test stage checks, how to run, the two-failure-mode model
(structural vs existence), how authenticated mode works, and how to extend.
Reference the VSCode setup doc.

### Done criteria

When Claude Code finishes:

1. `python verify.py --no-net` should run successfully (Eyecite-only)
2. `python verify.py --quick` should run successfully (skips slow stage)
3. `python verify.py --html report.html --memo sample_memo.txt` produces
   a valid standalone HTML file
4. The hybrid detector achieves 5/5 on the test cases
5. README documents everything; .gitignore excludes generated outputs
6. No external CSS/JS in the HTML report

After building, run a smoke test (`python verify.py --quick`) and confirm
zero failures before declaring done.
````

## ⤴ End of prompt

---

## Variants you might want

If you want a **smaller scope** (just the Eyecite test, no CourtListener calls):
remove items 1–3 and 5 from "Goals" and drop the network-related sections.

If you want to **add authenticated mode** as the default path: change the
`CL_TOKEN` description from "optional" to "required", remove the anonymous
fallback, and add error messaging when the token is absent.

If you want a **web UI** instead of HTML report: add a section asking for
a small Flask or FastAPI app with a single route that runs the verification
on demand and renders results as HTML. (I'd recommend keeping the CLI as the
primary interface — the HTML report is sufficient for sharing.)

---

## Re-running the prompt to update

Each time CourtListener changes their API, or you want to add a new test,
just edit the prompt and re-run. Claude Code will regenerate from your spec.
