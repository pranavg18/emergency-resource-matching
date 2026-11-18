"""A sync pass that outlives its caller must not change the node's data.

When the coordinator's call to POST /internal/sync/run times out, the node's
handler keeps running. If a later sync then completes and the node rejoins
the in-sync set, the old handler must not overwrite newer, acknowledged rows.
Every sync pass carries an epoch; a newer epoch fences off older passes.

The source here is a tiny fake node, so a pass can be stalled at a chosen
moment: it serves its digest at once but holds the row fetch until released.
"""

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
import requests

KEY = "request:11111111-1111-4111-8111-111111111111"
TS = "2026-11-18T09:00:00.000Z"


class FakeSource:
    """Serves one row as a storage node would; can hold row fetches until released."""

    def __init__(self, value: str, version: int, stall: bool):
        self.row = {"key": KEY, "value": value, "version": version, "deleted": False, "updated_at": TS}
        self.release = threading.Event()
        self.fetch_started = threading.Event()
        if not stall:
            self.release.set()
        source = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, body):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = urlsplit(self.path).path
                if path == "/internal/sync/digest":
                    digest = hashlib.sha256(source.row["value"].encode()).hexdigest()
                    self._json({"node_id": "fake", "entries": {KEY: [source.row["version"], digest]}})
                else:
                    source.fetch_started.set()
                    source.release.wait(timeout=30)
                    self._json(source.row)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.release.set()
        self.server.shutdown()


@pytest.fixture
def sources():
    made = []

    def make(value, version, stall=False):
        source = FakeSource(value, version, stall)
        made.append(source)
        return source

    yield make
    for source in made:
        source.close()


def sync_run(node, source, *, final, epoch):
    return requests.post(
        f"{node.url}/internal/sync/run",
        json={"source_url": source.url, "final": final, "epoch": epoch},
        timeout=30,
    )


def row(node):
    return requests.get(f"{node.url}/internal/data/{KEY}", params={"include_deleted": "true"}, timeout=2).json()


def test_a_superseded_final_pass_cannot_roll_back_newer_data(start_storage_node, sources):
    node = start_storage_node(node_id="s1-b", shard=1)
    old = sources('{"v":1}', 1, stall=True)

    # 1. Final pass, epoch 1: it reads the source's digest, then stalls on the row fetch
    #    (as if the coordinator's call had timed out but the handler kept going).
    results = {}
    first = threading.Thread(target=lambda: results.update(first=sync_run(node, old, final=True, epoch=1)))
    first.start()
    assert old.fetch_started.wait(timeout=10)

    # 2. A later attempt (epoch 2) completes, the node rejoins, and a newer write
    #    (version 3) is acknowledged on it.
    fresh = sources('{"v":1}', 1)
    assert sync_run(node, fresh, final=True, epoch=2).status_code == 200
    requests.put(
        f"{node.url}/internal/replica/{KEY}",
        json={"value": '{"v":3}', "version": 3, "deleted": False, "updated_at": TS},
        timeout=2,
    ).raise_for_status()

    # 3. The stale pass resumes. It must not touch the store.
    old.release.set()
    first.join(timeout=30)
    assert row(node)["version"] == 3
    assert row(node)["value"] == '{"v":3}'
    assert results["first"].status_code == 409
    assert results["first"].json()["error"]["code"] == "SYNC_SUPERSEDED"


def test_an_older_epoch_is_refused_up_front(start_storage_node, sources):
    node = start_storage_node(node_id="s1-b", shard=1)
    source = sources('{"v":1}', 1)
    assert sync_run(node, source, final=False, epoch=10).status_code == 200
    stale = sync_run(node, source, final=True, epoch=9)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "SYNC_SUPERSEDED"


@pytest.mark.parametrize("epoch", [None, -1, True, "7"])
def test_epoch_is_required(start_storage_node, sources, epoch):
    node = start_storage_node(node_id="s1-b", shard=1)
    source = sources('{"v":1}', 1)
    body = {"source_url": source.url, "final": False}
    if epoch is not None:
        body["epoch"] = epoch
    assert requests.post(f"{node.url}/internal/sync/run", json=body, timeout=10).status_code == 400
