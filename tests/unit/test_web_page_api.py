"""The map page's own api() helper, run under Node with a recording fetch.

client/web.py refuses writes that are not sent as JSON, so the page must send
every POST and PUT as JSON, even to endpoints that take no input. A button
once posted no body and got 415 (Match now); this test runs the real page code
so that regression cannot return unnoticed. Skipped when Node is not installed.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from client.web import STATIC_DIR, create_app

NODE = shutil.which("node")

# Loads api.js into a fresh context whose only global is a fetch that records
# each call, makes the calls listed in argv[2] (JSON), and prints what was sent.
HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const [file, callsJson] = process.argv.slice(1);
const sent = [];
const context = vm.createContext({
  fetch: async (url, init) => {
    sent.push({ url, method: init.method, headers: init.headers, body: init.body ?? null });
    return { ok: true, status: 200, json: async () => ({}) };
  },
});
vm.runInContext(fs.readFileSync(file, "utf8"), context, { filename: file });
(async () => {
  for (const [method, path, body] of JSON.parse(callsJson)) {
    await (body === undefined ? context.api(method, path) : context.api(method, path, body));
  }
  process.stdout.write(JSON.stringify(sent));
})().catch((err) => { console.error(err); process.exit(1); });
"""


def run_page_api(calls):
    result = subprocess.run([NODE, "-e", HARNESS, str(STATIC_DIR / "api.js"), json.dumps(calls)],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_every_write_is_sent_as_json_even_without_a_body():
    # Match now passes no body; the others pass one. Each must reach the proxy as JSON.
    calls = [
        ["POST", "match/r1"],
        ["POST", "requests", {"type": "water"}],
        ["PUT", "requests/r1/status", {"status": "COMPLETED"}],
        ["PUT", "resources/s1/status", {"availability": 0}],
    ]
    sent = run_page_api(calls)

    assert [(s["method"], s["url"]) for s in sent] == [
        ("POST", "/api/match/r1"), ("POST", "/api/requests"),
        ("PUT", "/api/requests/r1/status"), ("PUT", "/api/resources/s1/status"),
    ]
    for s, call in zip(sent, calls):
        assert s["headers"]["Content-Type"] == "application/json"
        assert json.loads(s["body"]) == (call[2] if len(call) > 2 else {})


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_reads_carry_no_body():
    (sent,) = run_page_api([["GET", "resources"]])
    assert sent["method"] == "GET" and sent["body"] is None and "Content-Type" not in sent["headers"]


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_what_the_page_sends_passes_the_proxy():
    # End to end at the boundary: replay the page's bodyless Match now through the real proxy guard.
    (sent,) = run_page_api([["POST", "match/r1"]])

    class Upstream:
        def request(self, method, url, **kwargs):
            reply = type("Reply", (), {})()
            reply.status_code, reply.content, reply.headers = 200, b"{}", {"Content-Type": "application/json"}
            return reply

    client = create_app("http://api.test", "http://coordinator.test", session=Upstream()).test_client()
    reply = client.post(sent["url"], data=sent["body"], headers=sent["headers"])
    assert reply.status_code == 200


def test_the_page_loads_api_js_before_app_js():
    page = Path(STATIC_DIR / "index.html").read_text()
    assert page.index('src="/static/api.js"') < page.index('src="/static/app.js"')
