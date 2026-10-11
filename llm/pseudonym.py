"""Reversible pseudonymisation of names and IBANs before text reaches the LLM.

Counterparty/payee names, people's names and IBANs become stable placeholders
(<CUSTOMER_1>, <SUPPLIER_2>, <PERSON_1>, <IBAN_3>). Identifiers that embed a
name (CUST-BRIGHTFIELD, PAYEE-CARTONIA) are masked consistently (<CUSTOMER_1_ID>,
<PAYEE_2_ID>) and unmasked again in tool-call arguments coming back.
"""

from __future__ import annotations

import re
from typing import Any

IBAN_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?(?![A-Za-z0-9])"
)
EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_NAME = r"[A-Z][a-zA-Z'\-]+"
_HEADER_NAME_RE = re.compile(rf"^(?:From|To|Cc|Bcc|Reply-To|Sender)\s*:\s*\"?({_NAME}(?: {_NAME}){{0,3}})\"?\s*<", re.M)
_CLOSER_RE = re.compile(rf"^(?:regards|kind regards|best regards|best|thanks|thank you|cheers|sincerely|yours|yours sincerely)\s*,?\s*$\s*^({_NAME}(?: {_NAME}){{0,3}})\s*$", re.M | re.I)
_LAST_LINE_RE = re.compile(rf"^({_NAME}(?: {_NAME}){{0,2}})\s*\Z", re.M)
_ROLE_LOCALS = {"accounts", "account", "billing", "finance", "info", "support", "sales", "payable", "payables",
                "receivable", "office", "admin", "team", "hello", "contact", "invoice", "invoices", "payroll",
                "noreply", "no", "reply", "service", "orders", "owner", "accountant", "ceo", "cfo", "hr"}
_SIGN_OFFS = {"regards", "thanks", "cheers", "best", "sincerely", "yours", "thank", "total", "subtotal", "amount"}
PLACEHOLDER_RE = re.compile(r"<([A-Z]+)_(\d+)(_[A-Z]+)?>")
_MIN_ALIAS_LEN = 6
_COMMON_WORDS = {"packaging", "company", "department", "team", "services", "service", "group", "management",
                 "accounting", "finance", "support", "sales", "logistics", "energy", "bank", "office",
                 "customer", "supplier", "invoice", "payment", "dear", "hello", "best", "kind", "many"}
_LEGAL_SUFFIXES = {"gmbh", "ltd", "llc", "inc", "corp", "bv", "sa", "ag", "co", "the", "limited"}


def _norm_iban(s: str) -> str:
    return re.sub(r"\s+", "", s).upper()


class Pseudonymizer:
    """Mask/unmask text and nested objects. Placeholders are stable per instance."""

    def __init__(self, session=None, people: list[str] | None = None) -> None:
        self._fwd: dict[str, str] = {}  # lowercase name literal -> placeholder (case-insensitive)
        self._ids: dict[str, str] = {}  # identifier literal -> placeholder (case-sensitive)
        self._rev: dict[str, str] = {}  # placeholder -> original literal
        self._counters: dict[str, int] = {}
        self._company_words: set[str] = set()  # words of own/counterparty company names: never a person
        self._regex: re.Pattern[str] | None = None
        if session is not None:
            self.load(session)
        for p in people or []:
            self.add("PERSON", p)

    # -- registration ------------------------------------------------------
    def _next(self, label: str) -> int:
        self._counters[label] = self._counters.get(label, 0) + 1
        return self._counters[label]

    def add(self, label: str, literal: str, ident: str | None = None, id_tag: str = "ID") -> str:
        """Register a literal (and optional embedded-name ID) under a label; return placeholder."""
        key = literal.strip().lower()
        if not key:
            return literal
        if key in self._fwd:
            ph = self._fwd[key]
        else:
            ph = f"<{label}_{self._next(label)}>"
            self._fwd[key] = ph
            self._rev[ph] = literal.strip()
            self._regex = None
            words = re.findall(r"[A-Za-z][A-Za-z0-9&'-]+", literal)
            if label == "PERSON":
                for w in words:
                    if len(w) >= 3 and w.lower() not in _SIGN_OFFS:
                        self._fwd.setdefault(w.lower(), ph)
            elif label not in ("EMAIL", "IBAN") and words and words[0].lower() not in _LEGAL_SUFFIXES and len(words[0]) >= _MIN_ALIAS_LEN:
                self._fwd.setdefault(words[0].lower(), ph)
        if ident:
            self._add_id(ph, ident, id_tag)
        return ph

    def _add_id(self, ph: str, ident: str, tag: str = "ID") -> None:
        idph = ph[:-1] + f"_{tag}>"
        # Plain-word IDs (PAYROLL, LANDLORD) would mask ordinary prose; only structured IDs are masked.
        if ("-" in ident or any(c.isdigit() for c in ident)) and ident not in self._ids:
            self._ids[ident] = idph
            self._rev[idph] = ident
            self._regex = None

    def load(self, session) -> None:
        """Load names, IDs and IBANs from the database."""
        from sqlmodel import select

        from common.db import Counterparty, Payee

        cp_ph: dict[str, str] = {}
        from common.db import Account

        for acc in session.exec(select(Account)):
            self.add_company_name(acc.name)
        for cp in session.exec(select(Counterparty).order_by(Counterparty.id)):
            self.add_company_name(cp.name)
            label = "CUSTOMER" if cp.kind == "customer" else "SUPPLIER"
            cp_ph[cp.id] = self.add(label, cp.name, ident=cp.id)
            if cp.email:
                self.learn(cp.email)
        for p in session.exec(select(Payee).order_by(Payee.id)):
            label = "SUPPLIER"
            if p.counterparty_id in cp_ph:
                label = cp_ph[p.counterparty_id][1:].split("_")[0]
            self.add(label, p.name, ident=p.id, id_tag="PAYEEID")
            self.add("IBAN", _norm_iban(p.iban))

    def add_company_name(self, name: str) -> None:
        """Remember a company name so its words are never learned as a person's name."""
        self._company_words |= {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9'-]+", name or "")}

    def _person_ok(self, name: str) -> bool:
        """A learnable person name: two capitalised tokens, none a company or common word."""
        from agent.outbox import COMPANY

        toks = name.split()
        if len(toks) != 2 or not all(re.fullmatch(_NAME, t) for t in toks):
            return False
        banned = self._company_words | _COMMON_WORDS | _SIGN_OFFS | _LEGAL_SUFFIXES | {
            w.lower() for w in COMPANY.split()}
        return not any(t.lower() in banned for t in toks)

    def add_person(self, name: str) -> str:
        return self.add("PERSON", name)

    def learn(self, text: str) -> None:
        """Register person names found in text (From:/To: headers, sign-offs, first.last email addresses)."""
        if not isinstance(text, str) or not text:
            return
        for m in EMAIL_RE.finditer(text):
            self.add("EMAIL", m.group(0))
            parts = m.group(0).split("@")[0].split(".")
            if len(parts) == 2 and all(p.isalpha() and 2 <= len(p) <= 20 for p in parts) \
                    and not {p.lower() for p in parts} & _ROLE_LOCALS:
                cand = " ".join(p.capitalize() for p in parts)
                if self._person_ok(cand):
                    self.add("PERSON", cand)
        for rx in (_HEADER_NAME_RE, _CLOSER_RE):
            for m in rx.finditer(text):
                if self._person_ok(m.group(1)):
                    self.add("PERSON", m.group(1))
        last = text.rstrip().rsplit("\n", 1)[-1] if "\n" in text.strip() else ""
        m = _LAST_LINE_RE.match(last.strip()) if last.strip() else None
        if m and self._person_ok(m.group(1)):
            self.add("PERSON", m.group(1))

    def learn_obj(self, obj: Any) -> None:
        self._walk(obj, lambda t: (self.learn(t), t)[1])

    def has_unknown_placeholder(self, obj: Any) -> bool:
        """True if any string in obj still holds a placeholder that unmask could not resolve."""
        found = []
        self._walk(obj, lambda t: (found.append(bool(PLACEHOLDER_RE.search(t))), t)[1])
        return any(found)

    # -- core --------------------------------------------------------------
    def _pattern(self) -> re.Pattern[str] | None:
        if self._regex is None and (self._fwd or self._ids):
            ids = "|".join(re.escape(a) for a in sorted(self._ids, key=len, reverse=True))
            names = "|".join(re.escape(a) for a in sorted(self._fwd, key=len, reverse=True))
            alts = ([f"(?-i:{ids})"] if ids else []) + ([names] if names else [])
            self._regex = re.compile(rf"(?<![A-Za-z0-9])(?:{'|'.join(alts)})(?![A-Za-z0-9])", re.IGNORECASE)
        return self._regex

    def mask(self, text: str) -> str:
        """Replace known names/IDs and every email/IBAN-shaped string with placeholders."""
        if not isinstance(text, str) or not text:
            return text

        def email_sub(m: re.Match[str]) -> str:
            return self.add("EMAIL", m.group(0))

        def iban_sub(m: re.Match[str]) -> str:
            n = _norm_iban(m.group(0))
            ph = self._fwd.get(n.lower())
            if ph is None:
                ph = self.add("IBAN", n)
            return ph

        text = EMAIL_RE.sub(email_sub, text)
        text = IBAN_RE.sub(iban_sub, text)
        pat = self._pattern()
        if pat is not None:
            text = pat.sub(lambda m: self._ids.get(m.group(0)) or self._fwd[m.group(0).lower()], text)
        return text

    def unmask(self, text: str) -> str:
        """Replace placeholders with the original literals; unknown ones are left as is."""
        if not isinstance(text, str) or not text:
            return text
        return PLACEHOLDER_RE.sub(lambda m: self._rev.get(m.group(0), m.group(0)), text)

    def mask_obj(self, obj: Any) -> Any:
        return self._walk(obj, self.mask)

    def unmask_obj(self, obj: Any) -> Any:
        return self._walk(obj, self.unmask)

    def _walk(self, obj: Any, fn):
        if isinstance(obj, str):
            return fn(obj)
        if isinstance(obj, dict):
            return {k: self._walk(v, fn) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self._walk(v, fn) for v in obj]
        return obj
