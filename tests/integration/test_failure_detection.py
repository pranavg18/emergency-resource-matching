"""Failure detection (spec §8): nodes go DOWN on silence or a failed call, and
the coordinator stops routing to them. Run with a 150 ms heartbeat interval
and a 1 s timeout so the tests are quick.
"""

import time

import requests

from tests.integration.conftest import wait_until

KEY = "request:11111111-1111-4111-8111-111111111111"  # shard 1


def put(cluster, value, key=KEY):
    return requests.put(f"{cluster.url}/data/{key}", json={"value": value}, timeout=10)


def get(cluster, key=KEY):
    return requests.get(f"{cluster.url}/data/{key}", timeout=10)


def raw(node, key=KEY):
    response = requests.get(f"{node.url}/internal/data/{key}", params={"include_deleted": "true"}, timeout=2)
    return response.json() if response.status_code == 200 else None


def test_a_silent_node_is_marked_down_after_the_timeout(start_cluster):
    cluster = start_cluster(replicas=2, shards=(1,))
    cluster.nodes["s1-b"].kill()
    started = time.monotonic()
    assert wait_until(lambda: cluster.state_of("s1-b") == "DOWN", timeout=5)
    assert time.monotonic() - started < 3  # 1 s timeout plus detector period and slack
    assert cluster.state_of("s1-a") == "UP"


def test_a_replica_that_fails_a_write_goes_down_at_once_and_the_write_stands(start_cluster):
    # Long heartbeat timeout: only the failed replica call can explain a fast DOWN.
    cluster = start_cluster(replicas=2, shards=(1,), heartbeat_timeout_ms=30000)
    cluster.nodes["s1-b"].kill()

    response = put(cluster, '{"n":1}')
    assert response.status_code == 200
    assert cluster.state_of("s1-b") == "DOWN"
    assert get(cluster).json()["value"] == '{"n":1}'

    # Later writes skip the DOWN node entirely and stay fast.
    started = time.monotonic()
    assert put(cluster, '{"n":2}').status_code == 200
    assert time.monotonic() - started < 1.5


def test_a_returning_replica_must_sync_before_serving(start_cluster):
    cluster = start_cluster(replicas=2, shards=(1,))
    put(cluster, '{"n":1}')
    replica = cluster.nodes["s1-b"]
    replica.kill()
    assert wait_until(lambda: cluster.state_of("s1-b") == "DOWN", timeout=5)
    put(cluster, '{"n":2}')  # missed by the replica

    replica.start()  # same port and data directory
    assert wait_until(lambda: cluster.state_of("s1-b") == "SYNCING", timeout=10), replica.logs()
    assert raw(replica)["version"] == 1  # it lacks the write it missed...
    assert get(cluster).json()["version"] == 2  # ...so reads keep coming from the primary


def test_losing_the_only_in_sync_node_makes_the_shard_unavailable(start_cluster):
    cluster = start_cluster(replicas=2, shards=(0, 1, 2))
    put(cluster, '{"n":1}')
    primary = cluster.nodes["s1-a"]
    primary.kill()
    assert wait_until(lambda: cluster.state_of("s1-a") == "DOWN", timeout=5)

    # s1-b never finished a sync, so it must not be promoted: it may lack writes.
    view = next(shard for shard in cluster.view()["shards"] if shard["shard"] == 1)
    assert view["in_sync"] == ["s1-a"]
    for response in (put(cluster, '{"n":2}'), get(cluster), requests.get(f"{cluster.url}/data?prefix=request:", timeout=10)):
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "STORAGE_UNAVAILABLE"

    # Other shards are unaffected.
    other = "request:22222222-2222-4222-8222-222222222222"  # shard 0
    assert put(cluster, '{"n":1}', key=other).status_code == 200

    primary.start()
    assert wait_until(lambda: cluster.state_of("s1-a") == "UP", timeout=10), primary.logs()
    assert get(cluster).json()["value"] == '{"n":1}'
    assert put(cluster, '{"n":2}').status_code == 200


def test_a_frozen_node_is_readmitted_by_its_next_heartbeat(start_cluster):
    cluster = start_cluster(replicas=1, shards=(1,))
    paused = "s1-a"
    put(cluster, '{"n":1}')
    node = cluster.nodes[paused]
    node.pause()
    try:
        assert wait_until(lambda: cluster.state_of(paused) == "DOWN", timeout=5)
        assert get(cluster).status_code == 503
    finally:
        node.resume()
    # Same process, never restarted: its heartbeat alone brings it back. As the
    # shard's only in-sync member it resumes as primary without a sync.
    assert wait_until(lambda: cluster.state_of(paused) == "UP", timeout=5)
    assert get(cluster).json()["value"] == '{"n":1}'
