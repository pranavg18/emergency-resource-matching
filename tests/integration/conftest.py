"""Start storage-layer components as real processes and talk to them over HTTP.

These tests never import the coordinator or storage node: they only know a
base URL. That keeps them valid for any implementation of the contract. To run
them against another build (for example a Go port), change `_command` below.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
START_TIMEOUT_S = 20.0


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _command(component: str, port: int) -> list[str]:
    package = {"storage-node": "storage_node", "coordinator": "coordinator"}[component]
    return [
        sys.executable, "-m", "gunicorn",
        "-c", f"{package}/gunicorn.conf.py",
        "--bind", f"127.0.0.1:{port}",
        f"{package}.app:create_app()",
    ]


def wait_until(predicate: Callable[[], bool], timeout: float = 10.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class Server:
    """One component running as a subprocess on a fixed local port."""

    def __init__(self, component: str, env: dict[str, str], log_dir: Path, port: Optional[int] = None):
        self.component = component
        self.env = env
        self.port = port or free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.log_path = log_dir / f"{component}-{self.port}.log"
        self.proc: Optional[subprocess.Popen] = None

    def start(self) -> "Server":
        env = {**os.environ, **self.env}
        log_file = open(self.log_path, "ab")
        # Own process group, so stop() and kill() reach gunicorn's worker too,
        # not only its master. A kill then behaves like a container dying.
        self.proc = subprocess.Popen(
            _command(self.component, self.port),
            cwd=REPO_ROOT, env=env, stdout=log_file, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log_file.close()
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                pytest.fail(f"{self.component} exited during startup:\n{self.logs()}")
            try:
                if requests.get(f"{self.url}/healthz", timeout=0.5).status_code == 200:
                    return self
            except requests.RequestException:
                pass
            time.sleep(0.05)
        self.stop()
        pytest.fail(f"{self.component} did not become healthy:\n{self.logs()}")

    def stop(self, sig: int = signal.SIGTERM) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            os.killpg(self.proc.pid, sig)
        except ProcessLookupError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait(timeout=5)
        if sig == signal.SIGKILL:
            # The killed worker may outlive the master briefly; wait for the port.
            wait_until(lambda: not _port_in_use(self.port), timeout=5)

    def kill(self) -> None:
        self.stop(signal.SIGKILL)

    def restart(self) -> "Server":
        self.stop()
        return self.start()

    def logs(self) -> str:
        try:
            return self.log_path.read_text(errors="replace")[-4000:]
        except FileNotFoundError:
            return "(no log output)"


@pytest.fixture
def servers(tmp_path):
    started: list[Server] = []
    yield started
    for server in reversed(started):
        server.stop()


@pytest.fixture
def start_storage_node(tmp_path, servers):
    def _start(
        node_id: str = "s0-a",
        shard: int = 0,
        coordinator_url: Optional[str] = None,
        data_dir: Optional[Path] = None,
    ) -> Server:
        port = free_port()
        env = {
            "NODE_ID": node_id,
            "SHARD_ID": str(shard),
            "NODE_URL": f"http://127.0.0.1:{port}",
            "DATA_DIR": str(data_dir or tmp_path / node_id),
            # By default nothing listens here, so the node runs on its own.
            "COORDINATOR_URL": coordinator_url or f"http://127.0.0.1:{free_port()}",
            "LOG_LEVEL": "DEBUG",
        }
        server = Server("storage-node", env, tmp_path, port=port)
        servers.append(server)
        return server.start()

    return _start


@pytest.fixture
def start_coordinator(tmp_path, servers):
    def _start(
        heartbeat_interval_ms: int = 200, heartbeat_timeout_ms: int = 1000, port: Optional[int] = None
    ) -> Server:
        env = {
            "HEARTBEAT_INTERVAL_MS": str(heartbeat_interval_ms),
            "HEARTBEAT_TIMEOUT_MS": str(heartbeat_timeout_ms),
            "LOG_LEVEL": "DEBUG",
        }
        server = Server("coordinator", env, tmp_path, port=port)
        servers.append(server)
        return server.start()

    return _start
