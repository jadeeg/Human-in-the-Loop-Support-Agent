"""Minimal RAG: chunk the markdown policies by heading and rank with an IDF-weighted keyword score.

Phase-4 upgrade path: swap `retrieve()` internals for Chroma + nomic-embed-text.
Keep the return shape identical and nothing else in the project changes.
"""
import math
import re
from functools import lru_cache

from . import config

STOP = {
    "the", "and", "for", "that", "this", "with", "can", "are", "has", "have", "not", "was", "were", "its",
    "from", "will", "must", "any", "after", "before", "only", "been", "who", "what", "how", "when", "does",
    "you", "your", "may", "than", "then", "into", "once",
}


def _tokens(text):
    """Lowercase words, stop-words removed, crude 4-letter stem ('cancel/cancelled/cancellation' -> 'canc').
    This is deliberately naive: a good first reason to upgrade to embeddings later and measure the difference."""
    words = [t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 2 and t not in STOP]
    return [w[:4] for w in words]


@lru_cache(maxsize=1)
def _chunks():
    chunks = []
    for path in sorted(config.KNOWLEDGE_DIR.glob("*.md")):
        title, heading, buf = "", "", []

        def flush():
            body = " ".join(buf).strip()
            if body:
                chunks.append({"source": path.name, "heading": heading or title, "text": body})

        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
            elif line.startswith("## "):
                flush()
                heading, buf = line[3:].strip(), []
            elif line.strip():
                buf.append(line.strip())
        flush()
    for ch in chunks:
        ch["tokens"] = set(_tokens(ch["heading"] + " " + ch["text"]))
    return chunks


def reload():
    _chunks.cache_clear()


def retrieve(query: str, k: int = 2):
    chunks = _chunks()
    q = set(_tokens(query))
    n = len(chunks) or 1
    df = {t: sum(1 for c in chunks if t in c["tokens"]) for t in q}
    scored = []
    for c in chunks:
        score = sum(math.log(1 + n / (1 + df[t])) for t in q if t in c["tokens"])
        if score > 0:
            scored.append((score, c))
    scored.sort(key=lambda x: -x[0])
    return [
        {"source": c["source"], "heading": c["heading"], "text": c["text"], "score": round(s, 3)}
        for s, c in scored[:k]
    ]


def all_documents():
    return [{"source": c["source"], "heading": c["heading"], "text": c["text"]} for c in _chunks()]
