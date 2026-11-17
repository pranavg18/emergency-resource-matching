"""Storage node data API (contract §4.4), tested over HTTP."""

import hashlib
import json
import re

import pytest
import requests

KEY = "request:11111111-1111-4111-8111-111111111111"
OTHER_KEY = "request:22222222-2222-4222-8222-222222222222"
RESOURCE_KEY = "resource:33333333-3333-4333-8333-333333333333"
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


@pytest.fixture
def node(start_storage_node):
    return start_storage_node()


def put(node, key, value, **extra):
    return requests.put(f"{node.url}/internal/data/{key}", json={"value": value, **extra}, timeout=2)


def get(node, key, **params):
    return requests.get(f"{node.url}/internal/data/{key}", params=params, timeout=2)


def delete(node, key, **params):
    return requests.delete(f"{node.url}/internal/data/{key}", params=params, timeout=2)


def test_first_write_is_version_1(node):
    response = put(node, KEY, '{"status":"PENDING"}')
    assert response.status_code == 200
    body = response.json()
    assert body["key"] == KEY
    assert body["version"] == 1
    assert TIMESTAMP.match(body["updated_at"])


def test_each_write_bumps_the_version(node):
    put(node, KEY, '{"n":1}')
    put(node, KEY, '{"n":2}')
    assert put(node, KEY, '{"n":3}').json()["version"] == 3
    assert get(node, KEY).json()["version"] == 3


def test_get_returns_the_envelope(node):
    put(node, KEY, '{"status":"PENDING"}')
    body = get(node, KEY).json()
    assert body["key"] == KEY
    assert body["value"] == '{"status":"PENDING"}'
    assert body["version"] == 1
    assert body["deleted"] is False
    assert TIMESTAMP.match(body["updated_at"])


@pytest.mark.parametrize(
    "value",
    [
        '{"label":"Café","details":"spaces  and\\nnewlines kept"}',
        '{ "b": 1,   "a": 2 }',
        '{"emoji":"🌊","quote":"\\"x\\""}',
    ],
)
def test_value_bytes_round_trip_exactly(node, value):
    put(node, KEY, value)
    stored = get(node, KEY).json()["value"]
    assert stored == value
    assert hashlib.sha256(stored.encode("utf-8")).digest() == hashlib.sha256(value.encode("utf-8")).digest()


def test_get_missing_key_is_404(node):
    response = get(node, KEY)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_delete_leaves_a_tombstone(node):
    put(node, KEY, '{"n":1}')
    response = delete(node, KEY)
    assert response.status_code == 200
    body = response.json()
    assert body["key"] == KEY
    assert body["version"] == 2
    assert body["deleted"] is True
    assert TIMESTAMP.match(body["updated_at"])

    assert get(node, KEY).status_code == 404
    tombstone = get(node, KEY, include_deleted="true").json()
    assert tombstone["deleted"] is True
    assert tombstone["value"] == ""
    assert tombstone["version"] == 2


def test_write_after_delete_continues_the_version(node):
    put(node, KEY, '{"n":1}')
    delete(node, KEY)
    response = put(node, KEY, '{"n":2}')
    assert response.json()["version"] == 3
    assert get(node, KEY).json()["deleted"] is False


def test_delete_missing_or_deleted_key_is_404(node):
    assert delete(node, KEY).status_code == 404
    put(node, KEY, '{"n":1}')
    delete(node, KEY)
    assert delete(node, KEY).status_code == 404


def test_include_deleted_must_be_a_boolean(node):
    response = get(node, KEY, include_deleted="maybe")
    assert response.status_code == 400


@pytest.mark.parametrize(
    "key",
    [
        "request:not-a-uuid",
        "order:11111111-1111-4111-8111-111111111111",
        "request:11111111-1111-4111-8111-11111111111G",
        "REQUEST:11111111-1111-4111-8111-111111111111",
    ],
)
def test_malformed_key_is_400(node, key):
    assert put(node, key, '{"n":1}').status_code == 400
    assert get(node, key).status_code == 400
    assert delete(node, key).status_code == 400


@pytest.mark.parametrize(
    "body",
    [
        {"value": ""},
        {"value": {"status": "PENDING"}},
        {"value": 5},
        {},
        ["value"],
    ],
)
def test_bad_value_is_400(node, body):
    response = requests.put(f"{node.url}/internal/data/{KEY}", json=body, timeout=2)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_invalid_json_is_400(node):
    response = requests.put(
        f"{node.url}/internal/data/{KEY}", data="{not json", headers={"Content-Type": "application/json"}, timeout=2
    )
    assert response.status_code == 400


def test_lone_surrogate_is_400(node):
    raw = json.dumps({"value": "x"}).replace('"x"', '"\\ud800"')
    response = requests.put(
        f"{node.url}/internal/data/{KEY}", data=raw, headers={"Content-Type": "application/json"}, timeout=2
    )
    assert response.status_code == 400


def test_data_survives_a_restart(node):
    put(node, KEY, '{"n":1}')
    put(node, OTHER_KEY, '{"n":2}')
    delete(node, OTHER_KEY)
    node.restart()
    assert get(node, KEY).json()["value"] == '{"n":1}'
    assert get(node, OTHER_KEY).status_code == 404
    assert get(node, OTHER_KEY, include_deleted="true").json()["version"] == 2


def test_survives_a_hard_kill(node):
    put(node, RESOURCE_KEY, '{"availability":5}')
    node.kill()
    node.start()
    assert get(node, RESOURCE_KEY).json()["value"] == '{"availability":5}'


# --- Conditional writes ------------------------------------------------------


def test_if_version_zero_creates_only_once(node):
    assert put(node, KEY, '{"n":1}', if_version=0).status_code == 200
    response = put(node, KEY, '{"n":2}', if_version=0)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "VERSION_CONFLICT"
    assert error["current_version"] == 1
    assert get(node, KEY).json()["value"] == '{"n":1}'


def test_if_version_match_writes(node):
    put(node, KEY, '{"n":1}')
    response = put(node, KEY, '{"n":2}', if_version=1)
    assert response.status_code == 200
    assert response.json()["version"] == 2


def test_if_version_mismatch_reports_current_version(node):
    put(node, KEY, '{"n":1}')
    put(node, KEY, '{"n":2}')
    response = put(node, KEY, '{"n":3}', if_version=1)
    assert response.status_code == 409
    assert response.json()["error"]["current_version"] == 2


def test_tombstone_version_counts_as_current(node):
    put(node, KEY, '{"n":1}')
    delete(node, KEY)
    assert put(node, KEY, '{"n":2}', if_version=0).status_code == 409
    assert put(node, KEY, '{"n":2}', if_version=2).json()["version"] == 3


def test_null_if_version_is_unconditional(node):
    put(node, KEY, '{"n":1}')
    assert put(node, KEY, '{"n":2}', if_version=None).status_code == 200


@pytest.mark.parametrize("bad", ["1", 1.5, -1, True])
def test_bad_if_version_in_body_is_400(node, bad):
    assert put(node, KEY, '{"n":1}', if_version=bad).status_code == 400


def test_delete_with_if_version(node):
    put(node, KEY, '{"n":1}')
    put(node, KEY, '{"n":2}')
    conflict = delete(node, KEY, if_version=1)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["current_version"] == 2
    assert delete(node, KEY, if_version=2).status_code == 200


@pytest.mark.parametrize("bad", ["abc", "-1", "1.0", ""])
def test_bad_if_version_in_query_is_400(node, bad):
    put(node, KEY, '{"n":1}')
    assert delete(node, KEY, if_version=bad).status_code == 400


def test_concurrent_conditional_writes_have_one_winner(node):
    from concurrent.futures import ThreadPoolExecutor

    put(node, RESOURCE_KEY, '{"availability":1}')

    def claim(i):
        return put(node, RESOURCE_KEY, json.dumps({"availability": 0, "claimed_by": i}), if_version=1).status_code

    with ThreadPoolExecutor(max_workers=16) as pool:
        statuses = list(pool.map(claim, range(32)))

    assert statuses.count(200) == 1
    assert statuses.count(409) == 31
    assert get(node, RESOURCE_KEY).json()["version"] == 2


# --- Scan and stats ----------------------------------------------------------


def test_scan_returns_live_records_for_the_prefix(node):
    put(node, KEY, '{"n":1}')
    put(node, OTHER_KEY, '{"n":2}')
    put(node, RESOURCE_KEY, '{"n":3}')
    delete(node, OTHER_KEY)

    response = requests.get(f"{node.url}/internal/data", params={"prefix": "request:"}, timeout=2)
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert [record["key"] for record in body["records"]] == [KEY]
    assert body["records"][0]["value"] == '{"n":1}'

    resources = requests.get(f"{node.url}/internal/data", params={"prefix": "resource:"}, timeout=2).json()
    assert [record["key"] for record in resources["records"]] == [RESOURCE_KEY]


def test_scan_of_empty_store(node):
    body = requests.get(f"{node.url}/internal/data", params={"prefix": "resource:"}, timeout=2).json()
    assert body == {"count": 0, "records": []}


@pytest.mark.parametrize("params", [{}, {"prefix": "order:"}, {"prefix": "req"}, {"prefix": ""}])
def test_scan_needs_a_known_prefix(node, params):
    assert requests.get(f"{node.url}/internal/data", params=params, timeout=2).status_code == 400


def test_stats(start_storage_node):
    node = start_storage_node(node_id="s2-b", shard=2)
    assert requests.get(f"{node.url}/internal/stats", timeout=2).json() == {
        "node_id": "s2-b", "shard": 2, "keys": 0, "tombstones": 0, "max_version": 0,
    }
    put(node, KEY, '{"n":1}')
    put(node, KEY, '{"n":2}')
    put(node, OTHER_KEY, '{"n":1}')
    delete(node, OTHER_KEY)
    assert requests.get(f"{node.url}/internal/stats", timeout=2).json() == {
        "node_id": "s2-b", "shard": 2, "keys": 1, "tombstones": 1, "max_version": 2,
    }
