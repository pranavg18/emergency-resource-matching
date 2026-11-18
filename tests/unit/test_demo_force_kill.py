"""client.demo's hard kill: fresh container ID per attempt, visible errors, one retry."""

import json
import subprocess
from types import SimpleNamespace

import pytest

import client.demo as demo_module
from client.demo import Demo

OLD_ID, NEW_ID = "a" * 64, "b" * 64


def pod_status(container_id, restarts):
    return json.dumps({"status": {"containerStatuses": [
        {"containerID": f"containerd://{container_id}", "restartCount": restarts}]}})


@pytest.fixture
def demo(monkeypatch):
    demo = Demo(SimpleNamespace(coordinator="http://coordinator.invalid", context="minikube"))
    monkeypatch.setattr(demo_module.time, "sleep", lambda seconds: None)
    return demo


def fake_cluster(monkeypatch, demo, statuses, stop_results):
    """statuses: what `kubectl get pod` returns on each call; stop_results: crictl outcomes."""
    stopped = []
    statuses, stop_results = iter(statuses), iter(stop_results)
    monkeypatch.setattr(demo, "kube", lambda *args, **kw: SimpleNamespace(stdout=next(statuses)))

    def run(command, **kwargs):
        stopped.append(command[-1])
        code, stderr = next(stop_results)
        return subprocess.CompletedProcess(command, code, "", stderr)

    monkeypatch.setattr(demo_module.subprocess, "run", run)
    return stopped


def test_a_stale_container_id_is_reread_and_retried(monkeypatch, demo, capsys):
    stopped = fake_cluster(
        monkeypatch, demo,
        statuses=[pod_status(OLD_ID, 0), pod_status(NEW_ID, 1)],
        stop_results=[(1, "rpc error: code = NotFound desc = container not found"), (0, "")],
    )
    assert demo.force_kill("matching-service-x") == 1  # restart count read just before the stop
    assert stopped == [OLD_ID, NEW_ID]
    err = capsys.readouterr().err
    assert "attempt 1 of 2" in err and "container not found" in err


def test_a_stop_that_keeps_failing_raises_with_crictl_error(monkeypatch, demo):
    fake_cluster(
        monkeypatch, demo,
        statuses=[pod_status(OLD_ID, 0), pod_status(OLD_ID, 0)],
        stop_results=[(1, "permission denied"), (1, "permission denied")],
    )
    with pytest.raises(RuntimeError, match="permission denied"):
        demo.force_kill("matching-service-x")


def test_the_first_successful_stop_returns_immediately(monkeypatch, demo):
    stopped = fake_cluster(monkeypatch, demo, statuses=[pod_status(OLD_ID, 3)], stop_results=[(0, "")])
    assert demo.force_kill("matching-service-x") == 3
    assert stopped == [OLD_ID]
