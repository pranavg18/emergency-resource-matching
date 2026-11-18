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


def shard_view(cluster, shard=1):
    return next(view for view in cluster.view()["shards"] if view["shard"] == shard)


def in_sync(cluster, *node_ids, shard=1):
    return sorted(shard_view(cluster, shard)["in_sync"]) == sorted(node_ids)


def test_a_returning_replica_catches_up_on_the_writes_it_missed(start_cluster):
    cluster = start_cluster(replicas=2, shards=(1,))
    assert wait_until(lambda: in_sync(cluster, "s1-a", "s1-b"), timeout=10)
    put(cluster, '{"n":1}')
    replica = cluster.nodes["s1-b"]
    replica.kill()
    assert wait_until(lambda: cluster.state_of("s1-b") == "DOWN", timeout=5)
    assert in_sync(cluster, "s1-a")  # it left the in-sync set when it went down
    put(cluster, '{"n":2}')  # missed by the replica

    replica.start()  # same port and data directory
    assert wait_until(lambda: in_sync(cluster, "s1-a", "s1-b"), timeout=15), replica.logs()
    assert cluster.state_of("s1-b") == "UP"
    assert raw(replica) == raw(cluster.nodes["s1-a"])
    assert raw(replica)["version"] == 2


def test_a_stale_replica_is_never_promoted(start_cluster):
    cluster = start_cluster(replicas=2, shards=(0, 1))
    assert wait_until(lambda: in_sync(cluster, "s1-a", "s1-b"), timeout=10)
    primary, replica = cluster.nodes["s1-a"], cluster.nodes["s1-b"]
    put(cluster, '{"n":1}')

    replica.kill()
    assert wait_until(lambda: cluster.state_of("s1-b") == "DOWN", timeout=5)
    put(cluster, '{"n":2}')  # only the primary has this write
    primary.kill()
    assert wait_until(lambda: cluster.state_of("s1-a") == "DOWN", timeout=5)
    assert in_sync(cluster, "s1-a")  # the last member is kept: it holds every write

    replica.start()  # back, but it missed n=2 and has nothing to sync from
    assert wait_until(lambda: cluster.state_of("s1-b") == "SYNCING", timeout=10), replica.logs()
    time.sleep(1.5)
    assert cluster.state_of("s1-b") == "SYNCING"  # never promoted
    for response in (put(cluster, '{"n":3}'), get(cluster), requests.get(f"{cluster.url}/data?prefix=request:", timeout=10)):
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "STORAGE_UNAVAILABLE"
    other = "request:22222222-2222-4222-8222-222222222222"  # shard 0 is unaffected
    assert put(cluster, '{"n":1}', key=other).status_code == 200

    primary.start()
    assert wait_until(lambda: cluster.state_of("s1-a") == "UP", timeout=10), primary.logs()
    assert get(cluster).json()["value"] == '{"n":2}'  # nothing was lost
    assert wait_until(lambda: in_sync(cluster, "s1-a", "s1-b"), timeout=15), replica.logs()
    assert raw(replica) == raw(primary)


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
