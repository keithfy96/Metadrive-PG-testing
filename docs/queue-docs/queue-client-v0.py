# Downloaded from http://localhost:8080/source/client.py
# This queue is at: http://localhost:8080
#     from client import QueueClient
#     q = QueueClient("http://localhost:8080")
"""Python client for the wfqueue HTTP API.

This module is self-contained: it imports nothing but the standard library and
nothing from the rest of the package. The server serves it verbatim at
GET /source/client.py, so it can be dropped next to your code and used as-is.
A copy fetched from a running server is stamped with that server's address in a
comment at the top of the file -- use that, not the loopback example below:

    curl -O http://<your-queue-host>:8080/source/client.py

    from client import QueueClient        # standalone copy
    from wfqueue import QueueClient       # or from the installed package

    q = QueueClient("http://<your-queue-host>:8080")
    q.put("emails", {"to": "a@b.com"}, priority=5)

    for msg in q.consume("emails"):   # polls once a minute
        with msg:                     # acks on success, nacks on exception
            send_email(msg.payload)
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterator

__all__ = ["QueueClient", "LeasedMessage", "QueueClientError", "QueueHTTPError"]


class QueueClientError(Exception):
    """Transport-level failure: connection refused, timeout, bad response."""


class QueueHTTPError(QueueClientError):
    """The server answered with a non-2xx status."""

    def __init__(self, status: int, message: str, payload: Any = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.payload = payload


class LeasedMessage:
    """A claimed message. Use as a context manager for automatic ack/nack."""

    __slots__ = ("client", "raw", "id", "payload", "lease_id", "topic", "attempts", "_settled")

    def __init__(self, client: "QueueClient", raw: dict):
        self.client = client
        self.raw = raw
        self.id = raw["id"]
        self.payload = raw.get("payload")
        self.lease_id = raw.get("lease_id")
        self.topic = raw.get("topic")
        self.attempts = raw.get("attempts", 0)
        self._settled = False

    def __repr__(self) -> str:
        return f"<LeasedMessage id={self.id} topic={self.topic!r} attempts={self.attempts}>"

    def ack(self) -> dict:
        self._settled = True
        return self.client.ack(self.id, self.lease_id)

    def nack(self, error: str | None = None, *, retry_after: float | None = None,
             dead: bool = False) -> dict:
        self._settled = True
        return self.client.nack(
            self.id, self.lease_id, error=error, retry_after=retry_after, dead=dead
        )

    def extend(self, visibility_timeout: float | None = None) -> dict:
        return self.client.extend(self.id, self.lease_id, visibility_timeout=visibility_timeout)

    def __enter__(self) -> "LeasedMessage":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._settled:
            return False
        if exc_type is None:
            self.ack()
        else:
            self.nack(f"{exc_type.__name__}: {exc}")
        return False


class QueueClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8080",
        *,
        token: str | None = None,
        admin_password: str | None = None,
        timeout: float = 65.0,
        retries: int = 2,
        retry_backoff: float = 0.5,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.admin_password = admin_password
        self.timeout = timeout
        self.retries = retries
        self.retry_backoff = retry_backoff

    # ------------------------------------------------------------------ plumbing

    def _request(self, method: str, path: str, body: Any = None,
                 params: dict | None = None) -> Any:
        url = self.base_url + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        # /admin/* is behind the console password rather than the API token.
        if self.admin_password and path.startswith("/admin"):
            basic = base64.b64encode(f"admin:{self.admin_password}".encode()).decode()
            headers["Authorization"] = f"Basic {basic}"
        elif self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        last: Exception | None = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    if not raw:
                        return None
                    ctype = resp.headers.get("Content-Type", "")
                    text = raw.decode("utf-8")
                    return json.loads(text) if "json" in ctype else text
            except urllib.error.HTTPError as exc:
                with exc:
                    raw = exc.read()
                try:
                    payload = json.loads(raw.decode("utf-8"))
                    message = payload.get("error", raw.decode("utf-8", "replace"))
                except Exception:
                    payload, message = None, raw.decode("utf-8", "replace")
                # 4xx is our fault -- do not retry it.
                if exc.code < 500:
                    raise QueueHTTPError(exc.code, message, payload) from None
                last = QueueHTTPError(exc.code, message, payload)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = QueueClientError(f"{method} {url} failed: {exc}")
            if attempt < self.retries:
                time.sleep(self.retry_backoff * (2 ** attempt))
        raise last if last else QueueClientError("request failed")

    # ---------------------------------------------------------------- discovery

    def describe(self, as_text: bool = False) -> Any:
        """Fetch the self-documenting root. Handy for agents and humans alike."""
        return self._request("GET", "/", params={"format": "text"} if as_text else None)

    def health(self) -> dict:
        return self._request("GET", "/health")

    # ------------------------------------------------------------------- topics

    def topics(self) -> list[dict]:
        return self._request("GET", "/topics")["topics"]

    def create_topic(self, topic: str, config: dict | None = None) -> dict:
        return self._request("POST", "/topics", {"name": topic, "config": config or {}})

    def delete_topic(self, topic: str, *, purge: bool = False) -> dict:
        return self._request(
            "DELETE", f"/topics/{urllib.parse.quote(topic)}",
            params={"purge": "true"} if purge else None,
        )

    def stats(self, topic: str) -> dict:
        return self._request("GET", f"/topics/{urllib.parse.quote(topic)}/stats")

    # ---------------------------------------------------------------- producing

    def put(
        self,
        topic: str,
        payload: Any,
        *,
        priority: int = 0,
        delay: float = 0.0,
        max_attempts: int | None = None,
        dedupe_key: str | None = None,
    ) -> dict:
        body = {"payload": payload, "priority": priority, "delay": delay}
        if max_attempts is not None:
            body["max_attempts"] = max_attempts
        if dedupe_key is not None:
            body["dedupe_key"] = dedupe_key
        return self._request(
            "POST", f"/topics/{urllib.parse.quote(topic)}/messages", body
        )

    def put_many(self, topic: str, messages: list[dict]) -> list[dict]:
        """Each item is a dict with 'payload' plus optional priority/delay/etc."""
        norm = [m if isinstance(m, dict) and "payload" in m else {"payload": m}
                for m in messages]
        return self._request(
            "POST", f"/topics/{urllib.parse.quote(topic)}/messages", {"messages": norm}
        )["messages"]

    # ---------------------------------------------------------------- consuming

    def lease(
        self,
        topic: str,
        *,
        count: int = 1,
        visibility_timeout: float | None = None,
        consumer: str | None = None,
        wait: float = 0.0,
    ) -> list[LeasedMessage]:
        body: dict[str, Any] = {"count": count, "wait": wait}
        if visibility_timeout is not None:
            body["visibility_timeout"] = visibility_timeout
        if consumer is not None:
            body["consumer"] = consumer
        resp = self._request("POST", f"/topics/{urllib.parse.quote(topic)}/lease", body)
        return [LeasedMessage(self, m) for m in resp["messages"]]

    def get_one(self, topic: str, **kwargs) -> LeasedMessage | None:
        msgs = self.lease(topic, count=1, **kwargs)
        return msgs[0] if msgs else None

    def ack(self, message_id: int, lease_id: str | None = None) -> dict:
        return self._request("POST", f"/messages/{message_id}/ack", {"lease_id": lease_id})

    def nack(self, message_id: int, lease_id: str | None = None, *,
             error: str | None = None, retry_after: float | None = None,
             dead: bool = False) -> dict:
        return self._request("POST", f"/messages/{message_id}/nack", {
            "lease_id": lease_id, "error": error,
            "retry_after": retry_after, "dead": dead,
        })

    def extend(self, message_id: int, lease_id: str | None = None, *,
               visibility_timeout: float | None = None) -> dict:
        return self._request("POST", f"/messages/{message_id}/extend", {
            "lease_id": lease_id, "visibility_timeout": visibility_timeout,
        })

    def consume(
        self,
        topic: str,
        *,
        count: int = 1,
        visibility_timeout: float | None = None,
        consumer: str | None = None,
        poll_interval: float = 60.0,
        max_poll_interval: float | None = None,
        wait: float = 0.0,
        max_messages: int | None = None,
        idle_timeout: float | None = None,
    ) -> Iterator[LeasedMessage]:
        """Yield leased messages forever (or until max_messages/idle_timeout).

        By default the worker polls once a minute. Set `max_poll_interval` to
        back off when the queue stays empty: the gap doubles on each empty poll
        up to that ceiling, and resets as soon as work shows up. Set
        `poll_interval=0` and `wait=N` instead to long-poll, trading a held
        connection for lower latency.

        The caller is responsible for ack/nack -- use each message as a context
        manager to get that automatically.
        """
        seen = 0
        idle_since = time.monotonic()
        gap = poll_interval
        while max_messages is None or seen < max_messages:
            batch = self.lease(
                topic, count=count, visibility_timeout=visibility_timeout,
                consumer=consumer, wait=wait,
            )
            if batch:
                idle_since = time.monotonic()
                gap = poll_interval
                for msg in batch:
                    yield msg
                    seen += 1
                    if max_messages is not None and seen >= max_messages:
                        return
                continue
            idle = time.monotonic() - idle_since
            if idle_timeout is not None and idle >= idle_timeout:
                return
            nap = gap
            if idle_timeout is not None:
                nap = min(nap, max(0.0, idle_timeout - idle))
            if nap > 0:
                time.sleep(nap)
            if max_poll_interval is not None:
                gap = min(gap * 2, max_poll_interval)

    def work(
        self,
        topic: str,
        handler: Callable[[Any], Any],
        *,
        consumer: str | None = None,
        visibility_timeout: float | None = None,
        poll_interval: float = 60.0,
        max_poll_interval: float | None = None,
        wait: float = 0.0,
        max_messages: int | None = None,
        idle_timeout: float | None = None,
        on_error: Callable[[LeasedMessage, BaseException], None] | None = None,
    ) -> int:
        """Run `handler(payload)` over the topic, acking and nacking for you.

        Returns the number of messages processed successfully.
        """
        done = 0
        for msg in self.consume(
            topic, consumer=consumer, visibility_timeout=visibility_timeout,
            poll_interval=poll_interval, max_poll_interval=max_poll_interval,
            wait=wait, max_messages=max_messages, idle_timeout=idle_timeout,
        ):
            try:
                handler(msg.payload)
            except Exception as exc:
                if on_error is not None:
                    on_error(msg, exc)
                msg.nack(f"{type(exc).__name__}: {exc}")
            else:
                msg.ack()
                done += 1
        return done

    # ------------------------------------------------------------------ reading

    def list(
        self,
        topic: str,
        *,
        state: str | None = None,
        limit: int = 50,
        offset: int = 0,
        order: str = "asc",
        include_payload: bool = True,
    ) -> dict:
        return self._request(
            "GET", f"/topics/{urllib.parse.quote(topic)}/messages",
            params={
                "state": state, "limit": limit, "offset": offset, "order": order,
                "payload": "true" if include_payload else "false",
            },
        )

    def get(self, message_id: int) -> dict:
        return self._request("GET", f"/messages/{message_id}")

    def delete(self, message_id: int) -> dict:
        return self._request("DELETE", f"/messages/{message_id}")

    # ---------------------------------------------------------------- lifecycle

    def purge(self, topic: str, *, state: str | None = None,
              older_than: float | None = None) -> dict:
        return self._request("POST", f"/topics/{urllib.parse.quote(topic)}/purge",
                             {"state": state, "older_than": older_than})

    def requeue(self, topic: str, *, state: str = "dead", limit: int = 1000) -> dict:
        return self._request("POST", f"/topics/{urllib.parse.quote(topic)}/requeue",
                             {"state": state, "limit": limit})

    def reap(self) -> dict:
        return self._request("POST", "/admin/reap", {})

    def compact(self) -> dict:
        """Rebuild the database file, returning freed space to the disk.

        Needs admin credentials (the console password or a valid key).
        """
        return self._request("POST", "/admin/compact", {})

