"""Idle keep-alive connections must stay open longer than clients reuse them.

Nodes heartbeat every 2 s on a pooled connection and the coordinator pools its
connections to nodes; ingress-nginx reuses upstream connections for up to
60 s. If the server closes an idle connection just as a client sends on it,
that request fails ("Connection aborted", or a 502 at the Ingress).
"""

import http.client
import re
import runpy
import time

import pytest

from tests.integration.conftest import REPO_ROOT

INGRESS_UPSTREAM_KEEPALIVE_S = 60  # ingress-nginx default upstream-keepalive-timeout
IDLE_S = 3  # longer than gunicorn's old default of 2 s


@pytest.fixture(params=["storage-node", "coordinator"])
def server(request, start_storage_node, start_coordinator):
    return start_storage_node() if request.param == "storage-node" else start_coordinator()


def test_an_idle_connection_is_still_served_after_a_pause(server):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    try:
        conn.request("GET", "/healthz")
        first = conn.getresponse()
        assert first.status == 200
        first.read()
        local_port = conn.sock.getsockname()[1]

        time.sleep(IDLE_S)

        conn.request("GET", "/healthz")  # raises RemoteDisconnected if the server closed it
        response = conn.getresponse()
        assert response.status == 200
        response.read()
        assert conn.sock.getsockname()[1] == local_port  # the same connection, not a new one
    finally:
        conn.close()


@pytest.mark.parametrize("path", ["services/request_service/Dockerfile", "services/resource_service/Dockerfile"])
def test_api_service_images_outlast_the_ingress_keepalive(path):
    match = re.search(r'"--keep-alive",\s*"(\d+)"', (REPO_ROOT / path).read_text())
    assert match, f"{path} does not set --keep-alive"
    assert int(match.group(1)) > INGRESS_UPSTREAM_KEEPALIVE_S


@pytest.mark.parametrize(
    "path",
    ["coordinator/gunicorn.conf.py", "storage_node/gunicorn.conf.py", "services/matching_service/gunicorn.conf.py"],
)
def test_gunicorn_configs_outlast_the_ingress_keepalive(path):
    settings = runpy.run_path(str(REPO_ROOT / path))
    assert settings.get("keepalive", 2) > INGRESS_UPSTREAM_KEEPALIVE_S
