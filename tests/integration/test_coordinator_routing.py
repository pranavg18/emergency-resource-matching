"""The coordinator's data API (contract §4.2): routing, scans and errors, over HTTP."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests

# Worked examples from the contract (§4.1): key -> owning shard.
SHARD_EXAMPLES = {
    "request:11111111-1111-4111-8111-111111111111": 1,
    "request:22222222-2222-4222-8222-222222222222": 0,
    "request:44444444-4444-4444-8444-444444444444": 2,
    "resource:33333333-3333-4333-8333-333333333333": 2,
}
KEY = "request:11111111-1111-4111-8111-111111111111"  # shard 1


@pytest.fixture
def cluster(start_cluster):
    return start_cluster(replicas=1)


def put(base, key, value, **extra):
    return requests.put(f"{base}/data/{key}", json={"value": value, **extra}, timeout=5)


def get(base, key, **params):
    return requests.get(f"{base}/data/{key}", params=params, timeout=5)


def delete(base, key, **params):
    return requests.delete(f"{base}/data/{key}", params=params, timeout=5)


def scan(base, **params):
    return requests.get(f"{base}/data", params=params, timeout=10)


def test_each_key_lands_on_the_shard_the_contract_names(cluster):
    for key, shard in SHARD_EXAMPLES.items():
        assert put(cluster.url, key, '{"n":1}').status_code == 200
        for node_id, node in cluster.nodes.items():
            direct = requests.get(f"{node.url}/internal/data/{key}", timeout=2)
            expected = 200 if node_id == f"s{shard}-a" else 404
            assert direct.status_code == expected, (key, node_id)


def test_write_read_delete_round_trip(cluster):
    created = put(cluster.url, KEY, '{"status":"PENDING"}')
    assert created.status_code == 200
    assert created.json()["key"] == KEY
    assert created.json()["version"] == 1
    assert created.json()["updated_at"].endswith("Z")

    envelope = get(cluster.url, KEY).json()
    assert envelope["value"] == '{"status":"PENDING"}'
    assert envelope["version"] == 1
    assert envelope["deleted"] is False

    assert put(cluster.url, KEY, '{"status":"MATCHED"}').json()["version"] == 2
    removed = delete(cluster.url, KEY)
    assert removed.status_code == 200
    assert removed.json()["deleted"] is True
    assert removed.json()["version"] == 3
    assert get(cluster.url, KEY).status_code == 404
    assert delete(cluster.url, KEY).status_code == 404


def test_value_bytes_are_preserved_end_to_end(cluster):
    value = '{ "label":"Café 🌊",  "details":"two  spaces" }'
    put(cluster.url, KEY, value)
    through_coordinator = get(cluster.url, KEY).json()["value"]
    on_node = requests.get(f"{cluster.nodes['s1-a'].url}/internal/data/{KEY}", timeout=2).json()["value"]
    assert through_coordinator == on_node == value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    assert hashlib.sha256(through_coordinator.encode("utf-8")).hexdigest() == digest


def test_conditional_write_conflict_reports_current_version(cluster):
    put(cluster.url, KEY, '{"n":1}', if_version=0)
    response = put(cluster.url, KEY, '{"n":2}', if_version=0)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "VERSION_CONFLICT"
    assert error["current_version"] == 1
    assert put(cluster.url, KEY, '{"n":2}', if_version=1).json()["version"] == 2

    conflict = delete(cluster.url, KEY, if_version=1)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["current_version"] == 2


def test_concurrent_conditional_writes_have_one_winner(cluster):
    put(cluster.url, KEY, '{"availability":1}')

    def claim(i):
        return put(cluster.url, KEY, json.dumps({"availability": 0, "by": i}), if_version=1).status_code

    with ThreadPoolExecutor(max_workers=16) as pool:
        statuses = list(pool.map(claim, range(24)))
    assert statuses.count(200) == 1
    assert statuses.count(409) == 23


def test_scan_merges_every_shard_and_filters(cluster):
    records = {
        "request:11111111-1111-4111-8111-111111111111": {"type": "water", "status": "PENDING"},
        "request:22222222-2222-4222-8222-222222222222": {"type": "food", "status": "PENDING"},
        "request:44444444-4444-4444-8444-444444444444": {"type": "water", "status": "MATCHED"},
        "resource:33333333-3333-4333-8333-333333333333": {"type": "water", "status": "AVAILABLE"},
    }
    for key, value in records.items():
        put(cluster.url, key, json.dumps(value))

    body = scan(cluster.url, prefix="request:").json()
    assert body["count"] == 3
    assert {record["key"] for record in body["records"]} == {k for k in records if k.startswith("request:")}
    assert all(isinstance(record["value"], str) for record in body["records"])

    water = scan(cluster.url, prefix="request:", type="water").json()
    assert water["count"] == 2
    pending_water = scan(cluster.url, prefix="request:", type="water", status="PENDING").json()
    assert [r["key"] for r in pending_water["records"]] == ["request:11111111-1111-4111-8111-111111111111"]

    delete(cluster.url, "request:22222222-2222-4222-8222-222222222222")
    assert scan(cluster.url, prefix="request:").json()["count"] == 2
    assert scan(cluster.url, prefix="resource:").json()["count"] == 1


@pytest.mark.parametrize("params", [{}, {"prefix": "order:"}, {"prefix": "request:", "type": ""}])
def test_scan_rejects_bad_parameters(cluster, params):
    assert scan(cluster.url, **params).status_code == 400


def test_scan_fails_whole_when_a_shard_has_no_node(start_cluster):
    cluster = start_cluster(replicas=1, shards=(0, 1))
    response = scan(cluster.url, prefix="request:")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "STORAGE_UNAVAILABLE"


def test_shard_without_nodes_is_503(start_coordinator):
    coordinator = start_coordinator()
    for response in (put(coordinator.url, KEY, '{"n":1}'), get(coordinator.url, KEY), delete(coordinator.url, KEY)):
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "STORAGE_UNAVAILABLE"


@pytest.mark.parametrize(
    "key,body",
    [
        ("request:not-a-uuid", {"value": "{}"}),
        (KEY, {"value": ""}),
        (KEY, {"value": {"nested": True}}),
        (KEY, {"value": "{}", "if_version": -1}),
        (KEY, {"value": "{}", "if_version": "1"}),
        (KEY, {}),
    ],
)
def test_bad_writes_are_rejected_before_routing(start_coordinator, key, body):
    coordinator = start_coordinator()  # no nodes at all: validation must come first
    response = requests.put(f"{coordinator.url}/data/{key}", json=body, timeout=5)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_request_id_reaches_the_nodes_on_writes_reads_and_scans(cluster):
    """Scans fan out on pool threads, which have no request context of their
    own; the id must still reach every shard so a request can be traced."""
    from tests.integration.conftest import wait_until

    def logged_on(node, request_id):
        return request_id in node.log_path.read_text(errors="replace")

    trace = {"X-Request-ID": "trace-write-0001"}
    requests.put(f"{cluster.url}/data/{KEY}", json={"value": "{}"}, headers=trace, timeout=5)
    requests.get(f"{cluster.url}/data/{KEY}", headers={"X-Request-ID": "trace-read-0001"}, timeout=5)
    owner = cluster.nodes["s1-a"]  # KEY lives on shard 1
    assert wait_until(lambda: logged_on(owner, "trace-write-0001"), timeout=5)
    assert wait_until(lambda: logged_on(owner, "trace-read-0001"), timeout=5)

    response = requests.get(
        f"{cluster.url}/data", params={"prefix": "request:"}, headers={"X-Request-ID": "trace-scan-0001"}, timeout=10
    )
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "trace-scan-0001"
    for node_id, node in cluster.nodes.items():
        assert wait_until(lambda: logged_on(node, "trace-scan-0001"), timeout=5), node_id

    requests.get(f"{cluster.url}/cluster", headers={"X-Request-ID": "trace-cluster-0001"}, timeout=10)
    for node_id, node in cluster.nodes.items():
        assert wait_until(lambda: logged_on(node, "trace-cluster-0001"), timeout=5), node_id
