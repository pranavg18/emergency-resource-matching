"""Exercise the real Gunicorn SIGTERM path while a delivery is in its handler."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from common.core import app_base

from tests.integration.conftest import REPO_ROOT, free_port, wait_until


def create_app():
    # A controlled broker lets the test hold the handler open at a known point.
    # Gunicorn, its signals, and the application's consumer are the real code.
    from services.matching_service import worker as module
    directory = Path(os.environ["SHUTDOWN_TEST_DIR"])

    def record(event):
        with (directory / "events").open("a") as stream:
            stream.write(event + "\n")

    class Matcher:
        def match(self, request_id):
            (directory / "entered").touch()
            while not (directory / "finish").exists():
                if consumer.stop.is_set():
                    (directory / "stopping").touch()
                time.sleep(.01)
            record("handled")
            return {"matched": True}

        def sweep(self, publisher):
            pass

    class Broker:
        is_open = True

        def channel(self):
            return self

        def basic_qos(self, **kwargs):
            pass

        def basic_consume(self, queue, callback):
            self.callback = callback

        def process_data_events(self, **kwargs):
            self.callback(self,
                SimpleNamespace(routing_key="request.created", delivery_tag=1, redelivered=False),
                SimpleNamespace(correlation_id="sigterm"), b'{"request_id":"r","attempt":0}')

        def basic_ack(self, tag):
            record("acked")

        def close(self):
            self.is_open = False
            record("closed")

    module.connection = Broker
    module.topology = lambda channel: None
    app = app_base("shutdown-test")
    consumer = module.Worker(Matcher())
    app.extensions["worker"] = consumer
    consumer.start()
    return app


@pytest.mark.skipif(sys.platform == "win32", reason="Gunicorn needs POSIX signals")
def test_sigterm_drains_active_handler_before_worker_exits(tmp_path):
    port = free_port()
    with (tmp_path / "gunicorn.log").open("w+") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "gunicorn", "-c", "services/matching_service/gunicorn.conf.py",
             "--bind", f"127.0.0.1:{port}", "tests.integration.test_worker_signal:create_app()"],
            cwd=REPO_ROOT, env={**os.environ, "SHUTDOWN_TEST_DIR": str(tmp_path)},
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            def ready():
                try:
                    return requests.get(f"http://127.0.0.1:{port}/healthz", timeout=.5).ok
                except requests.RequestException:
                    return False

            assert wait_until(ready, timeout=15)
            assert wait_until(lambda: (tmp_path / "entered").exists())
            proc.send_signal(signal.SIGTERM)
            assert wait_until(lambda: (tmp_path / "stopping").exists())
            assert proc.poll() is None
            assert not (tmp_path / "events").exists()
            (tmp_path / "finish").touch()
            assert proc.wait(timeout=10) == 0
            assert (tmp_path / "events").read_text().splitlines() == ["handled", "acked", "closed"]
            log.seek(0)
            assert '"drained": true' in log.read()
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
