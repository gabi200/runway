# Testing

Runway has 339 automated tests (a symlink test skips itself where the OS does not allow symlinks, as on default Windows) (counted with `pytest --collect-only -q -o addopts=""`). All of them run offline: no network, no API key, no running server. Every test uses its own temporary SQLite database, so tests never touch `var/runway.db`.

## How to run

```bash
.venv/bin/pytest                      # everything (about 15 seconds on Linux, 95 on Windows)
.venv/bin/pytest tests/security       # the attack scenarios A1 to A7 only
.venv/bin/pytest tests/test_policy.py # one file
.venv/bin/pytest -k a3                # by name
```

Without the virtual environment, install first: `uv sync` (includes the `dev` group with pytest) or `pip install -e . pytest`. `pyproject.toml` sets `testpaths = ["tests"]` and `addopts = "-q"`.

Isolation: fixtures set `RUNWAY_DB` to a path in `tmp_path`, call `common.db.reset_engines()`, and (where files are written) set `RUNWAY_VAR_DIR` so e-mails and PDFs go to the temp directory. Agent tests set `RUNWAY_LLM=offline` so the deterministic planner is used even if `DEEPSEEK_API_KEY` is set in the shell.

## Latest result

```
338 passed, 1 skipped in 99.3 s   (Python 3.12.12, Windows 11, run on 2026-10-15 with `pytest`)
```

Re-run `pytest` before submitting; counts below are from this run.

## Inventory

### Unit and integration tests (`tests/`)

| File | Tests | What it proves |
|---|---|---|
| `test_generate.py` | 6 | The generator is deterministic for a seed; settings and opening cash (40,000.00 on 2026-10-19) are correct; canonical ids exist; the demo invoices (Brightfield, Cartonia, Inkworks with scheduled `PAY-0001`, exactly two overdue small-customer invoices) exist; paid invoices have matching transactions and VAT; CSV and attack documents are written. |
| `test_forecast.py` | 11 | 13 weekly buckets that reconcile (`closing = opening + receipts + payments`); the week-6 trough is within 2,500 of -18,000 and weeks 1 to 4 are non-negative; the 95,000 Brightfield invoice lands in week 11 while Cartonia, payroll and VAT fall in weeks 5 to 6; every line has `source_refs`; scenarios (`move_payment`, `delay_customer`, `inflow`) change the result in the right direction; `explain_figure` returns lines and source rows; transactions created from payments do not change the recurring estimates (anti-manipulation); week-1 opening balance explanation lists the account and the booked transactions. |
| `test_financing_need.py` | 5 | `financing_need`: the amount covers the worst week rounded up to a whole 1,000; the term ends at the first receipt after the last shortfall week; a scenario changes the need; no shortfall gives amount 0; the `get_financing_need` tool and the reminder tone escalation by earlier reminders. |
| `test_financing.py` | 7 | The catalogue has five products; true cost is in integer cents and matches hand calculations; slow or ineligible options are excluded (advance cap, overdraft limit); an early `needed_by` leaves only fast options; nothing deliverable gives no recommendation. |
| `test_policy.py` | 72 | Boundaries of the 1,000 / 3,000 / 5,000 thresholds and of `max_payment_cents`; invalid amounts (None, bool, zero, negative, float, string) are denied; T4 and unknown tools denied (A4); IBAN-like keys denied at any depth; split payments (A3); daily totals ignore cancelled and non-automatic rows; payee, invoice, currency and flagged-document rules; invoice overpayment (cumulative total, cancelled payments do not count); approvals are re-checked and cannot override a DENY; reschedule rules (critical invoice, due-date limit, past dates, cannot be pulled earlier); financing-application hard denials (unknown product, too slow, ineligible amount); financing roles; `get_financing_need` tier; reminder limits (daily, per invoice, overdue only); tiers and kill switch; external changes to the database are seen (no stale cache); malformed policy file fails closed; the rules file is cached and explicitly reloadable. |
| `test_audit.py` | 22 | The hash contract and canonical JSON; UPDATE and DELETE triggers; 15 raw-SQL tampering variants are detected even after dropping the triggers (A6); reordering is detected; changing the anchors is detected; failed commits roll back cleanly; concurrent appends from 4 threads keep the chain valid. |
| `test_bank.py` | 53 | Bearer-token auth on every endpoint; enforced scopes (payee creation, verification and clock advance need the human token, 403 for the executor token); environment-configured tokens; idempotency survives rescheduling and execution; unverified payee, wrong currency, past date, bad invoice rejected; a huge payment and the exact maximum; cumulative invoice limit; rescheduling cannot pull a payment forward or move it past the invoice due date; `advance` books due payments exactly once, marks AP invoices paid and classifies unlinked payments as `other`; concurrent duplicate requests create one payment; the remote HTTP client path against a mock transport; history filters and error codes; `advance` fails a payment whose linked document was flagged after scheduling and books it once the flag is cleared; a missing IBAN can be set once by a human (403 for the executor token, 422 for an invalid IBAN, 409 when one is already set) and leaves the payee unverified; `PATCH /payees/{id}` (human token only, strict boolean `verified`, 404 for unknown payee, IBAN cannot be changed). |
| `test_executor.py` | 56 | `run_tool` flow: read tools are audited; unknown tool and IBAN arguments denied; small payment automatic and idempotent (with `automatic_requires_invoice` switched off by the `unbacked_automatic` fixture in `tests/conftest.py`, as in the cap tests); unverified payee denied; payments above 1,000 need the owner, above 5,000 both roles; reject (the agent cannot reject); the same person cannot supply two approval roles, including for the financing application; legacy approval records are read and upgraded; flagging a document alerts the owner and blocks linked payments; reminders write an outbox row and `.eml`; `draft_email` does not send; forecast and financing tools; financing defaults are evaluated and audited; reschedule needs approval then moves the payment; the application PDF is created; failures are audited; the idempotent replay path (replay before caps, reused key with a different body, replay after execution, after human approval, after reschedule, malformed key); the kill switch blocks a payment replay, also without a bank call; the kill switch at approval time keeps the request pending; two concurrent final approvals execute the action once; `clear_document_flag` (owner and accountant only, audited as `HUMAN_ACTION`, non-human roles and unknown documents rejected). |
| `test_documents.py` | 18 | The scanner passes a clean invoice and catches bank-detail change plus IBAN, "skip approval", prompt injection and impersonation, also in sender and filename; hidden PDF text is extracted; a clean PDF is not flagged; a malicious PDF is flagged through `flag_document`; `ingest_file` reads inside the allowed root and rejects a path outside it; a path-like string passed as text is stored as text and never read; `ingest_document` handles bytes, text and PDF; metadata cannot break out of the untrusted wrapper and is length-capped; `wrap_untrusted` delimits the content and neutralises the terminator; the generated demo invoices scan as expected (the clean Cartonia invoice is not flagged, the A1 invoice is). |
| `test_pseudonym.py` | 10 | Mask and unmask round trip; placeholders are stable and unknown IBANs are masked; ids are masked and restored in tool arguments; unknown placeholders are left alone; substrings of other words are not masked; plain-word ids are not masked and structured ids are matched case-sensitively; all e-mail addresses are masked and person names are learned from headers and signatures; a counterparty e-mail local part registers a person. |
| `test_orchestrator.py` | 25 | Replies get a visible "Check by Runway" note for money amounts no tool result, message or document contains and for claimed actions (also with a curly apostrophe) that no tool call backs; history records each turn's tool calls; The LLM sees only masked data and its tool arguments are unmasked; the step limit stops the loop; a crashing tool does not crash the agent; bypass patterns are detected and normal messages are not; a refusal is logged without calling the LLM; the adapter parses an OpenAI-format response; `get_adapter()` returns the offline planner without an API key; tool arguments with an unknown placeholder are rejected; an e-mail pasted into chat leaks no real names to the LLM; end to end with the offline planner: Monday brief (forecast, two reminders, reschedule and financing application pending approval), jailbreak refused and logged, malicious document flagged and discussed. |
| `test_importer.py` | 13 | Importing all six CSV files reproduces the source database's 13 weekly closings (including the week-6 shortfall); the four basic files build a 13-week forecast and unverified payees with the right opening balance; an unbalanced trial balance is rejected and leaves the database untouched; missing columns are rejected. |
| `test_ui_smoke.py` | 19 | Streamlit `AppTest` clicks through the real `ui/app.py` on a temp database: all nine pages render; Monday brief, approval queue, inbox, Verify chain; the kill switch is refused when not signed in and works after sign-in; currency switch to CNY; ingesting a demo document; approve, clear a flag (audited as `clear_document_flag`), advance the clock, add a scenario change; payee verify and unverify are owner-only and audited; human actions need sign-in and a wrong PIN is rejected; five wrong PINs lock only that browser session, 20 lock the role everywhere, and both lockouts expire; the owner can fill in a missing IBAN once; switching role needs the other PIN; one browser session cannot fill both approval roles; model markdown (links, images) is neutralised. |
| `test_env.py` | 1 | `.env` loading: comments, `export` and quotes handled; a variable already in the environment wins. (`tests/conftest.py` also blanks `DEEPSEEK_API_KEY` for every test, so a real key never reaches the API.) |

### Security scenarios (`tests/security/`)

Shared fixture `stack` in `tests/security/conftest.py` generates a fresh database and sample files in a temp directory and uses the offline planner and the in-process bank.

| File | Tests | Scenario |
|---|---|---|
| `test_a1_hidden_iban_invoice.py` | 1 | A1: hidden-text invoice is flagged, owner alerted, linked and unverified-payee payments denied, bank refuses the unverified payee (422), no IBAN in any tool schema. |
| `test_a2_owner_impersonation_email.py` | 1 | A2: the "pay 9,800 now" e-mail is flagged; a 9,800 payment needs owner and accountant; the `agent` role cannot approve; pasting the e-mail into chat creates no payment. |
| `test_a3_split_payments.py` | 3 | A3: payments without an invoice are never automatic, whatever the amount; with that rule off, four 950 payments to one payee give ok, then three approvals, and the global 3,000 cap triggers on the fourth payee. |
| `test_a4_jailbreak.py` | 1 | A4: jailbreak refused and logged; no forbidden tool exists and `evaluate` denies their names; kill switch denies all non-T0 tools and still allows T0. |
| `test_a5_duplicate_payment.py` | 1 | A5: a repeated request returns the original (`idempotent_replay`); one `Payment` row; at the bank same key with different body gives 409. |
| `test_a6_audit_tamper.py` | 4 | A6: untouched chain verifies; an edit, a middle deletion and a tail deletion (with triggers dropped) are detected with the right `first_bad_id`; tail deletion reports truncation. |
| `test_a7_unlinked_flagged_invoice.py` | 8 | A7: with a flagged document naming the supplier, a payment by `invoice_id` alone is denied, the bank refuses it (422), and a payment scheduled before the flag fails at execution; clearing the flag unblocks it; approvals are re-checked; a clean invoice is linked and not blocked; link matching uses whole tokens; `list_documents` exposes the links. |

The mapping from attack to code path is in `docs/security-self-assessment.md` section 5.

## What the tests do not cover

- A real LLM. Everything that involves the agent uses `OfflinePlanner` or a scripted fake adapter. The OpenAI-format response parsing is tested against a stub client.
- A real browser. `AppTest` simulates Streamlit widgets but does not render HTML.
- A real network between UI, agent and bank. Bank tests call the FastAPI app in-process (ASGI test client); the remote HTTP client is tested only against a mock transport. A live `uvicorn` server is not started by any test.
- Load or performance.
- The `RUNWAY_COUNTRY=cn` path for payments and for agent text (the display conversion is covered by the UI currency-switch test; the test fixtures use `eu`).
- The Onboarding import through the UI (only `data/importer.py` is tested directly); the optional paid-invoices and scheduled-payments files exist only in the importer and its test.
- Two physical browsers: the UI test simulates the approver identity with one `AppTest` session; the two-window case relies on Streamlit giving each session its own state.

## Execution evidence

`python scripts/run_demo.py` runs the whole story (steps 1 to 7 of the demo, attacks A1 to A7) on a temporary database and writes evidence files to `docs/execution_logs/`. See `docs/deployment.md`. The checked-in run (seed 42, 45 audit entries, chain verified, tampered copy detected at entry 2) shows:

- forecast minimum -18,303.32 in week 6 (`forecast_min_closing_cents: -1830332`), shortfall weeks 6 and 10;
- Monday brief: shortfall in week 6 at -18,303.32 and week 10 at -9,494.98; 2 reminders sent; `PAY-0001` (12,000.00) moved from 2026-11-26 to 2026-12-14 (pending `APR-0001`); financing need 10,000.00 for 38 days with the move (19,000.00 without); recommended product supply-chain finance at 120.00, cash by 2026-10-23; the state-guaranteed loan, 700.00, arrives 2026-11-09 and is ranked last, not excluded; application pending `APR-0002`;
- approvals: the owner executes the reschedule; the financing application needs the owner (`owner@demo`) and then the accountant (`accountant@demo`) and produces `FIN-0001.pdf` for 10,000.00;
- A1 hidden-text invoice flagged and a linked payment and a payment to the unverified payee both denied; A7 a payment to Cartonia by `invoice_id` alone denied (the flagged document names the supplier); A2 9,800 payment (A2 and A3 use PAYEE-VOLTA, since Cartonia stays blocked) needs both roles and `agent` cannot approve; A3 split payments: no invoice behind them, so all four need approval; A4 refused; A5 the owner approves the original payment (`PAY-0002`) and the exact replay returns it; clock advanced 56 days to 2026-12-14, which books `PAY-0001` and `PAY-0002`.
