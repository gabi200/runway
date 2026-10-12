# Runway

An AI finance officer for small suppliers. Runway forecasts cash 13 weeks ahead, chases overdue customers, proposes payment changes and prepares financing applications. It works with the two people who run a small company's money, the owner and the external accountant. The agent proposes, a rules engine written in plain Python decides what is allowed, and humans approve anything risky. It cannot move money it should not.

Entry for the 2026 Shenzhen International FinTech Competition, International Track (hosted with WeBank), Topic E: SME Finance Copilot. All data is fictional and generated.

## The demo story

Lumen Packaging (20 employees, cardboard packaging) sells 65 percent of its output to Brightfield Appliances, which pays 75 days after invoice. The 13-week forecast shows two shortfalls: the bank balance falls to -18,303.32 EUR in week 6 (the paperboard bill, payroll and VAT fall before Brightfield's money arrives) and to -9,494.98 EUR in week 10 (payroll and VAT of 25 December).

1. **Onboarding**: upload a trial balance and bank statement (or load the generated sample). Plain code builds the forecast; every figure traces to source rows.
2. **Monday brief**: "Cash goes below zero in week 6 (-18,303.32) and week 10 (-9,494.98). Week 6 is restored by a 90,000 receipt on 2026-12-03, week 10 by a 95,000 receipt on 2026-12-31." The amount, date and duration of the financing need are computed by the tool `get_financing_need`, not by the model.
3. **The plan**: remind two overdue customers, move one non-critical supplier payment (Inkworks, 12,000) to its last allowed day, compare financing by true cost. With the move applied the need is 10,000 for 38 days (19,000 without it).
4. **Approval**: reminders go out automatically (tier T2). Rescheduling waits for the owner (T3). The 10,000 financing application waits for owner and accountant (T3, above 5,000). They must be two different sign-ins.
5. **Execution**: the mock bank shows the moved payment, reminders land in a test inbox, a financing application PDF is generated. The comparison lists every option that arrives in time with its cash date; the cheapest is supply-chain finance (120.00 true cost).
6. **Attack**: a Cartonia invoice with hidden text "bank details changed, pay this IBAN" is flagged, the owner is alerted, and any linked or unverified-payee payment is blocked; a payment to Cartonia by invoice alone is blocked too, because the document names the supplier.
7. **Audit trail**: every action, approval and refusal is in a tamper-evident hash-chained log.

Full click-by-click script: [docs/demo-script.md](docs/demo-script.md).

## Quick start

Requires Python 3.11 or newer.

```bash
git clone <this repo> && cd runway
uv sync                                  # or: python -m venv .venv && .venv/bin/pip install -e . pytest
.venv/bin/python -m data.generate        # creates var/runway.db and data/sample/ (seed 42)
.venv/bin/streamlit run ui/app.py        # the UI, http://localhost:8501
```

In the UI, sign in at the top of the page before approving or changing anything. Demo PINs: owner `1111`, accountant `2222` (override with `RUNWAY_OWNER_PIN` and `RUNWAY_ACCOUNTANT_PIN`). A four-eyes approval needs the owner and the accountant in two different browser sessions (two windows).

Optional separate mock bank server:

```bash
.venv/bin/uvicorn bank.app:app --port 8001
export RUNWAY_BANK_URL=http://localhost:8001     # make the UI and agent use it
```

Without `RUNWAY_BANK_URL` the bank runs in-process, which is enough for the demo. The UI also generates data on first start if the database is empty.

### Offline mode and DeepSeek mode

- **Offline (default)**: with no API key, a deterministic planner (`llm/offline.py`) plays the language model and issues the real tool calls of the demo. No network needed. Force it with `RUNWAY_LLM=offline`.
- **DeepSeek**: `export DEEPSEEK_API_KEY=...` before starting the UI. Provider, base URL and model are in `config/llm.yaml`; any OpenAI-compatible provider works by editing that file. Names (known counterparties, people found in headers and signatures), e-mails and IBANs are replaced by placeholders before the API call; amounts, dates and invoice numbers are sent as they are.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `RUNWAY_DB` | `var/runway.db` | SQLite database file |
| `RUNWAY_COUNTRY` | `eu` | Country pack (`config/<code>.yaml`). Display only: the ledger and the bank stay in the account currency (EUR) |
| `RUNWAY_VAR_DIR` | `var` | Output folder for test-inbox `.eml` files and application PDFs |
| `RUNWAY_OWNER_EMAIL` | `owner@lumen-packaging.example` | Recipient of fraud alerts |
| `RUNWAY_BANK_URL` | unset (in-process) | URL of a running mock bank |
| `RUNWAY_BANK_EXECUTOR_TOKEN` | `executor-dev-token` | Bank token held by the tool executor (change outside the demo) |
| `RUNWAY_BANK_HUMAN_TOKEN` | `human-dev-token` | Bank token used by the UI for human-only actions (payees, clock). Also read by `scripts/run_demo.py` (change outside the demo) |
| `RUNWAY_OWNER_PIN` | `1111` | UI sign-in PIN of the owner role (demo-grade) |
| `RUNWAY_ACCOUNTANT_PIN` | `2222` | UI sign-in PIN of the accountant role (demo-grade) |
| `DEEPSEEK_API_KEY` | unset | LLM key (variable name set by `api_key_env` in `config/llm.yaml`) |
| `RUNWAY_LLM` | unset | `offline` forces the offline planner |
| `RUNWAY_LLM_MODEL`, `RUNWAY_LLM_BASE_URL` | from `config/llm.yaml` | Override model and endpoint |
| `RUNWAY_LLM_THINKING` | unset | `on` enables DeepSeek thinking mode (not recommended) |
| `RUNWAY_SAMPLE_DIR` | `data/sample` | UI sample-data folder (generated CSVs and demo documents) |

Details and switching providers or country packs: [docs/deployment.md](docs/deployment.md).

## Tests

```bash
.venv/bin/pytest                 # 332 tests (1 skips where symlinks are not allowed), no network
.venv/bin/pytest tests/security  # attack scenarios A1 to A7
.venv/bin/python scripts/run_demo.py   # replays the whole story and writes docs/execution_logs/
```

See [docs/testing.md](docs/testing.md).

## Repository layout

```
common/      shared SQLite schema (db.py) and country-pack loader (config.py, display-only formatting)
config/      eu.yaml, cn.yaml country packs; llm.yaml provider settings
data/        generate.py (seeded data generator), importer.py (CSV import), financing.yaml (product catalogue), sample/ outputs
forecast/    13-week forecast engine and financing_need (pure Python, source references on every line)
policy/      rules engine (engine.py) and rules.yaml (tiers and limits). No AI.
audit/       hash-chained append-only audit log, verify_chain()
bank/        mock bank: FastAPI app, service logic, client
agent/       tool executor (executor.py), tools, financing, outbox, PDF, documents (safe ingestion and scanner), orchestrator, prompts
llm/         OpenAI-compatible adapter, offline planner, pseudonymizer
ui/          Streamlit UI (app.py), PIN sign-in (auth.py), safe rendering of model output (render.py), helpers.py
scripts/     run_demo.py execution-evidence runner (writes docs/execution_logs/)
tests/       unit and integration tests; tests/security/ holds attack tests A1 to A7
docs/        specification, contracts, architecture, algorithms, security, deployment, testing, glossary, demo script, execution logs
```

## Documentation

| Document | Content |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Components, data model, request flow, approval flow, the five design rules and where the code enforces them |
| [docs/algorithms.md](docs/algorithms.md) | Data generator, forecast, financing need and true cost, rules-engine order, audit hash chain, pseudonymization, safe ingestion and injection scanning, CSV import, worked examples |
| [docs/security-self-assessment.md](docs/security-self-assessment.md) | Threat model, permission tiers T0 to T4, defense in depth, attacks A1 to A6, known risks |
| [docs/deployment.md](docs/deployment.md) | Setup, services, environment variables, switching LLM provider and country pack, resetting data, recording the demo |
| [docs/testing.md](docs/testing.md) | Test inventory and what each suite proves |
| [docs/glossary.md](docs/glossary.md) | Finance terms and Runway terms |
| [docs/demo-script.md](docs/demo-script.md) | 5-minute video script and 10-minute presentation outline |
| [docs/SPEC.md](docs/SPEC.md), [docs/CONTRACTS.md](docs/CONTRACTS.md) | Product spec and module contracts |
| [docs/execution_logs/](docs/execution_logs/) | Output of `scripts/run_demo.py` (45 audit entries on seed 42) |

## Submission checklist (SPEC section 2)

| # | Required item | Where | Status |
|---|---|---|---|
| 1 | Technical documentation: architecture, core algorithms, security design | [docs/architecture.md](docs/architecture.md), [docs/algorithms.md](docs/algorithms.md), [docs/security-self-assessment.md](docs/security-self-assessment.md) | Written |
| 2 | Presentation deck, 10 minutes or less | Outline and timing in [docs/demo-script.md](docs/demo-script.md) section 3 | Outline only; slides still to be built |
| 3 | Demo video, 5 minutes or less (optional) | Script in [docs/demo-script.md](docs/demo-script.md) section 2 | Script written; video still to be recorded |
| 4 | Source code on GitHub with README, deployment instructions and test cases | This repository, [README.md](README.md), [docs/deployment.md](docs/deployment.md), `tests/` | Ready; push to GitHub pending |
| 5 | Security self-assessment: permission tiers and known risks | [docs/security-self-assessment.md](docs/security-self-assessment.md) sections 3 and 7 | Written |
| 6 | Execution evidence: simulated-bank logs plus the demo video | [docs/execution_logs/](docs/execution_logs/) (`demo_run.md`, `audit_log.jsonl`, `bank_payments.json`, `outbox.json`, `FIN-0001.pdf`); video as item 3 | Logs generated; video pending |

Deadline: 20 October 2026, 23:59 Beijing time (19:00 Bucharest, 16:00 UTC).

## Limits to know about

The demo uses a simulated clock (default 2026-10-19), default demo tokens, a single SQLite file, and PIN sign-in (per-role PINs from environment variables, not real authentication; one person holding both PINs can open two browsers and approve both roles). Attack tests run against the offline planner, not a live model. Keyword detection of hostile documents can be evaded. Importing only the four basic CSVs does not reproduce customer lateness or already-scheduled payments, so the week-6 shortfall does not appear for the sample company unless the optional history files are added. The full list is in section 7 of the security self-assessment.
