import importlib
import json
import logging

import pytest

from common.core import APIError, Conflict, app_base, new_record, transition
from common.storage import FakeStorage


@pytest.mark.parametrize("service", ["request", "resource", "matching"])
def test_service_health_and_configuration(service):
    app = importlib.import_module(f"services.{service}_service.app").create_app(storage=FakeStorage())
    client = app.test_client()
    for path in ("/healthz", "/readyz"):
        response = client.get(path, headers={"X-Request-ID": "day1"})
        assert response.status_code == 200
        assert response.headers["X-Request-ID"] == "day1"
    assert app.config["SETTINGS"].max_attempts == 5
    expected = 400 if service == "request" else 404
    assert client.post("/requests", json={}).status_code == expected


@pytest.mark.parametrize("location", [{"lat": True, "lon": 0}, {"lat": 91, "lon": 0},
                                      {"lat": 0, "lon": float("nan")}])
def test_reject_invalid_location(location):
    with pytest.raises(APIError):
        new_record({"type": "water", "location": location})


def test_models_and_transitions():
    data = {"type": "water", "details": "water needed", "location": {"lat": 28, "lon": 77}}
    assert new_record(data)["status"] == "PENDING"
    assert new_record({**data, "availability": 0}, resource=True)["status"] == "BUSY"
    with pytest.raises(APIError):
        new_record({**data, "availability": True}, resource=True)
    transition("MATCHED", "IN_PROGRESS", client=True)
    with pytest.raises(Conflict):
        transition("PENDING", "COMPLETED", client=True)
    with pytest.raises(APIError):
        transition("PENDING", "MATCHED", client=True)


def test_json_http_log(caplog):
    caplog.set_level(logging.INFO, logger="emergency")
    client = app_base("test").test_client()
    assert client.get("/missing", headers={"X-Request-ID": "trace-1"}).status_code == 404
    record = json.loads(caplog.records[-1].message)
    assert record["correlation_id"] == "trace-1"
    assert record["status"] == 404
