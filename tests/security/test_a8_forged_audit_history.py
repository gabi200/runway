"""A8: an insider with write access rewrites the audit history consistently; only the published head catches it."""

from agent.executor import run_tool
from audit.anchor import anchor_head, verify_anchors
from audit.log import verify_chain
from redteam.tamper import rewrite_history


def test_forged_history_caught_by_published_head(stack):
    s, _, _ = stack
    for _ in range(3):
        assert run_tool(s, "get_balances", {}).status == "ok"
    anchor_head(s, "owner")
    rewrite_history(s, 2, "nothing happened here")
    assert verify_chain(s).ok
    assert verify_anchors(s).status == "tampered"
