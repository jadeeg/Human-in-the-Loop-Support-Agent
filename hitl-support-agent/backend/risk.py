"""The approval gate: the heart of the project.

Rules enforced HERE, in code, never in the prompt:
  1. Every tool has a risk level in RISK. Unknown tools are rejected.
  2. LOW tools execute immediately.
  3. HIGH tools never execute from an LLM call. They validate (verified customer, policy, amount),
     then create an approval request and STOP.
  4. Only approve() runs a high-risk executor, once (atomic claim), only if the request is still fresh,
     and only if the order still satisfies the policy at approval time.
"""
import json
import sqlite3
from datetime import timedelta

from . import config, db, policy, rag, responses, tools

RISK = {
    "check_order": "low",
    "check_policy": "low",
    "track_shipment": "low",
    "escalate": "low",
    "cancel_order": "high",
    "issue_refund": "high",
    "change_address": "high",
    "delete_account": "high",
}

POLICY_QUERY = {
    "cancel_order": "cancellation window shipped approval",
    "issue_refund": "refund window delivered approval",
}


def _clean_args(name, args):
    schema = tools.SCHEMA_BY_NAME[name]
    args = args if isinstance(args, dict) else {}
    allowed = schema["properties"].keys()
    cleaned = {k: v for k, v in args.items() if k in allowed and v not in (None, "")}
    missing = [k for k in schema["required"] if k not in cleaned]
    return cleaned, missing


def request_tool(name: str, args: dict, session_id: str) -> dict:
    """Single entry point for every tool call coming from the LLM."""
    risk = RISK.get(name)
    if risk is None or name not in tools.SCHEMA_BY_NAME:
        db.log(session_id, "ai", "TOOL_REJECTED", {"tool": name, "reason": "unknown_or_unimplemented"})
        return {"error": "unknown_tool"}

    cleaned, missing = _clean_args(name, args)
    db.log(session_id, "ai", "TOOL_REQUESTED", {"tool": name, "args": cleaned, "risk": risk})
    if missing:
        return {"error": "invalid_arguments", "missing": missing}

    if risk == "low":
        result = tools.LOW_IMPL[name](session_id, **cleaned)
        db.log(session_id, "system", "TOOL_EXECUTED", {"tool": name, "ok": "error" not in result})
        return result

    return _request_high_risk(name, cleaned, session_id)


def _request_high_risk(name, args, session_id):
    db.log(session_id, "system", "ACTION_CLASSIFIED_HIGH_RISK", {"tool": name})

    order, err = tools.require_verified(session_id, args["order_id"])
    if err:
        db.log(session_id, "system", "GATE_BLOCKED", {"tool": name, "reason": "customer_not_verified"})
        return err

    decision = policy.evaluate(name, order)
    db.log(session_id, "system", "POLICY_EVALUATED", {"tool": name, "order_id": order["order_id"], **decision})
    if not decision["eligible"]:
        return {"status": "denied", "reason": decision["reason"], "rule": decision["rule"]}

    if name == "issue_refund":
        amount = args.get("amount")
        try:
            amount = round(float(amount), 2) if amount is not None else decision["max_amount"]
        except (TypeError, ValueError):
            return {"status": "denied", "reason": "The refund amount is not valid."}
        if amount <= 0 or amount > decision["max_amount"]:
            return {"status": "denied", "reason": f"The refund amount must be between $0.01 and ${decision['max_amount']:.2f}."}
        args["amount"] = amount
    else:
        amount = decision["refund_amount"]

    customer = db.get_customer(customer_id=order["customer_id"])
    summary = {
        "customer": customer["name"],
        "customer_email": customer["email"],
        "order_id": order["order_id"],
        "items": json.loads(order["items"]),
        "order_status": order["status"],
        "action": name,
        "action_label": responses.ACTION_LABELS[name],
        "amount": amount,
        "risk": "high",
        "policy_decision": decision,
        "policy_passages": rag.retrieve(POLICY_QUERY[name], k=2),
        "reason": args.get("reason") or "Customer request",
    }
    created = db.now()
    try:
        with db.tx() as c:
            cur = c.execute(
                "INSERT INTO approval_requests(session_id, action, order_id, args, summary, created_at, expires_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    session_id, name, order["order_id"], json.dumps(args), json.dumps(summary, ensure_ascii=False),
                    db.iso(created), db.iso(created + timedelta(minutes=config.APPROVAL_TTL_MINUTES)),
                ),
            )
            request_id = cur.lastrowid
    except sqlite3.IntegrityError:
        with db.tx() as c:
            row = c.execute(
                "SELECT id FROM approval_requests WHERE order_id=? AND action=? AND status='pending'",
                (order["order_id"], name),
            ).fetchone()
        db.log(session_id, "system", "APPROVAL_ALREADY_PENDING", {"request_id": row["id"]})
        return {"status": "pending_approval", "request_id": row["id"], "note": "already_pending"}

    db.log(session_id, "system", "APPROVAL_REQUESTED", {"request_id": request_id, "tool": name, "order_id": order["order_id"], "amount": amount})
    return {"status": "pending_approval", "request_id": request_id}


def _notice(conn, session_id, key, **kw):
    db.add_message(session_id, {"role": "assistant", "content": responses.render(key, **kw), "notice": True}, conn=conn)


def approve(request_id: int, reviewer: str = "reviewer") -> dict:
    with db.tx() as c:
        row = c.execute("SELECT * FROM approval_requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            return {"error": "not_found"}
        req = dict(row)
        sid, action, order_id = req["session_id"], req["action"], req["order_id"]
        label = responses.ACTION_LABELS[action]

        if req["status"] != "pending":
            return {"error": "not_pending", "status": req["status"]}

        if db.now() > db.parse(req["expires_at"]):
            c.execute("UPDATE approval_requests SET status='expired', decided_at=? WHERE id=?", (db.iso(db.now()), request_id))
            db.log(sid, "system", "APPROVAL_EXPIRED", {"request_id": request_id}, conn=c)
            _notice(c, sid, "expired", order_id=order_id, action_label=label)
            return {"error": "expired"}

        cur = c.execute(
            "UPDATE approval_requests SET status='approved', decided_at=?, decided_by=? WHERE id=? AND status='pending'",
            (db.iso(db.now()), reviewer, request_id),
        )
        if cur.rowcount != 1:
            return {"error": "not_pending"}

        order = db.get_order(order_id, conn=c)
        decision = policy.evaluate(action, order)
        if not decision["eligible"]:
            c.execute("UPDATE approval_requests SET status='failed', decision_reason=? WHERE id=?", (decision["reason"], request_id))
            db.log(sid, "system", "APPROVAL_RECHECK_FAILED", {"request_id": request_id, **decision}, conn=c)
            _notice(c, sid, "failed", order_id=order_id, action_label=label)
            return {"error": "recheck_failed", "reason": decision["reason"]}

        args = json.loads(req["args"])
        db.log(sid, "reviewer", "APPROVED", {"request_id": request_id, "reviewer": reviewer}, conn=c)
        result = tools.EXECUTORS[action](c, order, args)
        c.execute("UPDATE approval_requests SET result=? WHERE id=?", (json.dumps(result), request_id))
        db.log(sid, "system", "ACTION_EXECUTED", {"request_id": request_id, "tool": action, **result}, conn=c)
        if action == "cancel_order":
            _notice(c, sid, "approved_cancel", order_id=order_id, amount=responses.money(result["refunded"]))
        else:
            _notice(c, sid, "approved_refund", order_id=order_id, amount=responses.money(result["refunded"]))
    return {"status": "approved", "result": result}


def reject(request_id: int, reason: str = "", reviewer: str = "reviewer") -> dict:
    reason = (reason or "").strip()[:500]
    with db.tx() as c:
        row = c.execute("SELECT * FROM approval_requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            return {"error": "not_found"}
        req = dict(row)
        cur = c.execute(
            "UPDATE approval_requests SET status='rejected', decided_at=?, decided_by=?, decision_reason=? "
            "WHERE id=? AND status='pending'",
            (db.iso(db.now()), reviewer, reason, request_id),
        )
        if cur.rowcount != 1:
            return {"error": "not_pending", "status": req["status"]}
        sid = req["session_id"]
        db.log(sid, "reviewer", "REJECTED", {"request_id": request_id, "reviewer": reviewer, "reason": reason}, conn=c)
        _notice(
            c, sid, "rejected", order_id=req["order_id"], action_label=responses.ACTION_LABELS[req["action"]],
            reason_line=(f"Reason: {reason}. " if reason else ""),
        )
    return {"status": "rejected"}


def expire_stale() -> int:
    """Close pending requests past their deadline. Called whenever the reviewer queue is read."""
    count = 0
    for req in db.list_approvals("pending"):
        if db.now() > db.parse(req["expires_at"]):
            with db.tx() as c:
                cur = c.execute(
                    "UPDATE approval_requests SET status='expired', decided_at=? WHERE id=? AND status='pending'",
                    (db.iso(db.now()), req["id"]),
                )
                if cur.rowcount == 1:
                    db.log(req["session_id"], "system", "APPROVAL_EXPIRED", {"request_id": req["id"]}, conn=c)
                    _notice(c, req["session_id"], "expired", order_id=req["order_id"], action_label=responses.ACTION_LABELS[req["action"]])
                    count += 1
    return count
