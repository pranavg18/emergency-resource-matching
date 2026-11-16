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
