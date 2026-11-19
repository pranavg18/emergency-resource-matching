"""client.web: serves the map page and forwards its calls, so the browser only talks to one origin."""

import json

import pytest
import requests

from client.web import MAX_LISTED_CLOSED, create_app


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
    assert body["count"] == 2 and body["closed_omitted"] == 0
    assert session.calls[0]["url"] == "http://coordinator.test/data"
    assert session.calls[0]["params"] == {"prefix": "request:"}


def stored_request(i, status, hour):
    return {"key": "request:x", "value": json.dumps({"id": i, "status": status, "location": {"lat": 0, "lon": 0},
                                                      "created_at": f"2026-11-19T{hour:02d}:00:00.000Z"})}


def test_every_open_request_is_listed_however_many_there_are():
    records = [stored_request(f"open-{n}", "PENDING", n % 24) for n in range(MAX_LISTED_CLOSED + 1)]
    client, _ = client_for(FakeResponse(200, {"records": records}))

    body = client.get("/api/all-requests").get_json()

    assert len(body["requests"]) == body["open"] == body["count"] == MAX_LISTED_CLOSED + 1
    assert body["closed_omitted"] == 0


def test_only_the_oldest_closed_requests_are_left_out():
    # The open request is the oldest record; the newer closed ones exceed the cap.
    records = [stored_request("old-open", "PENDING", 0)]
    records += [stored_request(f"closed-{n}", "COMPLETED" if n % 2 else "CANCELLED", 1 + n % 20)
                for n in range(MAX_LISTED_CLOSED + 10)]
    client, _ = client_for(FakeResponse(200, {"records": records}))

    body = client.get("/api/all-requests").get_json()

    ids = [r["id"] for r in body["requests"]]
    assert ids[0] == "old-open" and len(ids) == 1 + MAX_LISTED_CLOSED
    assert (body["count"], body["open"], body["closed"]) == (MAX_LISTED_CLOSED + 11, 1, MAX_LISTED_CLOSED + 10)
    assert body["closed_omitted"] == 10


def test_match_now_is_forwarded_with_its_empty_json_body():
    # Regression: the page's "Match now" button once sent no body (so no
    # Content-Type) and the JSON guard rejected it with 415.
    client, session = client_for(FakeResponse(200, {"id": "r1", "status": "MATCHED"}))
    reply = client.post("/api/match/r1", json={}, headers={"Origin": "http://localhost"})
    assert reply.status_code == 200
    assert (session.calls[0]["method"], session.calls[0]["url"]) == ("POST", "http://api.test/match/r1")


def test_writes_must_be_json():
    client, session = client_for()
    reply = client.post("/api/requests", data='{"type": "water"}', content_type="text/plain")
    assert reply.status_code == 415 and reply.get_json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"
    # A bodyless write is refused too: a cross-site page can send one without a preflight.
    assert client.post("/api/match/r1").status_code == 415
    assert session.calls == []


@pytest.mark.parametrize("origin", [
    "https://evil.example",
    "https://localhost",        # same host and default port, different scheme
    "http://localhost:8080",    # same host, different port
    "null",                     # sandboxed or opaque origins
])
def test_writes_from_another_origin_are_refused(origin):
    client, session = client_for()
    reply = client.post("/api/requests", json={}, headers={"Origin": origin})
    assert reply.status_code == 403 and reply.get_json()["error"]["code"] == "FORBIDDEN_ORIGIN"
    assert session.calls == []


@pytest.mark.parametrize("origin, host", [("http://localhost", "localhost"), ("http://localhost:80", "localhost"),
                                          ("http://127.0.0.1:5050", "127.0.0.1:5050"), ("http://[::1]:5050", "[::1]:5050")])
def test_writes_from_this_page_are_accepted(origin, host):
    client, session = client_for(FakeResponse(201, {"id": "r1"}))
    reply = client.post("/api/requests", json={}, headers={"Origin": origin, "Host": host})
    assert reply.status_code == 201 and len(session.calls) == 1


@pytest.mark.parametrize("host", ["evil.example", "evil.example:5050"])
def test_a_rebound_hostname_is_refused(host):
    client, session = client_for()
    reply = client.get("/api/resources", headers={"Host": host})
    assert reply.status_code == 403 and reply.get_json()["error"]["code"] == "FORBIDDEN_HOST"
    assert session.calls == []


@pytest.mark.parametrize("host", ["localhost:5050", "127.0.0.1:5050", "[::1]:5050"])
def test_loopback_hostnames_are_answered(host):
    client, _ = client_for(FakeResponse(200, {"count": 0, "resources": []}))
    assert client.get("/api/resources", headers={"Host": host}).status_code == 200
