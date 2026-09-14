"""`agent/lock.py`: the two locks that stand between a worker and a GPU.

Nothing here needs a simulator, a GPU or docker. It needs a kernel, because the thing under test
*is* the kernel's `flock(2)` -- so every assertion about exclusion is made by taking the lock
from a second, independent process and seeing what happens, never by asking the module what it
thinks it did.

The two properties the design rests on, and the reason this file exists:

- our two workers share a rig, so the rig lock is taken SHARED and two cards run at once;
- CARLA does not share, so an exclusive taker -- `deployment/with_rig_lock.sh`, a hand-run
  script, a GitLab job -- is refused while either of ours is up, and refuses us while it is.

`/proc/locks` is a witness and not the authority: a container without `--pid host` sees the locks
taken inside it and none of the host's (measured on a rig -- 0 of the host's 18, then its own 2).
So the tests that read it check what it is *for* -- naming a holder -- and the tests about
exclusion never touch it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from scenariobank.agent.lock import (
    CARD_LOCK_FILE,
    HOLDER_FILE,
    LOCK_ROOT_VAR,
    RIG_LOCK_FILE,
    Busy,
    CardLock,
    Holder,
    LockError,
    holders,
    lock_root,
)

#: A second process that takes one lock and holds it until it is killed. It prints a line once
#: the lock is its own, so a test never has to sleep and hope.
HOLD = """
import fcntl, os, sys, time
handle = os.open(sys.argv[1], os.O_RDONLY)
fcntl.flock(handle, (fcntl.LOCK_SH if sys.argv[2] == "shared" else fcntl.LOCK_EX))
print("held", os.getpid(), flush=True)
time.sleep(300)
"""


class Stranger:
    """Somebody else on this rig: CARLA, a GitLab job, a hand-run script."""

    def __init__(self, path: Path, how: str = "exclusive") -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-c", HOLD, str(path), how],
            stdout=subprocess.PIPE,
            text=True,
        )
        first = self.process.stdout.readline()
        assert first.startswith("held"), f"the stranger never took {path}: {first!r}"
        self.pid = int(first.split()[1])

    def __enter__(self) -> Stranger:
        return self

    def __exit__(self, *_exception) -> None:
        self.process.kill()
        self.process.wait()


def takes(path: Path, how: str = "exclusive") -> bool:
    """Whether a separate process can take this lock right now. The only honest question."""
    code = "import fcntl,os,sys;fcntl.flock(os.open(sys.argv[1],os.O_RDONLY),%s|fcntl.LOCK_NB)" % (
        "fcntl.LOCK_SH" if how == "shared" else "fcntl.LOCK_EX"
    )
    return subprocess.run([sys.executable, "-c", code, str(path)]).returncode == 0


# -- where the files are ------------------------------------------------------------------------


def test_the_root_comes_from_the_same_variable_the_shell_script_reads(tmp_path, monkeypatch):
    monkeypatch.setenv(LOCK_ROOT_VAR, str(tmp_path / "simulation"))
    assert lock_root() == tmp_path / "simulation"
    # an argument still wins, so a test or a second rig layout never has to set the environment
    assert lock_root(tmp_path / "elsewhere") == tmp_path / "elsewhere"


def test_the_default_root_is_the_one_wing_sims_script_computes(monkeypatch):
    monkeypatch.delenv(LOCK_ROOT_VAR, raising=False)
    assert lock_root() == Path.home() / "simulation"


def test_the_card_is_named_per_device_and_the_rig_lock_is_the_shared_one(tmp_path):
    lock = CardLock(gpu=1, root=tmp_path)
    assert lock.rig_file.name == RIG_LOCK_FILE == ".wing-sim.gpu.lock"
    assert lock.card_file.name == CARD_LOCK_FILE.format(gpu=1) == ".wing-sim.gpu1.lock"
    # ours alone, and deliberately not named like his: a shared path, a private format
    assert lock.holder_file.name == HOLDER_FILE.format(gpu=1)
    assert not lock.holder_file.name.startswith(".wing-sim")


def test_the_lock_files_are_made_once_and_keep_their_inode(tmp_path):
    lock = CardLock(gpu=0, root=tmp_path / "simulation")
    lock.ensure_exists()
    before = [path.stat().st_ino for path in (lock.rig_file, lock.card_file)]
    assert oct(lock.card_file.stat().st_mode)[-3:] == "666"  # the rig is shared
    lock.ensure_exists()
    # exclusion is a property of the inode: a second call that replaced the file would exclude
    # nobody, silently
    assert [path.stat().st_ino for path in (lock.rig_file, lock.card_file)] == before


# -- taking it ----------------------------------------------------------------------------------


def test_taking_a_card_takes_the_rig_too_and_publishes_who_has_it(tmp_path):
    lock = CardLock(gpu=0, root=tmp_path)
    with lock.acquire(out="/out/j7") as held:
        assert not takes(lock.card_file), "the card is ours"
        assert not takes(lock.rig_file), "CARLA cannot start beside us"
        assert takes(lock.rig_file, "shared"), "but our other worker can"

        record = json.loads(lock.holder_file.read_text())
        assert record["pid"] == os.getpid()
        assert record["gpu"] == 0
        assert record["job_id"] is None  # the card is taken BEFORE the queue is asked for work
        assert record["out"] == "/out/j7"
        assert held.confirmed and held.note == ""  # this machine is not a pid namespace
    assert takes(lock.card_file), "released"
    assert not lock.holder_file.exists(), "a record with no lock is history, so it goes with it"


def test_two_cards_run_at_once_which_is_the_whole_reason_the_rig_lock_is_shared(tmp_path):
    zero, one = CardLock(gpu=0, root=tmp_path), CardLock(gpu=1, root=tmp_path)
    with zero.acquire(), one.acquire():
        assert not takes(zero.card_file)
        assert not takes(one.card_file)
        assert not takes(zero.rig_file), "CARLA is still shut out by both of them"
    assert takes(zero.rig_file), "and gets the rig back the moment the last of ours lets go"


def test_a_second_worker_on_one_card_is_refused_and_can_say_it_is_us(tmp_path):
    lock = CardLock(gpu=0, root=tmp_path)
    with lock.acquire(job_id="7", attempt=1), pytest.raises(Busy) as refused:
        CardLock(gpu=0, root=tmp_path).acquire()
    holding = refused.value.holding
    assert holding.scope == "card"
    assert holding.verdict == "ours"
    assert holding.holder is not None and holding.holder.job_id == "7"
    assert "held by this agent" in holding.sentence()


def test_a_stranger_on_the_rig_lock_refuses_every_card_and_is_left_alone(tmp_path):
    """The step's own verification: hold it from a shell, and the helper reports it foreign.

    This is `bash wing-sim/deployment/with_rig_lock.sh sleep 60 &` in one process: an exclusive
    taker of `.wing-sim.gpu.lock` that is not us.
    """
    lock = CardLock(gpu=0, root=tmp_path)
    lock.ensure_exists()
    with Stranger(lock.rig_file) as carla:
        with pytest.raises(Busy) as refused:
            lock.acquire()
        holding = refused.value.holding
        assert holding.scope == "rig"
        assert holding.verdict == "foreign", "nothing of ours holds it, so we wait"
        assert carla.pid in holding.pids
        assert "somebody else" in holding.sentence() and "waiting" in holding.sentence()
        assert not lock.holder_file.exists(), "nothing was taken, so nothing was published"
        # and the card itself was never touched: a refusal must not leave the rig or the card
        # locked by a worker that got nothing
        assert takes(lock.card_file)


def test_a_card_refused_gives_the_rig_lock_straight_back(tmp_path):
    """Holding the rig alone locks CARLA out of a machine we are not using."""
    lock = CardLock(gpu=0, root=tmp_path)
    lock.ensure_exists()
    with Stranger(lock.card_file), pytest.raises(Busy) as refused:
        CardLock(gpu=0, root=tmp_path).acquire()
    assert refused.value.holding.scope == "card"
    assert takes(lock.rig_file), "the rig lock was dropped again when the card was refused"


def test_a_holder_from_another_boot_is_a_foreign_holder(tmp_path):
    """A record is believed only while the kernel still agrees with it.

    Pids are recycled and machines reboot, so a record naming a pid that holds the lock is not
    enough: it must be this boot, and a process created at the recorded moment. Anything less is
    foreign -- which is a perfectly good answer, and one a worker knows how to wait on.
    """
    lock = CardLock(gpu=0, root=tmp_path)
    with lock.acquire() as held:
        stale = held.holder.model_copy(update={"boot_id": "not-this-boot"})
        lock.holder_file.write_text(stale.model_dump_json())
        holding = lock.look("card")
    assert holding.verdict == "foreign"


def test_an_unreadable_record_is_no_record(tmp_path):
    lock = CardLock(gpu=0, root=tmp_path)
    lock.ensure_exists()
    lock.holder_file.write_text("{ half a fi")
    assert lock.read_holder() is None
    with Stranger(lock.card_file), pytest.raises(Busy) as refused:
        CardLock(gpu=0, root=tmp_path).acquire()
    assert refused.value.holding.verdict == "foreign"


# -- the record --------------------------------------------------------------------------------


def test_the_job_is_published_when_it_is_leased_and_the_acquisition_time_stands(tmp_path):
    lock = CardLock(gpu=0, root=tmp_path)
    with lock.acquire() as held:
        acquired = held.holder.acquired
        inode = lock.card_file.stat().st_ino
        time.sleep(1.05)  # the stamp is the UTC second, so a republish has to cross one
        held.publish(job_id="j7", attempt=2, container="sim-gpu0")

        record = lock.read_holder()
        assert (record.job_id, record.attempt, record.container) == ("j7", 2, "sim-gpu0")
        assert record.acquired == acquired, "when we took the card does not move"
        assert record.updated > acquired, "when it last said anything does"
        assert record.pid == os.getpid()
        # the record is replaced by rename, which is exactly what the LOCK file must never be
        assert lock.card_file.stat().st_ino == inode


def test_the_record_is_replaced_whole_and_left_readable_on_a_shared_rig(tmp_path):
    lock = CardLock(gpu=0, root=tmp_path)
    with lock.acquire() as held:
        held.publish(job_id="j7")
        assert oct(lock.holder_file.stat().st_mode)[-3:] == "644", "the other user reads it"
        # nothing half-written is ever observable, and no staging file is left behind
        assert Holder.model_validate_json(lock.holder_file.read_text()).job_id == "j7"
    assert [p.name for p in tmp_path.glob(".holder.*")] == []


def test_the_record_refuses_a_field_nobody_reads():
    with pytest.raises(ValidationError):
        Holder(
            gpu=0, pid=1, pgid=1, boot_id="b", starttime="1", host="rig",
            acquired="u", updated="u", note="whatever",
        )


# -- /proc/locks, the witness -------------------------------------------------------------------


def test_nobody_holding_it_is_no_pids(tmp_path):
    lock = CardLock(gpu=0, root=tmp_path)
    lock.ensure_exists()
    assert holders(lock.card_file) == []
    assert lock.look("card") is None
    assert lock.look("rig") is None


def test_a_missing_file_is_not_an_error_to_read(tmp_path):
    assert holders(tmp_path / "never-made.lock") == []


def test_the_witness_names_both_a_shared_holder_and_an_exclusive_one(tmp_path):
    """Our rig lock is shared, so a reader of `/proc/locks` that only matched `WRITE` rows would
    report the rig as free while two of our workers held it."""
    lock = CardLock(gpu=0, root=tmp_path)
    lock.ensure_exists()
    with Stranger(lock.rig_file, "shared") as reader:
        assert reader.pid in holders(lock.rig_file)
    with Stranger(lock.card_file, "exclusive") as writer:
        assert writer.pid in holders(lock.card_file)


def test_a_lock_the_kernel_does_not_report_is_a_contradiction(tmp_path, monkeypatch):
    """The confirmation, and the reason it is not just an assert.

    `flock(2)` said yes and `/proc/locks` shows other locks but not ours: the file we locked is
    not excluding anybody, which on a rig means the lock directory is on a network mount. That is
    worth refusing over. An EMPTY `/proc/locks` is the opposite case and is tested below.
    """
    lock = CardLock(gpu=0, root=tmp_path)
    monkeypatch.setattr(
        "scenariobank.agent.lock._proc_locks",
        lambda: ["1: FLOCK  ADVISORY  WRITE 1 00:00:1 0 EOF"],
    )
    with pytest.raises(LockError, match="network mount"):
        lock.acquire()
    assert takes(lock.card_file), "and it let go of both locks on the way out"
    assert takes(lock.rig_file)


def test_a_blind_witness_keeps_the_lock_and_says_so(tmp_path, monkeypatch):
    """`/proc/locks` showing nothing at all, not even the lock we just took.

    Measured under a user namespace; a plain container is not this case -- it hides the host's
    locks but still shows its own, so the confirmation there passes. Either way the `flock` that
    stops a double-booking works, so the run goes ahead and the helper says what it cannot see.
    """
    lock = CardLock(gpu=0, root=tmp_path)
    monkeypatch.setattr("scenariobank.agent.lock._proc_locks", lambda: [])
    with lock.acquire() as held:
        assert held.confirmed is False
        assert "not even the lock just taken" in held.note
        assert not takes(lock.card_file), "the lock is real whether or not we can see it"

        # and with no witness, a holder cannot be named -- "unknown", never "free"
        with pytest.raises(Busy) as refused:
            CardLock(gpu=0, root=tmp_path).acquire()
        assert refused.value.holding.verdict == "unknown"
        assert "cannot see" in refused.value.holding.sentence()


def test_a_record_is_never_left_beside_a_lock_nobody_holds(tmp_path):
    lock = CardLock(gpu=0, root=tmp_path)
    held = lock.acquire()
    held.release()
    with pytest.raises(LockError, match="released"):
        held.publish(job_id="j7")
    assert not lock.holder_file.exists()
