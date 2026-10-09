"""The agent loop: LLM <-> tools, with every tool call routed through the risk gate."""
import json
import urllib.request

from . import config, db, responses, risk, tools


def ollama_chat(messages, tool_schemas):
    payload = json.dumps(
        {
            "model": config.OLLAMA_MODEL,
            "messages": messages,
            "tools": tool_schemas,
            "stream": False,
            "options": {"temperature": 0.2},
        }
    ).encode()
    req = urllib.request.Request(
        f"{config.OLLAMA_URL}/api/chat", data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.load(resp)["message"]


def get_llm():
    if config.LLM_MODE == "ollama":
        return ollama_chat
    from .mock_llm import mock_llm

    return mock_llm


def build_system_prompt(version=None):
    text = (config.PROMPTS_DIR / f"{version or config.PROMPT_VERSION}.md").read_text(encoding="utf-8")
    persona = config.PERSONA_FILE.read_text(encoding="utf-8") if config.PERSONA_FILE.exists() else ""
    return text.replace("{{PERSONA}}", persona.strip())


def _for_llm(msg):
    """Strip our private keys (e.g. `notice`) before sending history to the model."""
    out = {k: v for k, v in msg.items() if k in ("role", "content", "tool_calls", "tool_name")}
    out.setdefault("content", "")
    return out


def _parse_args(raw):
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw or {}


def run_turn(session_id: str, user_text: str, llm=None, prompt_version=None) -> dict:
    llm = llm or get_llm()
    db.ensure_session(session_id)
    db.log(session_id, "customer", "USER_MESSAGE", {"text": user_text})
    db.add_message(session_id, {"role": "user", "content": user_text})

    msgs = [{"role": "system", "content": build_system_prompt(prompt_version)}]
    msgs += [_for_llm(m) for m in db.get_messages(session_id)]

    pending = None 
    final = None

    for _ in range(config.MAX_AGENT_STEPS):
        try:
            reply = llm(msgs, tools.TOOL_SCHEMAS)
        except Exception as e:
            db.log(session_id, "system", "LLM_ERROR", {"error": str(e)[:300]})
            tools.escalate(session_id, f"LLM error: {e}"[:300])
            final = responses.render("llm_error")
            break

        calls = reply.get("tool_calls") or []
        if not calls:
            final = (reply.get("content") or "").strip()
            break

        assistant_msg = _for_llm(reply)
        db.add_message(session_id, assistant_msg)
        msgs.append(assistant_msg)

        for call in calls:
            fn = call.get("function", {})
            name, args = fn.get("name", ""), _parse_args(fn.get("arguments"))
            result = risk.request_tool(name, args, session_id)
            if result.get("status") == "pending_approval":
                pending = result["request_id"]
            tool_msg = {"role": "tool", "tool_name": name, "content": json.dumps(result, ensure_ascii=False)}
            db.add_message(session_id, tool_msg)
            msgs.append(tool_msg)
    else:
        db.log(session_id, "system", "MAX_STEPS_REACHED", {})
        tools.escalate(session_id, "Agent exceeded step limit")
        final = responses.render("max_steps")

    if pending is not None:
        req = db.get_approval(pending)
        override = responses.render("approval_pending", order_id=req["order_id"], action_label=req["summary"]["action_label"])
        if final != override:
            db.log(session_id, "system", "REPLY_OVERRIDDEN", {"reason": "pending_approval", "model_reply": final})
        final = override

    if not final:
        final = responses.render("out_of_scope")

    db.add_message(session_id, {"role": "assistant", "content": final})
    db.log(session_id, "ai", "ASSISTANT_REPLY", {"text": final})
    return {"reply": final, "pending_request_id": pending}
