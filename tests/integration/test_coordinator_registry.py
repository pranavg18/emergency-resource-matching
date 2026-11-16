"""Node registration, heartbeats and /cluster (contract §4.2–4.3), over HTTP.

Nodes here are simulated with plain HTTP calls; the end-to-end tests use a
real storage node.
"""

import time

import pytest
import requests

from tests.integration.conftest import free_port, wait_until


@pytest.fixture
def coordinator(start_coordinator):
    return start_coordinator(heartbeat_interval_ms=150, heartbeat_timeout_ms=1000)


def register(coordinator, node_id, shard, url=None, max_version=0):
    body = {"node_id": node_id, "shard": shard, "url": url or f"http://{node_id}.invalid:8080", "max_version": max_version}
    return requests.post(f"{coordinator.url}/internal/nodes/register", json=body, timeout=2)


def heartbeat(coordinator, node_id):
    return requests.post(f"{coordinator.url}/internal/nodes/heartbeat", json={"node_id": node_id}, timeout=2)


def cluster(coordinator):
    return requests.get(f"{coordinator.url}/cluster", timeout=10).json()


def test_first_node_of_a_shard_becomes_primary(coordinator):
    response = register(coordinator, "s0-a", 0)
    assert response.status_code == 200
    assert response.json() == {"node_id": "s0-a", "state": "UP", "role": "primary", "heartbeat_interval_ms": 150}


def test_second_node_must_sync_first(coordinator):
    register(coordinator, "s0-a", 0)
    body = register(coordinator, "s0-b", 0).json()
    assert (body["state"], body["role"]) == ("SYNCING", "replica")


def test_shards_are_independent(coordinator):
    for shard in range(3):
        body = register(coordinator, f"s{shard}-a", shard).json()
        assert (body["state"], body["role"]) == ("UP", "primary")


def test_only_in_sync_member_resumes_as_primary(coordinator):
    register(coordinator, "s1-a", 1)
    register(coordinator, "s1-b", 1)
    body = register(coordinator, "s1-a", 1).json()
    assert (body["state"], body["role"]) == ("UP", "primary")
    shard = cluster(coordinator)["shards"][1]
    assert shard["primary"] == "s1-a"
    assert shard["in_sync"] == ["s1-a"]


def test_heartbeat_reports_current_view(coordinator):
    register(coordinator, "s0-a", 0)
    register(coordinator, "s0-b", 0)
    assert heartbeat(coordinator, "s0-a").json() == {"state": "UP", "role": "primary"}
    assert heartbeat(coordinator, "s0-b").json() == {"state": "SYNCING", "role": "replica"}


def test_heartbeat_from_unknown_node_is_404(coordinator):
    response = heartbeat(coordinator, "ghost")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "UNKNOWN_NODE"


@pytest.mark.parametrize(
    "body",
    [
        {"node_id": "s0-a", "shard": 3, "url": "http://x:1"},
        {"node_id": "s0-a", "shard": -1, "url": "http://x:1"},
        {"node_id": "s0-a", "shard": True, "url": "http://x:1"},
        {"node_id": "s0-a", "shard": "0", "url": "http://x:1"},
        {"node_id": "", "shard": 0, "url": "http://x:1"},
        {"node_id": "s0-a", "shard": 0, "url": "x:1"},
        {"node_id": "s0-a", "shard": 0},
        {"node_id": "s0-a", "shard": 0, "url": "http://x:1", "max_version": -1},
    ],
)
def test_invalid_registration_is_400(coordinator, body):
    response = requests.post(f"{coordinator.url}/internal/nodes/register", json=body, timeout=2)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_node_cannot_change_shard(coordinator):
    register(coordinator, "s0-a", 0)
    response = register(coordinator, "s0-a", 1)
    assert response.status_code == 400
    assert "shard 0" in response.json()["error"]["message"]


def test_cluster_lists_every_shard(coordinator):
    register(coordinator, "s2-a", 2)
    register(coordinator, "s2-b", 2)
    body = cluster(coordinator)
    assert [shard["shard"] for shard in body["shards"]] == [0, 1, 2]
    assert body["shards"][0] == {"shard": 0, "primary": None, "in_sync": [], "nodes": []}

    shard = body["shards"][2]
    assert shard["primary"] == "s2-a"
    assert shard["in_sync"] == ["s2-a"]
    nodes = {node["node_id"]: node for node in shard["nodes"]}
    assert (nodes["s2-a"]["state"], nodes["s2-a"]["role"]) == ("UP", "primary")
    assert (nodes["s2-b"]["state"], nodes["s2-b"]["role"]) == ("SYNCING", "replica")
    assert nodes["s2-a"]["keys"] is None  # nothing listens at the fake URL
    assert nodes["s2-a"]["last_heartbeat"].endswith("Z")


# --- With a real storage node ------------------------------------------------


def _node_view(coordinator, node_id):
    for shard in cluster(coordinator)["shards"]:
        for node in shard["nodes"]:
            if node["node_id"] == node_id:
                return node
    return None


def test_storage_node_registers_and_reports_keys(coordinator, start_storage_node):
    node = start_storage_node(node_id="s1-a", shard=1, coordinator_url=coordinator.url)
    assert wait_until(lambda: _node_view(coordinator, "s1-a") is not None, timeout=10), node.logs()

    view = _node_view(coordinator, "s1-a")
    assert (view["state"], view["role"], view["url"]) == ("UP", "primary", node.url)
    assert view["keys"] == 0

    requests.put(
        f"{node.url}/internal/data/request:11111111-1111-4111-8111-111111111111", json={"value": "{}"}, timeout=2
    )
    assert _node_view(coordinator, "s1-a")["keys"] == 1


def test_storage_node_registers_again_after_coordinator_restart(coordinator, start_storage_node):
    start_storage_node(node_id="s0-a", shard=0, coordinator_url=coordinator.url)
    assert wait_until(lambda: _node_view(coordinator, "s0-a") is not None, timeout=10)

    coordinator.restart()  # same port, empty registry
    assert wait_until(lambda: _node_view(coordinator, "s0-a") is not None, timeout=10), coordinator.logs()
    assert _node_view(coordinator, "s0-a")["state"] == "UP"


def test_storage_node_waits_for_a_late_coordinator(start_coordinator, start_storage_node):
    port = free_port()
    node = start_storage_node(node_id="s2-a", shard=2, coordinator_url=f"http://127.0.0.1:{port}")
    time.sleep(1.0)  # a few failed registration attempts
    coordinator = start_coordinator(heartbeat_interval_ms=150, port=port)
    assert wait_until(lambda: _node_view(coordinator, "s2-a") is not None, timeout=10), node.logs()
    assert _node_view(coordinator, "s2-a")["state"] == "UP"
