"""Tools the LLM can request.

LOW-risk tools run immediately (LOW_IMPL).
HIGH-risk tools are *requests*: the LLM-facing call only creates an approval request (see risk.py).
The code that really changes data lives in EXECUTORS and is reachable ONLY from risk.approve().
"""
import json

from . import db, policy, rag

def _fn(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOL_SCHEMAS = [
    _fn(
        "check_order",
        "Look up an order and verify the customer. Requires BOTH the order number and the email used for the order. "
        "Returns the order details and whether it is eligible for cancellation or refund. Always call this first.",
        {
            "order_id": {"type": "string", "description": "Order number, digits only, e.g. 12345"},
            "email": {"type": "string", "description": "Email address used for the order"},
        },
        ["order_id", "email"],
    ),
    _fn(
        "check_policy",
        "Search the company policies (cancellation, refund, shipping). Use for general policy questions.",
        {"topic": {"type": "string", "description": "What to look up, e.g. 'refund window'"}},
        ["topic"],
    ),
    _fn(
        "track_shipment",
        "Get shipping status and tracking number for a verified order.",
        {"order_id": {"type": "string"}},
        ["order_id"],
    ),
    _fn(
        "cancel_order",
        "REQUEST cancellation of an order. This does NOT cancel immediately: it creates an approval request that a human "
        "specialist must approve. Only call after check_order says the order is cancellable AND the customer has confirmed.",
        {"order_id": {"type": "string"}, "reason": {"type": "string", "description": "Customer's reason, short"}},
        ["order_id"],
    ),
    _fn(
        "issue_refund",
        "REQUEST a refund for a delivered order. This does NOT refund immediately: it creates an approval request that a human "
        "specialist must approve. Only call after check_order says the order is refundable AND the customer has confirmed.",
        {
            "order_id": {"type": "string"},
            "reason": {"type": "string", "description": "Customer's reason, short"},
            "amount": {"type": "number", "description": "Optional partial amount. Omit for the full refundable amount."},
        },
        ["order_id"],
    ),
    _fn(
        "escalate",
        "Hand the conversation to a human specialist (angry customer, request outside your scope, repeated failures, "
        "customer explicitly asks for a person).",
        {"reason": {"type": "string"}},
        ["reason"],
    ),
]

SCHEMA_BY_NAME = {t["function"]["name"]: t["function"]["parameters"] for t in TOOL_SCHEMAS}


def public_order(o: dict) -> dict:
    return {
        "order_id": o["order_id"],
        "status": o["status"],
        "amount": o["amount"],
        "items": json.loads(o["items"]),
        "placed_at": o["placed_at"],
        "shipped_at": o["shipped_at"],
        "delivered_at": o["delivered_at"],
        "tracking_number": o["tracking_number"],
    }


def require_verified(session_id: str, order_id):
    """Code-level customer verification: the order must belong to the customer verified in THIS session."""
    order = db.get_order(order_id) if order_id else None
    cust_id = db.session_customer_id(session_id)
    if order is None or cust_id is None or order["customer_id"] != cust_id:
        return None, {"error": "not_verified", "message": "Verify the customer with check_order first."}
    return order, None


def check_order(session_id, order_id, email):
    order = db.get_order(order_id)
    cust = db.get_customer(email=email)
    if not order or not cust or order["customer_id"] != cust["id"]:
        db.log(session_id, "system", "VERIFICATION_FAILED", {"order_id": str(order_id)})
        return {"error": "verification_failed", "message": "Order number and email do not match."}
    db.set_session_customer(session_id, cust["id"])
    db.log(session_id, "system", "CUSTOMER_VERIFIED", {"customer_id": cust["id"], "order_id": order["order_id"]})
    return {
        "order": public_order(order),
        "cancellation": policy.evaluate("cancel_order", order),
        "refund": policy.evaluate("issue_refund", order),
    }


def check_policy(session_id, topic):
    passages = rag.retrieve(topic, k=2)
    db.log(session_id, "system", "POLICY_RETRIEVED", {"topic": topic, "sources": [p["source"] for p in passages]})
    return {"passages": [{"source": p["source"], "heading": p["heading"], "text": p["text"]} for p in passages]}


def track_shipment(session_id, order_id):
    order, err = require_verified(session_id, order_id)
    if err:
        return err
    return {
        "order_id": order["order_id"],
        "status": order["status"],
        "tracking_number": order["tracking_number"],
        "shipped_at": order["shipped_at"],
        "delivered_at": order["delivered_at"],
    }


def escalate(session_id, reason):
    with db.tx() as c:
        c.execute(
            "INSERT INTO escalations(session_id, reason, created_at) VALUES (?,?,?)",
            (session_id, str(reason)[:500], db.iso(db.now())),
        )
    db.log(session_id, "ai", "ESCALATED", {"reason": reason})
    return {"status": "escalated"}


LOW_IMPL = {
    "check_order": check_order,
    "check_policy": check_policy,
    "track_shipment": track_shipment,
    "escalate": escalate,
}


def exec_cancel_order(conn, order: dict, args: dict) -> dict:
    refund = round(order["amount"] - order["refunded_amount"], 2)
    cur = conn.execute(
        "UPDATE orders SET status='cancelled', cancelled_at=?, refunded_amount=amount "
        "WHERE order_id=? AND status='processing'",
        (db.iso(db.now()), order["order_id"]),
    )
    if cur.rowcount != 1:
        raise RuntimeError("Order is no longer cancellable")
    return {"order_id": order["order_id"], "status": "cancelled", "refunded": refund}


def exec_issue_refund(conn, order: dict, args: dict) -> dict:
    remaining = round(order["amount"] - order["refunded_amount"], 2)
    amount = round(float(args.get("amount") or remaining), 2)
    if amount <= 0 or amount > remaining:
        raise RuntimeError("Invalid refund amount")
    cur = conn.execute(
        "UPDATE orders SET refunded_amount=refunded_amount+? WHERE order_id=? AND refunded_amount+?<=amount",
        (amount, order["order_id"], amount),
    )
    if cur.rowcount != 1:
        raise RuntimeError("Refund exceeds order amount")
    return {"order_id": order["order_id"], "refunded": amount}


EXECUTORS = {"cancel_order": exec_cancel_order, "issue_refund": exec_issue_refund}
