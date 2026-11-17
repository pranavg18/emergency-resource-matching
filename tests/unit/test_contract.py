from unittest.mock import Mock

import pytest

from common.core import Conflict, app_base
from common.storage import FakeStorage
from services.request_service.app import create_app


def test_http_errors_and_internal_failure():
    app = app_base("errors")
    @app.get("/crash")
    def crash():
        raise RuntimeError("private detail")
    client = app.test_client()
    for method, path, status, code in [("get", "/missing", 404, "NOT_FOUND"),
                                      ("post", "/healthz", 405, "METHOD_NOT_ALLOWED"),
                                      ("get", "/crash", 500, "INTERNAL")]:
        reply = getattr(client, method)(path)
        assert reply.status_code == status
        assert reply.json["error"]["code"] == code
        assert "private detail" not in reply.json["error"]["message"]


def test_bad_and_oversize_bodies():
    client = create_app(FakeStorage(), lambda *a: None).test_client()
    for body, status in [("{", 400), ('x' * (1024 * 1024 + 1), 413)]:
        reply = client.post("/requests", data=body, content_type="application/json")
        assert reply.status_code == status
        assert reply.json["error"]["code"] == "INVALID_REQUEST"


def test_status_conflicts_retry_five_times():
    storage = FakeStorage()
    client = create_app(storage, lambda *a: None).test_client()
    created = client.post("/requests", json={"type": "water", "location": {"lat": 0, "lon": 0}, "details": "water"}).json
    storage.put = Mock(side_effect=Conflict())
    reply = client.put("/requests/" + created["id"] + "/status", json={"status": "CANCELLED"})
    assert reply.status_code == 503
    assert reply.json["error"]["code"] == "STORAGE_UNAVAILABLE"
    assert storage.put.call_count == 5


@pytest.mark.parametrize("status", ["MATCHED", "UNMATCHED", "COMPLETED"])
def test_illegal_transition_code(status):
    client = create_app(FakeStorage(), lambda *a: None).test_client()
    created = client.post("/requests", json={"type": "water", "location": {"lat": 0, "lon": 0}, "details": "water"}).json
    reply = client.put("/requests/" + created["id"] + "/status", json={"status": status})
    assert reply.status_code == 409
    assert reply.json["error"]["code"] == "ILLEGAL_TRANSITION"


@pytest.mark.parametrize("details", [None, "", "x" * 2001, 5])
def test_details_bounds(details):
    client = create_app(FakeStorage(), lambda *a: None).test_client()
    reply = client.post("/requests", json={"type": "water", "location": {"lat": 0, "lon": 0}, "details": details})
    assert reply.status_code == 400
    assert reply.json["error"]["code"] == "INVALID_REQUEST"


def test_timestamp_label_filters_and_noop_status():
    import re
    from services.resource_service.app import create_app as resource_app
    storage = FakeStorage()
    client = create_app(storage, lambda *a: None).test_client()
    payload = {"type": "water", "location": {"lat": 0, "lon": 0, "label": "x" * 200}, "details": "x" * 2000}
    reply = client.post("/requests", json=payload)
    assert reply.status_code == 201
    value = reply.json
    assert value["created_at"] == value["updated_at"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", value["created_at"])
    key = "request:" + value["id"]
    before = storage.get(key)
    assert client.put("/requests/" + value["id"] + "/status", json={"status": "PENDING"}).json == value
    assert storage.get(key) == before
    payload["location"]["label"] += "x"
    assert client.post("/requests", json=payload).json["error"]["code"] == "INVALID_REQUEST"
    resources = resource_app(storage).test_client()
    for query in ["type=unknown", "status=unknown", "type=", "status="]:
        response = resources.get("/resources?" + query)
        assert response.status_code == 400
        assert response.json["error"]["code"] == "INVALID_REQUEST"
    assert resources.get("/resources").json == {"count": 0, "resources": []}


def test_cli_non_json_response(monkeypatch, capsys):
    import sys
    import requests
    from client.cli import main
    response = requests.Response()
    response.status_code = 502
    response._content = ("<html>" + "x" * 300).encode()
    monkeypatch.setattr(sys, "argv", ["cli", "list-resources"])
    monkeypatch.setattr(requests, "request", lambda *a, **k: response)
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert capsys.readouterr().out.strip() == "HTTP 502: " + response.text[:200]
