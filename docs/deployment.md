# Deployment and operation

Runway is a local application: a Python package, one SQLite file, an optional FastAPI server for the mock bank, and a Streamlit UI. There is no Docker setup or hosted deployment in this repository.

Requirements: Python 3.11 or newer (the lock file and `.python-version` use 3.12), about 500 MB for dependencies. Network access is needed only for installing packages and, optionally, for the DeepSeek API.

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

It prints a summary: ids of the key demo invoices, opening balance, and the forecast minimum (seed 42: week 6, -1811887 cents, that is -18,118.87 EUR). The UI also generates data automatically on first start if the database is empty.

## 2. Running the services

### UI (the main entry point)

```bash
.venv/bin/streamlit run ui/app.py
```

Open the URL Streamlit prints (default http://localhost:8501). With no extra settings the UI runs the agent with the offline planner and talks to the mock bank in-process.

### Mock bank as a separate server (optional)

```bash
.venv/bin/uvicorn bank.app:app --port 8001
```

Check it: `curl -H "Authorization: Bearer executor-dev-token" http://localhost:8001/accounts`. Interactive API docs are at http://localhost:8001/docs.

The UI and the agent use this server only when `RUNWAY_BANK_URL` is set:

```bash
export RUNWAY_BANK_URL=http://localhost:8001
.venv/bin/streamlit run ui/app.py
```

Both the server and the UI read the same SQLite file, so set the same `RUNWAY_DB` for both if you change it. Without `RUNWAY_BANK_URL`, the bank runs in-process through FastAPI's test client and the uvicorn server is not needed.

Bank endpoints: `GET /accounts`, `GET /accounts/{id}/transactions?from=&to=`, `GET /payees`, `POST /payees` and `PATCH /payees/{id}` (human token only; the PATCH body is `{"verified": true|false}`, nothing else can change), `POST /payments`, `GET /payments?status=`, `PATCH /payments/{id}` (only `execute_date`), `POST /simulate/advance?days=N`. All need `Authorization: Bearer <token>`.

### Command-line demo run (no UI)

```bash
.venv/bin/python scripts/run_demo.py                    # writes docs/execution_logs/
.venv/bin/python scripts/run_demo.py --out /tmp/evidence --seed 42
.venv/bin/python scripts/run_demo.py --bank-transport service   # only if TestClient hangs in your sandbox (skips HTTP auth)
```

It runs on a temporary database (your `var/runway.db` is untouched) in offline mode and asserts every step. It writes:

| File | Content |
|---|---|
| `demo_run.md` | Step-by-step log with the JSON of each tool result, labelled by demo step and attack id |
| `audit_log.jsonl` | The full audit chain of the run, one JSON object per line (40 entries on seed 42) |
| `bank_payments.json` | All payments in the mock bank at the end |
| `outbox.json` | All e-mails in the test inbox (reminders, fraud alerts) |
| `FIN-0001.pdf` | The generated financing application |

The checked-in files in `docs/execution_logs/` were generated with the default `--bank-transport api` (through the FastAPI app's test client, with bearer-token authentication). `--bank-transport service` exists only for sandboxes where the test client hangs; it calls the bank service functions directly and the log says so.

## 3. Environment variables

Complete list, found by searching the code for `os.environ` and `os.getenv`:

| Variable | Default | Used in | Meaning |
|---|---|---|---|
| `RUNWAY_DB` | `var/runway.db` | `common/db.py:db_url` | SQLite file for everything. Set it to a temp path in tests. |
| `RUNWAY_COUNTRY` | `eu` | `common/config.py:load_country_pack` | Country pack code (`config/<code>.yaml`) used by the backend for currency, VAT. |
| `RUNWAY_VAR_DIR` | `<repo>/var` | `agent/outbox.py:var_dir` | Where `outbox/*.eml` and `applications/*.pdf` are written. |
| `RUNWAY_OWNER_EMAIL` | `owner@lumen-packaging.example` | `agent/outbox.py:owner_email` | Address for fraud alerts. A `Setting` row `owner_email` takes precedence. |
| `RUNWAY_BANK_URL` | unset (in-process bank) | `agent/tools.py:bank_client`, `ui/app.py:human_bank` | Base URL of a running bank, for example `http://localhost:8001`. |
| `RUNWAY_BANK_EXECUTOR_TOKEN` | `executor-dev-token` | `bank/app.py`, `bank/client.py`, `agent/tools.py` | Token the tool executor uses. The bank accepts it as actor `agent`. |
| `RUNWAY_BANK_HUMAN_TOKEN` | `human-dev-token` | `bank/app.py`, `ui/app.py` | Token the UI uses for human-only actions (create and verify payees). Actor `owner`. |
| `DEEPSEEK_API_KEY` | unset | `llm/adapter.py:get_adapter` | API key. The variable name comes from `api_key_env` in `config/llm.yaml`. If unset, the offline planner is used. |
| `RUNWAY_LLM` | unset | `llm/adapter.py:get_adapter` | `offline` forces the offline planner even if a key is set. |
| `RUNWAY_LLM_MODEL` | `model` in `config/llm.yaml` | `llm/adapter.py` | Override the model id. |
| `RUNWAY_LLM_BASE_URL` | `base_url` in `config/llm.yaml` | `llm/adapter.py` | Override the API base URL. |
| `RUNWAY_LLM_THINKING` | unset | `llm/adapter.py:chat` | Set to `on` to stop sending `thinking: disabled` to DeepSeek. Leave it unset: the adapter does not echo `reasoning_content` between tool turns. |
| `RUNWAY_SAMPLE_DIR` | `data/sample` | `ui/helpers.py` | Where the UI writes and reads sample CSVs and demo documents. |

Never commit keys. `.gitignore` already excludes `.env` and `var/`.

For anything beyond a demo, set both bank tokens to long random values. The defaults are public in the repository (see KNOWN RISKS in `docs/security-self-assessment.md`).

## 4. Offline mode and DeepSeek mode

**Offline (default).** No `DEEPSEEK_API_KEY`, or `RUNWAY_LLM=offline`. `llm/adapter.py:get_adapter()` returns `OfflinePlanner`, a deterministic stand-in that issues the real tool calls of the demo story (forecast, reminders, reschedule, financing, document review, refusals). Everything the agent does still passes the rules engine, approval queue and audit log. It needs no network. Use it for the demo video, tests and judging machines without internet.

**DeepSeek.**

```bash
export DEEPSEEK_API_KEY=sk-...        # your key
.venv/bin/streamlit run ui/app.py
```

`config/llm.yaml` currently says: provider `deepseek`, base URL `https://api.deepseek.com`, model `deepseek-flash`, temperature 0.2, timeout 60 s. The model id `deepseek-flash` was checked against the current DeepSeek documentation by the team; no automated test calls the live API, so send one chat message with the key set before the demo. Messages are pseudonymised first (names, e-mails, IBANs become placeholders); amounts, dates and invoice numbers are sent as they are. The agent loop stops after 8 tool-calling steps.

Tests never call the API.

## 5. Switching the LLM provider (one config change)

Any OpenAI-compatible chat API with tool calling works. Edit `config/llm.yaml`:

```yaml
provider: qwen                      # any value other than "deepseek" skips the DeepSeek-specific request option
base_url: https://example-provider.com/v1   # the provider's OpenAI-compatible endpoint
model: provider-model-id
api_key_env: PROVIDER_API_KEY       # name of the environment variable that holds the key
temperature: 0.2
timeout_seconds: 60
```

Then `export PROVIDER_API_KEY=...`. Without editing the file you can also set `RUNWAY_LLM_BASE_URL` and `RUNWAY_LLM_MODEL`, but the key variable name still comes from `api_key_env`. Look up the exact endpoint and model names in the provider's documentation; they are not verified here. No code change is needed.

## 6. Switching the country pack

A country pack is a YAML file in `config/` with: `name`, `currency`, `currency_symbol`, `vat_rate`, `vat_day_of_month`, `default_customer_terms_days`, `default_supplier_terms_days`, `fx_from_eur`. Shipped packs: `eu` (EUR, 20%, VAT on the 25th) and `cn` (CNY, 13%, VAT on the 15th, 7.8 CNY per EUR; illustrative values).

There are two switches, and they do different things:

1. **Display switch in the UI** (header radio "Display currency", `eu` or `cn`). It converts stored EUR cents for display with `fx_from_eur` (`ui/helpers.py:money`). Stored data, policy limits and the bank stay in EUR. This is what the demo uses for the live "switch to RMB" moment.
2. **`RUNWAY_COUNTRY=cn`** for the backend. It changes the pack returned by `load_country_pack()` everywhere outside the UI. Known limits: the policy denies `schedule_payment` because the pack currency no longer equals the account currency (EUR); e-mail text, the financing comparison text and the application PDF print the CNY symbol on unconverted EUR amounts. Use the UI display switch for demos; do not run payments under `RUNWAY_COUNTRY=cn`.

To add a pack, copy `config/eu.yaml` to `config/<code>.yaml`, edit the values, and select it with `RUNWAY_COUNTRY=<code>` (or add it to the UI radio in `ui/app.py:header`). The generated data itself is always built with the `eu` pack's VAT rate (`data/generate.py`).

## 7. Resetting demo data

Choose one:

- UI: header button "Reset demo data", then "Confirm reset". This drops every table (including the audit log), regenerates the data, clears the chat and switches the kill switch off.
- UI: Onboarding page, "Use generated sample data". Same regeneration, without the confirmation step.
- CLI: `.venv/bin/python -m data.generate`.
- Manual: stop the services, delete `var/runway.db`, start again (the UI regenerates on first load).

Resetting does not delete the e-mail and PDF files in `var/outbox/` and `var/applications/`. New e-mails restart at `MAIL-0001` and overwrite the old file with that name, but stale files with higher numbers can remain. Before recording: `rm -rf var/outbox var/applications`.

The simulated date returns to 2026-10-19 on reset. If you advance the clock (Mock bank page, "Advance"), reset before the next take.

## 8. Recording the demo video (5 minutes or less)

1. Run in offline mode for a repeatable take: `unset DEEPSEEK_API_KEY` (or `export RUNWAY_LLM=offline`).
2. `rm -rf var/outbox var/applications`, then `.venv/bin/streamlit run ui/app.py`, then press "Reset demo data" and "Confirm reset". Leave "Acting as" on `owner` and "Display currency" on EUR.
3. Use a 1920 x 1080 screen capture (OBS or the OS recorder), browser zoom 110 to 125 percent, sidebar visible, no other tabs. Close notifications.
4. Follow `docs/demo-script.md` for the click path and the words. Do a full dry run first; if a step goes wrong, reset and start the take again, because the audit log is cumulative.
5. Record a backup take early, and keep the output of `python scripts/run_demo.py` as written evidence.
6. Export as MP4, 5 minutes or less.

Also useful before recording: `.venv/bin/pytest` should be green.

## 9. Operational notes

- Concurrency: SQLite with the writer lock is enough for one UI session. Do not point several app instances at the same file.
- Backup: copy `var/runway.db` while no write is in progress. The audit anchors (`audit_head`, `audit_count`) are inside the same file.
- Logs: uvicorn and Streamlit print to the terminal. The audit log is in the database; export it with `scripts/run_demo.py` or read it on the Audit log page.
- Time: "today" is the `today` setting, not the system clock.
