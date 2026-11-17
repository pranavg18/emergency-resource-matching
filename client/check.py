"""Run after the load drains. Assert reciprocal assignments and capacity invariants."""
import argparse

from common.storage import StorageClient


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--coordinator", default="http://localhost:8008")
    args = p.parse_args()
    storage = StorageClient(args.coordinator)
    records = {record["key"]: record["value"]
               for prefix in ("request:", "resource:") for record in storage.scan(prefix)}
    claims = {}
    for key, value in records.items():
        if key.startswith("resource:"):
            assert value["availability"] >= 0, key
            for request_id, attempt in value["assigned_request_ids"].items():
                assert request_id not in claims, f"duplicate claim: {request_id}"
                claims[request_id] = value["id"]
                request = records["request:" + request_id]
                assert request["status"] not in {"COMPLETED", "CANCELLED"}, request_id
                assert (request["assigned_resource_id"] or request["candidate_resource_id"]) == value["id"]
                assert request["match_attempt"] == attempt, request_id
    for key, value in records.items():
        if key.startswith("request:") and value["status"] in {"MATCHED", "IN_PROGRESS"}:
            assert claims.get(value["id"]) == value["assigned_resource_id"], key
    print(f"Validated {len(records)} records and {len(claims)} unique claims.")


if __name__ == "__main__":
    main()
