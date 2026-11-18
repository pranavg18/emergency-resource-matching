"""scripts/k8s-up.sh with docker, minikube and kubectl replaced by stubs.

The stubs record what they are asked to do; `kubectl kustomize` is passed to
the real kubectl so the manifests are rendered for real. Needs bash and kubectl.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration.conftest import REPO_ROOT

REAL_KUBECTL = shutil.which("kubectl")
pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or REAL_KUBECTL is None or shutil.which("bash") is None,
    reason="needs bash and kubectl",
)

KUBECTL_STUB = """#!/usr/bin/env bash
echo "kubectl $*" >> "$STUB_DIR/calls.log"
if [ "$1 $2" = "config current-context" ]; then echo "${MINIKUBE_PROFILE:-minikube}"; exit 0; fi
if [ "$1" = kustomize ]; then exec "$REAL_KUBECTL" "$@"; fi
if [ "$1" = apply ] && [ "$2" = -f ]; then cp "$3" "$STUB_DIR/applied.yaml"; fi
if [[ "$*" == *"rollout status statefulset"* ]] && [ -n "${FAIL_STATEFULSET:-}" ]; then
  echo 'error: statefulset "storage-s1-a" exceeded its progress deadline' >&2
  exit 1
fi
exit 0
"""


@pytest.fixture
def stubs(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "kubectl").write_text(KUBECTL_STUB)
    for name in ("docker", "minikube"):
        (bin_dir / name).write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> "$STUB_DIR/calls.log"\n')
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    return tmp_path


def run(stubs: Path, **env) -> subprocess.CompletedProcess:
    full_env = {
        **os.environ,
        "PATH": f"{stubs / 'bin'}:{os.environ['PATH']}",
        "STUB_DIR": str(stubs),
        "REAL_KUBECTL": REAL_KUBECTL,
        **env,
    }
    return subprocess.run(
        ["bash", "scripts/k8s-up.sh"], cwd=REPO_ROOT, env=full_env, capture_output=True, text=True, timeout=60
    )


def applied_images(stubs: Path) -> list[str]:
    text = (stubs / "applied.yaml").read_text()
    return [line.split("image:", 1)[1].strip() for line in text.splitlines() if "image:" in line]


def test_default_tag_deploys_dev_images(stubs):
    result = run(stubs)
    assert result.returncode == 0, result.stderr
    images = applied_images(stubs)
    assert "emergency/coordinator:dev" in images
    assert "emergency/storage-node:dev" in images
    assert "emergency/matching-service:dev" in images
    assert "emergency/request-service:dev" in images
    assert "emergency/resource-service:dev" in images
    assert "rabbitmq:3.13-management" in images
    assert "Done." in result.stdout


def test_custom_tag_reaches_every_applied_image(stubs):
    result = run(stubs, TAG="review-v2")
    assert result.returncode == 0, result.stderr
    images = applied_images(stubs)
    assert images, "nothing was applied"
    assert all(image.endswith(":review-v2") for image in images
               if image.startswith("emergency/")), images
    assert "rabbitmq:3.13-management" in images
    calls = (stubs / "calls.log").read_text()
    assert "minikube -p minikube image load --overwrite=true emergency/coordinator:review-v2" in calls


def test_failed_storage_rollout_fails_the_script(stubs):
    result = run(stubs, FAIL_STATEFULSET="1")
    assert result.returncode != 0
    assert "Done." not in result.stdout
    assert "exceeded its progress deadline" in result.stderr
    assert "deployment failed" in result.stderr


@pytest.mark.parametrize("tag", ["bad tag", "x;rm -rf /", "-leading-dash", "a" * 129])
def test_unsafe_tags_are_refused_before_anything_runs(stubs, tag):
    result = run(stubs, TAG=tag)
    assert result.returncode == 2
    assert "TAG must be a valid image tag" in result.stderr
    assert not (stubs / "calls.log").exists()


def test_empty_tag_means_the_default(stubs):
    result = run(stubs, TAG="")
    assert result.returncode == 0, result.stderr
    assert "emergency/coordinator:dev" in applied_images(stubs)
