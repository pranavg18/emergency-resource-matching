"""scripts/demo-storage-failure.sh refuses bad input before touching the cluster.

kubectl and curl are replaced by stubs that record any call, so the test never
reaches a real cluster and can check that nothing ran.
"""

import os
import shutil
import signal
import subprocess
import sys

import pytest

from tests.integration.conftest import REPO_ROOT

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash"
)


@pytest.fixture
def stub_path(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("kubectl", "curl"):
        stub = bin_dir / name
        stub.write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> "{tmp_path}/calls.log"\nexit 1\n')
        stub.chmod(0o755)
    return tmp_path


def run(stub_path, shard):
    """Run the script; on timeout kill its whole process group, children included."""
    env = {**os.environ, "PATH": f"{stub_path / 'bin'}:{os.environ['PATH']}", "SHARD": shard}
    proc = subprocess.Popen(
        ["bash", "scripts/demo-storage-failure.sh"], cwd=REPO_ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        pytest.fail("the script did not exit within 10 s (endless key search?)")
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


@pytest.mark.parametrize("shard", ["3", "-1", "one", "1 ", "01"])
def test_a_shard_that_does_not_exist_is_refused_at_once(stub_path, shard):
    result = run(stub_path, shard)
    assert result.returncode == 2
    assert "SHARD must be 0, 1 or 2" in result.stderr
    assert not (stub_path / "calls.log").exists()
