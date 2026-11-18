"""Sync scheduling races in the coordinator.

These depend on thread timing that cannot be forced over HTTP, so they drive
the coordinator's sync manager directly with a scripted `_attempt`.
"""

import threading
import time

from coordinator.monitor import FailureDetector
from coordinator.sync import Outcome, SyncManager


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_a_readmission_while_the_worker_is_exiting_still_gets_synced():
    """The worker decides to give up (the node looked DOWN); before it retires,
    a heartbeat re-admits the node and asks for a sync. That request must not
    be swallowed, or the node stays SYNCING forever."""
    manager = SyncManager(registry=None, router=None)
    attempts = []
    decided, proceed = threading.Event(), threading.Event()

    def attempt(node_id):
        attempts.append(node_id)
        if len(attempts) == 1:
            decided.set()
            proceed.wait(5)  # paused between "abandon" and retiring
            return Outcome.ABANDON
        return Outcome.DONE

    manager._attempt = attempt
    manager.request("s1-b")
    assert decided.wait(5)
    manager.request("s1-b")  # the re-admission arrives now
    proceed.set()

    assert wait_until(lambda: not manager._running)
    assert attempts == ["s1-b", "s1-b"]


def test_requests_while_running_coalesce_into_one_more_attempt():
    manager = SyncManager(registry=None, router=None)
    attempts = []
    gate = threading.Event()

    def attempt(node_id):
        attempts.append(node_id)
        if len(attempts) == 1:
            gate.wait(5)
        return Outcome.DONE

    manager._attempt = attempt
    manager.request("s1-b")
    assert wait_until(lambda: attempts)
    for _ in range(5):
        manager.request("s1-b")
    gate.set()

    assert wait_until(lambda: not manager._running)
    assert len(attempts) == 2


def test_epochs_always_increase():
    manager = SyncManager(registry=None, router=None)
    epochs = [manager.next_epoch() for _ in range(1000)]
    assert epochs == sorted(set(epochs))
    assert epochs[0] > 1_600_000_000 * 10**9  # clock-based, so it outlives a coordinator restart


def test_the_failure_detector_keeps_asking_for_syncs_of_syncing_nodes():
    class Registry:
        def stale(self, timeout_s):
            return []

        def syncing(self):
            return ["s0-b", "s2-a"]

    requested = []
    detector = FailureDetector(Registry(), router=None, interval_ms=2000, timeout_ms=6000,
                               request_sync=requested.append)
    detector.check()
    assert requested == ["s0-b", "s2-a"]
