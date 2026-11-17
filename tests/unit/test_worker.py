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
