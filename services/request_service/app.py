from common.config import Settings
from flask import g

from common.core import APIError, Conflict, Unavailable, app_base, body, log, new_record, now, transition
from common.messaging import publish
from common.storage import StorageClient


def create_app(storage=None, publisher=None):
    storage = storage or StorageClient()
    publisher = publisher or publish
    app = app_base("request-service", storage.ready)
    app.config["SETTINGS"] = Settings.from_env()

    @app.post("/requests")
    def create():
        value = new_record(body())
        key = "request:" + value["id"]
        record = storage.put(key, value, 0)
        try:
            publisher("request.created", {"request_id": value["id"], "attempt": 0,
                                          "correlation_id": g.correlation_id})
        except Exception as exc:
            log("publish_failed", request_id=value["id"], error=type(exc).__name__)
        else:
            value["queued"] = True
            try:
                storage.put(key, value, record["version"])
            except APIError:
                # The durable record still exists; a failed flag update is swept later.
                pass
        return value, 201

    @app.get("/requests/<request_id>")
    def get(request_id):
        return storage.get("request:" + request_id)["value"]

    @app.put("/requests/<request_id>/status")
    def status(request_id):
        target = body().get("status")
        for _ in range(30):
            record = storage.get("request:" + request_id)
            value = record["value"]
            transition(value["status"], target, client=True)
            value.update(status=target, updated_at=now())
            try:
                storage.put(record["key"], value, record["version"])
            except Conflict:
                continue
            if target in {"COMPLETED", "CANCELLED"}:
                resource_id = value["assigned_resource_id"] or value["candidate_resource_id"]
                if resource_id:
                    try:
                        publisher("request.closed", {"request_id": request_id,
                                  "resource_id": resource_id, "attempt": value["match_attempt"],
                                  "correlation_id": g.correlation_id})
                    except Exception as exc:
                        log("release_publish_failed", request_id=request_id,
                            error=type(exc).__name__)
            return value
        raise Unavailable("request is busy; retry")

    return app
