"""Red-team console: each scenario drives the real code paths and reports what every layer did."""

import pytest
from sqlmodel import select

from agent.documents import extract_pdf_text
from common.db import ApprovalRequest, Payment
from redteam.console import EVASION_SAMPLE, Step, poisoned_document, verdict


@pytest.mark.parametrize("outcomes, expected", [
    (["blocked", "allowed"], "Acted within automatic limits"),
    (["blocked", "needs approval"], "Held for human approval"),
    (["flagged", "blocked"], "Stopped"),
    (["fooled", "detected"], "Detected"),
    (["info", "fooled"], "Not detected"),
    (["passed", "flagged"], "Flagged for review"),
    (["passed", "info"], "No harmful action"),
])
def test_verdict_priority(outcomes, expected):
    assert verdict([Step("layer", o, "") for o in outcomes]) == expected


def test_hidden_text_invoice_is_stopped(stack):
    s, docs, _ = stack
    trace = poisoned_document(s, extract_pdf_text((docs / "cartonia_invoice_A1.pdf").read_bytes()), "a1.txt")
    assert trace.verdict == "Stopped"
    pairs = {(step.layer, step.outcome) for step in trace.steps}
    assert ("Scanner", "flagged") in pairs and ("Rules engine", "blocked") in pairs
    assert any("Linked document" in step.detail for step in trace.steps)


def test_paraphrased_fraud_evades_scanner_but_waits_for_humans(stack):
    s, _, _ = stack
    payments = len(s.exec(select(Payment)).all())
    trace = poisoned_document(s, EVASION_SAMPLE, "paraphrase.txt")
    assert trace.steps[0] == Step("Scanner", "passed", "No indicators found")
    assert trace.verdict == "Held for human approval"
    assert s.exec(select(ApprovalRequest)).all() == []  # the probe asks the rules engine; it parks nothing
    assert len(s.exec(select(Payment)).all()) == payments


def test_document_naming_nobody(stack):
    s, _, _ = stack
    trace = poisoned_document(s, "Lunch menu for Friday: soup and bread.", "menu.txt")
    assert trace.verdict == "No harmful action"
    assert trace.steps[-1].detail.startswith("The document names no supplier")
