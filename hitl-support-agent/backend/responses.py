"""Customer-facing wording that must NOT depend on the LLM (approval status, errors, notices).

The text lives in conversation-design/responses.json so the conversation designer can edit it
without touching Python.
"""
import json

from . import config

ACTION_LABELS = {"cancel_order": "cancellation request", "issue_refund": "refund request"}


def money(x) -> str:
    return f"${float(x):.2f}"


class _Safe(dict):
    """Missing placeholders render as empty text instead of raising."""

    def __missing__(self, key):
        return ""


def _load():
    return json.loads(config.RESPONSES_FILE.read_text(encoding="utf-8"))


def render(key: str, **kw) -> str:
    templates = _load()
    text = templates.get(key, key)
    try:
        return text.format_map(_Safe(kw))
    except (ValueError, IndexError):
        return text
