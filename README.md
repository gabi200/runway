# Runway

An AI finance officer for small suppliers. Runway forecasts cash 13 weeks ahead, chases overdue customers, proposes payment changes and prepares financing applications. It works with the two people who run a small company's money, the owner and the external accountant. The agent proposes, a rules engine written in plain Python decides what is allowed, and humans approve anything risky. It cannot move money it should not.

Entry for the 2026 Shenzhen International FinTech Competition, International Track (hosted with WeBank), Topic E: SME Finance Copilot. All data is fictional and generated.

Source code: <https://github.com/gabi200/runway>

## The demo story

Lumen Packaging (20 employees, cardboard packaging) sells 65 percent of its output to Brightfield Appliances, which pays 75 days after invoice. The 13-week forecast walks the bank balance day by day (on the same day, payments are counted before receipts) and finds two stretches below zero. After the paperboard bill (60,000 on 18 November), payroll and VAT on 25 November and the 12,000 Inkworks payment on 26 November take the balance to -29,303.32 EUR; it stays below zero until Brightfield's 90,000 arrives on 3 December (weeks 6 and 7). Payroll and VAT of 25 December take it to -9,494.98 EUR until Brightfield's 95,000 arrives on 31 December (weeks 10 and 11). The week-end closings are -18,303.32 (week 6) and -9,494.98 (week 10); weeks 7 and 11 close positive but dip below zero inside the week.

1. **Onboarding**: upload a trial balance and bank statement (or load the generated sample). Plain code builds the forecast; every figure traces to source rows.
2. **Monday brief**: "The balance is below zero from 2026-11-25 (week 6) and lowest at -€29,303.32 on 2026-11-26. ... It ends with Receipt LUM-2026-0093 (€90,000.00, AR-1093), expected 2026-12-03 in week 7 ..." and the same for the stretch from 2026-12-25 (week 10), which ends with the 95,000 receipt on 2026-12-31. The amount, date and duration of the financing need are computed by the tool `get_financing_need`, not by the model.
3. **The plan**: remind two overdue customers, move one non-critical supplier payment (Inkworks, 12,000) to its last allowed day, compare financing by true cost. With the move applied the need is 18,000 from 25 November for 36 days (30,000 without it).
4. **Approval**: reminders go out automatically (tier T2). Rescheduling waits for the owner (T3). The 18,000 financing application waits for owner and accountant (T3, above 5,000). They must be two different sign-ins, and each approval is signed with that role's Ed25519 key. A request that nobody approves expires after 7 simulated days.
5. **Execution**: the mock bank shows the moved payment, reminders land in a test inbox, a financing application PDF is generated. The PDF carries the signed mandate (a QR code and a JSON block), so a bank can check that two human keys, not the agent, approved this exact request. The comparison lists every option that arrives in time with its cash date; the cheapest is supply-chain finance (216.00 true cost).
6. **Attack**: a Cartonia invoice with hidden text "bank details changed, pay this IBAN" is flagged, the owner is alerted, and any linked or unverified-payee payment is blocked; a payment to Cartonia by invoice alone is blocked too, because the document names the supplier. Lifting such a block (clearing the flag, releasing one named supplier, or verifying a payee), turning the kill switch off and moving the simulated clock need the owner and the accountant, from two sessions.
7. **Clarify, correct, fall back**: asked to remind "the overdue customer" when two are overdue, Runway asks which one; told "No, don't move that payment" after the brief, it withdraws its own pending reschedule request. If the language model times out or fails, the turn finishes with the built-in offline planner and the reply says so.
8. **Audit trail**: every action, approval and refusal is in a tamper-evident hash-chained log. The head of the chain can be published outside the database (a notary file, or an EVM blockchain transaction), so even someone who owns the database file cannot rewrite history unnoticed.
9. **Try to break it**: judges paste their own fraudulent invoice, jailbreak the chat, make the agent attempt forbidden actions, or rewrite the audit log, and watch which layer stops each attack. A forged audit history passes the local hash check but fails against the head published on chain.


## Quick start

Requires Python 3.11 or newer.

```bash
git clone https://github.com/gabi200/runway.git && cd runway
uv sync                                  # or: python -m venv .venv && .venv/bin/pip install -e . pytest
.venv/bin/python -m data.generate        # creates var/runway.db and data/sample/ (seed 42)
.venv/bin/streamlit run ui/app.py        # the UI, http://localhost:8501
```

In the UI, sign in under **Session & sign-in** in the sidebar before approving or changing anything. Demo PINs: owner `1111`, accountant `2222` (override with `RUNWAY_OWNER_PIN` and `RUNWAY_ACCOUNTANT_PIN`). A four-eyes approval needs the owner and the accountant in two different browser sessions (two windows).

The interface includes matching light and dark themes. Open the top-right menu and choose **Light** or **Dark**. The browser remembers your appearance as you navigate. Streamlit 1.65 or newer is required for the configured theme support.

Optional separate mock bank server:

```bash
.venv/bin/uvicorn bank.app:app --port 8001
export RUNWAY_BANK_URL=http://localhost:8001     # make the UI and agent use it
```

Without `RUNWAY_BANK_URL` the bank runs in-process, which is enough for the demo. The UI also generates data on first start if the database is empty.

### Invoices, reminders and charts in the copilot

- **Invoice buttons.** Wherever an invoice is named (a copilot reply, an approval card, a test-inbox email, a document, the forecast's source rows), a button with its number opens its details: amounts, dates, payments, reminders already sent and a PDF copy rendered from the ledger. Only ids and numbers that exist in the ledger become buttons.
- **Reminder buttons.** For an overdue customer invoice, **Remind: <number>** (or **Send reminder via copilot** in the details) asks the copilot to send the reminder. The request goes through the agent and the rules engine like any other: cooldown, daily limit and overdue checks still apply, and the result appears in the chat.
- **Charts.** After a reply with a table or a series of figures, the copilot asks whether you want a chart. Say yes, or ask for a chart, graph or plot, and it calls the T1 tool `make_chart`. The figures are computed in Python from the ledger (`agent/charts.py`), not by the model, and the app draws the chart below the reply.

### Offline, DeepSeek, Gemini and Ollama modes

Choose **Model mode** in the sidebar: **Offline**, **DeepSeek**, **Gemini** or **Ollama (local)**. The selection applies to chat, the Monday brief and chat injection tests in that browser session. Switching modes clears the conversation and displayed brief; saved actions and approvals remain in the database. `RUNWAY_LLM=offline|deepseek|gemini|ollama` sets the startup mode. With no explicit mode, DeepSeek is used if its key is set; otherwise the app starts offline. Selecting a provider without its key displays setup instructions.

- **Offline (default)**: with no API key, a deterministic planner (`llm/offline.py`) plays the language model and issues the real tool calls of the demo. No network needed. Force it with `RUNWAY_LLM=offline`.
- **DeepSeek**: copy `.env.example` to `.env` and put the key after `DEEPSEEK_API_KEY=` (or export the variable; the environment wins over `.env`, which git ignores). Provider, base URL and model are in `config/llm.yaml`; any OpenAI-compatible provider works by editing that file. Names (known counterparties, people found in headers and signatures), e-mails and IBANs are replaced by placeholders before the API call; amounts, dates and invoice numbers are sent as they are.
- **Gemini (Vertex AI)**: add `GEMINI_API_KEY=your-key` and `GOOGLE_CLOUD_PROJECT=your-project-id-or-number` to your existing `.env` and restart Runway, then choose **Gemini**. The key is a Google Cloud API key allowed to call the Vertex AI API in that project. `config/gemini.yaml` sets `google/gemini-3.8-flash` and Vertex AI's OpenAI-compatible endpoint; the key is sent in the `x-goog-api-key` header, because Vertex rejects it as a Bearer token. Gemini uses the same pseudonymisation, tool checks and approval gates. Both keys can coexist in `.env`; never commit that file.
- **Ollama (local)**: install Ollama (https://ollama.com), run `ollama serve` and pull a model that supports tool calls, for example `ollama pull llama3.1`, then choose **Ollama (local)**. No key is needed and nothing leaves the machine. `config/ollama.yaml` sets the address (`http://localhost:11434/v1`, Ollama's OpenAI-compatible API) and the model; the same tools, pseudonymisation, rules and approvals apply. If Ollama is not running, the turn falls back to the offline planner and says so.

### Chatting with the copilot

The conversation reads like a chat app: your message on the right, "Thinking..." right under it, then the answer, with the input disabled meanwhile so nothing is sent twice. Under each answer: a button per invoice it names, **Open <page>** for every app page it names (the copilot knows the pages and says where to go, for example **Payees** to verify a payee), **Copy** (the whole answer as plain text, and each id, invoice number, IBAN and name in it) and, on the latest answer, **Try again**, which asks the same question afresh through the same rules. "What the agent did" lists each step in words: done, waiting for approval or blocked, and why.

On **Approvals**, each request tells its story: who prepared it, who approved, whose turn it is, who rejected it, and what happened in the end; the table says where each one stands ("Waiting for accountant", "Rejected by owner", "Done").

### Review team: several agents check a decision

Open **Review team** in the sidebar and tick the agents that should check approval requests: the offline rules reviewer (it re-runs the rules engine as if every required person had approved), DeepSeek, Gemini and any local Ollama model listed in `config/review_team.yaml` (add your own). On an approval card, **Ask the review team** asks every ticked agent at once; each answers approve, concerns or reject with one sentence, shown on the card and audited as `team_review`. The team advises and never approves: the executor and the two-person rule do not read its verdicts. If any agent rejects, Approve also needs the tick "I read the review team's objections", audited as `team_objection_overridden`. Remote reviewers see placeholders instead of names, like the chat; local ones see the real text. Changing the team needs sign-in and is audited (`set_review_team`).

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `RUNWAY_DB` | `var/runway.db` | SQLite database file |
| `RUNWAY_COUNTRY` | `eu` | Country pack (`config/<code>.yaml`). Display only: the ledger and the bank stay in the account currency (EUR) |
| `RUNWAY_VAR_DIR` | `var` | Output folder for test-inbox `.eml` files and application PDFs |
| `RUNWAY_OWNER_EMAIL` | `owner@lumen-packaging.example` | Recipient of fraud alerts |
| `RUNWAY_BANK_URL` | unset (in-process) | URL of a running mock bank |
| `RUNWAY_BANK_EXECUTOR_TOKEN` | random, kept in `var/secrets/` | Bank token held by the tool executor. Unset, a random token is made on first use and shared by processes on the same `var/` folder; set it when the bank runs on another machine |
| `RUNWAY_BANK_HUMAN_TOKEN` | random, kept in `var/secrets/` | Bank token for human-only actions (payees, clock), used by the UI and by the executor once owner and accountant approved a payee verification or a clock advance. Verifying a payee also needs their signed mandate, which the bank checks itself |
| `RUNWAY_OWNER_PIN` | `1111` | UI sign-in PIN of the owner role (demo-grade) |
| `RUNWAY_ACCOUNTANT_PIN` | `2222` | UI sign-in PIN of the accountant role (demo-grade) |
| `DEEPSEEK_API_KEY` | unset | LLM key (variable name set by `api_key_env` in `config/llm.yaml`) |
| `GEMINI_API_KEY` | unset | Google Cloud API key for Gemini mode (Vertex AI) |
| `GOOGLE_CLOUD_PROJECT` | unset | Google Cloud project id or number for Gemini mode |
| `RUNWAY_LLM` | unset | Startup mode: `offline`, `deepseek`, `gemini` or `ollama`; the sidebar overrides it per session |
| `RUNWAY_LLM_MODEL`, `RUNWAY_LLM_BASE_URL` | from `config/llm.yaml` | Override model and endpoint |
| `RUNWAY_LLM_THINKING` | unset | `on` enables DeepSeek thinking mode (not recommended) |
| `RUNWAY_SAMPLE_DIR` | `data/sample` | UI sample-data folder (generated CSVs and demo documents) |
| `RUNWAY_ANCHOR` | `notary` | Where the audit head is published: `notary` (a file next to the database) or `evm` (any EVM JSON-RPC chain) |
| `RUNWAY_AUTO_ANCHOR` | `every` for `notary`, `high-risk` for `evm` | When the head is published automatically: after `every` audit entry, only after `high-risk` actions, or `off` |
| `RUNWAY_NOTARY_PATH` | `<db>.notary.jsonl` | Notary file (default: the database path with `.notary.jsonl` as suffix) |
| `RUNWAY_EVM_RPC_URL` | unset | JSON-RPC URL of the chain, for `RUNWAY_ANCHOR=evm`. Never commit a URL that contains an API key |
| `RUNWAY_EVM_PRIVATE_KEY` | unset | Funded key that sends the anchor transactions, for `RUNWAY_ANCHOR=evm`. Never commit it |

Details and switching providers or country packs: [docs/deployment.md](docs/deployment.md).

### Anchoring the audit log on a blockchain

The hash chain detects edits by anyone who cannot also rewrite the stored head. Someone with write access to the SQLite file can recompute every hash. Publishing the head outside the database closes that gap: **Publish head now** on the Audit log page (or `python -m audit.anchor publish`) writes `runway-audit:v1:<count>:<head hash>` to the backend and stores a receipt; **Verify against published heads** (or `python -m audit.anchor verify`) recomputes the chain and compares it with every published head.

The head is also published automatically. With the notary file (the default) it is published after every audit entry, so neither a rewritten nor a deleted entry goes unnoticed; these publishes leave a receipt but no audit entry of their own, so the log does not double. On a chain each publish is a paid transaction, so the default there is `high-risk`: the head is published right after any executed action that moves money or lifts a control (`schedule_payment`, T3 and T4 tools, and the two-person actions such as verifying a payee or switching the kill switch off), and after a demo reset, sample load or import. Entries after the last published head, such as reminders and reads, are then only as safe as the database until the next publish. If publishing fails, the action stands and the failure is audited.

With `RUNWAY_ANCHOR=evm` the payload is the calldata of a zero-value transaction from the anchor account to itself, so it works on any EVM chain without a smart contract (tested against a fake JSON-RPC node that checks real signatures, and run on a local Anvil chain: see `docs/execution_logs/anchor_run.md`; a testnet such as Sepolia works the same way). Publishing waits until the transaction is mined and successful, so a receipt always means the head is on chain. Install Anvil with `brew install foundry` (or `foundryup`), then:

```bash
RUNWAY_ANCHOR=evm RUNWAY_EVM_RPC_URL=http://127.0.0.1:8545 RUNWAY_EVM_PRIVATE_KEY=<funded key> \
  .venv/bin/python -m audit.anchor publish     # prints the receipt with the transaction hash
RUNWAY_ANCHOR=evm RUNWAY_EVM_RPC_URL=http://127.0.0.1:8545 RUNWAY_EVM_PRIVATE_KEY=<funded key> \
  .venv/bin/python -m audit.anchor verify      # exit code 0 only when the status is "verified"
```

`scripts/run_anchor_demo.py` reproduces the checked-in evidence (with `anvil` running and the same two variables set): it builds the demo workspace, runs the Monday brief, publishes and verifies the head, and runs the forged-history attack on a copy against the on-chain head.

### Signed approvals (verifiable mandate)

Every human approval is an Ed25519 signature by the approving role's key (`var/keys/<role>.pem`, created on first use, readable only by the account running Runway) over the approval request: company, request id, tool, arguments, currency and required roles. The public key and the signature go into the approval record and the audit log. A financing application PDF carries the whole signed mandate as a table, a QR code, a JSON block and PDF metadata. A bank that holds the company's registered public keys checks it without trusting Runway:

```bash
.venv/bin/python -m agent.mandate keys > keys.json                    # the public keys to register with the bank
.venv/bin/python -m agent.mandate verify var/applications/FIN-0001.pdf --registered keys.json
.venv/bin/python -m agent.mandate verify docs/execution_logs/FIN-0001.pdf --registered docs/execution_logs/mandate_keys.json
```

The check fails if the request was changed after signing, a required role did not sign, two roles used one key or one approver, or a key is not the registered one (attack A9: an approval faked directly in the database).

## Tests

```bash
.venv/bin/pytest                 # 703 tests (1 skips where symlinks are not allowed), no network
.venv/bin/pytest tests/security  # attack scenarios A1 to A10 and the red-team console
.venv/bin/python scripts/run_tasks.py --mode offline   # judge task pack; --mode deepseek for a live run
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
audit/       hash-chained append-only audit log, verify_chain(); anchor.py publishes the head (notary file or EVM chain)
bank/        mock bank: FastAPI app, service logic, client
agent/       tool executor (executor.py), signed approvals (mandate.py), tools, financing, outbox, PDF, documents (safe ingestion and scanner), orchestrator, prompts
llm/         OpenAI-compatible adapter, offline planner, pseudonymizer
redteam/     red-team console: attack scenarios run through the real code paths (console.py), insider audit-log tampering (tamper.py)
ui/          Streamlit UI (app.py), PIN sign-in (auth.py), safe rendering of model output (render.py), helpers.py
scripts/     run_demo.py execution-evidence runner, run_tasks.py judge task pack runner (both write docs/execution_logs/)
tasks/       judge_tasks.yaml: judge-style scripted tasks with expectations
tests/       unit and integration tests; tests/security/ holds attack tests A1 to A10 and the red-team console tests
docs/        specification, contracts, architecture, algorithms, security, deployment, testing, glossary, demo script, execution logs
```

## Documentation

| Document | Content |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Components, data model, request flow, approval flow, the five design rules and where the code enforces them |
| [docs/algorithms.md](docs/algorithms.md) | Data generator, forecast, financing need and true cost, rules-engine order, audit hash chain, pseudonymization, safe ingestion and injection scanning, CSV import, worked examples |
| [docs/security-self-assessment.md](docs/security-self-assessment.md) | Threat model, permission tiers T0 to T4, defense in depth, attacks A1 to A10, known risks, response to the 2026-10-16 review |
| [docs/deployment.md](docs/deployment.md) | Setup, services, environment variables, switching LLM provider and country pack, resetting data, recording the demo |
| [docs/testing.md](docs/testing.md) | Test inventory and what each suite proves |
| [docs/glossary.md](docs/glossary.md) | Finance terms and Runway terms |
| [docs/SPEC.md](docs/SPEC.md), [docs/CONTRACTS.md](docs/CONTRACTS.md) | Product spec and module contracts |
| [docs/execution_logs/](docs/execution_logs/) | Output of `scripts/run_demo.py` (45 audit entries on seed 42; `FIN-0001.pdf` with the signed mandate and `mandate_keys.json` with the public keys to check it against), of `scripts/run_tasks.py` (`task_report_<mode>.md`, `task_traces_<mode>.jsonl`), and `audit_log_ui_export.csv` (the audit log exported from the UI after the recorded demo, 89 entries) |

## Limits to know about

The demo uses a simulated clock (default 2026-10-19), a single SQLite file, and PIN sign-in (per-role PINs from environment variables, not real authentication; one person holding both PINs can open two browsers and approve both roles). Verifying a payee, clearing a flag, releasing a supplier, turning the kill switch off and advancing the clock need both roles; the mock bank checks both signatures itself only for payee verification. Reset and import archive the old database to var/archive/ and link the new audit log to it. Attack tests run against the offline planner; the judge task pack (21 tasks) passes 19 of 19 offline and passed 21 of 21 on live DeepSeek, outside the test suite (that run also found and led to fixing a placeholder leak; see risk 8). Keyword detection of hostile documents can be evaded. Importing only the four basic CSVs does not reproduce customer lateness or already-scheduled payments, so the forecast looks better than it is: for the sample company the lowest balance becomes -9,494.98 on 25 December instead of -29,303.32 on 26 November. An optional paid-invoices file (not offered in the UI) restores the lateness; scheduled payments cannot be imported, because imported payees are unverified. The full list is in section 7 of the security self-assessment.

## Dependencies

Runway is written in Python 3.11+ and uses the following open-source libraries, declared in `pyproject.toml` (exact versions are pinned in `uv.lock`). Each library is the work of its own authors and is distributed under its own license, shown here; none of them is covered by the Runway license below.

| Library | Locked version | License | Used for |
|---|---|---|---|
| [FastAPI](https://github.com/fastapi/fastapi) | 0.143.0 | MIT | Mock bank HTTP API (`bank/app.py`) |
| [Uvicorn](https://github.com/encode/uvicorn) | 0.54.0 | BSD-3-Clause | ASGI server for the mock bank |
| [SQLModel](https://github.com/fastapi/sqlmodel) | 0.0.48 | MIT | SQLite schema and data access (`common/db.py`) |
| [Pydantic](https://github.com/pydantic/pydantic) | 2.14.0 | MIT | Data validation for tools, contracts and the bank API |
| [PyYAML](https://github.com/yaml/pyyaml) | 6.0.3 | MIT | Rules, country packs, provider and task configuration |
| [HTTPX](https://github.com/encode/httpx) | 0.28.1 | BSD-3-Clause | Bank client and EVM JSON-RPC calls |
| [OpenAI Python SDK](https://github.com/openai/openai-python) | 3.28.0 | Apache-2.0 | OpenAI-compatible client for DeepSeek, Gemini (Vertex AI) and Ollama |
| [Streamlit](https://github.com/streamlit/streamlit) | 1.65.0 | Apache-2.0 | User interface (`ui/app.py`) |
| [ReportLab](https://www.reportlab.com/opensource/) | 5.0.1 | BSD-3-Clause | Financing application and invoice PDFs, mandate QR codes |
| [pypdf](https://github.com/py-pdf/pypdf) | 6.20.0 | BSD-3-Clause | Reading uploaded PDF documents and embedded mandates |
| [pandas](https://github.com/pandas-dev/pandas) | 3.0.6 | BSD-3-Clause | CSV import and tabular display |
| [eth-account](https://github.com/ethereum/eth-account) | 0.14.0 | MIT | Signing audit-anchor transactions for EVM chains |
| [cryptography](https://github.com/pyca/cryptography) | 50.0.2 | Apache-2.0 OR BSD-3-Clause | Ed25519 keys and signatures for approvals |
| [pytest](https://github.com/pytest-dev/pytest) (dev) | 9.1.1 | MIT | Test suite |

The code also imports a few libraries that come in as dependencies of the ones above: [SQLAlchemy](https://github.com/sqlalchemy/sqlalchemy) 2.1.4 (MIT, via SQLModel), [Altair](https://github.com/vega/altair) 6.3.0 (BSD-3-Clause, via Streamlit, for charts), and [eth-utils](https://github.com/ethereum/eth-utils) 6.0.0 and [rlp](https://github.com/ethereum/pyrlp) 5.0.0 (MIT, via eth-account). The other transitive packages are listed in `uv.lock`.

Optional external tools, which are not bundled or redistributed: [uv](https://github.com/astral-sh/uv) for installing, [Ollama](https://ollama.com) for local models, and [Anvil](https://github.com/foundry-rs/foundry) (Foundry) for the local EVM chain. The DeepSeek and Google Vertex AI services are used only when you set your own API keys, under those providers' terms.

## License and copyright

Copyright © 2026 Voicu Delia-Andreea, Georgescu Gabriel and Tomescu Vlad-Costin. All rights reserved.

Authors: Voicu Delia-Andreea, Georgescu Gabriel, Tomescu Vlad-Costin.

This applies to the Runway source code, documentation, configuration, generated sample data and other materials in this repository, except the third-party libraries listed under [Dependencies](#dependencies), which keep their own licenses.

- **No commercial use.** You may not use, copy, modify, distribute, sublicense or sell this software or any part of it, or use it in a product, service or other activity meant for commercial advantage or money, without the copyright holders' prior written permission.
- **Non-commercial use.** You may view, run and modify the software for personal, educational, research or evaluation purposes, including review for the 2026 Shenzhen International FinTech Competition.
- **Keep the notice.** Every copy, modified version or derivative work, in whole or in part, must keep the copyright notice above and this license (the [LICENSE](LICENSE) file) unchanged, and must state clearly that it was changed from the original.
- **No warranty.** The software is provided "as is", without warranty of any kind, express or implied. In no event shall the copyright holders be liable for any claim, damages or other liability arising from the software or its use. Runway is a competition prototype using fictional data and is not financial, legal or tax advice.

Any rights not expressly granted here are reserved. For permission to use Runway commercially, contact the copyright holders. The full text is in [LICENSE](LICENSE).
