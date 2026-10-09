"""Central configuration. Everything can be overridden with environment variables."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ROOT = BASE_DIR.parent

DB_PATH = os.getenv("HITL_DB", str(BASE_DIR / "data" / "support.db"))
KNOWLEDGE_DIR = ROOT / "knowledge"
PROMPTS_DIR = BASE_DIR / "prompts"
PERSONA_FILE = ROOT / "conversation-design" / "persona.md"
RESPONSES_FILE = ROOT / "conversation-design" / "responses.json"


LLM_MODE = os.getenv("LLM_MODE", "mock")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
PROMPT_VERSION = os.getenv("PROMPT_VERSION", "v1")

CANCEL_WINDOW_HOURS = 24
REFUND_WINDOW_DAYS = 30
APPROVAL_TTL_MINUTES = 30
MAX_AGENT_STEPS = 5

REVIEWER_KEY = os.getenv("REVIEWER_KEY", "dev-reviewer")
