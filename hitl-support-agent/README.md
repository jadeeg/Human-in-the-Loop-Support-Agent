# Human-in-the-Loop Support Agent

A customer-support AI agent that handles low-risk requests on its own and **routes high-risk actions (cancel, refund) to a human for approval**. Built with FastAPI, SQLite, a small RAG layer, a local LLM (Ollama), and React. Costs $0 to run.

**The core idea:** the LLM never executes a high-risk action. Safety is enforced in code (`backend/risk.py`), not in the prompt.

```
Customer -> Agent (LLM + tools) -> risk gate --low--> execute
                                        |
                                        +--high--> approval request -> human Approve/Reject -> execute -> notify
                                                         every step -> audit_log
```

## Quick start (about 5 minutes, no LLM needed)

```bash
# 1. backend (from the repo root)
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r backend/requirements.txt
uvicorn backend.main:app --reload  # http://127.0.0.1:8000/docs

# 2. frontend (new terminal)
cd frontend
npm install
npm run dev                        # http://localhost:5173
```

Open the app, click an example in **Customer chat**, then go to **Reviewer queue** and approve it. Check **Audit log**.

By default the app uses a rule-based **mock model**, so everything works without a GPU or Ollama.

## Use a real local LLM (free)

```bash
# install Ollama from https://ollama.com, then:
ollama pull llama3.1:8b            # or qwen2.5:7b, or llama3.2:3b on small machines
LLM_MODE=ollama OLLAMA_MODEL=llama3.1:8b uvicorn backend.main:app --reload
# Windows PowerShell:  $env:LLM_MODE="ollama"; $env:OLLAMA_MODEL="llama3.1:8b"; uvicorn backend.main:app --reload
```

## Tests and evaluation

```bash
python -m pytest backend/tests                 # safety tests for the gate (23 tests)
python -m evals.run_evals                      # eval suite, mock model
LLM_MODE=ollama OLLAMA_MODEL=llama3.1:8b python -m evals.run_evals --prompt v1
```

Results are saved to `evals/results/`. Compare models and prompt versions (`backend/prompts/v1.md`, `v2.md`, ...) and put the table in your case study.

## Project layout

| Path | What it is | Owner |
|---|---|---|
| `backend/risk.py` | approval gate, approve/reject, expiry, idempotency | engineer |
| `backend/tools.py` | tool schemas and implementations | engineer |
| `backend/policy.py` | deterministic eligibility rules | engineer |
| `backend/rag.py` + `knowledge/` | policy retrieval | engineer |
| `backend/agent.py` | LLM loop with a step cap | engineer |
| `backend/prompts/` | versioned system prompts | engineer |
| `backend/mock_llm.py` | deterministic stand-in model | engineer |
| `evals/` | test cases and metrics | engineer |
| `conversation-design/` | persona, wording, intents, flows | designer |
| `frontend/` | customer chat, reviewer queue, audit log | shared |

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/chat` | customer message |
| GET | `/sessions/{id}/messages` | transcript + pending requests (polled) |
| GET | `/policies` | policy passages |
| GET | `/approvals?status=pending` | reviewer queue (needs `X-Reviewer-Key`) |
| POST | `/approvals/{id}/approve` · `/reject` | human decision |
| GET | `/audit?session_id=` | audit trail |
| GET | `/admin/orders/{id}` | raw order (reviewer only) |
| POST | `/admin/reset` | reset demo data |

Demo data: customers `maria@`, `joao@`, `ana@`, `marc@example.com`; orders `12345` to `12352` (each designed to trigger a different policy outcome).

## Design decisions worth writing about
- **Gate in code, not in the prompt.** The tool the LLM calls only *requests* the action.
- **Eligibility is deterministic.** The LLM explains; `policy.py` decides. RAG passages are attached to the approval request as evidence.
- **Customer verification is code-level** (order + email must match; same error for "no such order" and "wrong email").
- **Approval is safe under concurrency:** atomic claim, expiry, state re-check at approval time, one pending request per order+action (unique index).
- **System-authored wording** for approval status, so the model can never claim "refunded!" before a human approves.
- **Everything is logged** in `audit_log`.
