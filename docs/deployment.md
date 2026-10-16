# Deployment and operation

Runway is a local application: a Python package, one SQLite file, an optional FastAPI server for the mock bank, and a Streamlit UI. There is no Docker setup or hosted deployment in this repository.

Requirements: Python 3.11 or newer (the lock file and `.python-version` use 3.12), about 500 MB for dependencies. Network access is needed only for installing packages and, optionally, for the DeepSeek or Gemini API.

## 1. Local setup

With `uv` (recommended; uses `uv.lock`):

```bash
cd runway
uv sync                      # creates .venv with runtime and dev (pytest) dependencies
```

With pip:

```bash
cd runway
python -m venv .venv
.venv/bin/pip install -e . pytest
```

All commands below use `.venv/bin/...`. If you activate the virtual environment you can drop the prefix.

Generate the demo data (creates `var/runway.db` and files in `data/sample/`):

```bash
.venv/bin/python -m data.generate              # seed 42, the demo seed
.venv/bin/python -m data.generate --seed 7     # different data (the week-6 trough varies by seed)
.venv/bin/python -m data.generate --db /path/to/other.db --out /path/to/sample
```

It prints a summary: ids of the key demo invoices, opening balance, and the lowest week-end closing of the forecast (seed 42: week 6, -1830332 cents, that is -18,303.32 EUR; inside that week the running balance goes down to -29,303.32 on 2026-11-26). The UI also generates data automatically on first start if the database is empty.

### 1a. Importing a company's CSV files

The UI Onboarding page imports a company's own exports through `data/importer.py:import_files`. The same function can be called from Python, with a SQLite path or a session as target:

```python
from pathlib import Path
from data.importer import import_files
# a str source is CSV text, never a path: pass files as Path (or bytes)
summary = import_files("var/runway.db", Path("trial_balance.csv"), Path("bank_statement.csv"),
                       Path("open_receivables.csv"), Path("open_payables.csv"),
                       paid_invoices_csv=Path("paid.csv"))
```

Required and optional columns are listed in `docs/algorithms.md` section 9. The generated `data/sample/` files are valid inputs. Files must be UTF-8 CSV; amounts use a dot for decimals (a comma only between groups of thousands). Every file is validated before anything is written (balanced trial balance, ISO dates, one currency, known bank categories, consistent overdue dates, no duplicate columns or open items; the full list is in `docs/algorithms.md` section 9); on an error `ImportErrorReport` says what is wrong (with the file and row where there is one) and nothing changes. The import replaces the whole database, including the audit log, by copying into the existing database file, so a separately running bank server sees the new data; if another connection holds the write lock for more than 5 seconds the import stops and the old database is kept. Imported suppliers become unverified payees with an empty IBAN.

Limit: the four basic files (trial balance, bank statement, open receivables, open payables) do not contain paid-invoice history or already-scheduled payments, so customer lateness starts at zero and scheduled payments are missing. For the sample company the forecast then looks better than it is: the lowest balance is -9,494.98 on 2026-12-25 instead of -29,303.32 on 2026-11-26. The optional `paid_invoices_csv` (`number, counterparty, due_date, paid_date, amount`) restores customer lateness; with it the sample company's lowest balance is -17,303.32 on 2026-11-25, the source forecast with the Inkworks payment at its due date. The optional `scheduled_payments_csv` (`counterparty, amount, execute_date`) is rejected while the imported payees are unverified, which is always the case right after an import, so already-scheduled payments cannot be imported. The UI offers only the four basic files.

## 2. Running the services

### UI (the main entry point)

```bash
.venv/bin/streamlit run ui/app.py
```

Open the URL Streamlit prints (default http://localhost:8501). With no extra settings the UI runs the agent with the offline planner and talks to the mock bank in-process.

Sign in first (sidebar, Session & sign-in): choose a role, type its PIN, press Sign in. Demo PINs are `1111` (owner) and `2222` (accountant); set `RUNWAY_OWNER_PIN` and `RUNWAY_ACCOUNTANT_PIN` to change them. These are demo credentials, not production authentication. Without a sign-in you can read every page, but you cannot chat with the agent, approve, reject, ask for a flag to be cleared, change the kill switch, add or verify payees, advance the clock, reset or import data. An approval that needs two roles must be given from two different browser sessions (two windows, or one normal and one private window), because one session cannot fill both roles. The same holds for verifying a payee, clearing a document flag, releasing one supplier from a flagged document, turning the kill switch off and advancing the simulated clock: one role asks (Payees, Documents or Bank simulator page, or the sidebar toggle), the other approves the request in the Approval queue. Turning the kill switch on needs one person. The sign-in box warns while the public demo PINs are active. A pending request expires after 7 simulated days (`approvals.expiry_days` in `policy/rules.yaml`).

### Mock bank as a separate server (optional)

```bash
.venv/bin/uvicorn bank.app:app --port 8001
```

Check it: `curl -H "Authorization: Bearer $(cat var/secrets/bank-executor.token)" http://localhost:8001/accounts` (the token file is made on first use when `RUNWAY_BANK_EXECUTOR_TOKEN` is unset; on another machine set both token variables instead). Interactive API docs are at http://localhost:8001/docs.

The UI and the agent use this server only when `RUNWAY_BANK_URL` is set:

```bash
export RUNWAY_BANK_URL=http://localhost:8001
.venv/bin/streamlit run ui/app.py
```

Both the server and the UI read the same SQLite file, so set the same `RUNWAY_DB` for both if you change it. Without `RUNWAY_BANK_URL`, the bank runs in-process through FastAPI's test client and the uvicorn server is not needed.

Bank endpoints: `GET /accounts`, `GET /accounts/{id}/transactions?from=&to=`, `GET /payees`, `POST /payees`, `PATCH /payees/{id}` and `PUT /payees/{id}/iban` (human token only; the PATCH body is `{"verified": true|false}`, nothing else can change; the PUT sets a valid IBAN only on a payee that has none and leaves it unverified; the app sends the PATCH with `true` only after owner and accountant approved, but the bank itself accepts it from any holder of the human token), `POST /payments`, `GET /payments?status=`, `PATCH /payments/{id}` (only `execute_date`; not earlier than today or the current date, not after the invoice due date), `POST /simulate/advance?days=N` (human token only). All need `Authorization: Bearer <token>`. Scopes are enforced: the executor token carries `read` and `payments`; the human token adds `payees` and `simulate`; a missing scope gives 403 and an unknown token 401. `POST /payments` also rejects a payment above `max_payment_cents` (1,000,000.00) and a payment that would make the total paid for an invoice exceed the invoice.

### Command-line demo run (no UI)

```bash
.venv/bin/python scripts/run_demo.py                    # writes docs/execution_logs/
.venv/bin/python scripts/run_demo.py --out /tmp/evidence --seed 42
```

The options are `--out` (default `docs/execution_logs`) and `--seed` (default 42). The script has no sandbox or transport flag any more.

It runs on a temporary database (your `var/runway.db` is untouched) in offline mode and asserts every step. It writes:

| File | Content |
|---|---|
| `demo_run.md` | Step-by-step log with the JSON of each tool result, labelled by demo step and attack id |
| `audit_log.jsonl` | The full audit chain of the run, one JSON object per line (45 entries on seed 42) |
| `bank_payments.json` | All payments in the mock bank at the end |
| `outbox.json` | All e-mails in the test inbox (reminders, fraud alerts) |
| `FIN-0001.pdf` | The generated financing application |

The checked-in files in `docs/execution_logs/` were generated by this script. All bank calls go through the FastAPI app via `BankClient` (the in-process test client, with bearer-token authentication and scopes; the clock advance uses the human token from `bank.client.bank_token("human")`). Approvals in the run use the identities `owner@demo` and `accountant@demo`. Document ingestion uses `ingest_file` with the sample folder as `allowed_root`. The script temporarily sets `RUNWAY_DB`, `RUNWAY_VAR_DIR` and `RUNWAY_LLM` and clears `RUNWAY_BANK_URL`, then restores them.

Key figures of the checked-in run (also quoted in `docs/testing.md`): lowest running balance -29,303.32 on 2026-11-26 (lowest week-end closing -18,303.32, week 6), shortfall weeks 6, 7, 10 and 11; financing need 18,000.00 needed by 2026-11-25 for 36 days with the reschedule applied (30,000.00 without); recommended product supply-chain finance at 216.00; `FIN-0001.pdf` is the 18,000.00 application with the signed mandate; the run ends with 45 audit entries, the chain verifies, and a tampered copy fails at entry 2.

## 3. Environment variables

Complete list, found by searching the code for `os.environ` and `os.getenv`:

| Variable | Default | Used in | Meaning |
|---|---|---|---|
| `RUNWAY_DB` | `var/runway.db` | `common/db.py:db_url` | SQLite file for everything. Set it to a temp path in tests. |
| `RUNWAY_COUNTRY` | `eu` | `common/config.py:load_country_pack` | Country pack code (`config/<code>.yaml`). Display only: it changes how amounts are formatted in tool results, e-mails, PDFs and agent text. The ledger and the bank stay in the account currency. |
| `RUNWAY_VAR_DIR` | `<repo>/var` | `agent/outbox.py:var_dir`, `bank/client.py:bank_token` | Where `outbox/*.eml`, `applications/*.pdf`, `keys/*.pem`, `secrets/bank-*.token` and `archive/*.db` are written. |
| `RUNWAY_OWNER_EMAIL` | `owner@lumen-packaging.example` | `agent/outbox.py:owner_email` | Address for fraud alerts. A `Setting` row `owner_email` takes precedence. |
| `RUNWAY_BANK_URL` | unset (in-process bank) | `agent/tools.py:bank_client`, `agent/executor.py:_human_bank`, `ui/app.py:human_bank` | Base URL of a running bank, for example `http://localhost:8001`. |
| `RUNWAY_BANK_EXECUTOR_TOKEN` | random, in `var/secrets/bank-executor.token` | `bank/client.py:bank_token` (used by `bank/app.py`, `agent/tools.py`) | Token the tool executor uses. The bank accepts it as actor `agent`. Unset, a random token is made on first use (mode 0600) and shared by every process using the same `var/` folder. |
| `RUNWAY_BANK_HUMAN_TOKEN` | random, in `var/secrets/bank-human.token` | `bank/client.py:bank_token` (used by `bank/app.py`, `ui/app.py`, `agent/executor.py:_human_bank`, `scripts/run_demo.py`) | Token for human-only bank actions: the UI uses it to create payees, fill in a missing IBAN and unverify payees, and the executor uses it to verify a payee or advance the clock once owner and accountant have approved the request. Verifying also needs their signed mandate, which the bank checks against `var/keys/`. Actor `owner`. |
| `RUNWAY_OWNER_PIN` | `1111` | `ui/auth.py:pin_for` | PIN for the owner sign-in in the UI. Demo-grade. |
| `RUNWAY_ACCOUNTANT_PIN` | `2222` | `ui/auth.py:pin_for` | PIN for the accountant sign-in in the UI. Demo-grade. |
| `DEEPSEEK_API_KEY` | unset | `llm/adapter.py:get_adapter` | API key. The variable name comes from `api_key_env` in `config/llm.yaml`. If unset, the offline planner is used. |
| `GEMINI_API_KEY` | unset | `llm/adapter.py:get_adapter` | Google Cloud API key for Gemini mode (`config/gemini.yaml`), sent in the `x-goog-api-key` header. |
| `GOOGLE_CLOUD_PROJECT` | unset | `llm/adapter.py` | Project id or number filled into the Vertex AI URL in `config/gemini.yaml` (`project_env`). Gemini mode requires it. |
| `RUNWAY_LLM` | unset | `llm/adapter.py:get_adapter` | Startup mode: `offline`, `deepseek`, or `gemini`. Sidebar selection overrides it per session. Explicit online modes require their key. |
| `RUNWAY_LLM_MODEL` | `model` in `config/llm.yaml` | `llm/adapter.py` | Override the model id. |
| `RUNWAY_LLM_BASE_URL` | `base_url` in `config/llm.yaml` | `llm/adapter.py` | Override the API base URL. |
| `RUNWAY_LLM_THINKING` | unset | `llm/adapter.py:chat` | Set to `on` to stop sending `thinking: disabled` to DeepSeek. Leave it unset: the adapter does not echo `reasoning_content` between tool turns. |
| `RUNWAY_SAMPLE_DIR` | `data/sample` | `ui/helpers.py` | Where the UI writes and reads sample CSVs and demo documents. The demo documents are ingested with this folder's `docs/` subfolder as the allowed root. |
| `RUNWAY_ANCHOR` | `notary` | `audit/anchor.py:get_backend` | Where the audit head is published: `notary` (an append-only JSON-lines file) or `evm` (any EVM JSON-RPC chain). |
| `RUNWAY_NOTARY_PATH` | `<db>.notary.jsonl` | `audit/anchor.py:get_backend` | Notary file. Default: the database path with the suffix `.notary.jsonl` (`var/runway.notary.jsonl`). |
| `RUNWAY_EVM_RPC_URL` | unset | `audit/anchor.py:get_backend` | JSON-RPC URL of the chain, for `RUNWAY_ANCHOR=evm`. Error messages never include it, since provider URLs often carry an API key. |
| `RUNWAY_EVM_PRIVATE_KEY` | unset | `audit/anchor.py:get_backend` | Funded key that signs the anchor transactions, for `RUNWAY_ANCHOR=evm`. A hot key: use a throwaway key with only gas money. |

Never commit keys. `.gitignore` already excludes `.env` and `var/`.

### Audit anchoring

`audit/anchor.py` publishes `runway-audit:v1:<count>:<head hash>` outside the database, stores a receipt row (`auditanchor` table) and audits the act as `anchor_audit_head`. Use **Publish head now** and **Verify against published heads** on the Audit log page, or the CLI (actor `system`):

```bash
.venv/bin/python -m audit.anchor publish   # prints the receipt as JSON
.venv/bin/python -m audit.anchor verify    # prints the report; exit code 1 unless "verified"
```

The default `notary` backend needs nothing. For `evm`, the payload goes into the calldata of a zero-value transaction from the anchor account to itself; no contract is deployed. Any EVM JSON-RPC node works:

- Local dev chain: install Foundry (`brew install foundry` on macOS, or `curl -L https://foundry.paradigm.xyz | bash && foundryup`), then run `anvil`, which listens on `127.0.0.1:8545` (chain id 31337) and use one of its pre-funded dev keys. `RUNWAY_ANCHOR=evm RUNWAY_EVM_RPC_URL=http://127.0.0.1:8545 RUNWAY_EVM_PRIVATE_KEY=<dev key>`.
- Public testnet (for example Sepolia): a provider URL and a faucet-funded throwaway key.

Publishing waits until the transaction is mined and successful (up to 120 s; about 12 s on a public testnet), so a stored receipt always points at a transaction on chain; if it is not mined in time or reverts, nothing is stored. `scripts/run_anchor_demo.py` reproduces the checked-in on-chain evidence. Verification fetches each receipt's transaction, refuses one that is unmined or sent by another account, and compares the published head with the log. Publishing and verifying with different backends reports "unavailable" for the receipts of the other backend. FISCO BCOS exposes a different RPC and has not been tried.

### Approval signing keys

Each approving role signs its approvals with an Ed25519 key in `<RUNWAY_VAR_DIR>/keys/<role>.pem` (default `var/keys/`), created on first use with mode 0600. `var/` is git-ignored; never commit the keys. Reset and import keep them, so the public keys registered with a bank stay valid. Deleting a key makes the next approval create a new one, which no longer matches the registered key. Print the public keys with `python -m agent.mandate keys` and check a financing PDF with `python -m agent.mandate verify <pdf> --registered keys.json`.

For anything beyond a demo, set both bank tokens to long random values and replace the PINs with real authentication. The defaults are public in the repository (see KNOWN RISKS in `docs/security-self-assessment.md`).

The list also covers `config/llm.yaml`, whose `api_key_env` names one more variable (`DEEPSEEK_API_KEY` by default).

## 4. Offline, DeepSeek and Gemini modes

Use **Model mode** in the sidebar to switch providers for the current browser session, including the red-team chat injection test. Each switch starts a fresh conversation and clears the displayed brief and red-team result; it does not undo stored actions or approvals. Keys are loaded from `.env` at process startup, so restart Runway after editing them. An explicitly selected provider without its key shows a setup warning and disables model actions.

**Offline (default).** No `DEEPSEEK_API_KEY`, or `RUNWAY_LLM=offline`. `llm/adapter.py:get_adapter()` returns `OfflinePlanner`, a deterministic stand-in that issues the real tool calls of the demo story (forecast, reminders, reschedule, financing, document review, refusals). Everything the agent does still passes the rules engine, approval queue and audit log. It needs no network. Use it for the demo video, tests and judging machines without internet.

**DeepSeek.**

```bash
cp .env.example .env                  # then set DEEPSEEK_API_KEY=sk-... in .env (or export it)
.venv/bin/streamlit run ui/app.py
```

`config/llm.yaml` currently says: provider `deepseek`, base URL `https://api.deepseek.com`, model `deepseek-flash`, temperature 0.2, timeout 60 s. The model id `deepseek-flash` was checked against the current DeepSeek documentation by the team; no automated test calls the live API, so send one chat message with the key set before the demo. Messages are pseudonymised first (names, e-mails, IBANs become placeholders); amounts, dates and invoice numbers are sent as they are. The agent loop stops after 8 tool-calling steps.

Tests never call the API.

**Gemini (Vertex AI).** Add `GEMINI_API_KEY=your-key` and `GOOGLE_CLOUD_PROJECT=your-project-id-or-number` to the existing `.env` (keep `DEEPSEEK_API_KEY` too), restart Runway and select **Gemini**. The key is a Google Cloud API key that may call the Vertex AI API in that project. To start in Gemini mode, set `RUNWAY_LLM=gemini` in `.env` or the process environment. `config/gemini.yaml` uses `google/gemini-3.8-flash` via Vertex AI's OpenAI-compatible endpoint (`https://aiplatform.googleapis.com/v1/projects/{project}/locations/global/endpoints/openapi/`), using the installed OpenAI SDK. Vertex accepts API keys only in the `x-goog-api-key` header and rejects them as a Bearer token, so `api_key_header` makes the adapter send that header and drop `Authorization`. Provider tool-call metadata is retained across tool turns. Both online modes use the same pseudonymisation and policy checks. API integration tests use fake responses; a live key and successful chat are still needed to confirm account access and quotas.

## 5. Switching the LLM provider (one config change)

Any OpenAI-compatible chat API with tool calling works. Edit `config/llm.yaml`:

```yaml
provider: qwen                      # any value other than "deepseek" skips the DeepSeek-specific request option
base_url: https://example-provider.com/v1   # the provider's OpenAI-compatible endpoint
model: provider-model-id
api_key_env: PROVIDER_API_KEY       # name of the environment variable that holds the key
temperature: 0.2
timeout_seconds: 20                 # a stalled provider falls back to the offline planner within ~40 s
max_retries: 1
```

Then `export PROVIDER_API_KEY=...`. Without editing the file you can also set `RUNWAY_LLM_BASE_URL` and `RUNWAY_LLM_MODEL`, but the key variable name still comes from `api_key_env`. These overrides apply to either selected online provider; remove them when switching if they point to a different provider. For Gemini-specific settings edit `config/gemini.yaml`. Look up the exact endpoint and model names in the provider's documentation; custom providers are not verified here. No adapter code change is needed.

## 6. Switching the country pack

A country pack is a YAML file in `config/` with: `name`, `currency`, `currency_symbol`, `vat_rate`, `vat_day_of_month`, `default_customer_terms_days`, `default_supplier_terms_days`, `fx_from_eur`. Shipped packs: `eu` (EUR, 20%, VAT on the 25th) and `cn` (CNY, 13%, VAT on the 15th, 7.8 CNY per EUR; illustrative values).

The pack is display-only for amounts. The ledger, the rules engine and the bank always use the account currency (EUR in the demo); `CountryPack.display(cents)` converts with `fx_from_eur` and formats, and nothing converts back. VAT rate, VAT day and default terms still come from the pack. There are two switches:

1. **Display switch in the UI** (header radio "Display currency", `eu` or `cn`). It changes the tables, charts, metrics and approval cards in the UI (`ui/helpers.py:money`). This is what the demo uses for the live "switch to RMB" moment. It does not change the text the agent writes.
2. **`RUNWAY_COUNTRY=cn`** for the process. It changes the pack returned by `load_country_pack()` outside the UI pages: tool results (`*_display` fields), the agent's replies, reminder e-mails, the financing comparison text and the application PDF show converted CNY amounts. Payments are not affected: the policy and the bank compare against the account currency, so `schedule_payment` keeps working. No automated test covers payments under `RUNWAY_COUNTRY=cn`.

The forecast engine reads the pack named by the `country` setting in the database (`eu` after generation or import) for the VAT day, not `RUNWAY_COUNTRY`; the two can differ.

To add a pack, copy `config/eu.yaml` to `config/<code>.yaml`, edit the values, and select it with `RUNWAY_COUNTRY=<code>` (or add it to the UI radio in `ui/app.py:header`). The generated data itself is always built with the `eu` pack's VAT rate (`data/generate.py`).

## 7. Resetting demo data

Choose one:

- UI: sign in, then header button "Reset demo data" and "Confirm reset". This copies the database to `var/archive/runway-<UTC time>.db`, drops every table, regenerates the data, clears the chat and switches the kill switch off. The first entry of the new audit log is `reset_demo`, with the role and browser session that pressed the button and the archived file, head and length of the old log (`audit.archive.check_link` verifies the link).
- UI: Onboarding page, "Use generated sample data". Same archive and regeneration; it needs a signed-in role but has no confirmation step.
- UI: Onboarding page, "Import uploaded files" then "Confirm import" replaces the database with the uploaded company (section 1a), after the same archive copy. It needs a signed-in role.
- CLI: `.venv/bin/python -m data.generate`.
- Manual: stop the services, delete `var/runway.db`, start again (the UI regenerates on first load).

Reset and import both drop every table, including the audit log; the old database survives in `var/archive/` (never pruned; delete old files by hand). Neither deletes the e-mail and PDF files in `var/outbox/` and `var/applications/`. New e-mails restart at `MAIL-0001` and overwrite the old file with that name, but stale files with higher numbers can remain. Before recording: `rm -rf var/outbox var/applications`.

The simulated date returns to 2026-10-19 on reset. If you advance the clock (Bank simulator page, "Advance", then the other role approves it), reset before the next take.

Reset also drops the anchor receipts, so "Verify against published heads" reads "unanchored" until the next publish. The notary file is not deleted; new publishes append to it. Documents ingested and actions attempted on the "Try to break it" page are real (a flagged document blocks payments to the suppliers it names), so reset before a recorded take.

## 8. Recording the demo video (5 minutes or less)

1. Run in offline mode for a repeatable take: `unset DEEPSEEK_API_KEY` (or `export RUNWAY_LLM=offline`).
2. `rm -rf var/outbox var/applications`, then `.venv/bin/streamlit run ui/app.py`, sign in as `owner` (PIN 1111), then press "Reset demo data" and "Confirm reset" (the reset needs a signed-in role). Keep a second window ready to sign in as `accountant` (PIN 2222). Leave "Display currency" on EUR.
3. Use a 1920 x 1080 screen capture (OBS or the OS recorder), browser zoom 110 to 125 percent, sidebar visible, no other tabs. Close notifications.
4. Follow `docs/demo-script.md` for the click path and the words. Do a full dry run first; if a step goes wrong, reset and start the take again, because the audit log is cumulative.
5. Record a backup take early, and keep the output of `python scripts/run_demo.py` as written evidence.
6. Export as MP4, 5 minutes or less.

Also useful before recording: `.venv/bin/pytest` should be green.

## 9. Operational notes

- Concurrency: SQLite with the writer lock is enough for one Streamlit server with a few browser sessions (the two-window approval needs two). Do not point several app instances at the same file.
- Backup: copy `var/runway.db` while no write is in progress, and `var/keys/` (the approval signing keys) separately and privately. The stored audit head and count (`audit_head`, `audit_count`) are inside the same file; the published heads are not (notary file or chain), so keep the receipts' references (transaction hashes) somewhere else too.
- Logs: uvicorn and Streamlit print to the terminal. The audit log is in the database; export it with `scripts/run_demo.py` or read it on the Audit log page.
- Time: "today" is the `today` setting, not the system clock.
