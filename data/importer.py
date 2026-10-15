"""Load a company's own CSV exports into a fresh Runway database.

Reads the same four files that ``data/generate.py`` writes (trial balance, bank
statement, open receivables, open payables), validates them and rebuilds the
ledger so the 13-week forecast can run on the imported data.

Honest limits of a CSV import (a real integration would read these from the bank
and ledger APIs):
* Paid-invoice history is not in the four basic files. Optionally pass
  ``paid_invoices_csv`` (number, counterparty, due_date, paid_date, amount) to
  keep customer lateness. Scheduled-payment CSVs are validated and rejected:
  imported payees require human verification before any payment is scheduled.
  Without paid history, paid
  customer invoices are rebuilt from "Payment from <customer> <number>" bank
  lines with the due date assumed equal to the payment date, so customer
  lateness starts at zero and is learned only from later data.
* Payees created here are always ``verified=False`` with no IBAN: only a human
  verifies a payee (Payees page), never an import.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from typing import Union

from sqlmodel import Session, SQLModel, create_engine

from common.config import load_country_pack
from common.db import (
    Account, Counterparty, Invoice, Payee, Setting, Transaction, reset_engines, db_url,
)

ACCOUNT = "ACC-MAIN"
TB_COLS = ("account_code", "account_name", "debit", "credit")
BANK_COLS = ("date", "description", "amount")
OPEN_COLS = ("number", "counterparty", "issue_date", "due_date", "amount")
CATEGORIES = {"sales_receipt", "supplier_payment", "payroll", "rent", "utilities", "vat",
              "loan_repayment", "financing", "other"}
MAX_DAYS = 36500  # days_overdue limit (100 years)
THOUSANDS = re.compile(r"[+-]?[0-9]{1,3}(,[0-9]{3})+(\.[0-9]+)?")  # 1,234,567.89
CASH_NAMES = ("cash", "bank", "cash at bank", "cash in bank", "bank account", "bank current account")
Source = Union[bytes, str, Path]
LOCK_WAIT_SECONDS = 5.0  # how long the final copy waits for another writer to finish


class ImportErrorReport(ValueError):
    """Raised when an uploaded file fails validation. The message lists every problem found."""


def _stop_if_locked(status: int, remaining: int, total: int) -> None:
    # Connection.backup retries a locked destination forever; give up after LOCK_WAIT_SECONDS.
    if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        raise sqlite3.OperationalError("the database is locked by another connection; try again")


def _text(src: Source, name: str) -> str:
    try:
        if isinstance(src, bytes):
            return src.decode("utf-8-sig")
        if isinstance(src, Path):
            return src.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise ImportErrorReport(f"{name}: not UTF-8 text; save the file as CSV UTF-8") from None
    return src  # a str is CSV text, never a path


def _rows(src: Source, name: str, required: tuple[str, ...]) -> list[dict]:
    reader = csv.DictReader(io.StringIO(_text(src, name), newline=""))  # newline="": \n, \r\n or \r
    rows = []
    try:
        cols = [c.strip() for c in (reader.fieldnames or [])]
        missing = [c for c in required if c not in cols]
        if missing:
            raise ImportErrorReport(f"{name}: missing required column(s): {', '.join(missing)}")
        duplicate = sorted({c for c in cols if c and cols.count(c) > 1})
        if duplicate:
            raise ImportErrorReport(f"{name}: duplicate column(s): {', '.join(duplicate)}")
        for n, r in enumerate(reader, 1):
            if None in r:
                raise ImportErrorReport(f"{name} row {n}: more fields than the header has columns "
                                        "(quote values that contain commas)")
            rows.append({k.strip(): (v or "").strip() for k, v in r.items()})
    except csv.Error as exc:
        raise ImportErrorReport(f"{name} line {reader.reader.line_num}: {exc}") from None
    return rows


def _cents(v: str, where: str) -> int:
    if v == "":
        return 0
    if "," in v and not THOUSANDS.fullmatch(v):
        raise ImportErrorReport(f"{where}: {v!r} is not a number (use a dot for decimals; "
                                "a comma only between groups of thousands)")
    try:
        return int((Decimal(v.replace(",", "")) * 100).quantize(Decimal(1)))
    except (InvalidOperation, ValueError, OverflowError):
        raise ImportErrorReport(f"{where}: {v!r} is not a number") from None


def _date(v: str, where: str) -> date:
    try:
        d = date.fromisoformat(v)
    except ValueError:
        raise ImportErrorReport(f"{where}: {v!r} is not an ISO date (YYYY-MM-DD)") from None
    # ponytail: business dates only; this also keeps "+ days" arithmetic far from year 9999.
    if not 1900 <= d.year <= 2999:
        raise ImportErrorReport(f"{where}: {v!r} is outside the years 1900 to 2999")
    return d


def _days(v: str, where: str) -> int:
    if v == "":
        return 0
    if not re.fullmatch(r"[0-9]{1,5}", v) or int(v) > MAX_DAYS:
        raise ImportErrorReport(f"{where}: {v!r} is not a whole number of days from 0 to {MAX_DAYS}")
    return int(v)


def _slug(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "-", name.upper()).strip("-")[:30] or "UNKNOWN"


def _key(name: str) -> str:
    """Name for matching: "Acme Ltd" and "ACME LTD." are one counterparty."""
    return re.sub(r"[\W_]+", "", name.casefold())


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
    unless every file validates; writes use a temporary DB and atomic replacement.
    Scheduled payments are rejected because imported payees are unverified.
    Returns a summary dict (counts, opening balance,
    today, warnings).
    """
    if as_of is not None and type(as_of) is not date:
        raise ImportErrorReport("as_of must be a date")
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
    # ponytail: the bank account is code 1000 or a plain cash/bank name; anything else needs code 1000.
    cash_rows = [r for r in tb if r["account_code"] == "1000"] or [
        r for r in tb if r["account_name"].lower() in CASH_NAMES or r["account_name"].lower().startswith("cash at bank")]
    if len(cash_rows) > 1:
        raise ImportErrorReport("trial balance: cannot tell which account is the bank account ("
                                + ", ".join(f"{r['account_code']} {r['account_name']}" for r in cash_rows)
                                + "); give it account code 1000")
    cash_cents = _cents(cash_rows[0]["debit"], "cash") - _cents(cash_rows[0]["credit"], "cash") if cash_rows else None

    bank = _rows(bank_statement_csv, "bank statement", BANK_COLS)
    txs, seen = [], set()
    for n, r in enumerate(bank, 1):
        tid = r.get("id") or f"TX-{n:06d}"
        if tid in seen:
            raise ImportErrorReport(f"bank statement: duplicate id {tid}")
        seen.add(tid)
        cat = (r.get("category") or "other").lower()
        if cat not in CATEGORIES:
            raise ImportErrorReport(f"bank statement {tid}: unknown category {r['category']!r}; use one of "
                                    f"{', '.join(sorted(CATEGORIES))} or leave it empty")
        if r["amount"] == "":
            raise ImportErrorReport(f"bank statement {tid}: amount is missing")
        txs.append(dict(id=tid, date=_date(r["date"], f"bank statement {tid} date"),
                        amount=_cents(r["amount"], f"bank statement {tid} amount"),
                        desc=r["description"], cat=cat,
                        currency=r.get("currency") or pack.currency, row=n, balance=r.get("balance", "")))
    # Exports list oldest or newest first; rows of one day keep their file order (reversed
    # in a newest-first file). ponytail: a statement covering a single day is read as oldest first.
    newest_first = len(txs) > 1 and txs[0]["date"] > txs[-1]["date"]
    txs.sort(key=lambda t: (t["date"], -t["row"] if newest_first else t["row"]))
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
            if r.get("currency") and r["currency"].upper() != currency.upper():
                raise ImportErrorReport(f"{where}: currency {r['currency']!r} differs from the account "
                                        f"currency {currency}; import one currency at a time")
            vat =_cents(r["vat"], f"{where} vat") if r.get("vat") else round(gross * pack.vat_rate / (1 + pack.vat_rate))
            overdue = _days(r.get("days_overdue", ""), f"{where} days_overdue")
            opens.append(dict(direction=direction, id=r.get("invoice_id") or "", number=r["number"],
                              cp_name=r["counterparty"], cp_id=r.get("counterparty_id") or "",
                              issue=_date(r["issue_date"], f"{where} issue_date"),
                              due=_date(r["due_date"], f"{where} due_date"), gross=gross, vat=vat,
                              overdue=overdue, where=where))
    hist = []
    if paid_invoices_csv is not None:
        for n, r in enumerate(_rows(paid_invoices_csv, "paid invoices",
                                    ("number", "counterparty", "due_date", "paid_date", "amount")), 1):
            where = f"paid invoices row {n}"
            if (r.get("direction") or "AR") not in {"AR", "AP"}:
                raise ImportErrorReport(f"{where}: direction must be AR or AP")
            if _cents(r["amount"], f"{where} amount") <= 0:
                raise ImportErrorReport(f"{where}: amount must be positive")
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
    from policy.engine import load_rules
    for sp in sched:
        if not 0 < sp["amount"] <= load_rules()["payment"]["max_payment_cents"]:
            raise ImportErrorReport("scheduled payments: amount must be positive and within max_payment_cents")
    counts = {"AR": 0, "AP": 0}
    for o in opens:
        counts[o["direction"]] += 1
        o["id"] = o["id"] or f"{o['direction']}-{(1000 if o['direction'] == 'AR' else 2000) + counts[o['direction']]}"
    ids = [o["id"] for o in opens]
    if len(ids) != len(set(ids)):
        raise ImportErrorReport("open items: duplicate invoice_id")

    if as_of is None:
        implied = {o["due"] + timedelta(days=o["overdue"]) for o in opens if o["overdue"] > 0}
        if len(implied) > 1:
            raise ImportErrorReport("open items: days_overdue values imply different 'today' dates")
        as_of = implied.pop() if implied else (txs[-1]["date"] + timedelta(days=1) if txs else date.today())

    opening, total = None, sum(t["amount"] for t in txs)
    last = _cents(txs[-1]["balance"], f"bank statement {txs[-1]['id']} balance") \
        if txs and txs[-1]["balance"] else None  # balance after the chronologically last row
    if cash_cents is not None:
        opening = cash_cents - total
        if last is not None and last != cash_cents:
            warnings.append(f"statement ending balance {last / 100:,.2f} differs from trial balance "
                            f"cash {cash_cents / 100:,.2f}; using the trial balance")
    elif last is not None:
        opening = last - total
    if opening is None:
        raise ImportErrorReport("cannot determine opening cash: no cash account in the trial balance "
                                "and no balance column in the bank statement")

    # ---- counterparties ------------------------------------------------------ #
    canon = _canonical()
    cps: dict[str, dict] = {}
    known: dict[tuple[str, str], str] = {}  # (direction, name key) -> counterparty id

    def counterparty(item: dict) -> dict:
        key = (item["direction"], _key(item["cp_name"]))
        cid = item["cp_id"] or known.get(key) or next((k for k, c in canon.items() if c[1] == item["cp_name"]), "")
        if not cid:
            cid = ("CUST-" if item["direction"] == "AR" else "SUP-") + _slug(item["cp_name"])
            if cid in cps:  # the slug belongs to another name: add a short stable hash of this one
                cid += "-" + hashlib.sha256(key[1].encode()).hexdigest()[:6].upper()
        entry = cps.setdefault(cid, dict(name=item["cp_name"], direction=item["direction"], terms=[]))
        if _key(entry["name"]) != key[1] or entry["direction"] != item["direction"]:
            raise ImportErrorReport(f"counterparties: conflicting id {cid}")
        known.setdefault(key, cid)
        item["cp_id"] = cid
        return entry

    open_items: set[tuple[str, str, str]] = set()
    for o in opens:
        counterparty(o)["terms"].append((o["due"] - o["issue"]).days)
        item = (o["direction"], o["cp_id"], o["number"])
        if item in open_items:
            raise ImportErrorReport(f"{o['where']}: invoice {o['number']} of {o['cp_name']} is listed twice")
        open_items.add(item)
    for h in hist:
        counterparty(h)
    for sp in sched:
        cid = next((k for k, c in cps.items() if c["name"] == sp["cp_name"] or k == sp["cp_name"]), None)
        if cid is None or cps[cid]["direction"] != "AP" or (cid in canon and canon[cid][2] == "customer"):
            raise ImportErrorReport("scheduled payments: counterparty must be a supplier with a payee")
        raise ImportErrorReport(f"scheduled payments: payee for {sp['cp_name']} is unverified; verify with a human before scheduling")
    by_name = {c["name"]: cid for cid, c in cps.items()}

    # ---- paid customer invoices rebuilt from receipts ------------------------- #
    paid: list[dict] = []
    hist_numbers = {h["number"] for h in hist}
    for t in txs:
        if t["cat"] != "sales_receipt" or t["amount"] <= 0:
            continue
        m = re.match(r"Payment from (.+) (\S+)$", t["desc"])
        cid = known.get(("AR", _key(m.group(1)))) if m else None
        if not cid:
            continue
        t["cp"] = cid
        if m.group(2) in hist_numbers:
            continue
        if ("AR", cid, m.group(2)) in open_items:  # a paid copy would expect the same cash twice
            warnings.append(f"bank statement {t['id']}: receipt for {m.group(2)}, which is still open in the "
                            "open receivables; kept it open (partly paid, or an older open-items file?)")
            continue
        paid.append(dict(tx=t, number=m.group(2), cp=cid))
    for t in txs:
        if "cp" not in t:
            hit = next((cid for nm, cid in by_name.items() if nm in t["desc"]), None)
            t["cp"] = hit

    invoice_ids = ids + [f"{h['direction']}-H{k:05d}" for k, h in enumerate(hist, 1)]
    invoice_ids += [f"AR-H{k:05d}" for k in range(len(hist) + 1, len(hist) + len(paid) + 1)]
    if len(invoice_ids) != len(set(invoice_ids)):
        raise ImportErrorReport("invoices: duplicate or colliding invoice id")
    payee_ids = [f"PAYEE-{cid.removeprefix('SUP-')}" for cid, c in cps.items() if c["direction"] == "AP"]
    if len(payee_ids) != len(set(payee_ids)):
        raise ImportErrorReport("payees: colliding id")

    # ---- write ----------------------------------------------------------------- #
    target_engine = target.get_bind() if isinstance(target, Session) else None
    destination = Path(target_engine.url.database if target_engine else db_url(target).removeprefix("sqlite:///"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".runway-import-", suffix=".db", dir=destination.parent)
    os.close(fd)
    eng = create_engine(f"sqlite:///{temporary}", connect_args={"check_same_thread": False})
    session = Session(eng)
    try:
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
            iid = o["id"]
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
        for k, p in enumerate(paid, len(hist) + 1):
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
        session.commit()
        for k, v in {"today": as_of.isoformat(), "kill_switch": "off", "country": country}.items():
            session.add(Setting(key=k, value=v))
        session.commit()
    except Exception as exc:
        session.rollback()
        raise ImportErrorReport(f"Import failed; existing database preserved: {exc}") from exc
    else:
        session.close()
        eng.dispose()
        if isinstance(target, Session):
            target.close()
            target_engine.dispose()
        reset_engines()
        # Copy into the live file instead of swapping it: same inode and mode, so connections
        # that stay open (a separate bank server) see the import. One locked, atomic write.
        try:
            with closing(sqlite3.connect(temporary)) as src, \
                    closing(sqlite3.connect(destination, timeout=LOCK_WAIT_SECONDS)) as dst:
                src.backup(dst, progress=_stop_if_locked)
        except sqlite3.Error as exc:
            raise ImportErrorReport(f"Import failed; existing database preserved: {exc}") from exc
    finally:
        session.close()
        eng.dispose()
        Path(temporary).unlink(missing_ok=True)
    return dict(transactions=len(txs), open_receivables=n_ar, open_payables=n_ap,
                counterparties=len(cps), paid_history_rebuilt=len(paid),
                opening_balance_cents=opening, today=as_of.isoformat(), warnings=warnings)
