"""Background thread that registers this node with the coordinator and heartbeats.

* Registration is retried with backoff until the coordinator answers, so the
  node can start before the coordinator does.
* The heartbeat interval comes from the registration response, so it is set
  in one place (the coordinator).
* A heartbeat answered with 404 means the coordinator restarted and forgot
  this node: register again.
* Any other failure is logged once and retried; the node keeps serving.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

import requests

from .config import NodeConfig

log = logging.getLogger(__name__)

CALL_TIMEOUT_S = 2.0
FIRST_BACKOFF_S = 0.5
MAX_BACKOFF_S = 5.0
DEFAULT_INTERVAL_S = 2.0


class HeartbeatClient:
    def __init__(self, config: NodeConfig, max_version: Callable[[], int]):
        self._config = config
        self._max_version = max_version
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="heartbeat", daemon=True)
        self._session = requests.Session()  # used only by this thread
        self._registered = False
        self._interval_s = DEFAULT_INTERVAL_S
        self._view: Optional[tuple[str, str]] = None  # (state, role) as the coordinator sees us
        self._failing = False

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=CALL_TIMEOUT_S + 1)

    def _run(self) -> None:
        backoff = FIRST_BACKOFF_S
        while not self._stop.is_set():
            if not self._registered:
                if self._register():
                    backoff = FIRST_BACKOFF_S
                else:
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, MAX_BACKOFF_S)
                continue
            if self._stop.wait(self._interval_s):
                break
            self._heartbeat()

    def _post(self, path: str, body: dict) -> requests.Response:
        return self._session.post(f"{self._config.coordinator_url}{path}", json=body, timeout=CALL_TIMEOUT_S)

    def _register(self) -> bool:
        body = {
            "node_id": self._config.node_id,
            "shard": self._config.shard,
            "url": self._config.node_url,
            "max_version": self._max_version(),
        }
        try:
            response = self._post("/internal/nodes/register", body)
            if response.status_code != 200:
                self._failed(f"registration rejected with {response.status_code}: {response.text[:200]}")
                return False
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            self._failed(f"cannot register with coordinator: {exc}")
            return False
        self._registered = True
        self._interval_s = data.get("heartbeat_interval_ms", DEFAULT_INTERVAL_S * 1000) / 1000
        self._recovered()
        self._update_view(data["state"], data["role"], "registered")
        return True

    def _heartbeat(self) -> None:
        try:
            response = self._post("/internal/nodes/heartbeat", {"node_id": self._config.node_id})
            if response.status_code == 404:
                log.warning("coordinator does not know this node (it restarted?); registering again")
                self._registered = False
                return
            if response.status_code != 200:
                self._failed(f"heartbeat rejected with {response.status_code}: {response.text[:200]}")
                return
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            self._failed(f"heartbeat failed: {exc}")
            return
        self._recovered()
        self._update_view(data["state"], data["role"], "now")

    def _update_view(self, state: str, role: str, verb: str) -> None:
        if (state, role) != self._view:
            log.info("%s as %s %s", verb, state, role)
            self._view = (state, role)

    def _failed(self, message: str) -> None:
        # Log the first failure in a run at WARNING, the rest at DEBUG.
        log.log(logging.DEBUG if self._failing else logging.WARNING, message)
        self._failing = True

    def _recovered(self) -> None:
        if self._failing:
            log.info("coordinator reachable again")
        self._failing = False
