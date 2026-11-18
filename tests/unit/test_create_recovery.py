from unittest.mock import Mock

from common.core import APIError, Unavailable
from common.storage import FakeStorage
from services.request_service.app import create_app


def test_create_recovers_committed_timeout_without_duplicate_publish():
    storage = FakeStorage()
    original = storage.put
    def put(key, value, version):
        result = original(key, value, version)
        if version == 0:
            raise Unavailable()
        return result
    storage.put = put
    publish = Mock()
    reply = create_app(storage, publish).test_client().post('/requests', json={
        'type': 'water', 'location': {'lat': 0, 'lon': 0}, 'details': 'test'})
    assert reply.status_code == 201
    assert reply.json['queued'] is True
    assert len(storage.records) == 1
    publish.assert_called_once()


def test_create_failure_is_bounded_and_reuses_one_key():
    storage = Mock()
    storage.put.side_effect = Unavailable()
    storage.get.side_effect = APIError('not found', 404)
    publish = Mock()
    reply = create_app(storage, publish).test_client().post('/requests', json={
        'type': 'water', 'location': {'lat': 0, 'lon': 0}, 'details': 'test'})
    assert reply.status_code == 503
    assert storage.put.call_count == storage.get.call_count == 5
    assert len({call.args[0] for call in storage.put.call_args_list}) == 1
    publish.assert_not_called()
