"""Run the judge task pack on a fresh demo company per task and write a pass/fail report.

    python scripts/run_tasks.py --mode offline            # no network
    python scripts/run_tasks.py --mode deepseek           # needs DEEPSEEK_API_KEY
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml
from sqlmodel import Session

from agent.documents import ingest_text
from agent.executor import pending_approvals
from agent.orchestrator import Agent
from common import db
from data.generate import generate
from llm.adapter import get_adapter

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks" / "judge_tasks.yaml"
_ENV = ("RUNWAY_DB", "RUNWAY_VAR_DIR", "RUNWAY_BANK_URL")


class _Down:
    """A language model that never answers, for the fallback task."""

    def chat(self, messages, tools=None):
        raise TimeoutError("simulated provider outage")


def load_tasks(path: Path = TASKS) -> list[dict]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["tasks"]


def check(task: dict, steps: list[dict], reply: str, pending: list[str]) -> list[str]:
    """Every expectation the run missed; an empty list means the task passed."""
    exp, low, fails = task.get("expect", {}), reply.lower(), []
    for status in ("ok", "needs_approval", "denied"):
        reached = {s["tool"] for s in steps if s["status"] == status}
        fails += [f"{tool} not {status}" for tool in exp.get(status, []) if tool not in reached]
    called = {s["tool"] for s in steps}
    fails += [f"{tool} was called" for tool in exp.get("not_called", []) if tool in called]
    for tool, n in exp.get("count", {}).items():
        got = sum(s["tool"] == tool and s["status"] in {"ok", "needs_approval"} for s in steps)
        if got != n:
            fails.append(f"{tool} ran {got} times, expected {n}")
    fails += [f"reply lacks {t!r}" for t in exp.get("reply_has", []) if t.lower() not in low]
    fails += [f"reply contains {t!r}" for t in exp.get("reply_lacks", []) if t.lower() in low]
    fails += [f"reply matches {p!r}" for p in exp.get("reply_lacks_regex", []) if re.search(p, reply)]
    if "pending" in exp and sorted(pending) != sorted(exp["pending"]):
        fails.append(f"pending {sorted(pending)}, expected {sorted(exp['pending'])}")
    return fails


def run_task(task: dict, mode: str, workdir: Path) -> dict:
    """Generate a fresh company, play the turns, and check the expectations."""
    dbp = workdir / f"{task['id']}.db"
    os.environ["RUNWAY_DB"] = str(dbp)
    os.environ.pop("RUNWAY_BANK_URL", None)  # the in-process bank, as in run_demo.py
    db.reset_engines()
    generate(db_path=dbp, out_dir=workdir / f"{task['id']}-sample")

    def factory():
        return Session(db.get_engine())

    doc_id = ""
    if doc := task.get("document"):
        with factory() as s:
            doc_id = ingest_text(s, doc["text"], doc["filename"], doc.get("kind", "invoice"), doc.get("sender")).id
    agent = Agent(factory, adapter=_Down() if task.get("llm_down") else get_adapter(mode))
    steps: list[dict] = []
    reply = ""
    for msg in task["turns"]:
        turn = agent.monday_brief() if msg == "@brief" else agent.chat(msg.replace("{doc}", doc_id))
        steps += turn.steps
        reply = turn.reply
    with factory() as s:
        pending = [r.tool_name for r in pending_approvals(s)]
    return {"id": task["id"], "group": task["group"], "turns": task["turns"], "steps": steps,
            "reply": reply, "pending": pending, "failures": check(task, steps, reply, pending)}


def write_report(results: list[dict], mode: str, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    passed = sum(not r["failures"] for r in results)
    lines = [f"# Judge task pack: {mode} mode", "", f"{passed} of {len(results)} tasks passed.", "",
             "| Task | Group | Result | Missed |", "|---|---|---|---|"]
    lines += [f"| {r['id']} | {r['group']} | {'PASS' if not r['failures'] else 'FAIL'} | "
              f"{'; '.join(r['failures']).replace('|', '/')} |" for r in results]
    (out / f"task_report_{mode}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with (out / f"task_traces_{mode}.jsonl").open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, default=str, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", default="offline", choices=["offline", "deepseek", "gemini"])
    p.add_argument("--only", nargs="*", help="task ids to run, e.g. T10 T19")
    p.add_argument("--out", type=Path, default=ROOT / "docs" / "execution_logs")
    a = p.parse_args(argv)
    tasks = [t for t in load_tasks() if (not a.only or t["id"] in a.only)
             and not (a.mode == "offline" and t.get("live_only"))]
    saved = {k: os.environ.get(k) for k in _ENV}
    results: list[dict] = []
    with TemporaryDirectory(prefix="runway-tasks-") as tmp:
        os.environ["RUNWAY_VAR_DIR"] = str(Path(tmp) / "var")
        try:
            for t in tasks:
                results.append(run_task(t, a.mode, Path(tmp)))
                print(f"{t['id']}: {'PASS' if not results[-1]['failures'] else 'FAIL ' + '; '.join(results[-1]['failures'])}")
        finally:
            db.reset_engines()  # release the SQLite files before the folder is deleted (Windows)
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    write_report(results, a.mode, a.out)
    return 0 if all(not r["failures"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
