"""Replica writes on a node (contract §4.4) and write fan-out through the coordinator."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests

KEY = "request:11111111-1111-4111-8111-111111111111"  # shard 1
TS = "2026-11-17T09:30:00.123Z"


def replica_put(node, key, **row):
    return requests.put(f"{node.url}/internal/replica/{key}", json=row, timeout=2)


def raw(node, key):
    response = requests.get(f"{node.url}/internal/data/{key}", params={"include_deleted": "true"}, timeout=2)
    return response.json() if response.status_code == 200 else None


# --- The replica endpoint on one node -----------------------------------------


@pytest.fixture
def node(start_storage_node):
    return start_storage_node(node_id="s1-b", shard=1)


def test_replica_write_stores_the_exact_row(node):
    response = replica_put(node, KEY, value='{"n":7}', version=7, deleted=False, updated_at=TS)
    assert response.status_code == 200
    assert response.json() == {"key": KEY, "version": 7, "applied": True}
    assert raw(node, KEY) == {"key": KEY, "value": '{"n":7}', "version": 7, "deleted": False, "updated_at": TS}


def test_replica_write_of_a_version_already_held_is_not_an_error(node):
    replica_put(node, KEY, value='{"n":7}', version=7, deleted=False, updated_at=TS)
    for version in (7, 3):
        response = replica_put(node, KEY, value='{"n":0}', version=version, deleted=False, updated_at=TS)
        assert response.status_code == 200
        assert response.json() == {"key": KEY, "version": 7, "applied": False}
    assert raw(node, KEY)["value"] == '{"n":7}'


def test_replica_tombstone(node):
    replica_put(node, KEY, value='{"n":1}', version=1, deleted=False, updated_at=TS)
    response = replica_put(node, KEY, value="", version=2, deleted=True, updated_at=TS)
    assert response.json()["applied"] is True
    assert requests.get(f"{node.url}/internal/data/{KEY}", timeout=2).status_code == 404
    assert raw(node, KEY)["deleted"] is True


def test_primary_write_continues_from_a_replicated_version(node):
    replica_put(node, KEY, value='{"n":1}', version=5, deleted=False, updated_at=TS)
    response = requests.put(f"{node.url}/internal/data/{KEY}", json={"value": '{"n":2}'}, timeout=2)
    assert response.json()["version"] == 6


@pytest.mark.parametrize(
    "row",
    [
        {"value": "{}", "version": 0, "deleted": False, "updated_at": TS},
        {"value": "{}", "version": True, "deleted": False, "updated_at": TS},
        {"value": "{}", "version": 1, "deleted": "no", "updated_at": TS},
        {"value": "{}", "version": 1, "deleted": False, "updated_at": "2026-11-17T09:30:00+00:00"},
        {"value": "{}", "version": 1, "deleted": False},
        {"value": "{}", "version": 1, "deleted": True, "updated_at": TS},
        {"value": "", "version": 1, "deleted": False, "updated_at": TS},
    ],
)
def test_bad_replica_rows_are_400(node, row):
    response = replica_put(node, KEY, **row)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


# --- Fan-out through the coordinator ------------------------------------------


@pytest.fixture
def cluster(start_cluster):
    return start_cluster(replicas=2)


def test_second_node_receives_writes_but_is_not_read_from_yet(cluster):
    assert cluster.state_of("s1-b") == "SYNCING"
    requests.put(f"{cluster.url}/data/{KEY}", json={"value": '{"n":1}'}, timeout=5)
    assert raw(cluster.nodes["s1-a"], KEY) == raw(cluster.nodes["s1-b"], KEY)
    assert raw(cluster.nodes["s1-b"], KEY)["version"] == 1


def test_writes_and_deletes_reach_both_nodes_identically(cluster):
    keys = [
        "request:11111111-1111-4111-8111-111111111111",
        "request:22222222-2222-4222-8222-222222222222",
        "request:44444444-4444-4444-8444-444444444444",
        "resource:33333333-3333-4333-8333-333333333333",
    ]
    for i, key in enumerate(keys):
        requests.put(f"{cluster.url}/data/{key}", json={"value": json.dumps({"i": i})}, timeout=5)
        requests.put(f"{cluster.url}/data/{key}", json={"value": json.dumps({"i": i, "again": True})}, timeout=5)
    requests.delete(f"{cluster.url}/data/{keys[1]}", timeout=5)

    for key in keys:
        owners = [node for node_id, node in cluster.nodes.items() if raw(node, key) is not None]
        assert len(owners) == 2, key
        assert raw(owners[0], key) == raw(owners[1], key)
    assert raw(cluster.nodes["s0-b"], keys[1])["deleted"] is True


def test_concurrent_writes_reach_the_replica_in_primary_order(cluster):
    def write(i):
        requests.put(f"{cluster.url}/data/{KEY}", json={"value": json.dumps({"i": i})}, timeout=10)

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(write, range(40)))

    primary, replica = raw(cluster.nodes["s1-a"], KEY), raw(cluster.nodes["s1-b"], KEY)
    assert primary["version"] == 40
    assert primary == replica
