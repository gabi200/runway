"""Load a company's own CSV exports into a fresh Runway database.

Reads the same four files that ``data/generate.py`` writes (trial balance, bank
statement, open receivables, open payables), validates them and rebuilds the
ledger so the 13-week forecast can run on the imported data.

Honest limits of a CSV import (a real integration would read these from the bank
and ledger APIs):
* Paid-invoice history and scheduled payments are not in the four basic files.
  Optionally pass ``paid_invoices_csv`` (number, counterparty, due_date, paid_date,
  amount) and ``scheduled_payments_csv`` (counterparty, amount, execute_date) to
  keep customer lateness and already-scheduled payments. Without them, paid
  customer invoices are rebuilt from "Payment from <customer> <number>" bank
  lines with the due date assumed equal to the payment date, so customer
  lateness starts at zero and is learned only from later data.
* Payees created here are always ``verified=False`` with no IBAN: only a human
  verifies a payee (Payees page), never an import.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from typing import Union

from sqlmodel import Session, SQLModel

from common.config import load_country_pack
from common.db import (
    Account, Counterparty, Invoice, Payee, Payment, Setting, Transaction, get_engine,
)

ACCOUNT = "ACC-MAIN"
TB_COLS = ("account_code", "account_name", "debit", "credit")
BANK_COLS = ("date", "description", "amount")
OPEN_COLS = ("number", "counterparty", "issue_date", "due_date", "amount")
CATEGORIES = {"sales_receipt", "supplier_payment", "payroll", "rent", "utilities", "vat",
              "loan_repayment", "financing", "other"}
Source = Union[bytes, str, Path]


class ImportErrorReport(ValueError):
    """Raised when an uploaded file fails validation. The message lists every problem found."""


def _text(src: Source) -> str:
    if isinstance(src, bytes):
        return src.decode("utf-8-sig")
    if isinstance(src, Path):
        return src.read_text(encoding="utf-8-sig")
    return src  # a str is CSV text, never a path


def _rows(src: Source, name: str, required: tuple[str, ...]) -> list[dict]:
    reader = csv.DictReader(io.StringIO(_text(src)))
    cols = [c.strip() for c in (reader.fieldnames or [])]
    missing = [c for c in required if c not in cols]
    if missing:
        raise ImportErrorReport(f"{name}: missing required column(s): {', '.join(missing)}")
    return [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in reader]


def _cents(v: str, where: str) -> int:
    if v == "":
        return 0
    try:
        return int((Decimal(v.replace(",", "")) * 100).quantize(Decimal(1)))
    except InvalidOperation:
        raise ImportErrorReport(f"{where}: {v!r} is not a number") from None


def _date(v: str, where: str) -> date:
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise ImportErrorReport(f"{where}: {v!r} is not an ISO date (YYYY-MM-DD)") from None


def _slug(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "-", name.upper()).strip("-")[:30] or "UNKNOWN"


def _canonical() -> dict[str, tuple]:
    from data.generate import COUNTERPARTIES
    return {c[0]: c for c in COUNTERPARTIES}


def import_files(target: Union[Session, str, Path, None], trial_balance_csv: Source,
                 bank_statement_csv: Source, open_receivables_csv: Source | None = None,
                 open_payables_csv: Source | None = None, *,
                 paid_invoices_csv: Source | None = None,
                 scheduled_payments_csv: Source | None = None, company: str = "Imported company",
                 country: str = "eu", as_of: date | None = None) -> dict:
    """Validate the files, then replace the database content with the imported company.

    ``target`` is a Session or a SQLite path (None = default DB). Nothing is written
    unless every file validates. Returns a summary dict (counts, opening balance,
    today, warnings).
    """
    pack = load_country_pack(country)
    warnings: list[str] = []

    # ---- validate everything before touching the database ------------------ #
    tb = _rows(trial_balance_csv, "trial balance", TB_COLS)
    if not tb:
        raise ImportErrorReport("trial balance: no rows")
    debit = sum(_cents(r["debit"], f"trial balance {r['account_code']} debit") for r in tb)
    credit = sum(_cents(r["credit"], f"trial balance {r['account_code']} credit") for r in tb)
    if debit != credit:
        raise ImportErrorReport(f"trial balance is not balanced: debits {debit / 100:,.2f} "
                                f"!= credits {credit / 100:,.2f}")
    cash_cents = next((_cents(r["debit"], "cash") - _cents(r["credit"], "cash") for r in tb
                       if r["account_code"] == "1000" or "cash" in r["account_name"].lower()), None)

    bank = _rows(bank_statement_csv, "bank statement", BANK_COLS)
    txs, seen = [], set()
    for n, r in enumerate(bank, 1):
        tid = r.get("id") or f"TX-{n:06d}"
        if tid in seen:
            raise ImportErrorReport(f"bank statement: duplicate id {tid}")
        seen.add(tid)
        cat = r.get("category") or "other"
        txs.append(dict(id=tid, date=_date(r["date"], f"bank statement {tid} date"),
                        amount=_cents(r["amount"], f"bank statement {tid} amount"),
                        desc=r["description"], cat=cat if cat in CATEGORIES else "other",
                        currency=r.get("currency") or pack.currency))
    txs.sort(key=lambda t: (t["date"], t["id"]))
    currency = txs[0]["currency"] if txs else pack.currency
    if any(t["currency"] != currency for t in txs):
        raise ImportErrorReport("bank statement mixes currencies; import one account at a time")

    opens: list[dict] = []
    for direction, src, name in (("AR", open_receivables_csv, "open receivables"),
                                 ("AP", open_payables_csv, "open payables")):
        if src is None:
            continue
        for n, r in enumerate(_rows(src, name, OPEN_COLS), 1):
            where = f"{name} row {n}"
            gross = _cents(r["amount"], f"{where} amount")
            if gross <= 0:
                raise ImportErrorReport(f"{where}: amount must be positive")
            vat = _cents(r["vat"], f"{where} vat") if r.get("vat") else round(gross * pack.vat_rate / (1 + pack.vat_rate))
            overdue = int(r["days_overdue"]) if r.get("days_overdue", "").isdigit() else 0
            opens.append(dict(direction=direction, id=r.get("invoice_id") or "", number=r["number"],
                              cp_name=r["counterparty"], cp_id=r.get("counterparty_id") or "",
                              issue=_date(r["issue_date"], f"{where} issue_date"),
                              due=_date(r["due_date"], f"{where} due_date"), gross=gross, vat=vat,
                              overdue=overdue))
    hist = []
    if paid_invoices_csv is not None:
        for n, r in enumerate(_rows(paid_invoices_csv, "paid invoices",
                                    ("number", "counterparty", "due_date", "paid_date", "amount")), 1):
            where = f"paid invoices row {n}"
            hist.append(dict(direction=r.get("direction") or "AR", number=r["number"], cp_name=r["counterparty"],
                             cp_id=r.get("counterparty_id") or "",
                             due=_date(r["due_date"], f"{where} due_date"),
                             issue=_date(r["issue_date"], f"{where} issue_date") if r.get("issue_date") else None,
                             paid=_date(r["paid_date"], f"{where} paid_date"),
                             gross=_cents(r["amount"], f"{where} amount")))
    sched = []
    if scheduled_payments_csv is not None:
        for n, r in enumerate(_rows(scheduled_payments_csv, "scheduled payments",
                                    ("counterparty", "amount", "execute_date")), 1):
            where = f"scheduled payments row {n}"
            sched.append(dict(cp_name=r["counterparty"], invoice=r.get("invoice_number", ""),
                              amount=_cents(r["amount"], f"{where} amount"),
                              execute=_date(r["execute_date"], f"{where} execute_date")))
    ids = [o["id"] for o in opens if o["id"]]
    if len(ids) != len(set(ids)):
        raise ImportErrorReport("open items: duplicate invoice_id")

    if as_of is None:
        implied = {o["due"] + timedelta(days=o["overdue"]) for o in opens if o["overdue"] > 0}
        if len(implied) > 1:
            raise ImportErrorReport("open items: days_overdue values imply different 'today' dates")
        as_of = implied.pop() if implied else (txs[-1]["date"] + timedelta(days=1) if txs else date.today())

    opening = None
    if cash_cents is not None:
        opening = cash_cents - sum(t["amount"] for t in txs)
        if bank and bank[-1].get("balance"):
            last = _cents(bank[-1]["balance"], "bank statement balance")
            if last != cash_cents:
                warnings.append(f"statement ending balance {last / 100:,.2f} differs from trial balance "
                                f"cash {cash_cents / 100:,.2f}; using the trial balance")
    elif bank and bank[0].get("balance"):
        opening = _cents(bank[0]["balance"], "bank statement balance") - txs[0]["amount"]
    if opening is None:
        raise ImportErrorReport("cannot determine opening cash: no cash account in the trial balance "
                                "and no balance column in the bank statement")

    # ---- counterparties ------------------------------------------------------ #
    canon = _canonical()
    cps: dict[str, dict] = {}
    for o in opens:
        cid = o["cp_id"] or next((k for k, c in canon.items() if c[1] == o["cp_name"]), "") \
            or ("CUST-" if o["direction"] == "AR" else "SUP-") + _slug(o["cp_name"])
        o["cp_id"] = cid
        entry = cps.setdefault(cid, dict(name=o["cp_name"], direction=o["direction"], terms=[]))
        entry["terms"].append((o["due"] - o["issue"]).days)
    for h in hist:
        h["cp_id"] = h["cp_id"] or next((k for k, c in canon.items() if c[1] == h["cp_name"]), "") \
            or next((k for k, c in cps.items() if c["name"] == h["cp_name"]), "") \
            or ("CUST-" if h["direction"] == "AR" else "SUP-") + _slug(h["cp_name"])
        cps.setdefault(h["cp_id"], dict(name=h["cp_name"], direction=h["direction"], terms=[]))
    for sp in sched:
        sp["cp_id"] = next((k for k, c in cps.items() if c["name"] == sp["cp_name"] or k == sp["cp_name"]), "") \
            or "SUP-" + _slug(sp["cp_name"])
        cps.setdefault(sp["cp_id"], dict(name=sp["cp_name"], direction="AP", terms=[]))
    by_name = {c["name"]: cid for cid, c in cps.items()}

    # ---- paid customer invoices rebuilt from receipts ------------------------- #
    paid: list[dict] = []
    hist_numbers = {h["number"] for h in hist}
    for t in txs:
        if t["cat"] != "sales_receipt" or t["amount"] <= 0:
            continue
        m = re.match(r"Payment from (.+) (\S+)$", t["desc"])
        cid = by_name.get(m.group(1)) if m else None
        if not cid:
            continue
        t["cp"] = cid
        if m.group(2) in hist_numbers:
            continue
        paid.append(dict(tx=t, number=m.group(2), cp=cid))
    for t in txs:
        if "cp" not in t:
            hit = next((cid for nm, cid in by_name.items() if nm in t["desc"]), None)
            t["cp"] = hit

    # ---- write ----------------------------------------------------------------- #
    if isinstance(target, Session):
        session, eng, own = target, target.get_bind(), False
    else:
        eng = get_engine(target)
        session, own = Session(eng), True
    try:
        SQLModel.metadata.drop_all(eng)
        SQLModel.metadata.create_all(eng)
        session.add(Account(id=ACCOUNT, name=f"{company} main account", currency=currency,
                            opening_balance_cents=opening))
        for cid, c in cps.items():
            base = canon.get(cid)
            terms = base[3] if base else (round(median(c["terms"])) if c["terms"] else
                                          (pack.default_customer_terms_days if c["direction"] == "AR"
                                           else pack.default_supplier_terms_days))
            session.add(Counterparty(id=cid, name=c["name"], kind=base[2] if base else
                                     ("customer" if c["direction"] == "AR" else "supplier"),
                                     payment_terms_days=terms, email=base[4] if base else None))
        session.commit()
        for cid, c in cps.items():
            if c["direction"] == "AP":
                session.add(Payee(id=f"PAYEE-{cid.removeprefix('SUP-')}", name=c["name"],
                                  counterparty_id=cid, iban="", verified=False, created_by="import"))
        n_ar = n_ap = 0
        for o in opens:
            n_ar, n_ap = (n_ar + 1, n_ap) if o["direction"] == "AR" else (n_ar, n_ap + 1)
            iid = o["id"] or f"{o['direction']}-{(1000 if o['direction'] == 'AR' else 2000) + (n_ar if o['direction'] == 'AR' else n_ap)}"
            o["iid"] = iid
            session.add(Invoice(id=iid, number=o["number"], direction=o["direction"],
                                counterparty_id=o["cp_id"], issue_date=o["issue"], due_date=o["due"],
                                amount_cents=o["gross"], vat_cents=o["vat"], currency=currency))
        session.commit()
        by_number = {}
        for k, h in enumerate(hist, 1):
            iid = f"{h['direction']}-H{k:05d}"
            by_number[h["number"]] = iid
            session.add(Invoice(id=iid, number=h["number"], direction=h["direction"],
                                counterparty_id=h["cp_id"], issue_date=h["issue"] or h["due"], due_date=h["due"],
                                amount_cents=h["gross"],
                                vat_cents=round(h["gross"] * pack.vat_rate / (1 + pack.vat_rate)),
                                currency=currency, status="paid", paid_date=h["paid"]))
        session.commit()
        for k, p in enumerate(paid, 1):
            t = p["tx"]
            iid = f"AR-H{k:05d}"
            session.add(Invoice(id=iid, number=p["number"], direction="AR", counterparty_id=p["cp"],
                                issue_date=t["date"], due_date=t["date"], amount_cents=t["amount"],
                                vat_cents=round(t["amount"] * pack.vat_rate / (1 + pack.vat_rate)),
                                currency=currency, status="paid", paid_date=t["date"]))
            t["inv"] = iid
        for t in txs:
            m = re.match(r"Payment (?:from|to) .+ (\S+)$", t["desc"])
            if m and m.group(1) in by_number:
                t["inv"] = by_number[m.group(1)]
        session.commit()
        for t in txs:
            session.add(Transaction(id=t["id"], account_id=ACCOUNT, booking_date=t["date"],
                                    amount_cents=t["amount"], currency=currency, description=t["desc"],
                                    category=t["cat"], counterparty_id=t.get("cp"),
                                    invoice_id=t.get("inv")))
        payee_of = {c: f"PAYEE-{c.removeprefix('SUP-')}" for c, v in cps.items() if v["direction"] == "AP"}
        for k, sp in enumerate(sched, 1):
            pid = payee_of[sp["cp_id"]]
            inv = next((i for i in opens if i["number"] == sp["invoice"]), None) if sp["invoice"] else None
            session.add(Payment(id=f"PAY-{k:04d}", idempotency_key=f"import-pay-{k:04d}", account_id=ACCOUNT,
                                payee_id=pid, amount_cents=sp["amount"], currency=currency,
                                execute_date=sp["execute"], status="scheduled", automatic=False,
                                invoice_id=inv["iid"] if inv else None, created_by="import",
                                created_sim_date=as_of))
        session.commit()
        for k, v in {"today": as_of.isoformat(), "kill_switch": "off", "country": country}.items():
            session.add(Setting(key=k, value=v))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        if own:
            session.close()
    return dict(transactions=len(txs), open_receivables=n_ar, open_payables=n_ap,
                counterparties=len(cps), paid_history_rebuilt=len(paid),
                opening_balance_cents=opening, today=as_of.isoformat(), warnings=warnings)
