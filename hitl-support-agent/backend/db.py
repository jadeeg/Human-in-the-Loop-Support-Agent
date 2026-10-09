"""SQLite helpers: connection, schema, seed data, audit log, chat storage."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

DB_PATH = config.DB_PATH


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse(s):
    return datetime.fromisoformat(s) if s else None


def connect() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def tx():
    """One transaction: commit on success, rollback on error."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def _use(conn):
    if conn is not None:
        yield conn
    else:
        with tx() as c:
            yield c


SCHEMA = """
CREATE TABLE IF NOT EXISTS customers(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL);

CREATE TABLE IF NOT EXISTS orders(
  order_id TEXT PRIMARY KEY,
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  status TEXT NOT NULL,              -- processing | shipped | delivered | cancelled
  amount REAL NOT NULL,
  items TEXT NOT NULL,               -- JSON list
  placed_at TEXT NOT NULL,
  shipped_at TEXT, delivered_at TEXT, cancelled_at TEXT,
  refunded_amount REAL NOT NULL DEFAULT 0,
  tracking_number TEXT);

CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, customer_id INTEGER, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
  payload TEXT NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS approval_requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  action TEXT NOT NULL,
  order_id TEXT NOT NULL,
  args TEXT NOT NULL,
  summary TEXT NOT NULL,             -- JSON snapshot shown to the reviewer
  status TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected|expired|failed
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  decided_at TEXT, decided_by TEXT, decision_reason TEXT, result TEXT);

-- At most one pending request per (order, action): stops duplicates at the DB level.
CREATE UNIQUE INDEX IF NOT EXISTS uq_pending
  ON approval_requests(order_id, action) WHERE status='pending';

CREATE TABLE IF NOT EXISTS escalations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
  reason TEXT NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, session_id TEXT,
  actor TEXT NOT NULL,               -- customer | ai | system | reviewer
  event TEXT NOT NULL, detail TEXT NOT NULL);
"""


def init_db():
    with tx() as c:
        c.executescript(SCHEMA)
        empty = c.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0
    if empty:
        seed()


def reset():
    """Delete the database file and rebuild it with fresh seed data."""
    p = Path(DB_PATH)
    if p.exists():
        p.unlink()
    init_db()


def seed():
    t = now()

    def ago(**kw):
        return iso(t - timedelta(**kw))

    customers = [
        (1, "Maria Silva", "maria@example.com"),
        (2, "João Santos", "joao@example.com"),
        (3, "Ana Costa", "ana@example.com"),
        (4, "Marc Dubois", "marc@example.com"),
    ]
    orders = [
        ("12345", 1, "processing", 149.00, ["Wireless headphones"], ago(hours=2), None, None, None, 0, None),
        ("12346", 1, "shipped", 59.90, ["Phone case", "Screen protector"], ago(hours=30), ago(hours=6), None, None, 0, "BR123456789"),
        ("12347", 2, "processing", 89.00, ["Desk lamp"], ago(hours=50), None, None, None, 0, None),
        ("12348", 2, "delivered", 120.00, ["Running shoes"], ago(days=12), ago(days=10), ago(days=8), None, 0, "BR222333444"),
        ("12349", 3, "delivered", 75.50, ["Backpack"], ago(days=50), ago(days=48), ago(days=45), None, 0, "BR555666777"),
        ("12350", 3, "cancelled", 39.90, ["USB cable"], ago(days=3), None, None, ago(days=3), 39.90, None),
        ("12351", 4, "processing", 89.90, ["Mechanical keyboard"], ago(hours=1), None, None, None, 0, None),
        ("12352", 4, "delivered", 210.00, ["Monitor stand", "HDMI cable"], ago(days=8), ago(days=7), ago(days=5), None, 0, "BR888999000"),
    ]
    with tx() as c:
        c.executemany("INSERT INTO customers VALUES (?,?,?)", customers)
        for o in orders:
            c.execute(
                "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (o[0], o[1], o[2], o[3], json.dumps(o[4]), o[5], o[6], o[7], o[8], o[9], o[10]),
            )


def log(session_id, actor, event, detail=None, conn=None):
    with _use(conn) as c:
        c.execute(
            "INSERT INTO audit_log(ts, session_id, actor, event, detail) VALUES (?,?,?,?,?)",
            (iso(now()), session_id, actor, event, json.dumps(detail or {}, default=str, ensure_ascii=False)),
        )


def get_audit(session_id=None, limit=200):
    sql, params = "SELECT * FROM audit_log", []
    if session_id:
        sql += " WHERE session_id=?"
        params.append(session_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with tx() as c:
        rows = [dict(r) for r in c.execute(sql, params)]
    for r in rows:
        r["detail"] = json.loads(r["detail"])
    return list(reversed(rows))


def ensure_session(session_id):
    with tx() as c:
        c.execute("INSERT OR IGNORE INTO sessions(id, created_at) VALUES (?,?)", (session_id, iso(now())))


def session_customer_id(session_id):
    with tx() as c:
        row = c.execute("SELECT customer_id FROM sessions WHERE id=?", (session_id,)).fetchone()
    return row["customer_id"] if row else None


def set_session_customer(session_id, customer_id):
    ensure_session(session_id)
    with tx() as c:
        c.execute("UPDATE sessions SET customer_id=? WHERE id=?", (customer_id, session_id))


def add_message(session_id, payload, conn=None):
    with _use(conn) as c:
        c.execute(
            "INSERT INTO messages(session_id, payload, created_at) VALUES (?,?,?)",
            (session_id, json.dumps(payload, ensure_ascii=False), iso(now())),
        )


def get_messages(session_id):
    with tx() as c:
        rows = c.execute("SELECT payload FROM messages WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def visible_messages(session_id):
    """What the customer sees: no tool calls, no tool results."""
    out = []
    for m in get_messages(session_id):
        if m.get("role") in ("user", "assistant") and m.get("content") and not m.get("tool_calls"):
            out.append({"role": m["role"], "content": m["content"], "notice": bool(m.get("notice"))})
    return out


def get_order(order_id, conn=None):
    with _use(conn) as c:
        row = c.execute("SELECT * FROM orders WHERE order_id=?", (str(order_id).strip().lstrip("#"),)).fetchone()
    return dict(row) if row else None


def get_customer(customer_id=None, email=None):
    with tx() as c:
        if customer_id is not None:
            row = c.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        else:
            row = c.execute("SELECT * FROM customers WHERE lower(email)=lower(?)", (email or "",)).fetchone()
    return dict(row) if row else None


def all_orders():
    with tx() as c:
        return [dict(r) for r in c.execute("SELECT * FROM orders ORDER BY order_id")]


def _approval_row(r):
    d = dict(r)
    d["args"] = json.loads(d["args"])
    d["summary"] = json.loads(d["summary"])
    d["result"] = json.loads(d["result"]) if d.get("result") else None
    return d


def get_approval(request_id):
    with tx() as c:
        row = c.execute("SELECT * FROM approval_requests WHERE id=?", (request_id,)).fetchone()
    return _approval_row(row) if row else None


def list_approvals(status=None, limit=100):
    sql, params = "SELECT * FROM approval_requests", []
    if status and status != "all":
        sql += " WHERE status=?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with tx() as c:
        return [_approval_row(r) for r in c.execute(sql, params)]


def pending_for_session(session_id):
    with tx() as c:
        rows = c.execute(
            "SELECT id FROM approval_requests WHERE session_id=? AND status='pending'", (session_id,)
        ).fetchall()
    return [r["id"] for r in rows]
