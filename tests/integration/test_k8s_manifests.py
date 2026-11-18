"""Properties of the rendered Kubernetes manifests the demo relies on.

Renders deploy/k8s with `kubectl kustomize` (offline, no cluster needed) and
checks each object's text, so no YAML library is required.
"""

import re
import shutil
import subprocess

import pytest

from tests.integration.conftest import REPO_ROOT

pytestmark = pytest.mark.skipif(shutil.which("kubectl") is None, reason="needs kubectl")


@pytest.fixture(scope="module")
def deployments():
    rendered = subprocess.run(
        ["kubectl", "kustomize", "deploy/k8s"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout
    found = {}
    for doc in rendered.split("\n---\n"):
        if re.search(r"^kind: Deployment$", doc, re.M):
            name = re.search(r"^  name: (\S+)$", doc, re.M).group(1)
            found[name] = doc
    return found


@pytest.mark.parametrize("name", ["request-service", "resource-service"])
def test_api_services_survive_losing_a_pod(deployments, name):
    """Deleting one pod (the spec's Kubernetes recovery demo) must not fail
    requests: a second ready pod keeps serving, and the deleted pod keeps
    answering while it is being removed from the Service and Ingress."""
    doc = deployments[name]
    replicas = int(re.search(r"^  replicas: (\d+)$", doc, re.M).group(1))
    assert replicas >= 2
    assert re.search(r"preStop:\s+exec:\s+command:\s+- sleep\s+- \"5\"", doc), doc

    grace = int(re.search(r"terminationGracePeriodSeconds: (\d+)", doc).group(1))
    assert grace > 5 + 30  # preStop sleep plus gunicorn's default graceful drain


def test_there_is_exactly_one_coordinator(deployments):
    doc = deployments["coordinator"]
    assert re.search(r"^  replicas: 1$", doc, re.M)
    assert "type: Recreate" in doc
