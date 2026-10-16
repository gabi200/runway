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
# Any case, groups split by up to 3 spaces (NBSP, tab, line break), dots or hyphens. Matches only count
# when they pass the mod-97 check, so look-alike prose is left alone.
_SEP = r"[\s.\-]{1,3}"
_IBAN_LOOSE_RE = re.compile(rf"(?<![A-Za-z0-9])[A-Za-z]{{2}}(?:{_SEP})?\d{{2}}(?:(?:{_SEP})?[A-Za-z0-9]{{4}}){{2,7}}"
                            rf"(?:(?:{_SEP})?[A-Za-z0-9]{{1,3}})?(?![A-Za-z0-9])")
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
                 "customer", "supplier", "invoice", "payment", "dear", "hello", "best", "kind", "many",
                 "accounts", "account", "payable", "receivable", "credit", "control", "collections", "billing",
                 "treasury", "direct", "debit", "invoices", "payments", "banking"}
_LEGAL_SUFFIXES = {"gmbh", "ltd", "llc", "inc", "corp", "bv", "sa", "ag", "co", "the", "limited"}
_MONTHS = {"january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december"}
# Never a one-word alias (they may still be part of a full name, which is masked as a whole).
# ponytail: a fixed list; other everyday words that are also names (e.g. "Hope") still alias when capitalised.
_NO_ALIAS = _COMMON_WORDS | _ROLE_LOCALS | _SIGN_OFFS | _LEGAL_SUFFIXES | _MONTHS | {
    "bill", "bills", "pay", "iban", "will", "mark", "grant", "rose", "frank", "art", "bob"}
_PH_RX = r"<[A-Z]+_\d+(?:_[A-Z]+)?>"


def _key(s: str) -> str:
    """Lookup key of a name: lower case, runs of whitespace and hyphens collapsed to one space."""
    return re.sub(r"[\s-]+", " ", s).strip().lower()


def _name_rx(literal: str) -> str:
    """Regex for a name literal whose words may be split by any run of whitespace or hyphens."""
    return r"[\s-]+".join(re.escape(w) for w in re.split(r"[\s-]+", literal) if w)


def _norm_iban(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", s).upper()


def _iban_ok(compact: str) -> bool:
    """ISO 13616 mod-97 check (the rule of bank.service.iban_is_valid) on a normalised IBAN."""
    return (15 <= len(compact) <= 34 and compact[:2].isalpha() and compact[2:4].isdigit()
            and int("".join(str(int(c, 36)) for c in compact[4:] + compact[:4])) % 97 == 1)


def _entity(m: re.Match[str]) -> str:
    """<SUPPLIER_1_ID> and <SUPPLIER_1> name the same entity."""
    return f"{m.group(1)}_{m.group(2)}"


def contains_iban(text: str) -> bool:
    """True when `text` holds an IBAN in any case or spacing that passes the mod-97 check."""
    # ponytail: a random code that happens to pass the checksum (about 1 in 97 IBAN-shaped strings) counts too
    return any(_iban_ok(_norm_iban(m.group(0))) for m in _IBAN_LOOSE_RE.finditer(text))


class Pseudonymizer:
    """Mask/unmask text and nested objects. Placeholders are stable per instance."""

    def __init__(self, session=None, people: list[str] | None = None) -> None:
        self._fwd: dict[str, str] = {}  # lowercase name literal -> placeholder (case-insensitive)
        self._lit: dict[str, str] = {}  # same keys -> literal in its original case (the regex is built from these)
        self._cap: set[str] = set()  # keys that match only with a capital first letter
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
        key = _key(literal)
        if not key:
            return literal
        if key in self._fwd:
            ph = self._fwd[key]
        else:
            ph = f"<{label}_{self._next(label)}>"
            self._fwd[key] = ph
            self._lit[key] = literal.strip()
            self._rev[ph] = literal.strip()
            self._regex = None
            words = re.findall(r"[A-Za-z][A-Za-z0-9&'-]+", literal)
            if label == "PERSON":
                for w in words:
                    if len(w) >= 3 and w.lower() not in _NO_ALIAS:
                        self._alias(w, ph, capitalised=True)
            elif label not in ("EMAIL", "IBAN") and words and words[0].lower() not in _NO_ALIAS and len(words[0]) >= _MIN_ALIAS_LEN:
                self._alias(words[0], ph)
        if ident:
            self._add_id(ph, label, ident, id_tag)
        return ph

    def _alias(self, word: str, ph: str, capitalised: bool = False) -> None:
        if _key(word) not in self._fwd:
            self._fwd[_key(word)] = ph
            self._lit[_key(word)] = word
            if capitalised:  # a person's single name matches only capitalised: "Weber", never "weber grills"
                self._cap.add(_key(word))

    def _add_id(self, ph: str, label: str, ident: str, tag: str = "ID") -> None:
        # Plain-word IDs (PAYROLL, LANDLORD) would mask ordinary prose and bare or short numbers (CSV ids 1, 2)
        # would mask amounts; only structured IDs with a letter and at least 4 characters are masked.
        structured = "-" in ident or any(c.isdigit() for c in ident)
        if not structured or not any(c.isalpha() for c in ident) or len(ident) < 4 or ident in self._ids:
            return
        idph = ph[:-1] + f"_{tag}>"
        while idph in self._rev:  # name shared with another entity: that ID keeps its placeholder, this one gets its own
            idph = f"<{label}_{self._next(label)}_{tag}>"
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
        banned = self._company_words | _COMMON_WORDS | _ROLE_LOCALS | _SIGN_OFFS | _LEGAL_SUFFIXES | {
            w.lower() for w in COMPANY.split()}
        return not any(t.lower() in banned for t in toks)

    def add_person(self, name: str) -> str:
        return self.add("PERSON", name)

    def people(self) -> list[str]:
        """Person names registered so far, in placeholder order (pass to the next instance as `people`)."""
        return [lit for ph, lit in self._rev.items() if ph.startswith("<PERSON_")]

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
            names = "|".join(self._rx(k) for k in sorted(self._lit, key=lambda k: len(self._lit[k]), reverse=True))
            alts = ([rf"(?<!\d[,.])(?-i:{ids})(?![,.]\d)"] if ids else []) + ([names] if names else [])
            # An existing placeholder is matched first and kept, so no name inside it (IBAN, PERSON) is rewritten.
            self._regex = re.compile(rf"(?P<ph>(?-i:{_PH_RX}))|(?<![A-Za-z0-9])(?:{'|'.join(alts)})(?![A-Za-z0-9])",
                                     re.IGNORECASE)
        return self._regex

    def _rx(self, key: str) -> str:
        lit = self._lit[key]
        if key in self._cap:
            return f"(?-i:{re.escape(lit[0].upper())}){_name_rx(lit[1:])}"
        return _name_rx(lit)

    def mask(self, text: str) -> str:
        """Replace known names/IDs and every email/IBAN-shaped string with placeholders."""
        if not isinstance(text, str) or not text:
            return text

        def email_sub(m: re.Match[str]) -> str:
            return self.add("EMAIL", m.group(0))

        def iban_sub(m: re.Match[str]) -> str:
            return self.add("IBAN", _norm_iban(m.group(0)))

        def loose_iban_sub(m: re.Match[str]) -> str:
            s = m.group(0)  # may run on into following words: keep the longest valid IBAN prefix
            for end in reversed([t.end() for t in re.finditer(r"[A-Za-z0-9]+", s)]):
                if _iban_ok(_norm_iban(s[:end])):
                    return self.add("IBAN", _norm_iban(s[:end])) + s[end:]
            return s

        text = EMAIL_RE.sub(email_sub, text)
        text = _IBAN_LOOSE_RE.sub(loose_iban_sub, text)
        text = IBAN_RE.sub(iban_sub, text)  # IBAN-shaped but failing the checksum: masked as before
        pat = self._pattern()
        if pat is not None:
            text = pat.sub(lambda m: m.group(0) if m.group("ph") else self._lookup(m.group(0)), text)
        return text

    def _lookup(self, found: str) -> str:
        """Placeholder for a regex match. Never raises: masking must not crash a turn."""
        ph = self._ids.get(found) or self._fwd.get(_key(found))
        if ph is None:  # re.IGNORECASE also pairs i/I with ı/İ and s with ſ, which lower() does not
            ph = next((self._fwd[k] for k in self._lit
                       if re.fullmatch(self._rx(k), found, re.IGNORECASE)), "<REDACTED_0>")
        return ph

    def unmask(self, text: str, allowed: set[str] | None = None, withheld: str | None = None) -> str:
        """Replace placeholders with the original literals; unknown ones are left as is.

        With `allowed` (the placeholders the model was shown), only entities it saw are resolved, in any
        form (<SUPPLIER_1> also allows <SUPPLIER_1_ID>); any other is left as is, or replaced by
        `withheld`. Every payee name and IBAN is registered up front, so without this a model that
        guesses <IBAN_2> would print a real IBAN it was never given.
        """
        if not isinstance(text, str) or not text:
            return text
        seen = None if allowed is None else {_entity(PLACEHOLDER_RE.fullmatch(p)) for p in allowed
                                             if PLACEHOLDER_RE.fullmatch(p)}

        def sub(m: re.Match[str]) -> str:
            ph = m.group(0)
            if seen is not None and _entity(m) not in seen:
                return withheld if withheld is not None else ph
            return self._rev.get(ph, ph)
        return PLACEHOLDER_RE.sub(sub, text)

    def mask_obj(self, obj: Any) -> Any:
        return self._walk(obj, self.mask)

    def unmask_obj(self, obj: Any, allowed: set[str] | None = None) -> Any:
        return self._walk(obj, lambda t: self.unmask(t, allowed))

    def _walk(self, obj: Any, fn):
        if isinstance(obj, str):
            return fn(obj)
        if isinstance(obj, dict):
            return {k: self._walk(v, fn) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self._walk(v, fn) for v in obj]
        return obj
