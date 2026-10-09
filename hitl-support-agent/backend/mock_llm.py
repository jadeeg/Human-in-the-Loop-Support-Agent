"""A rule-based stand-in for the LLM.

Why it exists:
  * unit/e2e tests and CI run without Ollama,
  * a public demo can run with no GPU,
  * you get a deterministic baseline to compare real models against in evals.

It has the same signature as a real backend: mock_llm(messages, tool_schemas) -> assistant message.
"""
import json
import re

from . import responses

ORDER_RE = re.compile(r"\b(\d{5})\b")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
YES_RE = re.compile(r"^\s*(yes|yep|yeah|sure|ok|okay|please do|go ahead|confirm|do it)\b", re.I)
THANKS_RE = re.compile(r"\b(thanks|thank you)\b", re.I)

INTENTS = [
    ("cancel_order", ("cancel",)),
    ("issue_refund", ("refund", "money back", "reimburse")),
    ("track_shipment", ("track", "where is", "shipping status", "shipment", "delivery status")),
]


def _tool_call(name, args):
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}


def _say(key, **kw):
    return {"role": "assistant", "content": responses.render(key, **kw)}


def _intent_in(text):
    low = text.lower()
    for name, words in INTENTS:
        if any(w in low for w in words):
            return name
    return None


def _is_ask(content):
    return "order number" in content.lower() or "email address" in content.lower()


def _resolve_intent(messages):
    """Latest intent, looking back only across our own 'please give me X' questions."""
    for m in reversed(messages):
        if m["role"] == "user":
            found = _intent_in(m["content"])
            if found:
                return found
        elif m["role"] == "assistant" and m.get("content") and not m.get("tool_calls"):
            if not _is_ask(m["content"]):
                return None
    return None


def _latest(regex, texts):
    for t in reversed(texts):
        found = regex.findall(t)
        if found:
            return found[-1]
    return None


def _last_tool(messages):
    for m in reversed(messages):
        if m["role"] == "tool":
            return m["tool_name"], json.loads(m["content"])
        if m["role"] == "user":
            return None
    return None


def _previous_assistant_text(messages):
    for m in reversed(messages[:-1]):
        if m["role"] == "assistant" and m.get("content") and not m.get("tool_calls"):
            return m["content"]
        if m["role"] == "user":
            continue
    return ""


def mock_llm(messages, tool_schemas):
    users = [m["content"] for m in messages if m["role"] == "user"]
    last = messages[-1]

    if last["role"] == "tool":
        name, result = _last_tool(messages)
        intent = _resolve_intent(messages)
        return _after_tool(name, result, intent)

    if YES_RE.match(last["content"]):
        prev = _previous_assistant_text(messages)
        if "submit the cancellation request" in prev or "submit the refund request" in prev:
            order_id = ORDER_RE.search(prev).group(1)
            if "cancellation" in prev:
                return _tool_call("cancel_order", {"order_id": order_id, "reason": "Customer requested cancellation"})
            return _tool_call("issue_refund", {"order_id": order_id, "reason": "Customer requested refund"})

    intent = _resolve_intent(messages)
    if not intent:
        return _say("thanks") if THANKS_RE.search(last["content"]) else _say("out_of_scope")
    order_id, email = _latest(ORDER_RE, users), _latest(EMAIL_RE, users)
    if not order_id:
        return _say("ask_order")
    if not email:
        return _say("ask_email")
    return _tool_call("check_order", {"order_id": order_id, "email": email})


def _after_tool(name, result, intent):
    if name == "check_order":
        if "error" in result:
            return _say("verification_failed")
        o = result["order"]
        if intent == "cancel_order":
            c = result["cancellation"]
            if not c["eligible"]:
                return _say("denied", order_id=o["order_id"], reason=c["reason"])
            return _say("confirm_cancel", order_id=o["order_id"], items=", ".join(o["items"]), amount=responses.money(c["refund_amount"]))
        if intent == "issue_refund":
            r = result["refund"]
            if not r["eligible"]:
                return _say("denied", order_id=o["order_id"], reason=r["reason"])
            return _say("confirm_refund", order_id=o["order_id"], amount=responses.money(r["max_amount"]))
        if intent == "track_shipment":
            return _tool_call("track_shipment", {"order_id": o["order_id"]})
        return _say("order_status", order_id=o["order_id"], status=o["status"])
    if name == "track_shipment":
        if "error" in result:
            return _say("verification_failed")
        line = f"Tracking number: {result['tracking_number']}." if result["tracking_number"] else "It has no tracking number yet because it hasn't shipped."
        return _say("tracking", order_id=result["order_id"], status=result["status"], tracking_line=line)
    if name in ("cancel_order", "issue_refund"):
        if result.get("status") == "denied":
            return {"role": "assistant", "content": f"I can't do that. {result['reason']}"}
        return {"role": "assistant", "content": "Your request has been sent for approval."}
    return _say("out_of_scope")
