# Handoff: Runway bug-fix round (written 2026-10-15)

## Context
- Repo: `D:\Projects\Shenzhen\runway`, GitHub `gabi200/runway`, branch `master` (clean, pushed).
- What it is: AI finance copilot for a small supplier, entry for the Shenzhen FinTech Competition (Topic E). **Deadline 20 October 2026, 16:00 UTC.** Read `runway/README.md` first, then `docs/security-self-assessment.md` section 7 (known risks).
- Stack: Python 3.12, SQLite/SQLModel, FastAPI mock bank (in-process by default), Streamlit UI, DeepSeek via OpenAI SDK.
- Run tests: `cd runway && .venv/Scripts/python.exe -m pytest -rfE` → baseline **338 passed, 1 skipped** (~100 s on Windows; the skip is a symlink test).
- Demo replay: `.venv/Scripts/python.exe scripts/run_demo.py --out <scratch dir>` (default `--out` overwrites `docs/execution_logs/`, which are submission evidence: regenerate them only on purpose, then update the "45 audit entries" figure in README, docs/testing.md and docs/deployment.md if it changes).
- App: `.claude/launch.json` in `D:\Projects\Shenzhen` has `runway-ui` (Streamlit, port 8501). Demo PINs owner `1111`, accountant `2222`.
- LLM: `runway/.env` (git-ignored) holds a working `DEEPSEEK_API_KEY`; model `deepseek-flash` verified live. Tests blank the key (`tests/conftest.py`), so they never call the API. Never print or commit the key.

## Working rules (from the user)
- One fix per commit, push after each fix and after each doc update. Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Work on a branch (`git checkout -b bugfix-round-2`), merge to `master` when the user says so.
- Stage files by explicit path, never `git add docs` / `git add .` (a past commit swept in unrelated files).
- Keep docs in step with code: `docs/security-self-assessment.md` (known risks, attack table, "Fixed since" list), `docs/testing.md` (per-file test counts, latest-result line), README test count.
- Every fix gets a regression test that fails on the old code.

## Already done (do not redo)
Review fixes, all on `master`: bank refuses payments whose document is flagged after scheduling; payments without an open AP invoice always need owner approval (`automatic_requires_invoice` in `policy/rules.yaml`); PIN lockout per browser session + per role; approvals use compare-and-set (no double execution); kill switch at approval keeps requests pending; owner can set a missing IBAN once; documents are linked at ingestion to invoices (number) and suppliers (name/e-mail domain), enforced by `common.db.blocking_documents` in policy, bank create and bank execute (attack test A7); scanner no longer flags plain "pay to IBAN"; `.env` loading (`common/__init__.py`); prompts make the live Monday brief follow the demo story; replies get a "Check by Runway" note for unbacked money amounts or claimed actions (`agent/orchestrator.py`).

## Task 1 (highest value): review never-reviewed code for real bugs
These modules have tests but no human/agent review yet. Look for wrong results, crashes on edge input, and trust-boundary gaps. Reproduce each bug before fixing.
1. `forecast/engine.py` (392 lines): drives every figure in the demo (week-6 low -18,303.32, week-10 -9,494.98, financing need 10,000 for 38 days with the Inkworks move, 19,000 without). Check week bucketing at boundaries, VAT/payroll date logic, customer lateness, `financing_need` (known limit in risk 17: only looks at the week after the last shortfall), scenario handling. Do not change demo figures unless they are wrong; if a fix changes them, update README, docs and execution logs.
2. `data/importer.py` (350 lines): parses uploaded CSVs (trust boundary). Check malformed numbers/dates, encodings/BOM, huge files, duplicate ids, formula-like cells, atomicity (it must leave the DB untouched on any error).
3. `llm/pseudonym.py` (211 lines): masking before data goes to DeepSeek. Look for names/IBANs/e-mails that escape masking (case, spacing, partial names, IBAN with odd spacing) and unmask collisions. A leak-scan script exists at `C:\Users\Delia\AppData\Local\Temp\claude\D--Projects-Shenzhen\ef33495c-c1c0-48cd-80cf-3c760a1d0f90\scratchpad\live_check.py` (may be gone; it runs the agent live on a temp DB and scans outgoing requests).
4. Lower priority: `agent/financing.py`, `agent/pdf.py`, `llm/offline.py`, `ui/app.py`.

## Task 2: known weaknesses the user may want fixed (ask before starting)
- Two-person rule for verifying a payee (`ui/app.py` Payees page, bank `PATCH /payees/{id}`) and clearing a document flag (`agent/executor.py:clear_document_flag`): today one person can do either, and both unblock payments.
- Approval expiry (known risk 20) and a weekly cap on automatic payments (risk 11).
- Live model answers from memory instead of calling a tool (risk 9 limits): history keeps only a record of tool calls, not their data.
- Known risk 21: one flagged document naming many suppliers blocks all of them.

## Gotchas
- Windows + Git Bash: Python heredocs passed through bash mangle `\b`/`\n` escapes; use the Edit tool for regex/escape-heavy code.
- Print Unicode output with `PYTHONIOENCODING=utf-8` (DeepSeek replies use `−`).
- `pytest -q` (plus `addopts = -q`) hides the summary line; use `pytest -rfE | tail -1`.
- Tests defined in `conftest.py` are not collected.
- Existing `var/runway.db` predates document linking: "Reset demo data" in the UI regenerates it.
- Codex agents do not work here (the ChatGPT-account login rejects all models tried); use Claude subagents.

## Done when
Each confirmed bug has a reproducing test, a fix, a commit and a push; the full suite passes; docs reflect any behaviour change; a short report lists bugs found/fixed, things checked and clean, and anything left open.
