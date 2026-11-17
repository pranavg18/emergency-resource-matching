"""Dictionary-facing storage interface over the coordinator's opaque JSON-text API.

The coordinator must route /data requests to Pranav's /internal/data endpoints
without changing record envelopes. Services never need to handle serialized values.
"""
import copy
import json
import threading
from urllib.parse import quote

import requests
from flask import g, has_request_context

from common.config import INTERNAL_HTTP_TIMEOUT_S, Settings
from common.core import APIError, Conflict, Unavailable, correlation


class StorageClient:
    def __init__(self, url=None):
        self.url = (url or Settings.from_env().coordinator_url).rstrip("/")

    def call(self, method, path, **kwargs):
        request_id = getattr(g, "correlation_id", correlation.get()) if has_request_context() else correlation.get()
        try:
            response = requests.request(
                method, self.url + path, timeout=INTERNAL_HTTP_TIMEOUT_S,
                headers={"X-Request-ID": request_id}, **kwargs,
            )
        except requests.RequestException as exc:
            raise Unavailable() from exc
        # Upstream crashes, proxy errors and malformed replies must not become 500s
        # in the public services or leak internal HTML/exception text to clients.
        if response.status_code >= 500 or 300 <= response.status_code < 400:
            raise Unavailable()
        if response.status_code == 409:
            raise Conflict()
        if response.status_code >= 400:
            if response.status_code == 404:
                raise APIError("not found", 404)
            raise APIError("storage rejected the request", response.status_code)
        try:
            value = response.json()
        except ValueError as exc:
            raise Unavailable("invalid storage response") from exc
        if not isinstance(value, dict):
            raise Unavailable("invalid storage response")
        return value

    @staticmethod
    def _record(record, *, key=None, submitted=None):
        if (not isinstance(record, dict) or not isinstance(record.get("key"), str)
                or (key is not None and record["key"] != key)
                or type(record.get("version")) is not int or record["version"] < 1
                or type(record.get("deleted", False)) is not bool):
            raise Unavailable("invalid storage record")
        result = dict(record)
        if submitted is not None and "value" not in record:
            # PUT replies contain metadata only. Use the exact submitted value;
            # a subsequent GET could already contain another writer's version.
            result["value"] = copy.deepcopy(submitted)
        else:
            try:
                raw = record["value"]
                if not isinstance(raw, str):
                    raise ValueError("expected JSON text")
                result["value"] = json.loads(raw)
                if not isinstance(result["value"], dict):
                    raise ValueError("expected a record object")
            except (KeyError, ValueError, TypeError) as exc:
                raise Unavailable("invalid storage record") from exc
        result.setdefault("deleted", False)
        return result

    def get(self, key):
        record = self._record(self.call("GET", "/data/" + quote(key, safe=":")), key=key)
        if record["deleted"]:
            raise APIError("not found", 404)
        return record

    def put(self, key, value, version):
        if not isinstance(value, dict) or type(version) is not int or version < 0:
            raise APIError("storage writes require an object and a nonnegative version")
        try:
            encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise APIError("storage value must be valid JSON") from exc
        reply = self.call("PUT", "/data/" + quote(key, safe=":"),
                          json={"value": encoded, "if_version": version})
        record = self._record(reply, key=key, submitted=value)
        if record["deleted"]:
            raise Unavailable("invalid storage write response")
        return record

    def scan(self, prefix, kind=None):
        reply = self.call("GET", "/data", params={"prefix": prefix})
        if not isinstance(reply.get("records"), list):
            raise Unavailable("invalid storage scan response")
        records = [self._record(record) for record in reply["records"]]
        if any(not record["key"].startswith(prefix) for record in records):
            raise Unavailable("invalid storage scan response")
        # Storage treats values as opaque text, so type filtering belongs here.
        return [record for record in records if not record["deleted"]
                and (kind is None or record["value"].get("type") == kind)]

    def ready(self):
        self.call("GET", "/readyz")
        return True


class FakeStorage:
    """Thread-safe CAS storage for service tests, with independent returned copies."""
    def __init__(self):
        self.records = {}
        self.lock = threading.RLock()

    def get(self, key):
        with self.lock:
            if key not in self.records:
                raise APIError("not found", 404)
            return copy.deepcopy(self.records[key])

    def put(self, key, value, version):
        with self.lock:
            old = self.records.get(key, {"version": 0})
            if old["version"] != version:
                raise Conflict()
            record = {"key": key, "value": copy.deepcopy(value), "version": version + 1,
                      "deleted": False}
            self.records[key] = record
            return copy.deepcopy(record)

    def scan(self, prefix, kind=None):
        with self.lock:
            return copy.deepcopy([record for key, record in self.records.items()
                                  if key.startswith(prefix)
                                  and (kind is None or record["value"].get("type") == kind)])

    def ready(self):
        return True
