# Glossary

Finance terms first, then terms specific to Runway. Examples use the demo company, Lumen Packaging, which makes cardboard packaging.

## Finance terms

| Term | Plain meaning | Example in the demo |
|---|---|---|
| Cash flow | Money actually moving in and out of the bank account, and when. A profitable company can still run out of cash. | Sales look good, but the money arrives 75 days after the invoice and wages are due sooner. |
| Cash-flow forecast | A prediction of the bank balance week by week. The standard horizon is 13 weeks (one quarter). | "Week 6 closes at -18,303.32; the balance is lowest at -29,303.32 on 2026-11-26." |
| Shortfall / cash gap | A week where the forecast balance goes below zero. | Week 6 and week 10 in the default data. |
| Invoice | A bill a company sends to a customer, or receives from a supplier. | Invoice `AR-1101`, 95,000, due 2026-12-26. |
| Accounts receivable (AR) | Invoices we sent that customers have not paid yet. Money owed to us. | Brightfield owes us 95,000 + 90,000 + 48,000. |
| Accounts payable (AP) | Invoices we received that we have not paid yet. Money we owe. | We owe Cartonia Mills 60,000. |
| Payment terms ("net 30", "net 75") | The number of days after the invoice date by which payment is due. | Brightfield is net 75, small customers net 30, Cartonia net 60. |
| Overdue | Past the due date and still unpaid. | `AR-1091` and `AR-1092` trigger reminder e-mails. |
| Dunning / payment reminder | Polite, escalating reminders to pay an overdue invoice. | Runway escalates by earlier reminders and days overdue: friendly, then firm (more than 7 days overdue, or a second reminder), then final notice (more than 14 days overdue with at least one earlier reminder, or a third). A first reminder is never a final notice. |
| Rescheduling a payment | Paying a supplier later but still within the agreed terms: never after the due date, and never earlier than planned. | Move the Inkworks payment from 11-26 to its due date 12-14. |
| Lateness (average) | How many days after the due date a customer typically pays, measured from past paid invoices. | Brightfield pays about 5 days late; the forecast shifts its invoices by 5 days. |
| Payroll | Employees' salaries. Must never be late. | About 55,000 on the 25th. |
| VAT | Value-added tax: sales tax collected from customers and paid to the state periodically. Rates differ by country. | Paid on the 25th (EU pack, 20%) or the 15th (CN pack, 13%). Rates are illustrative. |
| Trial balance | A list of every account in the books with its total, used to check that the books add up. | The onboarding input file `data/sample/trial_balance.csv`. |
| Chart of accounts | The list of account categories a company uses (cash, receivables, payables, salaries, ...). | Simplified codes 1000 cash, 1100 receivables, 2000 payables, 4000 sales. |
| Ledger / general ledger | The full record of every transaction, organised by account. The source of truth for every number. | Tables `invoice` and `transaction`. |
| IBAN | International Bank Account Number: where a payment goes. | Changing a supplier's IBAN is a classic fraud trick. |
| Payee | Someone we pay (supplier, landlord, tax office). Must be verified before the agent can schedule a payment to them. | `PAYEE-CARTONIA` is verified; `PAYEE-NEWVENDOR` is not. |
| Verified payee | A payee a human has checked and marked `verified`. | The bank refuses payments to unverified payees. |
| Overdraft | Permission to go below zero on the account up to a limit, paying interest only on what is used. Quick but costly. | Limit 50,000, 12% a year, 200 fee. |
| Invoice financing (factoring) | A financier advances most of an unpaid invoice now, for a fee, and is repaid when the customer pays. | Advance 80% of an invoice at 1.5% per 30 days. |
| Supply-chain finance | Like invoice financing, but the large customer's bank backs it, so it is cheaper. The customer must confirm the invoice. | Brightfield programme, 0.6% per 30 days. |
| Term loan | Borrow a fixed amount and repay over months with interest. | The 6-month loan at 9% a year plus 1% setup. |
| State-guaranteed SME loan | A bank loan partly guaranteed by the government. Cheap per year but slow to approve, and with no prepayment the interest runs for the whole term. | 6% a year plus 1% guarantee fee, 21 days to cash. With today 2026-10-19 it arrives 2026-11-09, in time for the 2026-11-25 need, but costs 1,260.00 on 18,000 against 216.00 for supply-chain finance, so it ranks last. |
| Effective / true cost | What financing really costs once interest and all fees are counted, for the actual amount and number of days. | 216.00 for 18,000 over 36 days with supply-chain finance (two started 30-day periods at 0.6%). |
| Advance rate | The share of an invoice's value that a financier pays up front. | 80%. |
| BEC (business email compromise) | Fraud where an attacker pretends to be a supplier or the boss and asks for payment to a new account. | Attack A1 and A2. |
| Prompt injection | Text inside data (a PDF, an e-mail) written to trick an AI into following it as if it were an instruction. | Hidden white text in the Cartonia invoice. |
| Audit trail | A record of who did what and when that cannot be quietly changed afterwards. | The hash-chained audit log. |
| Idempotent | Doing the same request twice has the same effect as doing it once. | A repeated payment request creates no second payment. |
| Pseudonymization | Replacing real names, e-mail addresses and numbers with placeholders and swapping them back afterwards. | `<SUPPLIER_2>`, `<PERSON_1>`, `<EMAIL_1>`, `<IBAN_3>`. |
| Median | The middle value of a list. Runway uses the median of the last three monthly totals for recurring costs, so one odd month does not move the estimate. | Payroll estimate 55,060.00 = median of 55,300 / 55,000 / 55,060. |

## Runway terms

| Term | Meaning |
|---|---|
| Agent | The AI part of Runway. It reads data, writes explanations and requests actions through tools. It cannot act outside the rules engine. |
| Offline planner | A deterministic stand-in for the language model (`llm/offline.py`), used when no API key is set. It runs the demo story without network access. |
| Tool | A single action the agent may request, for example `get_forecast` or `schedule_payment`. There are 16; none can change payees, limits or the kill switch. |
| Financing need | The amount, the date and the number of days of financing the forecast calls for, computed in code by `forecast.engine.financing_need` and returned by the tool `get_financing_need`. Amount = lowest running balance (inside the week, same-day payments before receipts) rounded up to a whole 1,000; needed from the first day below zero; term ends at the receipt that brings the balance back above zero (at least 30 days). | Demo: 30,000.00 for 36 days from 2026-11-25, or 18,000.00 if the Inkworks payment is moved. |
| Permission tier | The risk class of a tool: T0 read, T1 analyse, T2 low-risk automatic action, T3 needs human approval, T4 forbidden. Defined in `policy/rules.yaml`. See `docs/security-self-assessment.md`. |
| T0 / T1 / T2 / T3 / T4 | T0 reads data. T1 analyses (forecast, scenarios, financing comparison, drafts). T2 acts automatically within limits (payment reminders, small payments). T3 waits for human approval. T4 does not exist as a tool and is always denied. |
| Rules engine | `policy/engine.py`. Plain Python that returns ALLOW, NEEDS_APPROVAL (with required roles) or DENY for every tool call. |
| Decision | The rules engine's output: outcome, reason, tier, required roles. |
| Approval request | A tool call parked by the rules engine until a human approves or rejects it. Stored in `approvalrequest` (ids `APR-0001`...). |
| Owner / accountant | The two human roles. The owner approves spending. The accountant must also approve anything material (above 5,000). In the UI each role signs in with its own PIN. |
| Approver identity | Who gave an approval, as opposed to which role. The UI uses the browser session id. One identity cannot give both approvals of a four-eyes request, so the owner and the accountant must use two sessions. |
| PIN sign-in | The demo login of the UI: `RUNWAY_OWNER_PIN` and `RUNWAY_ACCOUNTANT_PIN` (defaults 1111 and 2222). It separates the roles for the demo; it is not production authentication. |
| Material amount | Above `accountant_above_cents` (5,000.00): requires both owner and accountant. |
| Automatic payment | A payment the agent may schedule without approval: payee verified, an open supplier invoice behind it, amount under 1,000, daily total to the payee under 1,000, daily total of all automatic payments under 3,000. |
| Daily cap | The per-payee and global daily limits on automatic payments. |
| Kill switch | A setting (`kill_switch = on`) that drops the agent to T0: only read tools are allowed until a human switches it off. |
| Tool executor | `agent/executor.py`. The only code that turns an allowed tool call into a side effect. It owns the audit calls and the approval workflow. |
| Executor token / human token | The two bearer tokens of the mock bank. The executor token is held only by the tool executor and has the scopes `read` and `payments`: it cannot create or verify payees or advance the clock. The human token also has `payees` and `simulate` and is used by the UI. |
| Idempotency key | A unique string attached to each payment request. The bank returns the original payment for a repeated key with the same body and rejects (409) a repeated key with a different body. |
| Flagged document | A document the scanner or the agent marked suspicious. Payments linked to it are denied until the owner and the accountant together clear it, or release one named supplier from it. |
| Untrusted data block | The delimiter format `<<<UNTRUSTED_DOCUMENT ... >>>` that wraps any document text shown to the model. |
| Hash chain | Each audit entry stores the hash of the previous one; changing any entry breaks every later hash. |
| Anchor | The stored head hash and entry count (`audit_head`, `audit_count` in `setting`). They detect deletion of the newest entries. |
| `verify_chain()` | Recomputes the whole chain and the anchors and reports the first bad entry. Shown in the UI audit page. |
| Simulated clock | The `today` setting (default 2026-10-19). The demo moves it with `POST /simulate/advance`. |
| Country pack | A YAML file in `config/` with currency, VAT rate, VAT day and a display FX factor. `eu` (EUR) and `cn` (CNY). Display only: the ledger and the bank stay in EUR and `CountryPack.display` formats amounts for people. |
| CSV import | `data/importer.py`: builds the database from a company's trial balance, bank statement and open items, optionally with paid-invoice history. Imported payees are unverified, so a scheduled-payments file is rejected. |
| Safe ingestion | Documents enter only through `ingest_text` (a string is data), `ingest_file` (path must be inside an allowed folder) or `ingest_document` (bytes or text); sender and filename are sanitised and scanned. |
| Mock bank | The FastAPI service in `bank/` that imitates an open-banking API. |
| Test inbox / outbox | Where "sent" e-mails land: table `outboxemail` and `var/outbox/*.eml`. Nothing leaves the machine. |
