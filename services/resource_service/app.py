from common.config import Settings
from flask import request

from common.core import APIError, Conflict, Unavailable, app_base, body, integer, new_record, now
from common.storage import StorageClient


def create_app(storage=None):
    storage = storage or StorageClient()
    app = app_base("resource-service", storage.ready)
    app.config["SETTINGS"] = Settings.from_env()

    @app.post("/resources")
    def create():
        value = new_record(body(), resource=True)
        storage.put("resource:" + value["id"], value, 0)
        return value, 201

    @app.get("/resources")
    def resources():
        values = [r["value"] for r in storage.scan("resource:", request.args.get("type"))]
        status = request.args.get("status")
        return {"resources": [v for v in values if not status or v["status"] == status]}

    @app.put("/resources/<resource_id>/status")
    def status(resource_id):
        payload = body()
        if not {"availability", "status"} & payload.keys():
            raise APIError("provide status and/or availability")
        if "availability" in payload:
            integer(payload["availability"], "availability")
        if "status" in payload and (not isinstance(payload["status"], str)
                                    or payload["status"] not in {"AVAILABLE", "BUSY", "OFFLINE"}):
            raise APIError("invalid resource status")
        for _ in range(30):
            record = storage.get("resource:" + resource_id)
            value = record["value"]
            value.update({k: payload[k] for k in ("availability", "status") if k in payload})
            if payload.get("status") == "AVAILABLE" and value["availability"] == 0:
                raise Conflict("AVAILABLE requires positive availability")
            if payload.get("status") == "BUSY" and value["availability"] > 0:
                raise Conflict("BUSY requires zero availability")
            if value["status"] != "OFFLINE":
                value["status"] = "AVAILABLE" if value["availability"] else "BUSY"
            value["updated_at"] = now()
            try:
                storage.put(record["key"], value, record["version"])
                return value
            except Conflict:
                continue
        raise Unavailable("resource is busy; retry")

    return app
