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
