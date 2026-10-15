"""EVM anchoring against a fake JSON-RPC node that checks real signatures."""

import json

import httpx
import pytest
import rlp
from eth_account import Account
from eth_utils import keccak
from sqlmodel import select

from audit.anchor import (AnchorError, EvmBackend, anchor_head, decode_payload, encode_payload, get_backend,
                          verify_anchors)
from audit.log import append, list_entries
from common.db import AuditAnchor, reset_engines, session_scope
from redteam.tamper import rewrite_history

DEV_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"  # public local dev-chain key
HEAD = "ab" * 32


class FakeNode:
    """Just enough eth_* to accept, mine and return legacy transactions."""

    def __init__(self):
        self.txs: dict[str, dict] = {}
        self.mine = True
        self.fail: str | None = None
        self.lag = 0  # polls that still see a sent transaction as pending (a real node mines a moment later)
        self.status = "0x1"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method, params = body["method"], body["params"]

        def reply(**kw):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], **kw})

        if self.fail:
            return reply(error={"code": -32000, "message": self.fail})
        if method == "eth_chainId":
            return reply(result=hex(31337))
        if method == "eth_gasPrice":
            return reply(result=hex(10**9))
        if method == "eth_getTransactionCount":
            return reply(result=hex(len(self.txs)))
        if method == "eth_estimateGas":
            return reply(result=hex(60_000))
        if method == "eth_sendRawTransaction":
            raw = bytes.fromhex(params[0].removeprefix("0x"))
            fields = rlp.decode(raw)  # legacy: nonce, gasPrice, gas, to, value, data, v, r, s
            tx_hash = "0x" + keccak(raw).hex()
            self.txs[tx_hash] = {"hash": tx_hash, "from": Account.recover_transaction(raw),
                                 "input": "0x" + fields[5].hex(), "lag": self.lag,
                                 "blockNumber": hex(len(self.txs) + 1) if self.mine else None}
            return reply(result=tx_hash)
        tx = self.txs.get(params[0]) if method.startswith("eth_getTransaction") else None
        if tx and tx["lag"] > 0:
            tx["lag"] -= 1
            return reply(result=None if method == "eth_getTransactionReceipt" else {**tx, "blockNumber": None})
        if method == "eth_getTransactionByHash":
            return reply(result=tx)
        if method == "eth_getTransactionReceipt":
            mined = tx is not None and tx["blockNumber"] is not None
            return reply(result={"transactionHash": params[0], "blockNumber": tx["blockNumber"],
                                 "status": self.status} if mined else None)
        return reply(error={"code": -32601, "message": "method not found"})


@pytest.fixture
def node():
    return FakeNode()


def evm(node, key=DEV_KEY, url="http://chain.test"):
    return EvmBackend(url, key, transport=httpx.MockTransport(node), confirm_timeout=0.3, poll_seconds=0.01)


def test_publish_signs_and_fetch_reads_back(node):
    backend = evm(node)
    ref = backend.publish(encode_payload(3, HEAD))
    assert node.txs[ref]["from"] == backend.address == Account.from_key(DEV_KEY).address
    assert decode_payload(backend.fetch(ref)) == (3, HEAD)


def test_fetch_rejects_other_senders(node):
    ref = evm(node, "0x" + "11" * 32).publish(encode_payload(3, HEAD))
    with pytest.raises(AnchorError, match="not sent by"):
        evm(node).fetch(ref)


def test_unmined_and_unknown_transactions(node):
    node.mine = False
    backend = evm(node)
    with pytest.raises(AnchorError, match="not mined"):
        backend.publish(encode_payload(3, HEAD))
    with pytest.raises(AnchorError, match="not on chain"):
        backend.fetch(next(iter(node.txs)))
    with pytest.raises(AnchorError, match="not on chain"):
        backend.fetch("0x" + "00" * 32)


def test_publish_waits_until_the_transaction_is_mined(node, session):
    """Anvil returned the hash before its block was readable, so publish-then-verify said 'not on chain'."""
    node.lag = 2
    backend = evm(node)
    anchor_head(session, "owner", backend)
    assert verify_anchors(session, backend).status == "verified"


def test_reverted_transaction_stores_nothing(node, session):
    node.status = "0x0"
    with pytest.raises(AnchorError, match="reverted"):
        anchor_head(session, "owner", evm(node))
    assert session.exec(select(AuditAnchor)).all() == []


def test_rpc_error_is_reported(node):
    node.fail = "insufficient funds for gas"
    with pytest.raises(AnchorError, match="insufficient funds"):
        evm(node).publish(encode_payload(3, HEAD))


def test_errors_do_not_leak_the_rpc_url():
    def down(request):
        raise httpx.ConnectError("connection refused", request=request)

    backend = EvmBackend("https://rpc.example/v3/SECRETKEY", DEV_KEY, transport=httpx.MockTransport(down))
    with pytest.raises(AnchorError, match="failed") as error:
        backend.publish(encode_payload(3, HEAD))
    assert "SECRETKEY" not in str(error.value)


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNWAY_DB", str(tmp_path / "evm.db"))
    reset_engines()
    with session_scope() as s:
        for i in range(3):
            append(s, "agent", "get_balances", {"index": i}, "ALLOW", "Read")
        yield s
    reset_engines()


def test_anchor_and_forgery_detection_on_chain(node, session):
    backend = evm(node)
    receipt = anchor_head(session, "owner", backend)
    assert receipt.backend == "evm" and receipt.ref in node.txs
    assert verify_anchors(session, backend).status == "verified"
    rewrite_history(session, 2, "attacker")
    assert verify_anchors(session, backend).status == "tampered"


def test_failed_publish_stores_nothing(node, session):
    node.fail = "connection reset"
    with pytest.raises(AnchorError):
        anchor_head(session, "owner", evm(node))
    assert session.exec(select(AuditAnchor)).all() == []
    assert len(list_entries(session)) == 3


def test_get_backend_evm_needs_url_and_valid_key(monkeypatch):
    monkeypatch.setenv("RUNWAY_ANCHOR", "evm")
    with pytest.raises(AnchorError, match="RUNWAY_EVM_RPC_URL"):
        get_backend()
    monkeypatch.setenv("RUNWAY_EVM_RPC_URL", "http://chain.test")
    monkeypatch.setenv("RUNWAY_EVM_PRIVATE_KEY", "not-a-key")
    with pytest.raises(AnchorError, match="not a valid private key") as error:
        get_backend()
    assert "not-a-key" not in str(error.value)
    monkeypatch.setenv("RUNWAY_EVM_PRIVATE_KEY", DEV_KEY)
    assert get_backend().name == "evm"
