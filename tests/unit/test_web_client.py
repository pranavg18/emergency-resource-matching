"""client.web: serves the map page and forwards its calls, so the browser only talks to one origin."""

import json

import pytest
import requests

from client.web import MAX_LISTED_REQUESTS, create_app


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self.content = json.dumps(body if body is not None else {}).encode()
        self.headers = {"Content-Type": "application/json", **(headers or {})}

    def json(self):
        return json.loads(self.content)


class FakeSession:
    """Records every upstream call; answers from a queue (or raises)."""

    def __init__(self, *replies):
        self.calls = []
        self.replies = list(replies)

    def _next(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def request(self, method, url, **kwargs):
        return self._next(method, url, **kwargs)

    def get(self, url, **kwargs):
        return self._next("GET", url, **kwargs)


def client_for(*replies):
    session = FakeSession(*replies)
    app = create_app("http://api.test", "http://coordinator.test", session=session)
    return app.test_client(), session


def test_the_page_and_its_assets_are_served():
    client, _ = client_for()
    page = client.get("/")
    assert page.status_code == 200 and b"leaflet" in page.data.lower()
    for asset in ("/static/app.js", "/static/style.css"):
        assert client.get(asset).status_code == 200


def test_api_calls_are_forwarded_unchanged_and_answers_relayed():
    created = FakeResponse(201, {"id": "r1", "status": "PENDING"}, {"X-Request-ID": "abc"})
    client, session = client_for(created)
    body = {"type": "water", "details": "x", "location": {"lat": 1.5, "lon": 2.5}}

    reply = client.post("/api/requests", json=body, headers={"X-Request-ID": "abc"})

    assert reply.status_code == 201
    assert reply.get_json() == {"id": "r1", "status": "PENDING"}
    assert reply.headers["X-Request-ID"] == "abc"
    call = session.calls[0]
    assert (call["method"], call["url"]) == ("POST", "http://api.test/requests")
    assert json.loads(call["data"]) == body
    assert call["headers"]["X-Request-ID"] == "abc"


def test_query_strings_and_error_answers_pass_through():
    conflict = FakeResponse(409, {"error": {"code": "ILLEGAL_TRANSITION", "message": "no"}})
    listed = FakeResponse(200, {"count": 0, "resources": []})
    client, session = client_for(conflict, listed)

    put = client.put("/api/requests/r1/status", json={"status": "COMPLETED"})
    assert put.status_code == 409 and put.get_json()["error"]["code"] == "ILLEGAL_TRANSITION"

    client.get("/api/resources?type=water&status=AVAILABLE")
    assert session.calls[1]["url"] == "http://api.test/resources"
    assert dict(session.calls[1]["params"]) == {"type": "water", "status": "AVAILABLE"}


@pytest.mark.parametrize("path", ["/api/internal/data/x", "/api/data?prefix=request:", "/api/../coordinator"])
def test_only_the_public_api_is_forwarded(path):
    client, session = client_for()
    assert client.get(path).status_code == 404
    assert session.calls == []


def test_a_stopped_port_forward_is_reported_clearly():
    client, _ = client_for(requests.ConnectionError("refused"))
    reply = client.get("/api/resources")
    assert reply.status_code == 502
    error = reply.get_json()["error"]
    assert error["code"] == "UPSTREAM_UNAVAILABLE" and "port-forward" in error["message"]


def test_the_cluster_view_comes_from_the_coordinator():
    client, session = client_for(FakeResponse(200, {"shards": []}))
    assert client.get("/api/cluster").get_json() == {"shards": []}
    assert session.calls[0]["url"] == "http://coordinator.test/cluster"


def test_all_requests_are_decoded_newest_first_and_foreign_records_skipped():
    def record(value):
        return {"key": "request:x", "value": json.dumps(value) if isinstance(value, dict) else value}

    stored = {"records": [
        record({"id": "old", "location": {"lat": 0, "lon": 0}, "created_at": "2026-11-19T01:00:00.000Z"}),
        record({"id": "new", "location": {"lat": 0, "lon": 0}, "created_at": "2026-11-19T02:00:00.000Z"}),
        record({"type": "water", "phase": "left by another tool"}),
        record("not json at all"),
    ]}
    client, session = client_for(FakeResponse(200, stored))

    body = client.get("/api/all-requests").get_json()

    assert [r["id"] for r in body["requests"]] == ["new", "old"]
    assert body["count"] == 2 and body["truncated"] is False
    assert session.calls[0]["url"] == "http://coordinator.test/data"
    assert session.calls[0]["params"] == {"prefix": "request:"}


def test_the_request_list_is_capped():
    many = {"records": [{"key": "request:x", "value": json.dumps(
        {"id": str(i), "location": {"lat": 0, "lon": 0}, "created_at": f"2026-11-19T00:00:{i % 60:02d}.000Z"})}
        for i in range(MAX_LISTED_REQUESTS + 10)]}
    client, _ = client_for(FakeResponse(200, many))
    body = client.get("/api/all-requests").get_json()
    assert len(body["requests"]) == MAX_LISTED_REQUESTS
    assert body["count"] == MAX_LISTED_REQUESTS + 10 and body["truncated"] is True
