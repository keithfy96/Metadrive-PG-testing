"""The queue contract the rig agent depends on, against the fake and against the real server.

Phase 7 Step 0. The same assertions run twice: against `tests/support/fake_wfqueue.py` always,
and against `$WFQUEUE_URL` when it is set (the colleague's server on localhost, or the NAS from
a machine that can route to it). Both are driven through the queue's own client,
`docs/queue-docs/queue-client-v0.py`, so the fake is held to the dialect our code will speak.

The assertions are Phase 7's three properties in miniature. **When the real server disagrees
with the fake, the fake is wrong** and `fake_wfqueue.py` is corrected; that is the whole point
of running both.
"""

from __future__ import annotations

import importlib.util
import os
import threading
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLIENT = ROOT / "docs" / "queue-docs" / "queue-client-v0.py"

needs_queue = pytest.mark.skipif(
    not os.environ.get("WFQUEUE_URL"),
    reason="needs_queue: WFQUEUE_URL is not set (a reachable wfqueue server)",
)


def _load_client():
    """The colleague's client, imported from where it is kept verbatim (not a package)."""
    spec = importlib.util.spec_from_file_location("wfqueue_client_v0", CLIENT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wfqueue = _load_client()


@pytest.fixture(params=["fake", pytest.param("real", marks=needs_queue)])
def client(request):
    """A `QueueClient` on the backend the param names. No retries: a 5xx should fail loudly."""
    if request.param == "fake":
        from tests.support.fake_wfqueue import FakeQueueServer

        with FakeQueueServer() as server:  # in-memory, ephemeral port
            yield wfqueue.QueueClient(server.url, timeout=5, retries=0)
    else:
        yield wfqueue.QueueClient(os.environ["WFQUEUE_URL"], timeout=10, retries=0)


@pytest.fixture
def topic(client):
    """A topic of our own per test, deleted afterwards so a shared server is left as found."""
    name = f"contract-test-{uuid.uuid4().hex[:12]}"
    yield name
    try:
        client.delete_topic(name, purge=True)
    except wfqueue.QueueHTTPError as exc:
        if exc.status != 404:
            raise


# --- property 1: delivery is at-least-once ------------------------------------------------------


def test_an_expired_lease_is_redelivered_and_counts_as_a_second_attempt(client, topic):
    put = client.put(topic, {"job_id": "j1"})
    first = client.get_one(topic, visibility_timeout=0.2)
    assert first is not None and first.id == put["id"]
    assert first.attempts == 1
    assert client.lease(topic, visibility_timeout=0.2) == []  # hidden while leased

    time.sleep(0.35)
    again = client.get_one(topic, visibility_timeout=5)
    assert again is not None and again.id == first.id
    assert again.attempts == 2
    assert again.lease_id != first.lease_id


def test_a_nack_with_retry_after_hides_the_message_until_then(client, topic):
    client.put(topic, {"job_id": "j1"})
    msg = client.get_one(topic, visibility_timeout=5)
    msg.nack("card busy", retry_after=0.2)
    assert client.lease(topic) == []

    time.sleep(0.3)
    again = client.get_one(topic, visibility_timeout=5)
    assert again is not None and again.id == msg.id
    assert again.attempts == 2
    assert client.get(msg.id)["last_error"] == "card busy"


# --- property 2: the lease_id proves the lease ---------------------------------------------------


def test_ack_with_a_stale_lease_id_is_409_and_the_live_lease_still_acks(client, topic):
    client.put(topic, {"job_id": "j1"})
    stale = client.get_one(topic, visibility_timeout=0.2)
    time.sleep(0.35)
    live = client.get_one(topic, visibility_timeout=5)
    assert live is not None and live.id == stale.id

    with pytest.raises(wfqueue.QueueHTTPError) as err:
        stale.ack()
    assert err.value.status == 409

    live.ack()
    assert client.get(live.id)["state"] == "done"
    with pytest.raises(wfqueue.QueueHTTPError) as err:  # and done is done
        live.ack()
    assert err.value.status == 409


def test_extend_keeps_a_lease_past_its_original_timeout(client, topic):
    client.put(topic, {"job_id": "j1"})
    msg = client.get_one(topic, visibility_timeout=0.3)
    msg.extend(visibility_timeout=5)
    time.sleep(0.45)
    assert client.lease(topic) == []  # still ours
    msg.ack()
    assert client.get(msg.id)["state"] == "done"


# --- property 3: nothing is lost -----------------------------------------------------------------


def test_nack_dead_lands_in_dead_and_requeue_returns_it(client, topic):
    client.put(topic, {"job_id": "j1"})
    msg = client.get_one(topic, visibility_timeout=5)
    msg.nack("model failed to load", dead=True)

    dead = client.list(topic, state="dead")["messages"]
    assert [m["id"] for m in dead] == [msg.id]
    assert dead[0]["last_error"] == "model failed to load"
    assert client.lease(topic) == []

    client.requeue(topic)
    assert client.list(topic, state="dead")["messages"] == []
    again = client.get_one(topic, visibility_timeout=5)
    assert again is not None and again.id == msg.id


def test_a_second_put_with_the_same_dedupe_key_is_a_duplicate_of_one_message(client, topic):
    first = client.put(topic, {"job_id": "j1"}, dedupe_key="j1")
    second = client.put(topic, {"job_id": "j1", "resubmitted": True}, dedupe_key="j1")
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["id"] == first["id"]
    assert len(client.list(topic)["messages"]) == 1
    assert client.get(first["id"])["payload"] == {"job_id": "j1"}  # the first payload stands


# --- what the agent's loop relies on -------------------------------------------------------------


def test_list_of_leased_messages_carries_the_consumer_label(client, topic):
    client.put(topic, {"job_id": "j1"})
    msg = client.get_one(topic, visibility_timeout=5, consumer="rig-a:gpu0")
    leased = client.list(topic, state="leased")["messages"]
    assert [m["id"] for m in leased] == [msg.id]
    assert leased[0]["consumer"] == "rig-a:gpu0"


def test_the_server_serves_a_client_that_defines_the_class_we_drive_it_with(client, topic):
    """`GET /source/client.py` is how a consumer bootstraps, so both backends must answer it.

    Not asserted byte-identical to our vendored copy: the real server stamps a served copy with
    its own address in a leading comment. What must hold is that the source it hands out defines
    the client this file drives. Against the real server this is the check that catches
    `docs/queue-docs/queue-client-v0.py` having drifted from theirs.
    """
    with urllib.request.urlopen(client.base_url + "/source/client.py", timeout=10) as response:
        source = response.read().decode("utf-8")

    namespace: dict = {}
    exec(compile(source, "client.py", "exec"), namespace)  # noqa: S102 - it is the queue's own
    assert "QueueClient" in namespace
    assert callable(namespace["QueueClient"].lease)
    assert callable(namespace["QueueClient"].ack)


def test_a_long_poll_returns_as_soon_as_a_message_arrives(client, topic):
    client.create_topic(topic)
    got: list = []

    def wait_for_work():
        got.extend(client.lease(topic, wait=5, visibility_timeout=5))

    waiter = threading.Thread(target=wait_for_work)
    started = time.monotonic()
    waiter.start()
    time.sleep(0.3)
    client.put(topic, {"job_id": "j1"})
    waiter.join(timeout=5)
    assert not waiter.is_alive()
    assert time.monotonic() - started < 2.0  # woke on the put, not on the 5 s wait
    assert [m.payload for m in got] == [{"job_id": "j1"}]
