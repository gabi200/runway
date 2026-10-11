# Testing

Runway has 201 automated tests (counted with `pytest --collect-only`). All of them run offline: no network, no API key, no running server. Every test uses its own temporary SQLite database, so tests never touch `var/runway.db`.

## How to run

```bash
.venv/bin/pytest                      # everything (about 10 seconds)
.venv/bin/pytest tests/security       # the attack scenarios A1 to A6 only
.venv/bin/pytest tests/test_policy.py # one file
.venv/bin/pytest -k a3                # by name
```

Without the virtual environment, install first: `uv sync` (includes the `dev` group with pytest) or `pip install -e . pytest`. `pyproject.toml` sets `testpaths = ["tests"]` and `addopts = "-q"`.

Isolation: fixtures set `RUNWAY_DB` to a path in `tmp_path`, call `common.db.reset_engines()`, and (where files are written) set `RUNWAY_VAR_DIR` so e-mails and PDFs go to the temp directory. Agent tests set `RUNWAY_LLM=offline` so the deterministic planner is used even if `DEEPSEEK_API_KEY` is set in the shell.

## Latest result

```
201 passed in about 11 s   (Python 3.12, Linux, run on 2026-10-11)
```

Re-run `pytest` before submitting; counts below are from this run.

## Inventory

### Unit and integration tests (`tests/`)

| File | Tests | What it proves |
|---|---|---|
| `test_generate.py` | 6 | The generator is deterministic for a seed; settings and opening cash (40,000.00 on 2026-10-19) are correct; canonical ids exist; the demo invoices (Brightfield, Cartonia, Inkworks with scheduled `PAY-0001`, exactly two overdue small-customer invoices) exist; paid invoices have matching transactions and VAT; CSV and attack documents are written. |
| `test_forecast.py` | 8 | 13 weekly buckets that reconcile (`closing = opening + receipts + payments`); the week-6 trough is within 2,500 of -18,000 and weeks 1 to 4 are non-negative; the 95,000 Brightfield invoice lands in week 11 while Cartonia, payroll and VAT fall in weeks 5 to 6; every line has `source_refs`; scenarios (`move_payment`, `delay_customer`, `inflow`) change the result in the right direction; `explain_figure` returns lines and source rows. |
| `test_financing.py` | 6 | The catalogue has five products; true cost is in integer cents and matches hand calculations; slow or ineligible options are excluded (advance cap, overdraft limit); an early `needed_by` leaves only fast options; nothing deliverable gives no recommendation. |
| `test_policy.py` | 43 | Boundaries of the 1,000 / 3,000 / 5,000 thresholds; invalid amounts (None, bool, zero, negative, float, string) are denied; T4 and unknown tools denied (A4); IBAN-like keys denied at any depth; split payments (A3); daily totals ignore cancelled and non-automatic rows; payee, invoice, currency and flagged-document rules; approvals are re-checked and cannot override a DENY; reschedule rules (critical invoice, due-date limit, past dates); financing approval roles; reminder limits (daily, per invoice, overdue only); tiers and kill switch; external changes to the database are seen (no stale cache); malformed policy file fails closed. |
| `test_audit.py` | 22 | The hash contract and canonical JSON; UPDATE and DELETE triggers; 15 raw-SQL tampering variants are detected even after dropping the triggers (A6); reordering is detected; changing the anchors is detected; failed commits roll back cleanly; concurrent appends from 4 threads keep the chain valid. |
| `test_bank.py` | 28 | Bearer-token auth on every endpoint; payee creation needs the human token (403 for the executor token); environment-configured tokens; idempotency survives rescheduling and execution; unverified payee, wrong currency, past date, bad invoice rejected; rescheduling rules; `advance` books due payments exactly once and marks AP invoices paid; concurrent duplicate requests create one payment; the remote HTTP client path (bearer header) against a mock transport; history filters and error codes; `PATCH /payees/{id}` (human token only, accepts only a strict boolean `verified`, 404 for unknown payee, IBAN cannot be changed). |
| `test_executor.py` | 40 | `run_tool` flow: read tools are audited; unknown tool and IBAN arguments denied; small payment automatic and idempotent; unverified payee denied; payments above 1,000 need the owner, above 5,000 both roles; reject; flagging a document alerts the owner and blocks linked payments; reminders write an outbox row and `.eml`; `draft_email` does not send; forecast and financing tools; reschedule needs approval then moves the payment; the application PDF is created; failures are audited; six tests for the idempotent replay path (replay before caps, reused key with a different body, replay after execution, after human approval, after reschedule, malformed key); `clear_document_flag` (owner and accountant only, audited as `HUMAN_ACTION`, non-human roles and unknown documents rejected). |
| `test_documents.py` | 10 | The scanner passes a clean invoice and catches bank-detail change plus IBAN, "skip approval", prompt injection and impersonation; hidden PDF text is extracted; a clean PDF is not flagged; a malicious PDF is flagged through `flag_document`; text files can be ingested by path; `wrap_untrusted` delimits the content and neutralises the terminator. |
| `test_pseudonym.py` | 5 | Mask and unmask round trip; placeholders are stable and unknown IBANs are masked; ids are masked and restored in tool arguments; unknown placeholders are left alone; substrings of other words are not masked. |
| `test_orchestrator.py` | 17 | The LLM sees only masked data and its tool arguments are unmasked; the step limit stops the loop; a crashing tool does not crash the agent; bypass patterns are detected and normal messages are not; a refusal is logged without calling the LLM; the adapter parses an OpenAI-format response; `get_adapter()` returns the offline planner without an API key; end to end with the offline planner: Monday brief (forecast, two reminders, reschedule and financing application pending approval), jailbreak refused and logged, malicious document flagged and discussed. |
| `test_ui_smoke.py` | 6 | Streamlit `AppTest` clicks through the real `ui/app.py` on a temp database: all nine pages render; Monday brief, approval queue, inbox, Verify chain, kill switch toggle; currency switch to CNY; ingesting a demo document; approve, clear a flag (audited as `clear_document_flag`), verify a payee (audited as `verify_payee`), advance the clock, add a scenario change. |

### Security scenarios (`tests/security/`)

Shared fixture `stack` in `tests/security/conftest.py` generates a fresh database and sample files in a temp directory and uses the offline planner and the in-process bank.

| File | Tests | Scenario |
|---|---|---|
| `test_a1_hidden_iban_invoice.py` | 1 | A1: hidden-text invoice is flagged, owner alerted, linked and unverified-payee payments denied, bank refuses the unverified payee (422), no IBAN in any tool schema. |
| `test_a2_owner_impersonation_email.py` | 1 | A2: the "pay 9,800 now" e-mail is flagged; a 9,800 payment needs owner and accountant; the `agent` role cannot approve; pasting the e-mail into chat creates no payment. |
| `test_a3_split_payments.py` | 2 | A3: four 950 payments to one payee give ok, then three approvals; the global 3,000 cap triggers on the fourth payee. |
| `test_a4_jailbreak.py` | 1 | A4: jailbreak refused and logged; no forbidden tool exists and `evaluate` denies their names; kill switch denies all non-T0 tools and still allows T0. |
| `test_a5_duplicate_payment.py` | 1 | A5: a repeated request returns the original (`idempotent_replay`); one `Payment` row; at the bank same key with different body gives 409. |
| `test_a6_audit_tamper.py` | 4 | A6: untouched chain verifies; an edit, a middle deletion and a tail deletion (with triggers dropped) are detected with the right `first_bad_id`; tail deletion reports truncation. |

The mapping from attack to code path is in `docs/security-self-assessment.md` section 5.

## What the tests do not cover

- A real LLM. Everything that involves the agent uses `OfflinePlanner` or a scripted fake adapter. The OpenAI-format response parsing is tested against a stub client.
- A real browser. `AppTest` simulates Streamlit widgets but does not render HTML.
- A real network between UI, agent and bank. Bank tests call the FastAPI app in-process (ASGI test client); the remote HTTP client is tested only against a mock transport. A live `uvicorn` server is not started by any test.
- Load or performance.
- The `RUNWAY_COUNTRY=cn` path for payments (see KNOWN RISKS item 18 in the security report; the test fixtures use `eu`).

## Execution evidence

`python scripts/run_demo.py` runs the whole story (steps 1 to 7 of the demo, attacks A1 to A6) on a temporary database and writes evidence files to `docs/execution_logs/`. See `docs/deployment.md`.
