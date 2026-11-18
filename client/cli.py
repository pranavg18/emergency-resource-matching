import argparse
import json
import os

import requests


def main():
    parser = argparse.ArgumentParser(description="Emergency resource matching client")
    parser.add_argument("--url", default=os.getenv("API_URL", "http://localhost:8088"))
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("create-request", "register-resource"):
        p = sub.add_parser(command)
        p.add_argument("--type", required=True)
        p.add_argument("--lat", type=float, required=True)
        p.add_argument("--lon", type=float, required=True)
        p.add_argument("--label", default="")
        if command == "create-request":
            p.add_argument("--details", required=True)
        else:
            p.add_argument("--availability", type=int, default=1)
    for command in ("get-request", "match", "request-status", "resource-status"):
        p = sub.add_parser(command)
        p.add_argument("id")
        if command.endswith("status"):
            p.add_argument("--status")
        if command == "resource-status":
            p.add_argument("--availability", type=int)
    p = sub.add_parser("list-resources")
    p.add_argument("--type")
    p.add_argument("--status")
    args = parser.parse_args()
    data, params = {}, {}
    command = args.command
    if command in {"create-request", "register-resource"}:
        method, path = "POST", "/requests" if command == "create-request" else "/resources"
        data = {"type": args.type, "location": {"lat": args.lat, "lon": args.lon, "label": args.label}}
        data.update({"details": args.details} if command == "create-request" else {"availability": args.availability})
    elif command == "list-resources":
        method, path = "GET", "/resources"
        params = {"type": args.type, "status": args.status}
    elif command == "get-request":
        method, path = "GET", "/requests/" + args.id
    elif command == "match":
        method, path = "POST", "/match/" + args.id
    else:
        method = "PUT"
        path = ("/requests/" if command == "request-status" else "/resources/") + args.id + "/status"
        data = {k: getattr(args, k) for k in ("status", "availability")
                if getattr(args, k, None) is not None}
    try:
        response = requests.request(method, args.url.rstrip("/") + path, json=data, params=params, timeout=60)
        try:
            print(json.dumps(response.json(), indent=2))
        except ValueError:
            print(f"HTTP {response.status_code}: {response.text[:200]}")
        raise SystemExit(0 if response.ok else 1)
    except requests.RequestException as exc:
        parser.exit(1, f"API unavailable: {exc}\n")


if __name__ == "__main__":
    main()
