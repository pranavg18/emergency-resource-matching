import json
from urllib.parse import urlsplit

import pytest
import requests

from common.config import INTERNAL_HTTP_TIMEOUT_S
from common.core import APIError, Conflict, Unavailable, app_base
from common.storage import StorageClient
from storage_node.app import create_app
from storage_node.config import NodeConfig
from storage_node.heartbeat import HeartbeatClient

KEY = "resource:00000000-0000-0000-0000-000000000001"
SECOND_KEY = "resource:00000000-0000-0000-0000-000000000002"


@pytest.fixture
def node_transport(tmp_path, monkeypatch):
    """Use Pranav's real Flask + SQLite contract; only /data routing is simulated."""
    monkeypatch.setattr(HeartbeatClient, "start", lambda self: None)
    config = NodeConfig("test-node", 0, "http://node:8080", str(tmp_path))
    node = create_app(config).test_client()
    calls = []

    def transport(method, url, *, timeout, headers, **kwargs):
        calls.append((method, url, timeout, headers, kwargs))
        path = urlsplit(url).path
        path = "/internal" + path if path.startswith("/data") else path
        reply = node.open(path, method=method, json=kwargs.get("json"),
                          query_string=kwargs.get("params"), headers=headers)
        response = requests.Response()
        response.status_code = reply.status_code
        response._content = reply.data
        return response

    monkeypatch.setattr(requests, "request", transport)
    return node, calls


def test_real_node_wire_format_cas_round_trip_and_filter(node_transport):
    node, calls = node_transport
    client = StorageClient("http://coordinator")
    value = {"type": "water", "details": "Hindi: पानी", "availability": 2}
    created = client.put(KEY, value, 0)
    assert created["key"] == KEY and created["version"] == 1
    assert created["value"] == value and created["deleted"] is False
    raw = node.get("/internal/data/" + KEY).json
    assert isinstance(raw["value"], str) and json.loads(raw["value"]) == value
    assert client.get(KEY)["value"] == value
    client.put(SECOND_KEY, {"type": "food"}, 0)
    assert [record["key"] for record in client.scan("resource:", "water")] == [KEY]
    with pytest.raises(Conflict):
        client.put(KEY, {"type": "water", "availability": 0}, 0)
    assert client.put(KEY, {**value, "availability": 1}, 1)["version"] == 2
    assert client.ready()
    assert all(call[2] == INTERNAL_HTTP_TIMEOUT_S for call in calls)


def test_real_node_not_found_and_tombstone_are_404(node_transport):
    node, _ = node_transport
    client = StorageClient("http://coordinator")
    with pytest.raises(APIError) as missing:
        client.get(KEY)
    assert missing.value.status == 404
    client.put(KEY, {"type": "water"}, 0)
    assert node.delete("/internal/data/" + KEY, query_string={"if_version": 1}).status_code == 200
    assert client.scan("resource:") == []
    with pytest.raises(APIError) as deleted:
        client.get(KEY)
    assert deleted.value.status == 404


def test_service_lifecycle_through_real_node_contract(node_transport):
    from services.matching_service.app import create_app as matching_app
    from services.request_service.app import create_app as request_app
    from services.resource_service.app import create_app as resource_app

    node, _ = node_transport
    storage = StorageClient("http://coordinator")
    events = []
    requests_api = request_app(storage, lambda *event: events.append(event)).test_client()
    resources_api = resource_app(storage).test_client()
    matcher_app = matching_app(storage)
    matching_api = matcher_app.test_client()
    location = {"lat": 28.61, "lon": 77.20}
    resource = resources_api.post("/resources", json={
        "type": "water", "location": location, "availability": 1,
    })
    assert resource.status_code == 201
    request = requests_api.post("/requests", json={
        "type": "water", "location": location, "details": "clean water",
    })
    assert request.status_code == 201 and request.json["queued"]
    request_id, resource_id = request.json["id"], resource.json["id"]
    assert events[0][0] == "request.created"
    assert events[0][1]["request_id"] == request_id

    first = matching_api.post("/match/" + request_id)
    repeated = matching_api.post("/match/" + request_id)
    assert first.status_code == repeated.status_code == 200
    assert first.json["matched"] and repeated.json["matched"]
    assert first.json["resource"]["id"] == resource_id
    claimed = storage.get("resource:" + resource_id)
    assert claimed["value"]["availability"] == 0
    assert list(claimed["value"]["assigned_request_ids"]) == [request_id]
    wire = node.get("/internal/data/resource:" + resource_id).json
    assert isinstance(wire["value"], str)
    assert json.loads(wire["value"]) == claimed["value"]
    assert requests_api.get("/requests/" + request_id).json["status"] == "MATCHED"

    status_url = f"/requests/{request_id}/status"
    assert requests_api.put(status_url, json={"status": "IN_PROGRESS"}).status_code == 200
    assert requests_api.put(status_url, json={"status": "COMPLETED"}).status_code == 200
    assert events[-1][0] == "request.closed"
    # Exercise the recovery path too: cleanup works even if the close message
    # has not been consumed, and repeating reconciliation cannot over-release.
    matcher_app.extensions["matcher"].sweep(lambda *event: events.append(event))
    matcher_app.extensions["matcher"].sweep(lambda *event: events.append(event))
    restored = storage.get("resource:" + resource_id)
    assert restored["version"] > claimed["version"]
    assert restored["value"]["availability"] == 1
    assert restored["value"]["assigned_request_ids"] == {}
    listed = resources_api.get("/resources?type=water").json["resources"]
    assert listed == [restored["value"]]


def test_request_id_propagation_and_environment_url(node_transport, monkeypatch):
    _, calls = node_transport
    monkeypatch.setenv("COORDINATOR_URL", "http://configured/")
    app = app_base("test")
    with app.test_request_context(headers={"X-Request-ID": "trace-day2"}):
        app.preprocess_request()
        StorageClient().ready()
    assert calls[-1][1] == "http://configured/readyz"
    assert calls[-1][3]["X-Request-ID"] == "trace-day2"


def reply(monkeypatch, body, status=200):
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(body).encode()
    monkeypatch.setattr(requests, "request", lambda *args, **kwargs: response)
    return response


@pytest.mark.parametrize("body", [
    [], {}, {"key": KEY, "version": True, "value": "{}"},
    {"key": KEY, "version": 1, "value": {}},
    {"key": KEY, "version": 1, "value": "not-json"},
    {"key": KEY, "version": 1, "value": "[]"},
    {"key": SECOND_KEY, "version": 1, "value": "{}"},
    {"key": KEY, "version": 1, "value": "{}", "deleted": "false"},
])
def test_malformed_record_is_unavailable(monkeypatch, body):
    reply(monkeypatch, body)
    with pytest.raises(Unavailable):
        StorageClient().get(KEY)


@pytest.mark.parametrize("body", [{}, {"records": {}}, {"records": [None]}])
def test_malformed_scan_is_unavailable(monkeypatch, body):
    reply(monkeypatch, body)
    with pytest.raises(Unavailable):
        StorageClient().scan("resource:")


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_upstream_server_errors_are_503(monkeypatch, status):
    reply(monkeypatch, "internal details must stay private", status)
    with pytest.raises(Unavailable) as error:
        StorageClient().get(KEY)
    assert error.value.status == 503
    assert "internal details" not in str(error.value)


def test_bad_json_and_network_failure_are_503(monkeypatch):
    response = reply(monkeypatch, None)
    response._content = b"<html>proxy error</html>"
    with pytest.raises(Unavailable):
        StorageClient().ready()

    def offline(*args, **kwargs):
        raise requests.Timeout("private connection details")
    monkeypatch.setattr(requests, "request", offline)
    with pytest.raises(Unavailable):
        StorageClient().ready()


def test_put_rejects_mismatched_metadata(monkeypatch):
    reply(monkeypatch, {"key": SECOND_KEY, "version": 1})
    with pytest.raises(Unavailable):
        StorageClient().put(KEY, {"type": "water"}, 0)
