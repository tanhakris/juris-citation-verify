# VSCode Setup — `juris-citation-verify`

Step-by-step setup for running this project (or any modern Python project)
on your local machine using Visual Studio Code.

---

## 1. Install Python 3.10+

**macOS** (recommended via Homebrew):

```bash
brew install python@3.12
python3 --version       # should print 3.12.x
```

**Windows** (recommended via official installer):

1. Download from <https://www.python.org/downloads/>
2. **Important:** check ✅ *"Add python.exe to PATH"* on the first installer screen
3. After install, open PowerShell and run `python --version` to confirm

**Linux:**

```bash
sudo apt update && sudo apt install -y python3.12 python3.12-venv python3-pip
```

---

## 2. Install VSCode

Download from <https://code.visualstudio.com/> and install.

---

## 3. Install the essential VSCode extensions

Open VSCode → click the Extensions icon in the left sidebar (or `⌘+Shift+X` /
`Ctrl+Shift+X`) and install these:

| Extension | Publisher | Why |
|---|---|---|
| **Python**          | Microsoft | Core Python support — interpreter selection, debugging |
| **Pylance**         | Microsoft | Fast type-checker, autocomplete, go-to-definition |
| **Black Formatter** | Microsoft | Auto-format on save (PEP-8 compliant) |
| **Ruff**            | Astral Software | Lightning-fast linter (catches bugs as you type) |
| **GitLens**         | GitKraken | Inline git blame, history navigation |
| **Even Better TOML** | tamasfe | Pretty editing for `pyproject.toml` files |

> **Optional but recommended**
>
> | Extension | Why |
> |---|---|
> | **Claude Dev** or **Continue** | Inline AI assistance inside VSCode (alternative to the Claude Code CLI) |
> | **Error Lens** | Shows lint/type errors inline next to the offending line |
> | **Indent Rainbow** | Visual indent levels — useful in heavily-nested Python |

---

## 4. Open the project

```bash
cd path/to/juris-citation-verify
code .                                    # opens VSCode in this folder
```

---

## 5. Create a virtual environment

VSCode integrated terminal: **Terminal → New Terminal** (`` Ctrl+` ``).

```bash
python3 -m venv .venv

# macOS / Linux
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1

# Windows cmd.exe
.venv\Scripts\activate.bat
```

You should now see `(.venv)` in your prompt.

---

## 6. Install dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

---

## 7. Tell VSCode to use the venv

`⌘+Shift+P` (mac) or `Ctrl+Shift+P` (Windows/Linux) → type
**Python: Select Interpreter** → choose the one inside `.venv/`.

VSCode will remember this per-project.

---

## 8. Configure run / debug

Create `.vscode/launch.json` with these contents (VSCode will auto-create the folder if needed):

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Run verify.py (HTML report)",
      "type": "debugpy",
      "request": "launch",
      "program": "${workspaceFolder}/verify.py",
      "args": ["--html", "report.html", "--memo", "sample_memo.txt"],
      "console": "integratedTerminal",
      "justMyCode": false
    },
    {
      "name": "Run verify.py (no-net, fastest)",
      "type": "debugpy",
      "request": "launch",
      "program": "${workspaceFolder}/verify.py",
      "args": ["--no-net"],
      "console": "integratedTerminal"
    },
    {
      "name": "Run verify.py (authenticated)",
      "type": "debugpy",
      "request": "launch",
      "program": "${workspaceFolder}/verify.py",
      "args": ["--html", "report.html"],
      "console": "integratedTerminal",
      "envFile": "${workspaceFolder}/.env"
    }
  ]
}
```

Now press **F5** to run with debugging, or pick the configuration from the
Run-and-Debug sidebar (`⌘+Shift+D` / `Ctrl+Shift+D`).

---

## 9. Set up format-on-save (recommended)

Create `.vscode/settings.json`:

```json
{
  "[python]": {
    "editor.defaultFormatter": "ms-python.black-formatter",
    "editor.formatOnSave": true,
    "editor.codeActionsOnSave": {
      "source.organizeImports": "explicit"
    }
  },
  "python.analysis.typeCheckingMode": "basic",
  "ruff.enable": true
}
```

---

## 10. (Optional) Install Claude Code CLI

The Claude Code CLI gives you the full agentic experience inside your terminal —
the same one used to author this project.

```bash
# macOS / Linux / WSL
curl -fsSL https://claude.ai/install.sh | bash

# Then sign in
claude
```

Once installed, you can `cd` into any project folder, run `claude`, and paste
the prompt from `prompts/CLAUDE_CODE_PROMPT.md` to regenerate / extend the
project autonomously.

> Reference: <https://docs.claude.com> for the latest install instructions.

---

## 11. Quick keyboard cheat-sheet

| Action | macOS | Windows/Linux |
|---|---|---|
| Open command palette | `⌘+Shift+P` | `Ctrl+Shift+P` |
| Open terminal | `` Ctrl+` `` | `` Ctrl+` `` |
| Run/debug current file | `F5` | `F5` |
| Format current file | `⌘+Shift+I` | `Shift+Alt+F` |
| Go to definition | `F12` | `F12` |
| Find in all files | `⌘+Shift+F` | `Ctrl+Shift+F` |
| Toggle sidebar | `⌘+B` | `Ctrl+B` |
| Multi-cursor (column) | `Option+Click` | `Alt+Click` |

---

## 12. Troubleshooting

**"python: command not found"** — Python isn't on your PATH. On Windows, re-run
the installer and tick *"Add python.exe to PATH"*. On macOS, use `python3`
instead of `python`.

**"No module named eyecite"** — You activated the wrong venv (or none).
Check the `(.venv)` prefix in your prompt; if absent, re-run `source .venv/bin/activate`.

**Linter complains about missing imports** — VSCode is using the system Python,
not your venv. Re-run *Python: Select Interpreter* (step 7).

**`Could not find a version that satisfies the requirement eyecite>=2.6`** —
Your Python is too old. Either pin `eyecite>=2.5,<3` in `requirements.txt`
(works on Python 3.9), or install a newer Python via `brew install python@3.12`
on macOS.

**`pip` warns it's out of date** — Run `pip install --upgrade pip` first,
then re-run `pip install -r requirements.txt`.

**HTML report shows raw `{}`** — You're on Python <3.10. Upgrade.

**`401 Unauthorized` on every API call** — That's expected for the Citation
Lookup endpoint. Anonymous works only for some GETs. Set `CL_TOKEN` to
switch to authenticated mode.

---

## What "good" looks like

Once everything is set up, this should work:

```bash
$ python verify.py --quick --html report.html
╔══════════════════════════════════════════════════════════════╗
║ juris-citation-verify                                        ║
║ Hands-on verification of CourtListener API + Eyecite         ║
║ Auth mode: anonymous   Eyecite: 2.x                          ║
╚══════════════════════════════════════════════════════════════╝

──────── Stage 1 · API reachability ────────
  ✅ API root reachable (47 endpoints exposed)
... [more stages] ...
  ✅ Detector accuracy: 5/5 correct
  ✅ Wrote HTML: report.html  (open in browser)
```

Open `report.html` in any browser. Take a screenshot. That's your demo.
