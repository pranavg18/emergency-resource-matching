from concurrent.futures import ThreadPoolExecutor

import pytest

from common.core import Conflict, new_record
from common.storage import FakeStorage
from services.matching_service.app import create_app as matching_app
from services.matching_service.engine import ClaimResult, Matcher, distance
from services.request_service.app import create_app as request_app
from services.resource_service.app import create_app as resource_app

LOCATION = {"lat": 28.61, "lon": 77.20}


def seed(storage, resource=False, **fields):
    data = {"type": "water", "location": LOCATION, "availability": 1, **fields}
    value = new_record(data, resource)
    storage.put(("resource:" if resource else "request:") + value["id"], value, 0)
    return value


def test_service_lifecycle_and_release():
    s = FakeStorage()
    events = []
    req = request_app(s, lambda *a: events.append(a)).test_client()
    res = resource_app(s).test_client()
    match = matching_app(s).test_client()
    r = res.post("/resources", json={"type": "water", "location": LOCATION, "availability": 1})
    assert r.status_code == 201
    q = req.post("/requests", json={"type": "water", "location": LOCATION})
    assert q.status_code == 201 and q.json["queued"]
    rid = q.json["id"]
    assert match.post("/match/" + rid).json["matched"]
    assert match.post("/match/" + rid).json["matched"]
    assert req.put(f"/requests/{rid}/status", json={"status": "COMPLETED"}).status_code == 409
    assert req.put(f"/requests/{rid}/status", json={"status": "IN_PROGRESS"}).status_code == 200
    assert req.put(f"/requests/{rid}/status", json={"status": "COMPLETED"}).status_code == 200
    assert match.post("/match/" + rid).status_code == 409
    Matcher(s).sweep(lambda *a: None)
    Matcher(s).sweep(lambda *a: None)
    resource = s.get("resource:" + r.json["id"])["value"]
    assert resource["availability"] == 1 and resource["assigned_request_ids"] == {}


@pytest.mark.parametrize("payload", [None, [], {}, {"type": "fire", "location": LOCATION},
    {"type": "water", "location": {"lat": 91, "lon": 1}},
    {"type": "water", "location": {"lat": True, "lon": 1}},
    {"type": "water", "location": LOCATION, "details": []},
    {"type": [], "location": LOCATION}])
def test_bad_requests(payload):
    c = request_app(FakeStorage(), lambda *a: None).test_client()
    assert c.post("/requests", json=payload).status_code == 400


def test_broker_down_and_sweeper():
    s = FakeStorage()

    def failed(*args):
        raise OSError("broker down")
    c = request_app(s, failed).test_client()
    r = c.post("/requests", json={"type": "water", "location": LOCATION})
    assert r.status_code == 201 and not r.json["queued"]
    messages = []
    Matcher(s).sweep(lambda *a: messages.append(a), age=0)
    assert len(messages) == 1
    assert s.get("request:" + r.json["id"])["value"]["queued"]


def test_nearest_radius_type_and_tie_break():
    s = FakeStorage()
    seed(s, True, type="food")
    seed(s, True, location={"lat": 0, "lon": 0})
    best = seed(s, True, availability=3)
    seed(s, True, availability=1)
    q = seed(s)
    assert Matcher(s).match(q["id"])["resource"]["id"] == best["id"]
    assert distance(LOCATION, LOCATION) == 0
    no_match = seed(s, type="shelter")
    assert not Matcher(s).match(no_match["id"])["matched"]
    assert s.get("request:" + no_match["id"])["value"]["status"] == "PENDING"


def test_concurrent_requests_do_not_overassign():
    s = FakeStorage()
    r = seed(s, True, availability=10)
    requests = [seed(s) for _ in range(40)]
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda q: Matcher(s).match(q["id"]), requests))
    assert sum(r["matched"] for r in results) == 10
    resource = s.get("resource:" + r["id"])["value"]
    assert resource["availability"] == 0
    assert len(resource["assigned_request_ids"]) == 10


def test_same_request_many_workers_one_claim():
    s = FakeStorage()
    r = seed(s, True, availability=10)
    q = seed(s)
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: Matcher(s).match(q["id"]), range(50)))
    assert all(x["matched"] for x in results)
    assert s.get("resource:" + r["id"])["value"]["availability"] == 9


def test_attempt_fences_takeover_and_release():
    s = FakeStorage()
    r, q = seed(s, True), seed(s)
    m = Matcher(s)
    assert m.claim(r["id"], q["id"], 1)
    assert m.claim(r["id"], q["id"], 3)
    assert m.claim(r["id"], q["id"], 1) is ClaimResult.STALE
    m.release(r["id"], q["id"], 1)
    v = s.get("resource:" + r["id"])["value"]
    assert v["availability"] == 0 and v["assigned_request_ids"][q["id"]] == 3
    m.release(r["id"], q["id"], 3)
    m.release(r["id"], q["id"], 3)
    assert s.get("resource:" + r["id"])["value"]["availability"] == 1


def test_cancel_during_claim_reconciles():
    s = FakeStorage()
    r, q = seed(s, True), seed(s)
    m = Matcher(s)
    original = m.claim

    def cancel_then_claim(*args):
        request_app(s, lambda *a: None).test_client().put(
            f"/requests/{q['id']}/status", json={"status": "CANCELLED"})
        return original(*args)
    m.claim = cancel_then_claim
    with pytest.raises(Conflict):
        m.match(q["id"])
    m.sweep(lambda *a: None)
    assert s.get("resource:" + r["id"])["value"]["availability"] == 1


def test_resource_status_and_offline_release():
    s = FakeStorage()
    r, q = seed(s, True), seed(s)
    m = Matcher(s)
    m.match(q["id"])
    c = resource_app(s).test_client()
    path = f"/resources/{r['id']}/status"
    assert c.put(path, json={"status": "AVAILABLE"}).status_code == 409
    assert c.put(path, json={"availability": -1}).status_code == 400
    assert c.put(path, json={"status": "OFFLINE"}).status_code == 200
    m.release(r["id"], q["id"], 1)
    v = s.get("resource:" + r["id"])["value"]
    assert v["availability"] == 1 and v["status"] == "OFFLINE"


def test_reconciliation_preserves_in_progress_claim():
    s = FakeStorage()
    r, q = seed(s, True), seed(s)
    record = s.get("request:" + q["id"])
    record["value"].update(candidate_resource_id=r["id"], match_attempt=1)
    s.put(record["key"], record["value"], record["version"])
    m = Matcher(s)
    m.claim(r["id"], q["id"], 1)
    m.sweep(lambda *a: None)
    assert s.get("resource:" + r["id"])["value"]["availability"] == 0
    assert m.match(q["id"])["matched"]


def test_old_finalizer_cannot_finalize_reselected_candidate():
    s = FakeStorage()
    r, q = seed(s, True), seed(s)
    old = Matcher(s)
    original = old.claim
    paused = False

    def switch_attempt(resource_id, request_id, attempt):
        nonlocal paused
        result = original(resource_id, request_id, attempt)
        if not paused:
            paused = True
            record = s.get("request:" + q["id"])
            record["value"]["match_attempt"] = 3
            s.put(record["key"], record["value"], record["version"])
            # New attempt took over the old resource after R1 -> R2 -> R1.
            assert Matcher(s).claim(r["id"], q["id"], 3)
        return result
    old.claim = switch_attempt
    result = old.match(q["id"])
    assert result["request"]["match_attempt"] == 3
    assert result["resource"]["assigned_request_ids"][q["id"]] == 3
    assert result["resource"]["availability"] == 0


def test_competing_rankings_reserve_only_request_selected_resource():
    import threading
    s = FakeStorage()
    r1, r2, q = seed(s, True), seed(s, True), seed(s)
    barrier = threading.Barrier(2)
    matchers = [Matcher(s), Matcher(s)]
    for matcher, preferred in zip(matchers, [r1, r2]):
        def rank(value, excluded, preferred=preferred):
            barrier.wait(timeout=5)
            return [preferred]
        matcher.ranked = rank
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda m: m.match(q["id"]), matchers))
    assert results[0]["resource"]["id"] == results[1]["resource"]["id"]
    assert sum(len(r["value"]["assigned_request_ids"]) for r in s.scan("resource:")) == 1


def test_queued_flag_does_not_overwrite_fast_matcher():
    s = FakeStorage()
    seed(s, True)

    def immediate_consumer(event, message):
        Matcher(s).match(message["request_id"])
    c = request_app(s, immediate_consumer).test_client()
    response = c.post("/requests", json={"type": "water", "location": LOCATION})
    assert response.status_code == 201
    actual = s.get("request:" + response.json["id"])["value"]
    assert actual["status"] == "MATCHED" and actual["assigned_resource_id"]


def test_manual_resource_update_preserves_racing_claim():
    s = FakeStorage()
    r, q = seed(s, True), seed(s)
    original = s.put
    raced = False

    def race(key, value, version):
        nonlocal raced
        if key == "resource:" + r["id"] and not raced:
            raced = True
            Matcher(s).claim(r["id"], q["id"], 1)
        return original(key, value, version)
    s.put = race
    c = resource_app(s).test_client()
    response = c.put(f"/resources/{r['id']}/status", json={"availability": 5})
    assert response.status_code == 200
    assert response.json["availability"] == 5
    assert response.json["assigned_request_ids"] == {q["id"]: 1}


def test_status_payloads_with_wrong_types_are_validation_errors():
    s = FakeStorage()
    r, q = seed(s, True), seed(s)
    rc = resource_app(s).test_client()
    qc = request_app(s, lambda *a: None).test_client()
    assert rc.put(f"/resources/{r['id']}/status", json={"status": []}).status_code == 400
    assert qc.put(f"/requests/{q['id']}/status", json={"status": {}}).status_code == 400


def test_broker_outage_does_not_block_claim_reconciliation():
    s = FakeStorage()
    r, q = seed(s, True), seed(s)
    m = Matcher(s)
    m.match(q["id"])
    request_app(s, lambda *a: None).test_client().put(
        f"/requests/{q['id']}/status", json={"status": "CANCELLED"})
    seed(s)  # An unqueued request must not prevent the later claim-repair pass.

    def failed(*args):
        raise OSError("broker down")
    m.sweep(failed, age=0)
    assert s.get("resource:" + r["id"])["value"]["availability"] == 1


def test_stale_worker_resumes_newer_claim_on_only_resource():
    storage = FakeStorage()
    resource, request = seed(storage, True), seed(storage)
    record = storage.get("request:" + request["id"])
    record["value"].update(candidate_resource_id=resource["id"], match_attempt=1)
    storage.put(record["key"], record["value"], record["version"])
    old = Matcher(storage)
    original = old.claim
    paused = False

    def newer_claim(resource_id, request_id, attempt):
        nonlocal paused
        if not paused:
            paused = True
            record = storage.get("request:" + request_id)
            record["value"]["match_attempt"] = 3
            storage.put(record["key"], record["value"], record["version"])
            assert Matcher(storage).claim(resource_id, request_id, 3) is ClaimResult.CLAIMED
        return original(resource_id, request_id, attempt)

    old.claim = newer_claim
    result = old.match(request["id"])
    assert result["matched"] and result["resource"]["id"] == resource["id"]
    assert result["resource"]["availability"] == 0
    assert result["resource"]["assigned_request_ids"] == {request["id"]: 3}
