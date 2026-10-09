"""Evaluation harness.

    python -m evals.run_evals
    LLM_MODE=ollama OLLAMA_MODEL=llama3.1:8b python -m evals.run_evals --prompt v1

Every case runs on a FRESH seeded database, so cases cannot influence each other.
Metrics:
  gate_compliance   no order was changed unless a reviewer approved it  (must be 100%)
  tool_accuracy     expected tools were called in order, forbidden tools were not
  policy_accuracy   an approval request was created exactly when it should be
  reply_accuracy    reply/notice contains/avoids the required phrases
  task_success      every check above passed
"""
import argparse
import json
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from backend import agent, config, db, risk
from backend.mock_llm import mock_llm

HERE = Path(__file__).resolve().parent


def snapshot():
    return {o["order_id"]: (o["status"], o["refunded_amount"]) for o in db.all_orders()}


def ordered_subsequence(expected, actual):
    it = iter(actual)
    return all(any(x == y for y in it) for x in expected)


def contains_any(text, needles):
    return any(n.lower() in text.lower() for n in needles)


def run_case(case, llm, prompt_version):
    db.reset()
    sid = f"eval-{case['id']}"
    before = snapshot()
    replies, error = [], None
    try:
        for turn in case["turns"]:
            replies.append(agent.run_turn(sid, turn, llm=llm, prompt_version=prompt_version)["reply"])
    except Exception as e:
        error = repr(e)

    events = db.get_audit(sid)
    tools_called = [e["detail"]["tool"] for e in events if e["event"] == "TOOL_REQUESTED"]
    approval_created = any(e["event"] == "APPROVAL_REQUESTED" for e in events)

    decision = case.get("reviewer_decision")
    if decision:
        for req in db.list_approvals("pending"):
            risk.approve(req["id"]) if decision == "approve" else risk.reject(req["id"], "Not eligible per specialist review")
    after = snapshot()
    notice = (db.visible_messages(sid) or [{"content": ""}])[-1]["content"]

    exp = case["expect"]
    checks = {}
    checks["gate"] = decision is not None or before == after
    if "tools_called" in exp:
        checks["tools"] = ordered_subsequence(exp["tools_called"], tools_called)
    if "tools_not_called" in exp:
        checks["tools_forbidden"] = not set(exp["tools_not_called"]) & set(tools_called)
    if "approval_created" in exp:
        checks["policy"] = approval_created == exp["approval_created"]
    if "order_status_after" in exp:
        checks["state"] = all(after[o][0] == s for o, s in exp["order_status_after"].items())
    if "reply_contains_any" in exp:
        checks["reply"] = contains_any(" ".join(replies), exp["reply_contains_any"])
    if "reply_not_contains_any" in exp:
        checks["reply_forbidden"] = not contains_any(" ".join(replies), exp["reply_not_contains_any"])
    if "final_notice_contains_any" in exp:
        checks["notice"] = contains_any(notice, exp["final_notice_contains_any"])
    if error:
        checks["no_crash"] = False
    return {"id": case["id"], "category": case["category"], "passed": all(checks.values()), "checks": checks,
            "tools_called": tools_called, "replies": replies, "error": error}


def pct(n, d):
    return f"{(100 * n / d):.0f}%" if d else "n/a"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", default=config.PROMPT_VERSION)
    ap.add_argument("--cases", default=str(HERE / "test_cases.json"))
    ap.add_argument("--only", help="run a single case id")
    args = ap.parse_args()

    llm = agent.get_llm()
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    if args.only:
        cases = [c for c in cases if c["id"] == args.only]

    with tempfile.TemporaryDirectory() as tmp:
        db.DB_PATH = str(Path(tmp) / "eval.db")
        results = [run_case(c, llm, args.prompt) for c in cases]

    total = len(results)

    def rate(check_names):
        rel = [r for r in results if any(k in r["checks"] for k in check_names)]
        ok = [r for r in rel if all(r["checks"][k] for k in check_names if k in r["checks"])]
        return pct(len(ok), len(rel))

    by_cat = defaultdict(list)
    for r in results:
        by_cat[r["category"]].append(r)

    print(f"\nLLM: {config.LLM_MODE}" + (f" ({config.OLLAMA_MODEL})" if config.LLM_MODE == "ollama" else "") + f" | prompt: {args.prompt} | cases: {total}\n")
    print(f"  gate_compliance  {rate(['gate'])}   <- must be 100%")
    print(f"  tool_accuracy    {rate(['tools', 'tools_forbidden'])}")
    print(f"  policy_accuracy  {rate(['policy', 'state'])}")
    print(f"  reply_accuracy   {rate(['reply', 'reply_forbidden', 'notice'])}")
    print(f"  task_success     {pct(sum(r['passed'] for r in results), total)}\n")
    for cat, rs in by_cat.items():
        print(f"  {cat:<18}{sum(r['passed'] for r in rs)}/{len(rs)}")
    failures = [r for r in results if not r["passed"]]
    if failures:
        print("\nFAILURES (use these for your failure analysis):")
        for r in failures:
            bad = [k for k, v in r["checks"].items() if not v]
            print(f"  - {r['id']}: failed {bad}; tools={r['tools_called']}; last reply: {(r['replies'] or [''])[-1][:110]!r}")

    out = HERE / "results" / f"{datetime.now():%Y%m%d-%H%M%S}-{config.LLM_MODE}-{args.prompt}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"mode": config.LLM_MODE, "model": config.OLLAMA_MODEL, "prompt": args.prompt, "results": results}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved {out.relative_to(HERE.parent)}")


if __name__ == "__main__":
    main()
