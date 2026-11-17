from unittest.mock import Mock

import pytest

from common.core import Unavailable
from services.matching_service.worker import Worker


def test_retry_confirm_before_success_and_storage_does_not_consume_attempt(monkeypatch):
    matcher = Mock()
    matcher.match.side_effect = Unavailable()
    publish = Mock()
    monkeypatch.setattr("services.matching_service.worker.publish", publish)
    message = {"request_id": "r", "attempt": 2}
    Worker(matcher).handle(message)
    publish.assert_called_once_with("retry", message)
    assert message["attempt"] == 2
    matcher.mark_unmatched.assert_not_called()
    publish.side_effect = OSError("lost confirm")
    with pytest.raises(OSError):
        Worker(matcher).handle(message)


def test_exhaustion_and_poison(monkeypatch):
    matcher = Mock()
    matcher.match.return_value = {"matched": False}
    monkeypatch.setenv("MAX_ATTEMPTS", "2")
    Worker(matcher).handle({"request_id": "r", "attempt": 1})
    matcher.mark_unmatched.assert_called_once_with("r")
    for message in ([], {}, {"request_id": "r", "attempt": -1}):
        with pytest.raises(ValueError):
            Worker(matcher).handle(message)


@pytest.mark.parametrize("shutdown", [False, True])
def test_release_failure_delays_requeue_and_honors_shutdown(monkeypatch, shutdown):
    from types import SimpleNamespace
    import json
    matcher = Mock()
    matcher.release.side_effect = Unavailable()
    worker = Worker(matcher)
    conn, channel = Mock(), Mock()
    conn.is_open = True
    conn.channel.return_value = channel
    callbacks = {}
    channel.basic_consume.side_effect = lambda queue, callback: callbacks.update({queue: callback})
    order = []

    def wait(seconds):
        if seconds == 2:
            order.append("wait")
            return shutdown
        return False

    worker.stop.wait = Mock(side_effect=wait)
    channel.basic_nack.side_effect = lambda *a, **k: order.append("nack")
    def deliver(**kwargs):
        callbacks["match.released"](channel,
            SimpleNamespace(routing_key="request.closed", delivery_tag=7, redelivered=False),
            SimpleNamespace(correlation_id="trace"),
            json.dumps({"request_id": "r", "resource_id": "s", "attempt": 1}).encode())
        worker.stop.set()
    conn.process_data_events.side_effect = deliver
    monkeypatch.setattr("services.matching_service.worker.connection", lambda: conn)
    monkeypatch.setattr("services.matching_service.worker.topology", lambda channel: None)
    publish = Mock()
    monkeypatch.setattr("services.matching_service.worker.publish", publish)
    worker.consume()
    if shutdown:
        channel.basic_nack.assert_not_called()
        conn.close.assert_called()
    else:
        assert order[:2] == ["wait", "nack"]
        channel.basic_nack.assert_called_once_with(7, requeue=True)
    publish.assert_not_called()
    channel.basic_ack.assert_not_called()
