import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests


def main():
    p = argparse.ArgumentParser(description="Concurrent burst or steady-rate load")
    p.add_argument("--url", default="http://localhost:8088")
    p.add_argument("--count", type=int, default=50)
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--rate", type=float, default=0, help="requests/sec; 0 means burst")
    p.add_argument("--output", default="artifacts/load.json")
    args = p.parse_args()

    def submit(i):
        start = time.monotonic()
        r = requests.post(args.url + "/requests", json={"type": "water", "details": f"load-{i}",
                          "location": {"lat": 28.61, "lon": 77.20}}, timeout=60)
        r.raise_for_status()
        return {"id": r.json()["id"], "seconds": time.monotonic() - start}

    results, failures = [], []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = []
        start = time.monotonic()
        for i in range(args.count):
            if args.rate > 0:
                time.sleep(max(0, start + i / args.rate - time.monotonic()))
            futures.append(pool.submit(submit, i))
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                failures.append(str(exc))
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"requests": results, "failures": failures}, indent=2))
    print(f"Created {len(results)} requests; {len(failures)} failures. Results: {path}")
    raise SystemExit(bool(failures))


if __name__ == "__main__":
    main()
