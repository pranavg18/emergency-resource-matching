"""client.demo only looks at its own run's requests, whatever else is stored."""

from types import SimpleNamespace

from client.demo import Demo


def test_records_skip_foreign_and_malformed_request_records(monkeypatch):
    demo = Demo(SimpleNamespace(coordinator="http://coordinator.invalid"))
    stored = [
        {"key": "request:a", "value": {"id": "a", "status": "MATCHED"}},
        {"key": "request:b", "value": {"id": "b", "status": "PENDING"}},
        {"key": "request:x", "value": {"type": "water", "phase": "left by another tool"}},
        {"key": "request:y", "value": {"id": "y", "status": "CANCELLED"}},
    ]
    monkeypatch.setattr(demo.storage, "scan", lambda prefix: stored)

    records = demo.records({"a", "b"})

    assert set(records) == {"a", "b"}
    assert records["a"]["status"] == "MATCHED"
