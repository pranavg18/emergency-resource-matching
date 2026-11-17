import math
from enum import Enum
from datetime import datetime, timezone

from common.config import Settings
from common.core import APIError, CLOSED, OPEN, Conflict, Unavailable, log, now


class ClaimResult(Enum):
    CLAIMED = "claimed"
    UNAVAILABLE = "unavailable"
    STALE = "stale"


def distance(a, b):
    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    dlat, dlon = lat2 - lat1, math.radians(b["lon"] - a["lon"])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371 * 2 * math.asin(min(1, math.sqrt(h)))


class Matcher:
    def __init__(self, storage, radius=None):
        self.storage = storage
        self.radius = float(radius if radius is not None else Settings.from_env().max_radius_km)

    def ranked(self, value, excluded):
        candidates = [r["value"] for r in self.storage.scan("resource:", value["type"])]
        return sorted((r for r in candidates if r["id"] not in excluded
                       and r["status"] == "AVAILABLE" and r["availability"] > 0
                       and distance(value["location"], r["location"]) <= self.radius),
                      key=lambda r: (distance(value["location"], r["location"]),
                                     -r["availability"], r["created_at"], r["id"]))

    def release(self, resource_id, request_id, attempt):
        for _ in range(5):
            record = self.storage.get("resource:" + resource_id)
            value = record["value"]
            if value["assigned_request_ids"].get(request_id) != attempt:
                return
            del value["assigned_request_ids"][request_id]
            value["availability"] += 1
            if value["status"] != "OFFLINE":
                value["status"] = "AVAILABLE"
            value["updated_at"] = now()
            try:
                self.storage.put(record["key"], value, record["version"])
                return
            except Conflict:
                continue
        raise Unavailable("release contention")

    def claim(self, resource_id, request_id, attempt):
        for _ in range(5):
            record = self.storage.get("resource:" + resource_id)
            value = record["value"]
            held = value["assigned_request_ids"].get(request_id)
            if held == attempt:
                return ClaimResult.CLAIMED
            if held is not None and held > attempt:
                return ClaimResult.STALE
            if held is None:
                if value["status"] != "AVAILABLE" or value["availability"] <= 0:
                    return ClaimResult.UNAVAILABLE
                value["availability"] -= 1
            value["assigned_request_ids"][request_id] = attempt
            if value["status"] != "OFFLINE":
                value["status"] = "AVAILABLE" if value["availability"] else "BUSY"
            value["updated_at"] = now()
            try:
                self.storage.put(record["key"], value, record["version"])
                return ClaimResult.CLAIMED
            except Conflict:
                continue
        raise Unavailable("claim contention")

    def match(self, request_id):
        excluded = set()
        key = "request:" + request_id
        for _ in range(5):
            record = self.storage.get(key)
            value = record["value"]
            if value["status"] in CLOSED:
                raise Conflict("request is closed", "REQUEST_CLOSED")
            if value["status"] not in OPEN:
                return self.result(value)
            candidate = value["candidate_resource_id"]
            if not candidate or candidate in excluded:
                ranked = self.ranked(value, excluded)
                if not ranked:
                    return {"matched": False, "request": value, "resource": None}
                candidate = ranked[0]["id"]
                value.update(candidate_resource_id=candidate,
                             match_attempt=value["match_attempt"] + 1, updated_at=now())
                try:
                    record = self.storage.put(key, value, record["version"])
                except Conflict:
                    continue
            attempt = value["match_attempt"]
            outcome = self.claim(candidate, request_id, attempt)
            if outcome is ClaimResult.STALE:
                continue
            if outcome is ClaimResult.UNAVAILABLE:
                excluded.add(candidate)
                continue
            # A paused worker can only finalize the exact candidate AND attempt it owns.
            for _ in range(5):
                fresh = self.storage.get(key)
                current = fresh["value"]
                if current["status"] not in OPEN:
                    if current["status"] not in CLOSED and current["assigned_resource_id"] == candidate:
                        return self.result(current)
                    self.release(candidate, request_id, attempt)
                    break
                if current["candidate_resource_id"] != candidate:
                    self.release(candidate, request_id, attempt)
                    break
                if current["match_attempt"] != attempt:
                    break
                current.update(status="MATCHED", assigned_resource_id=candidate, updated_at=now())
                try:
                    self.storage.put(key, current, fresh["version"])
                    return self.result(current)
                except Conflict:
                    continue
            else:
                raise Unavailable("matching contention; retry")
        raise Unavailable("matching contention; retry")

    def result(self, value):
        return {"matched": True, "request": value,
                "resource": self.storage.get("resource:" + value["assigned_resource_id"])["value"]}

    def mark_unmatched(self, request_id):
        for _ in range(5):
            record = self.storage.get("request:" + request_id)
            value = record["value"]
            if value["status"] != "PENDING":
                return
            value.update(status="UNMATCHED", updated_at=now())
            try:
                self.storage.put(record["key"], value, record["version"])
                return
            except Conflict:
                continue
        raise Unavailable("request contention")

    def sweep(self, publisher, age=30):
        for record in self.storage.scan("request:"):
            value = record["value"]
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(value["created_at"])).total_seconds()
            if value["status"] == "PENDING" and not value["queued"] and elapsed >= age:
                try:
                    publisher("request.created", {"request_id": value["id"], "attempt": 0,
                                                  "correlation_id": "sweep-" + value["id"]})
                except Exception as exc:
                    # Broker failure must not prevent the independent claim-repair pass.
                    log("sweep_publish_failed", request_id=value["id"], error=type(exc).__name__)
                    continue
                value["queued"] = True
                try:
                    self.storage.put(record["key"], value, record["version"])
                except Conflict:
                    pass
        for record in self.storage.scan("resource:"):
            resource = record["value"]
            for request_id, attempt in resource["assigned_request_ids"].items():
                try:
                    value = self.storage.get("request:" + request_id)["value"]
                except APIError as exc:
                    if exc.status != 404:
                        raise
                    continue  # Missing requests are not proof of a safe release.
                named = value["assigned_resource_id"] or value["candidate_resource_id"]
                if value["status"] in CLOSED or named != resource["id"]:
                    self.release(resource["id"], request_id, attempt)
