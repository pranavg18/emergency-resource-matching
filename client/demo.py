"""Repeatable application checks against a running demo stack.

Creates isolated test records, retains JSON evidence, and restores matcher
settings. Recovery/scaling deliberately restart or resize matching-service.
"""
import argparse
import json
import random
import re
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import requests

from common.core import Unavailable
from common.storage import StorageClient
from services.matching_service.engine import distance


class Demo:
    def __init__(self, args):
        self.args = args
        self.storage = StorageClient(args.coordinator)
        self.results = []

    def http(self, method, path, payload=None):
        response = requests.request(method, self.args.url.rstrip("/") + path,
                                    json=payload, timeout=60)
        response.raise_for_status()
        return response.json()

    def kube(self, *args, check=True):
        result = subprocess.run(
            ["kubectl", "--context", self.args.context, "-n", "emergency", *args],
            capture_output=True, text=True, timeout=240)
        if check and result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return result

    @staticmethod
    def until(predicate, timeout=180):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                result = predicate()
                if result:
                    return result
            except (Unavailable, requests.RequestException):
                pass
            time.sleep(1)
        raise AssertionError("condition did not settle before the demo timeout")

    def configure(self, replicas, delay):
        # Finish the previous scenario at its current delay before changing
        # worker speed. Empty claims do not imply release messages are drained.
        self.until(lambda: all(q["messages_ready"] == 0 and q["messages_unacknowledged"] == 0
                               for q in self.queues() if q["name"] != "match.dead"), self.args.timeout)
        self.kube("set", "env", "deployment/matching-service", f"MATCH_DELAY_MS={delay}")
        self.kube("scale", "deployment/matching-service", f"--replicas={replicas}")
        self.kube("rollout", "status", "deployment/matching-service", "--timeout=180s")

        def all_replaced():
            pods = json.loads(self.kube("get", "pods", "-l", "app=matching-service", "-o", "json").stdout)["items"]
            return len(pods) == replicas and all(
                not pod["metadata"].get("deletionTimestamp")
                and pod["status"].get("containerStatuses")
                and all(c["ready"] for c in pod["status"].get("containerStatuses", []))
                for pod in pods)

        self.until(all_replaced, 120)

    @contextmanager
    def matcher_settings(self):
        spec = json.loads(self.kube("get", "deployment/matching-service", "-o", "json").stdout)["spec"]
        env = spec["template"]["spec"]["containers"][0].get("env", [])
        original = next((item for item in env if item["name"] == "MATCH_DELAY_MS"), None)
        if original and "value" not in original:
            raise RuntimeError("demo will not replace a MATCH_DELAY_MS valueFrom setting")
        try:
            yield
        finally:
            value = f"MATCH_DELAY_MS={original['value']}" if original else "MATCH_DELAY_MS-"
            self.kube("set", "env", "deployment/matching-service", value)
            self.kube("scale", "deployment/matching-service", f"--replicas={spec['replicas']}")
            self.kube("rollout", "status", "deployment/matching-service", "--timeout=180s")

    def records(self, ids):
        # Only this run's requests matter; skip any other record in the shared
        # keyspace, including ones that do not follow the request schema.
        return {r["value"]["id"]: r["value"] for r in self.storage.scan("request:")
                if r["value"].get("id") in ids}

    def settled(self, ids):
        records = self.records(ids)
        return records if len(records) == len(ids) and all(
            r["status"] in {"MATCHED", "UNMATCHED"} for r in records.values()) else None

    def batch(self, name, count, capacity, interrupt=None):
        # Keep each run outside every existing resource's matching radius.
        existing = self.http("GET", "/resources")["resources"]
        location = None
        for _ in range(1000):
            candidate = {"lat": random.uniform(-60, 60), "lon": random.uniform(-170, 170)}
            if all(distance(candidate, r["location"]) > 200 for r in existing):
                location = candidate
                break
        if location is None:
            raise AssertionError("could not find an isolated demo location")
        run_id = str(uuid.uuid4())
        location["label"] = "demo-" + run_id
        resource = self.http("POST", "/resources", {
            "type": "water", "location": location, "availability": capacity})
        ids, latencies, failures = set(), [], []
        evidence = {"scenario": name, "run_id": run_id, "resource_id": resource["id"],
                    "count": count, "capacity": capacity, "passed": False}
        self.results.append(evidence)
        print(f"{name}: creating {count} requests against {capacity} units", flush=True)

        def create(i):
            start = time.monotonic()
            value = self.http("POST", "/requests", {"type": "water", "location": location,
                              "details": f"demo {run_id} request {i}"})
            return value["id"], time.monotonic() - start

        try:
            start = time.monotonic()
            with ThreadPoolExecutor(max_workers=self.args.workers) as pool:
                futures = [pool.submit(create, i) for i in range(count)]
                for future in futures:
                    try:
                        request_id, latency = future.result()
                        ids.add(request_id)
                        latencies.append(latency)
                    except Exception as exc:
                        failures.append(str(exc))
            evidence["request_ids"] = sorted(ids)
            evidence["creation_failures"] = failures
            assert not failures, failures
            if interrupt:
                evidence["recovery"] = interrupt()
            values = self.until(lambda: self.settled(ids), self.args.timeout)
            elapsed = time.monotonic() - start
            matched = [v for v in values.values() if v["status"] == "MATCHED"]
            stored = self.storage.get("resource:" + resource["id"])["value"]
            expected = min(count, capacity)
            assert len(matched) == expected, f"matched {len(matched)}, expected {expected}"
            assert all(v["assigned_resource_id"] == resource["id"] for v in matched)
            assert set(stored["assigned_request_ids"]) == {v["id"] for v in matched}
            assert stored["availability"] == capacity - expected
            check = subprocess.run([sys.executable, "-m", "client.check", "--coordinator",
                                    self.args.coordinator], capture_output=True, text=True, check=True)
            evidence.update(passed=True, matched=len(matched), unmatched=count - len(matched),
                            seconds=round(elapsed, 3), matches_per_second=round(expected / elapsed, 3),
                            create_p95_seconds=round(sorted(latencies)[int((len(latencies)-1)*.95)], 3),
                            consistency=check.stdout.strip())
            print(f"{name}: {len(matched)} matched; consistency passed ({elapsed:.2f}s)", flush=True)
            return evidence
        finally:
            # Never erase other records. Retire this run's resource and close only
            # requests whose successful POST responses belong to this run.
            self.http("PUT", f"/resources/{resource['id']}/status", {"status": "OFFLINE"})

            def cancel(request_id):
                for retry in range(5):
                    try:
                        return self.http("PUT", f"/requests/{request_id}/status", {"status": "CANCELLED"})
                    except requests.HTTPError as exc:
                        if exc.response.status_code != 503 or retry == 4:
                            raise
                        time.sleep(1)

            with ThreadPoolExecutor(max_workers=self.args.workers) as pool:
                list(pool.map(cancel, ids))
            self.until(lambda: not self.storage.get("resource:" + resource["id"])["value"]["assigned_request_ids"],
                       self.args.timeout)
            restored = self.storage.get("resource:" + resource["id"])["value"]
            assert restored["availability"] == capacity
            evidence["released"] = True

    def queues(self):
        result = self.kube("exec", "rabbitmq-0", "--", "rabbitmqctl", "list_queues",
                           "name", "messages_ready", "messages_unacknowledged", "--quiet", "--formatter=json")
        return json.loads(result.stdout[result.stdout.index("["):])

    def force_kill(self, name, attempts=2):
        """Stop the pod's container with no grace through the node's runtime.

        A namespace's PID 1 may ignore signals sent from inside it, so the stop
        goes through crictl on the Minikube node. The container ID is read
        fresh on every attempt: if the pod restarted after its status was last
        read, a stale ID makes crictl fail. A failure prints crictl's own error
        and is retried. Returns the restart count read just before the
        successful stop.
        """
        error = ""
        for attempt in range(1, attempts + 1):
            status = json.loads(self.kube("get", "pod", name, "-o", "json").stdout)["status"]
            container = status["containerStatuses"][0]
            container_id = container["containerID"].split("://")[-1]
            assert re.fullmatch(r"[0-9a-f]{64}", container_id), container_id
            result = subprocess.run(["minikube", "-p", self.args.context, "ssh", "--", "sudo",
                                     "crictl", "stop", "--timeout", "0", container_id],
                                    capture_output=True, text=True, timeout=60)
            if result.returncode == 0:
                return container["restartCount"]
            error = (result.stderr or result.stdout).strip() or f"exit status {result.returncode}"
            print(f"crictl stop failed (attempt {attempt} of {attempts}): {error}",
                  file=sys.stderr, flush=True)
            if attempt < attempts:
                time.sleep(2)
        raise RuntimeError(f"could not stop the {name} container: {error}")

    def interrupt(self, hard):
        self.until(lambda: any(q["name"] == "match.requests" and q["messages_unacknowledged"] > 0
                               for q in self.queues()), 60)
        pods = json.loads(self.kube("get", "pods", "-l", "app=matching-service", "-o", "json").stdout)["items"]
        active = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
        assert len(active) == 1, "recovery test expects exactly one matcher pod"
        pod = active[0]
        name = pod["metadata"]["name"]
        exit_code = None
        if hard:
            restarts = self.force_kill(name)

            def restarted():
                current = json.loads(self.kube("get", "pod", name, "-o", "json").stdout)
                state = current["status"].get("containerStatuses", [{}])[0]
                return state if state.get("restartCount", 0) > restarts and state.get("ready") else None

            state = self.until(restarted, 120)
            exit_code = state["lastState"]["terminated"]["exitCode"]
            assert exit_code == 137, f"expected a forced kill, got exit code {exit_code}"
        else:
            self.kube("delete", "pod", name, "--wait=false")
            self.kube("rollout", "status", "deployment/matching-service", "--timeout=180s")
        return {"mode": "SIGKILL" if hard else "SIGTERM", "pod": name,
                "exit_code": exit_code,
                "unacknowledged_delivery_observed": True}

    def run(self):
        if self.args.scenario in ("scaling", "all"):
            with self.matcher_settings():
                for replicas in (1, 4):
                    self.configure(replicas, 200)
                    result = self.batch(f"scale-{replicas}", 40, 40)
                    logs = self.kube("logs", "-l", "app=matching-service", "--all-pods=true",
                                     "--tail=-1", "--max-log-requests=8").stdout
                    result["claim_contention_retries"] = logs.count('"reason": "claim contention"')
                self.configure(4, 0)
                self.batch("concurrency", self.args.count, self.args.capacity)
        elif self.args.scenario == "concurrency":
            self.batch("concurrency", self.args.count, self.args.capacity)
        if self.args.scenario in ("recovery", "all"):
            with self.matcher_settings():
                self.configure(1, 10000)
                for hard in (False, True):
                    result = self.batch("hard-recovery" if hard else "graceful-recovery", 1, 1,
                                        interrupt=lambda: self.interrupt(hard))
                    logs = self.kube("logs", "-l", "app=matching-service", "--tail=-1").stdout
                    result["redelivery_observed"] = '"redelivered": true' in logs
                    assert result["redelivery_observed"], "replacement did not record redelivery"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8088")
    parser.add_argument("--coordinator", default="http://localhost:8008")
    parser.add_argument("--context", default="minikube")
    parser.add_argument("--scenario", choices=("concurrency", "scaling", "recovery", "all"), default="concurrency")
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--capacity", type=int, default=70)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--output", default="artifacts/demo.json")
    args = parser.parse_args()
    if min(args.count, args.capacity, args.workers, args.timeout) < 1:
        parser.error("count, capacity, workers and timeout must be positive")
    demo = Demo(args)
    report = {"scenario": args.scenario, "results": demo.results, "passed": False}
    try:
        demo.run()
        report["passed"] = True
    except Exception as exc:
        report["error"] = str(exc)
        raise
    finally:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Evidence: {path}", flush=True)


if __name__ == "__main__":
    main()
