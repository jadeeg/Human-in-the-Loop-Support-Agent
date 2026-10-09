"""Deterministic business rules.

The LLM never decides eligibility. Code does, so it is testable and auditable.
The RAG passages (knowledge/*.md) explain the rules to humans; these functions enforce them.
Keep both in sync.
"""
from datetime import timedelta

from . import config, db


def _result(eligible, rule, reason, **extra):
    return {"eligible": eligible, "rule": rule, "reason": reason, **extra}


def evaluate(action: str, order: dict) -> dict:
    if action == "cancel_order":
        return _cancel(order)
    if action == "issue_refund":
        return _refund(order)
    return _result(False, "UNKNOWN", f"No policy defined for {action}.")


def _cancel(order):
    status = order["status"]
    if status == "cancelled":
        return _result(False, "CANCEL-0", "This order has already been cancelled.")
    if status in ("shipped", "delivered"):
        return _result(False, "CANCEL-2", "This order has already shipped, so it can no longer be cancelled.")
    age = db.now() - db.parse(order["placed_at"])
    if age > timedelta(hours=config.CANCEL_WINDOW_HOURS):
        return _result(False, "CANCEL-1", f"The {config.CANCEL_WINDOW_HOURS}-hour cancellation window has passed.")
    remaining = round(order["amount"] - order["refunded_amount"], 2)
    return _result(True, "CANCEL-OK", "Order is within the cancellation window and has not shipped.", refund_amount=remaining)


def _refund(order):
    status = order["status"]
    remaining = round(order["amount"] - order["refunded_amount"], 2)
    if order["refunded_amount"] > 0 or remaining <= 0:
        return _result(False, "REFUND-3", "This order has already been refunded.")
    if status != "delivered":
        return _result(False, "REFUND-2", "Only delivered orders can be refunded. If the order has not shipped, it can be cancelled instead.")
    age = db.now() - db.parse(order["delivered_at"])
    if age > timedelta(days=config.REFUND_WINDOW_DAYS):
        return _result(False, "REFUND-1", f"The {config.REFUND_WINDOW_DAYS}-day refund window has passed.")
    return _result(True, "REFUND-OK", "Order was delivered within the refund window.", max_amount=remaining)
