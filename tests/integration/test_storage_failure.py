"""Recovery sync and failover (spec §8, §10 "Storage failure" / "Node recovery").

Heartbeat interval 150 ms, timeout 1 s, so failures are noticed quickly.
"""

import hashlib
import json
import threading
import time
import uuid

import pytest
import requests

from tests.integration.conftest import wait_until

KEY = "request:11111111-1111-4111-8111-111111111111"  # shard 1 (contract example)


def keys_on_shard(shard, count, prefix="request:"):
    """Keys owned by `shard` under the contract's rule: MD5 of the key, mod 3."""
    keys = []
    while len(keys) < count:
        key = f"{prefix}{uuid.uuid4()}"
        if int.from_bytes(hashlib.md5(key.encode("utf-8")).digest(), "big") % 3 == shard:
            keys.append(key)
    return keys


def put(base, key, value):
    return requests.put(f"{base}/data/{key}", json={"value": value}, timeout=10)


def raw(node, key=KEY):
    response = requests.get(f"{node.url}/internal/data/{key}", params={"include_deleted": "true"}, timeout=2)
    return response.json() if response.status_code == 200 else None


def digest(node):
    return requests.get(f"{node.url}/internal/sync/digest", timeout=5).json()["entries"]


def shard_view(cluster, shard=1):
    return next(view for view in cluster.view()["shards"] if view["shard"] == shard)


def in_sync(cluster, *node_ids, shard=1):
    return sorted(shard_view(cluster, shard)["in_sync"]) == sorted(node_ids)


@pytest.fixture
def pair(start_cluster):
    """Shard 1 with two nodes, both in sync."""
    cluster = start_cluster(replicas=2, shards=(1,))
    assert wait_until(lambda: in_sync(cluster, "s1-a", "s1-b"), timeout=10)
    return cluster


# --- The node's sync endpoints ------------------------------------------------


def test_digest_lists_every_row_with_the_contract_hash(start_storage_node):
    node = start_storage_node(node_id="s1-a", shard=1)
    value = '{"type":"water","availability":5}'
    requests.put(f"{node.url}/internal/data/{KEY}", json={"value": value}, timeout=2)
    gone = "request:22222222-2222-4222-8222-222222222222"
    requests.put(f"{node.url}/internal/data/{gone}", json={"value": "{}"}, timeout=2)
    requests.delete(f"{node.url}/internal/data/{gone}", timeout=2)

    body = requests.get(f"{node.url}/internal/sync/digest", timeout=2).json()
    assert body["node_id"] == "s1-a"
    # Both hashes are the worked examples in the contract (§4.1).
    assert body["entries"] == {
        KEY: [1, "ad049c2e08e19df5bc025a3f66021ab09a7ac5ddcbaf63fad6515d3935a5a764"],
        gone: [2, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"],
    }


@pytest.mark.parametrize(
    "body",
    [{}, {"source_url": "node:8080"}, {"source_url": "http://x:1", "final": "yes"}],
)
def test_sync_run_rejects_bad_input(start_storage_node, body):
    node = start_storage_node(node_id="s1-b", shard=1)
    response = requests.post(f"{node.url}/internal/sync/run", json=body, timeout=5)
    assert response.status_code == 400


def test_sync_run_refuses_to_copy_from_itself(start_storage_node):
    node = start_storage_node(node_id="s1-b", shard=1)
    response = requests.post(f"{node.url}/internal/sync/run", json={"source_url": node.url, "epoch": 1}, timeout=5)
    assert response.status_code == 400


def test_sync_run_reports_an_unreachable_source(start_storage_node):
    from tests.integration.conftest import free_port

    node = start_storage_node(node_id="s1-b", shard=1)
    response = requests.post(
        f"{node.url}/internal/sync/run", json={"source_url": f"http://127.0.0.1:{free_port()}", "epoch": 1}, timeout=15
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "SYNC_FAILED"


# --- Recovery ------------------------------------------------------------------


def test_a_node_that_joins_late_copies_everything_including_deletes(start_cluster, start_storage_node):
    cluster = start_cluster(replicas=1, shards=(1,))
    keys = keys_on_shard(1, 12)
    for i, key in enumerate(keys):
        put(cluster.url, key, json.dumps({"i": i}))
    requests.delete(f"{cluster.url}/data/{keys[0]}", timeout=5)

    late = start_storage_node(node_id="s1-b", shard=1, coordinator_url=cluster.url)
    assert wait_until(lambda: in_sync(cluster, "s1-a", "s1-b"), timeout=15), late.logs()
    assert digest(late) == digest(cluster.nodes["s1-a"])
    assert raw(late, keys[0])["deleted"] is True


def test_primary_failure_promotes_the_replica_and_the_old_primary_returns_as_replica(start_cluster):
    pair = start_cluster(replicas=2)  # all three shards, so scans can be checked too
    assert wait_until(lambda: all(in_sync(pair, f"s{s}-a", f"s{s}-b", shard=s) for s in range(3)), timeout=15)
    old_primary, replica = pair.nodes["s1-a"], pair.nodes["s1-b"]
    put(pair.url, KEY, '{"n":1}')

    old_primary.kill()
    assert wait_until(lambda: shard_view(pair)["primary"] == "s1-b", timeout=5)
    assert pair.state_of("s1-a") == "DOWN"

    # Service continues on the promoted replica: reads, writes, scans.
    assert requests.get(f"{pair.url}/data/{KEY}", timeout=5).json()["value"] == '{"n":1}'
    assert put(pair.url, KEY, '{"n":2}').status_code == 200
    assert requests.get(f"{pair.url}/data", params={"prefix": "request:"}, timeout=10).status_code == 200

    old_primary.start()
    assert wait_until(lambda: in_sync(pair, "s1-a", "s1-b"), timeout=15), old_primary.logs()
    view = shard_view(pair)
    assert view["primary"] == "s1-b"  # roles swapped: the returning node is now the replica
    assert {n["node_id"]: n["role"] for n in view["nodes"]} == {"s1-a": "replica", "s1-b": "primary"}
    assert raw(old_primary)["value"] == '{"n":2}'  # it received the write it missed
    assert digest(old_primary) == digest(replica)


def test_a_write_in_flight_when_the_primary_dies_succeeds_on_the_replica(start_cluster):
    # A 30 s heartbeat timeout: only the failed write itself can reveal the death.
    cluster = start_cluster(replicas=2, shards=(1,), heartbeat_timeout_ms=30000)
    assert wait_until(lambda: in_sync(cluster, "s1-a", "s1-b"), timeout=10)
    put(cluster.url, KEY, '{"n":1}')

    cluster.nodes["s1-a"].kill()
    response = put(cluster.url, KEY, '{"n":2}')
    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert shard_view(cluster)["primary"] == "s1-b"
    assert cluster.state_of("s1-a") == "DOWN"


def test_recovery_repairs_rows_that_diverged(pair):
    """A returning node must end up an exact copy, even where it holds a row
    at the same version with different content, or rows the primary never had
    (writes it took as primary but never replicated)."""
    primary, stale = pair.nodes["s1-a"], pair.nodes["s1-b"]
    same_version, ahead = keys_on_shard(1, 2)
    unknown = keys_on_shard(1, 1)[0]

    # Same version, different content: write behind the coordinator's back
    # first, so the replicated copy (also version 1) is refused as not newer.
    requests.put(f"{stale.url}/internal/data/{same_version}", json={"value": '{"rogue":true}'}, timeout=2)
    put(pair.url, same_version, '{"good":true}')
    assert raw(stale, same_version)["version"] == raw(primary, same_version)["version"] == 1
    assert raw(stale, same_version)["value"] != raw(primary, same_version)["value"]

    # A newer version than the primary's, and a row the primary never had.
    put(pair.url, ahead, '{"good":true}')
    requests.put(
        f"{stale.url}/internal/replica/{ahead}",
        json={"value": '{"rogue":true}', "version": 9, "deleted": False, "updated_at": "2026-11-18T00:00:00.000Z"},
        timeout=2,
    )
    requests.put(f"{stale.url}/internal/data/{unknown}", json={"value": '{"rogue":true}'}, timeout=2)

    stale.kill()
    assert wait_until(lambda: pair.state_of("s1-b") == "DOWN", timeout=5)
    stale.start()
    assert wait_until(lambda: in_sync(pair, "s1-a", "s1-b"), timeout=15), stale.logs()

    assert raw(stale, same_version) == raw(primary, same_version)
    assert raw(stale, ahead) == raw(primary, ahead)
    assert raw(stale, unknown) is None
    assert digest(stale) == digest(primary)


def test_writes_that_land_during_recovery_are_not_lost(pair):
    primary, returning = pair.nodes["s1-a"], pair.nodes["s1-b"]
    keys = keys_on_shard(1, 40)
    for key in keys[:20]:
        put(pair.url, key, '{"phase":"before"}')
    returning.kill()
    assert wait_until(lambda: pair.state_of("s1-b") == "DOWN", timeout=5)
    for key in keys[:20]:
        put(pair.url, key, '{"phase":"while down"}')

    stop = threading.Event()
    written = []

    def writer():
        i = 20
        while not stop.is_set() and i < len(keys):
            if put(pair.url, keys[i], json.dumps({"phase": "during sync", "i": i})).status_code == 200:
                written.append(keys[i])
            i += 1
            time.sleep(0.05)

    thread = threading.Thread(target=writer)
    thread.start()
    returning.start()
    try:
        assert wait_until(lambda: in_sync(pair, "s1-a", "s1-b"), timeout=15), returning.logs()
    finally:
        time.sleep(0.5)  # let a few more writes land after the node is back in sync
        stop.set()
        thread.join()

    assert written, "the writer made no progress"
    assert digest(returning) == digest(primary)
    for key in written:
        assert raw(returning, key) == raw(primary, key)
