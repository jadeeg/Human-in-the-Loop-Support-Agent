"""FastAPI app. Run from the repo root:  uvicorn backend.main:app --reload"""
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import agent, config, db, rag, risk


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Human-in-the-Loop Support Agent", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_reviewer(x_reviewer_key: str = Header(default="")):
    if x_reviewer_key != config.REVIEWER_KEY:
        raise HTTPException(status_code=401, detail="Reviewer key required")


class ChatIn(BaseModel):
    message: str
    session_id: str | None = None


class RejectIn(BaseModel):
    reason: str = ""


@app.post("/chat")
def chat(body: ChatIn):
    text = body.message.strip()[:1000]
    if not text:
        raise HTTPException(400, "Empty message")
    sid = body.session_id or uuid.uuid4().hex[:12]
    return {"session_id": sid, **agent.run_turn(sid, text)}


@app.get("/sessions/{sid}/messages")
def session_messages(sid: str):
    risk.expire_stale()
    return {"messages": db.visible_messages(sid), "pending": db.pending_for_session(sid)}


@app.get("/policies")
def policies():
    return {"passages": rag.all_documents()}


@app.get("/health")
def health():
    return {"ok": True, "llm_mode": config.LLM_MODE, "model": config.OLLAMA_MODEL, "prompt": config.PROMPT_VERSION}


@app.get("/approvals", dependencies=[Depends(require_reviewer)])
def approvals(status: str = "pending"):
    risk.expire_stale()
    return {"approvals": db.list_approvals(status)}


@app.get("/approvals/{request_id}", dependencies=[Depends(require_reviewer)])
def approval(request_id: int):
    a = db.get_approval(request_id)
    if not a:
        raise HTTPException(404, "Not found")
    return a


@app.post("/approvals/{request_id}/approve", dependencies=[Depends(require_reviewer)])
def approve(request_id: int):
    result = risk.approve(request_id, reviewer="reviewer")
    if "error" in result:
        raise HTTPException(409 if result["error"] != "not_found" else 404, result)
    return result


@app.post("/approvals/{request_id}/reject", dependencies=[Depends(require_reviewer)])
def reject(request_id: int, body: RejectIn):
    result = risk.reject(request_id, body.reason, reviewer="reviewer")
    if "error" in result:
        raise HTTPException(409 if result["error"] != "not_found" else 404, result)
    return result


@app.get("/admin/orders/{order_id}", dependencies=[Depends(require_reviewer)])
def admin_order(order_id: str):
    o = db.get_order(order_id)
    if not o:
        raise HTTPException(404, "Not found")
    return o


@app.get("/audit", dependencies=[Depends(require_reviewer)])
def audit(session_id: str | None = None, limit: int = 200):
    return {"events": db.get_audit(session_id, min(limit, 500))}


@app.post("/admin/reset", dependencies=[Depends(require_reviewer)])
def reset_demo():
    """Demo helper: wipe everything and re-seed."""
    db.reset()
    return {"ok": True}
