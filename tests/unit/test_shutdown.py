import json
import runpy
import signal
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from common.messaging import connection
from services.matching_service.worker import Worker


def test_shutdown_finishes_and_acks_active_delivery_before_connection_close(monkeypatch):
    entered, finish = threading.Event(), threading.Event()
    order = []
    matcher = Mock()

    def match(request_id):
        entered.set()
        assert finish.wait(5), "test did not release the handler"
        order.append("handled")
        return {"matched": True}

    matcher.match.side_effect = match
    worker = Worker(matcher)
    conn, channel = Mock(), Mock()
    conn.is_open = True
    conn.channel.return_value = channel
    callbacks = {}
    channel.basic_consume.side_effect = lambda queue, fn: callbacks.update({queue: fn})
    channel.basic_ack.side_effect = lambda tag: order.append("acked")

    def close():
        order.append("closed")
        conn.is_open = False

    conn.close.side_effect = close

    def deliver(**kwargs):
        callbacks["match.requests"](channel,
            SimpleNamespace(routing_key="request.created", delivery_tag=1, redelivered=False),
            SimpleNamespace(correlation_id="shutdown"),
            json.dumps({"request_id": "r", "attempt": 0}).encode())

    conn.process_data_events.side_effect = deliver
    monkeypatch.setattr("services.matching_service.worker.connection", lambda: conn)
    monkeypatch.setattr("services.matching_service.worker.topology", lambda channel: None)
    thread = threading.Thread(target=worker.consume)
    worker.threads = [thread]
    thread.start()
    assert entered.wait(2)
    result = []
    shutdown = threading.Thread(target=lambda: result.append(worker.shutdown(timeout=3)))
    shutdown.start()
    try:
        assert worker.stop.wait(1)
        assert conn.is_open
        channel.basic_ack.assert_not_called()
    finally:
        finish.set()
        shutdown.join(4)
        thread.join(4)
    assert result == [True]
    assert order == ["handled", "acked", "closed"]
    matcher.match.assert_called_once_with("r")


def test_shutdown_before_delivery_leaves_message_for_redelivery(monkeypatch):
    worker = Worker(Mock())
    conn, channel = Mock(), Mock()
    conn.is_open = True
    conn.channel.return_value = channel
    callbacks = {}
    channel.basic_consume.side_effect = lambda queue, fn: callbacks.update({queue: fn})

    def deliver(**kwargs):
        worker.stop.set()
        callbacks["match.requests"](channel, None, None, b"unused")

    conn.process_data_events.side_effect = deliver
    monkeypatch.setattr("services.matching_service.worker.connection", lambda: conn)
    monkeypatch.setattr("services.matching_service.worker.topology", lambda channel: None)
    worker.consume()
    worker.matcher.match.assert_not_called()
    channel.basic_ack.assert_not_called()
    channel.basic_reject.assert_not_called()
    conn.close.assert_called_once()


def test_gunicorn_signal_preserves_server_handler_and_drains_on_exit(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "services/matching_service/gunicorn.conf.py"
    config = runpy.run_path(str(path))
    previous = Mock()
    registered = {}
    monkeypatch.setattr(signal, "getsignal", lambda signum: previous)
    monkeypatch.setattr(signal, "signal", lambda signum, fn: registered.update({signum: fn}))
    consumer = Worker(Mock())
    consumer.shutdown = Mock()
    process = SimpleNamespace(wsgi=SimpleNamespace(extensions={"worker": consumer}))
    config["post_worker_init"](process)
    registered[signal.SIGTERM](signal.SIGTERM, None)
    assert consumer.stop.is_set()
    previous.assert_called_once_with(signal.SIGTERM, None)
    config["worker_exit"](None, process)
    consumer.shutdown.assert_called_once_with(timeout=25)


def test_secret_credentials_are_not_interpreted_as_url_syntax(monkeypatch):
    monkeypatch.setenv("RABBITMQ_URL", "amqp://rabbitmq:5672/")
    monkeypatch.setenv("RABBITMQ_USERNAME", "demo@user")
    monkeypatch.setenv("RABBITMQ_PASSWORD", "pass:/@?#word")
    connect = Mock()
    monkeypatch.setattr("common.messaging.pika.BlockingConnection", connect)
    connection()
    params = connect.call_args.args[0]
    assert params.host == "rabbitmq"
    assert params.credentials.username == "demo@user"
    assert params.credentials.password == "pass:/@?#word"
