"""A local double of the `wfqueue` HTTP API (Phase 7 Step 0).

The real queue runs on the NAS, which the development machine cannot route to, and the colleague
shared the client (`docs/queue-docs/queue-client-v0.py`), not the server. This file is the
*documented* contract -- `docs/queue-docs/queue-doc-v0.json` -- as a stdlib `http.server` on
localhost, SQLite-backed so a restart keeps its messages, so the studio's submit and the rig
agent can be developed here with `WFQUEUE_URL=http://localhost:9090`.

    python -m tests.support.fake_wfqueue --port 9090          # from the repo root

It exists to exercise *our* code, not to stand in for the queue in any claim: **when the real
server disagrees with it, this file is wrong** and is corrected. Where the doc is silent, the
choice made here is a guess, listed so Step 7 can check each one against the real server:

- `attempts` counts deliveries: it is incremented by every lease, including the re-lease after
  an expired lease, so a message dropped mid-run comes back with `attempts == 2`.
- A nack without `retry_after` backs off `2 ** (attempts - 1)` seconds, capped at an hour.
- An expired lease returns the message to `ready`, or to `dead` when `attempts` has already
  reached `max_attempts`.
- `requeue` resets `attempts` to 0 (otherwise a requeued dead message dies again on its first
  nack). `last_error` is kept.
- `lease`, `list`, `stats` on a topic that was never created answer 404, as the doc's error table
  reads literally. The agent therefore creates its topic before it first leases.
- The consumer label is stored and listed under the key `consumer`.
- `stats` answers `{"topic", "counts": {state: n}, "depth", "oldest_ready_age",
  "next_available_at"}`.
- **The message row's field set.** Thirteen of the fifteen names below are the doc's own --
  `id`, `topic`, `payload`, `priority`, `state`, `attempts`, `max_attempts`, `dedupe_key`,
  `lease_id`, `lease_expires_at`, `consumer`, `last_error`, `available_at` -- and the client
  reads five of them off a leased message (`LeasedMessage.__slots__`, `queue-client-v0.py:55`).
  `created_at` and `updated_at` appear nowhere in the doc: they are this file's invention, so
  nothing of ours may depend on them until the real server is seen to send them.

What is deliberately *not* here: the six human-facing routes of the doc's twenty-four -- the HTML
console at `/admin` and `/admin/list`, its form posts `/admin/delete` and `/admin/compact`, and
key management at `/admin/keys` and `/admin/keys/revoke`. The doc calls the console "for humans,
not for code", and nothing of ours will ever call it. `/admin/reap` *is* here, because an agent
test may need to force a sweep.

Never imported by `src/`.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

# The real server serves its own client at this path, stamped with its address in a leading
# comment; ours serves the copy that comment came from, unstamped. It is how a consumer
# bootstraps, so the fake answers it too.
CLIENT_ROUTE = "/source/client.py"
CLIENT_SOURCE = Path(__file__).resolve().parents[2] / "docs" / "queue-docs" / "queue-client-v0.py"

TOPIC_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
STATES = ("ready", "leased", "done", "dead")
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_VISIBILITY_TIMEOUT = 30.0
BACKOFF_CAP_S = 3600.0
# How often a long-polling lease wakes to look for a delayed message that has just become due.
POLL_TICK_S = 0.25

SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
    name        TEXT PRIMARY KEY,
    config      TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    topic            TEXT NOT NULL REFERENCES topics(name),
    payload          TEXT NOT NULL,
    priority         INTEGER NOT NULL DEFAULT 0,
    state            TEXT NOT NULL DEFAULT 'ready',
    attempts         INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 5,
    dedupe_key       TEXT,
    lease_id         TEXT,
    lease_expires_at REAL,
    consumer         TEXT,
    last_error       TEXT,
    created_at       REAL NOT NULL,
    available_at     REAL NOT NULL,
    updated_at       REAL NOT NULL,
    UNIQUE (topic, dedupe_key)
);
CREATE INDEX IF NOT EXISTS messages_by_topic_state ON messages (topic, state, available_at);
"""


class ApiError(Exception):
    """Answered as `{"error": message, "status": status}`, the doc's error shape."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _backoff(attempts: int) -> float:
    return min(2.0 ** max(attempts - 1, 0), BACKOFF_CAP_S)


def _number(body: dict, key: str, default: float, *, lo: float, hi: float | None = None) -> float:
    value = body.get(key, default)
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError(400, f"{key} must be a number")
    if value < lo or (hi is not None and value > hi):
        raise ApiError(400, f"{key} out of range")
    return float(value)


class Store:
    """The queue's state: one SQLite connection, one lock, one condition for long-polls."""

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)
        self.started_at = time.time()

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # ------------------------------------------------------------------ helpers (lock held)

    @staticmethod
    def _row(row: sqlite3.Row | None, **extra: Any) -> dict:
        if row is None:
            raise ApiError(404, "no such message")
        out = dict(row)
        out["payload"] = json.loads(out["payload"])
        out.update(extra)
        return out

    def _fetch(self, message_id: int, **extra: Any) -> dict:
        row = self.conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return self._row(row, **extra)

    def _topic_exists(self, topic: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM topics WHERE name = ?", (topic,)).fetchone()
        return row is not None

    def _require_topic(self, topic: str) -> None:
        if not self._topic_exists(topic):
            raise ApiError(404, f"no such topic: {topic}")

    def _ensure_topic(self, topic: str, config: dict | None = None) -> bool:
        if not TOPIC_NAME.match(topic):
            raise ApiError(400, f"bad topic name: {topic!r}")
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO topics (name, config, created_at) VALUES (?, ?, ?)",
            (topic, json.dumps(config or {}), time.time()),
        )
        return cur.rowcount == 1

    def _reap(self, now: float) -> int:
        """Expired leases go back to `ready`, or to `dead` once the attempts are spent."""
        with self.conn:
            cur = self.conn.execute(
                """UPDATE messages
                   SET state = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'ready' END,
                       last_error = CASE WHEN attempts >= max_attempts
                                         THEN 'lease expired' ELSE last_error END,
                       lease_id = NULL, lease_expires_at = NULL, available_at = ?, updated_at = ?
                   WHERE state = 'leased' AND lease_expires_at <= ?""",
                (now, now, now),
            )
        if cur.rowcount:
            self.changed.notify_all()
        return cur.rowcount

    def _leased(self, message_id: int, lease_id: str | None, now: float) -> sqlite3.Row:
        """The row, if the caller still holds its lease; 404 or 409 otherwise."""
        self._reap(now)
        row = self.conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        if row is None:
            raise ApiError(404, "no such message")
        if row["state"] != "leased":
            raise ApiError(409, f"message {message_id} is not leased (state {row['state']})")
        if not lease_id or row["lease_id"] != lease_id:
            raise ApiError(409, "wrong or expired lease_id")
        return row

    def _counts(self, topic: str) -> dict[str, int]:
        counts = dict.fromkeys(STATES, 0)
        for state, n in self.conn.execute(
            "SELECT state, COUNT(*) FROM messages WHERE topic = ? GROUP BY state", (topic,)
        ):
            counts[state] = n
        return counts

    # ------------------------------------------------------------------ topics

    def topics(self) -> dict:
        with self.lock:
            self._reap(time.time())
            names = [r["name"] for r in self.conn.execute("SELECT name FROM topics ORDER BY name")]
            return {"topics": [{"name": n, "counts": self._counts(n)} for n in names]}

    def create_topic(self, name: str, config: dict | None) -> dict:
        with self.lock, self.conn:
            created = self._ensure_topic(name, config)
            return {"name": name, "config": config or {}, "created": created}

    def delete_topic(self, topic: str, purge: bool) -> dict:
        with self.lock, self.conn:
            self._require_topic(topic)
            pending = self.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE topic = ? AND state IN ('ready', 'leased')",
                (topic,),
            ).fetchone()[0]
            if pending and not purge:
                raise ApiError(409, f"topic {topic} has {pending} pending message(s)")
            deleted = self.conn.execute("DELETE FROM messages WHERE topic = ?", (topic,)).rowcount
            self.conn.execute("DELETE FROM topics WHERE name = ?", (topic,))
            return {"deleted": True, "topic": topic, "messages": deleted}

    def stats(self, topic: str) -> dict:
        with self.lock:
            now = time.time()
            self._reap(now)
            self._require_topic(topic)
            depth, oldest = self.conn.execute(
                """SELECT COUNT(*), MIN(created_at) FROM messages
                   WHERE topic = ? AND state = 'ready' AND available_at <= ?""",
                (topic, now),
            ).fetchone()
            (next_due,) = self.conn.execute(
                """SELECT MIN(available_at) FROM messages
                   WHERE topic = ? AND state = 'ready' AND available_at > ?""",
                (topic, now),
            ).fetchone()
            return {
                "topic": topic,
                "counts": self._counts(topic),
                "depth": depth,
                "oldest_ready_age": None if oldest is None else now - oldest,
                "next_available_at": next_due,
            }

    # ------------------------------------------------------------------ producing

    def put(self, topic: str, item: dict) -> dict:
        if not isinstance(item, dict) or "payload" not in item:
            raise ApiError(400, "payload is required")
        priority = item.get("priority", 0)
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ApiError(400, "priority must be an integer")
        delay = _number(item, "delay", 0.0, lo=0.0)
        max_attempts = item.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ApiError(400, "max_attempts must be a positive integer")
        dedupe_key = item.get("dedupe_key")
        if dedupe_key is not None and not isinstance(dedupe_key, str):
            raise ApiError(400, "dedupe_key must be a string")
        with self.lock, self.conn:
            self._ensure_topic(topic)
            if dedupe_key is not None:
                existing = self.conn.execute(
                    "SELECT * FROM messages WHERE topic = ? AND dedupe_key = ?", (topic, dedupe_key)
                ).fetchone()
                if existing is not None:
                    return self._row(existing, duplicate=True)
            now = time.time()
            cur = self.conn.execute(
                """INSERT INTO messages (topic, payload, priority, max_attempts, dedupe_key,
                                         created_at, available_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (topic, json.dumps(item["payload"]), priority, max_attempts, dedupe_key,
                 now, now + delay, now),
            )
            self.changed.notify_all()
            return self._fetch(cur.lastrowid, duplicate=False)

    # ------------------------------------------------------------------ consuming

    def lease(self, topic: str, body: dict) -> dict:
        count = int(_number(body, "count", 1, lo=1, hi=1000))
        visibility = _number(body, "visibility_timeout", DEFAULT_VISIBILITY_TIMEOUT, lo=0.0)
        wait = _number(body, "wait", 0.0, lo=0.0)
        consumer = body.get("consumer")
        if consumer is not None and not isinstance(consumer, str):
            raise ApiError(400, "consumer must be a string")
        deadline = time.monotonic() + wait
        with self.lock:
            self._require_topic(topic)
            while True:
                now = time.time()
                self._reap(now)
                rows = self.conn.execute(
                    """SELECT * FROM messages
                       WHERE topic = ? AND state = 'ready' AND available_at <= ?
                       ORDER BY priority DESC, id ASC LIMIT ?""",
                    (topic, now, count),
                ).fetchall()
                if rows:
                    out = []
                    with self.conn:
                        for row in rows:
                            lease_id = secrets.token_hex(16)
                            self.conn.execute(
                                """UPDATE messages
                                   SET state = 'leased', attempts = attempts + 1, lease_id = ?,
                                       lease_expires_at = ?, consumer = ?, updated_at = ?
                                   WHERE id = ?""",
                                (lease_id, now + visibility, consumer, now, row["id"]),
                            )
                            out.append(self._fetch(row["id"]))
                    return {"messages": out}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"messages": []}
                self.changed.wait(timeout=min(remaining, POLL_TICK_S))

    def ack(self, message_id: int, body: dict) -> dict:
        with self.lock, self.conn:
            now = time.time()
            row = self._leased(message_id, body.get("lease_id"), now)
            self.conn.execute(
                """UPDATE messages SET state = 'done', lease_id = NULL, lease_expires_at = NULL,
                                       updated_at = ? WHERE id = ?""",
                (now, row["id"]),
            )
            return self._fetch(row["id"])

    def nack(self, message_id: int, body: dict) -> dict:
        error = body.get("error")
        if error is not None and not isinstance(error, str):
            raise ApiError(400, "error must be a string")
        dead = bool(body.get("dead", False))
        retry_after = body.get("retry_after")
        if retry_after is not None:
            retry_after = _number(body, "retry_after", 0.0, lo=0.0)
        with self.lock, self.conn:
            now = time.time()
            row = self._leased(message_id, body.get("lease_id"), now)
            if dead or row["attempts"] >= row["max_attempts"]:
                self.conn.execute(
                    """UPDATE messages SET state = 'dead', lease_id = NULL, lease_expires_at = NULL,
                                           last_error = ?, updated_at = ? WHERE id = ?""",
                    (error, now, row["id"]),
                )
            else:
                delay = _backoff(row["attempts"]) if retry_after is None else retry_after
                self.conn.execute(
                    """UPDATE messages
                       SET state = 'ready', lease_id = NULL, lease_expires_at = NULL,
                           last_error = ?, available_at = ?, updated_at = ?
                       WHERE id = ?""",
                    (error, now + delay, now, row["id"]),
                )
            self.changed.notify_all()
            return self._fetch(row["id"])

    def extend(self, message_id: int, body: dict) -> dict:
        visibility = _number(body, "visibility_timeout", DEFAULT_VISIBILITY_TIMEOUT, lo=0.0)
        with self.lock, self.conn:
            now = time.time()
            row = self._leased(message_id, body.get("lease_id"), now)
            self.conn.execute(
                "UPDATE messages SET lease_expires_at = ?, updated_at = ? WHERE id = ?",
                (now + visibility, now, row["id"]),
            )
            return self._fetch(row["id"])

    # ------------------------------------------------------------------ reading

    def list(self, topic: str, query: dict[str, str]) -> dict:
        state = query.get("state")
        if state is not None and state not in STATES:
            raise ApiError(400, f"unknown state: {state}")
        try:
            limit = int(query.get("limit", 50))
            offset = int(query.get("offset", 0))
        except ValueError:
            raise ApiError(400, "limit and offset must be integers") from None
        if not 1 <= limit <= 1000 or offset < 0:
            raise ApiError(400, "limit or offset out of range")
        order = query.get("order", "asc")
        if order not in ("asc", "desc"):
            raise ApiError(400, "order must be asc or desc")
        with_payload = query.get("payload", "true") != "false"
        with self.lock:
            self._reap(time.time())
            self._require_topic(topic)
            sql = "SELECT * FROM messages WHERE topic = ?"
            args: list[Any] = [topic]
            if state is not None:
                sql += " AND state = ?"
                args.append(state)
            sql += f" ORDER BY id {'ASC' if order == 'asc' else 'DESC'} LIMIT ? OFFSET ?"
            args += [limit, offset]
            messages = [self._row(r) for r in self.conn.execute(sql, args)]
            if not with_payload:
                for m in messages:
                    m.pop("payload", None)
            return {"topic": topic, "messages": messages, "limit": limit, "offset": offset}

    def get(self, message_id: int) -> dict:
        with self.lock:
            self._reap(time.time())
            return self._fetch(message_id)

    def delete(self, message_id: int) -> dict:
        with self.lock, self.conn:
            if self.conn.execute("DELETE FROM messages WHERE id = ?", (message_id,)).rowcount == 0:
                raise ApiError(404, "no such message")
            return {"deleted": True, "id": message_id}

    # ------------------------------------------------------------------ lifecycle

    def purge(self, topic: str, body: dict) -> dict:
        state = body.get("state")
        if state is not None and state not in STATES:
            raise ApiError(400, f"unknown state: {state}")
        older_than = body.get("older_than")
        if older_than is not None:
            older_than = _number(body, "older_than", 0.0, lo=0.0)
        with self.lock, self.conn:
            self._require_topic(topic)
            sql, args = "DELETE FROM messages WHERE topic = ?", [topic]
            if state is not None:
                sql += " AND state = ?"
                args.append(state)
            if older_than is not None:
                sql += " AND created_at <= ?"
                args.append(time.time() - older_than)
            return {"purged": self.conn.execute(sql, args).rowcount, "topic": topic}

    def requeue(self, topic: str, body: dict) -> dict:
        state = body.get("state", "dead")
        if state not in STATES:
            raise ApiError(400, f"unknown state: {state}")
        limit = int(_number(body, "limit", 1000, lo=1))
        with self.lock, self.conn:
            self._require_topic(topic)
            ids = [r["id"] for r in self.conn.execute(
                "SELECT id FROM messages WHERE topic = ? AND state = ? ORDER BY id LIMIT ?",
                (topic, state, limit),
            )]
            now = time.time()
            for message_id in ids:
                self.conn.execute(
                    """UPDATE messages SET state = 'ready', attempts = 0, lease_id = NULL,
                                           lease_expires_at = NULL, available_at = ?, updated_at = ?
                       WHERE id = ?""",
                    (now, now, message_id),
                )
            if ids:
                self.changed.notify_all()
            return {"requeued": len(ids), "ids": ids, "topic": topic}

    def reap(self) -> dict:
        with self.lock:
            return {"reaped": self._reap(time.time())}

    def health(self) -> dict:
        with self.lock:
            topics = self.conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
            messages = self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        return {
            "ok": True, "service": "wfqueue (fake, tests/support/fake_wfqueue.py)",
            "db": self.path, "topics": topics, "messages": messages,
            "uptime": time.time() - self.started_at,
        }


DESCRIBE = {
    "service": "wfqueue",
    "version": "fake",
    "note": "A local double of docs/queue-docs/queue-doc-v0.json. When the real server disagrees "
            "with this one, this one is wrong.",
    "endpoints": [
        "GET /", "GET /health", "GET /source/client.py", "GET /topics", "POST /topics",
        "GET /topics/{topic}/stats",
        "DELETE /topics/{topic}?purge=true", "POST /topics/{topic}/messages",
        "GET /topics/{topic}/messages?state=&limit=&offset=&order=&payload=",
        "POST /topics/{topic}/lease", "POST /topics/{topic}/purge", "POST /topics/{topic}/requeue",
        "GET /messages/{id}", "DELETE /messages/{id}", "POST /messages/{id}/ack",
        "POST /messages/{id}/nack", "POST /messages/{id}/extend", "POST /admin/reap",
    ],
}

_TOPIC_ROUTE = re.compile(r"/topics/([^/]+)(?:/(stats|messages|lease|purge|requeue))?\Z")
_MESSAGE_ROUTE = re.compile(r"/messages/(\d+)(?:/(ack|nack|extend))?\Z")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: FakeQueueServer  # set by ThreadingHTTPServer

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.server.verbose:
            super().log_message(fmt, *args)

    # ---- plumbing

    def _send(self, status: int, body: Any) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_source(self) -> None:
        """`GET /source/client.py`: the only route whose answer is not JSON."""
        if not CLIENT_SOURCE.exists():
            raise ApiError(404, f"no client source at {CLIENT_SOURCE}")
        data = CLIENT_SOURCE.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/x-python; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            raise ApiError(400, "malformed JSON body") from None
        if not isinstance(body, dict):
            raise ApiError(400, "body must be a JSON object")
        return body

    def _dispatch(self, method: str) -> None:
        try:
            url = urlsplit(self.path)
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            if method == "GET" and url.path == CLIENT_ROUTE:
                self._send_source()
                return
            self._send(200, self._route(method, url.path, query))
        except ApiError as exc:
            self._send(exc.status, {"error": exc.message, "status": exc.status})
        except Exception as exc:  # a bug in the fake, not a contract answer
            self._send(500, {"error": f"{type(exc).__name__}: {exc}", "status": 500})

    def do_GET(self) -> None:  # noqa: N802 (http.server's names)
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    # ---- routes

    def _route(self, method: str, path: str, query: dict[str, str]) -> Any:
        store = self.server.store
        if path == "/" and method == "GET":
            return DESCRIBE
        if path == "/health" and method == "GET":
            return store.health()
        if path == "/topics":
            if method == "GET":
                return store.topics()
            if method == "POST":
                body = self._body()
                name = body.get("name")
                if not isinstance(name, str):
                    raise ApiError(400, "name is required")
                return store.create_topic(name, body.get("config"))
        if path == "/admin/reap" and method == "POST":
            return store.reap()

        m = _TOPIC_ROUTE.match(path)
        if m:
            topic, action = unquote(m.group(1)), m.group(2)
            if action is None and method == "DELETE":
                return store.delete_topic(topic, query.get("purge") == "true")
            if action == "stats" and method == "GET":
                return store.stats(topic)
            if action == "messages" and method == "GET":
                return store.list(topic, query)
            if action == "messages" and method == "POST":
                body = self._body()
                if "messages" in body:
                    if not isinstance(body["messages"], list):
                        raise ApiError(400, "messages must be a list")
                    return {"messages": [store.put(topic, item) for item in body["messages"]]}
                return store.put(topic, body)
            if action == "lease" and method == "POST":
                return store.lease(topic, self._body())
            if action == "purge" and method == "POST":
                return store.purge(topic, self._body())
            if action == "requeue" and method == "POST":
                return store.requeue(topic, self._body())

        m = _MESSAGE_ROUTE.match(path)
        if m:
            message_id, action = int(m.group(1)), m.group(2)
            if action is None and method == "GET":
                return store.get(message_id)
            if action is None and method == "DELETE":
                return store.delete(message_id)
            if action == "ack" and method == "POST":
                return store.ack(message_id, self._body())
            if action == "nack" and method == "POST":
                return store.nack(message_id, self._body())
            if action == "extend" and method == "POST":
                return store.extend(message_id, self._body())

        raise ApiError(404, f"no such route: {method} {path}")


class FakeQueueServer(ThreadingHTTPServer):
    """The fake, bound and ready. `start()` serves on a thread; `serve_forever()` blocks.

    `port=0` takes an ephemeral port (the tests do); `db=":memory:"` forgets on close.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *, host: str = "127.0.0.1", port: int = 0, db: str = ":memory:",
                 verbose: bool = False):
        super().__init__((host, port), Handler)
        self.store = Store(db)
        self.verbose = verbose
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> FakeQueueServer:
        # A short poll so `stop()` returns promptly (serve_forever's default is half a second).
        self._thread = threading.Thread(
            target=self.serve_forever, kwargs={"poll_interval": 0.05}, name="fake-wfqueue",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.store.close()

    def __enter__(self) -> FakeQueueServer:
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.support.fake_wfqueue",
        description="A local double of the wfqueue HTTP API. Point WFQUEUE_URL at it.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9090)
    parser.add_argument("--db", default=".studio/fake-wfqueue.sqlite",
                        help="SQLite file; a restart keeps its messages (default: %(default)s)")
    parser.add_argument("--verbose", action="store_true", help="log every request")
    args = parser.parse_args(argv)
    if args.db != ":memory:":
        import os

        os.makedirs(os.path.dirname(args.db) or ".", exist_ok=True)
    server = FakeQueueServer(host=args.host, port=args.port, db=args.db, verbose=args.verbose)
    print(f"fake wfqueue listening on {server.url}  (db: {args.db})", flush=True)
    print(f"  export WFQUEUE_URL={server.url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
