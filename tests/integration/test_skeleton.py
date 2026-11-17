"""Behaviour every storage-layer component shares: health checks and errors."""

import pytest
import requests


@pytest.fixture(params=["storage-node", "coordinator"])
def component(request, start_storage_node, start_coordinator):
    return start_storage_node() if request.param == "storage-node" else start_coordinator()


def test_healthz(component):
    response = requests.get(f"{component.url}/healthz", timeout=2)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz(component):
    response = requests.get(f"{component.url}/readyz", timeout=2)
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_unknown_route_is_json_404(component):
    response = requests.get(f"{component.url}/no/such/route", timeout=2)
    assert response.status_code == 404
    assert response.headers["Content-Type"].startswith("application/json")
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_wrong_method_is_json_405(component):
    response = requests.delete(f"{component.url}/healthz", timeout=2)
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_request_id_is_echoed(component):
    response = requests.get(f"{component.url}/healthz", headers={"X-Request-ID": "abc-123"}, timeout=2)
    assert response.headers["X-Request-ID"] == "abc-123"


def test_request_id_is_generated_when_missing(component):
    response = requests.get(f"{component.url}/healthz", timeout=2)
    assert len(response.headers["X-Request-ID"]) == 36


def test_storage_node_refuses_to_start_without_required_config(tmp_path):
    import subprocess
    import sys

    from tests.integration.conftest import REPO_ROOT, _command, free_port

    env = {"PATH": "/usr/bin:/bin", "SHARD_ID": "0"}  # NODE_ID, NODE_URL, DATA_DIR missing
    result = subprocess.run(
        _command("storage-node", free_port()), cwd=REPO_ROOT, env=env, capture_output=True, timeout=20
    )
    assert result.returncode != 0
    assert b"NODE_ID is required" in result.stdout + result.stderr
