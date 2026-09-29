"""
Smoke test of a deployed MEIO API (standard library only).

    python scripts/smoke_test.py https://meio-simopt-api.onrender.com --key <MEIO_API_KEY>
    python scripts/smoke_test.py http://127.0.0.1:8000                  (local, no key)

Checks, in this order:
    1. GET /health answers {"status": "ok"}
    2. with a key configured: a request without the key is refused (401)
    3. an invalid input is refused (422) and does not start a run
    4. POST /runs with examples/example_input.json starts a run
    5. GET /runs/{id} is polled until the run is completed (default limit 40 min)
    6. the summary has the expected sections and GET /runs/{id}/results.xlsx downloads
Exit code 0 = all checks passed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

EXAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples",
                       "example_input.json")
SUMMARY_SECTIONS = {"run", "decisions_to_commit", "service", "costs", "kpis", "policy", "weekly_means_test_seeds"}


def call(method: str, url: str, key: str | None, body: dict | None = None, timeout: int = 90):
    """(status code, raw body) of one request; HTTP errors are returned, not raised."""
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-API-Key"] = key
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def check(condition: bool, message: str) -> None:
    print(("ok    " if condition else "FAIL  ") + message)
    if not condition:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke test of a deployed MEIO API")
    parser.add_argument("base_url")
    parser.add_argument("--key", default=os.environ.get("MEIO_API_KEY"), help="API key (or env MEIO_API_KEY)")
    parser.add_argument("--preset", default="quick")
    parser.add_argument("--max-minutes", type=float, default=40)
    parser.add_argument("--poll-seconds", type=float, default=20)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    # a sleeping free instance needs up to about a minute to wake up
    status, body = call("GET", f"{base}/health", None, timeout=120)
    check(status == 200 and json.loads(body) == {"status": "ok"}, f"GET /health -> {status}")

    if args.key:
        status, _ = call("POST", f"{base}/runs?preset={args.preset}", None, {})
        check(status == 401, f"POST /runs without key is refused -> {status}")

    status, body = call("POST", f"{base}/runs?preset={args.preset}", args.key, {"horizon": 5})
    check(status == 422, f"invalid input is refused -> {status} {body[:80]!r}")

    with open(EXAMPLE, encoding="utf-8") as fh:
        example = json.load(fh)
    status, body = call("POST", f"{base}/runs?preset={args.preset}", args.key, example)
    check(status == 200, f"POST /runs (example input, preset {args.preset}) -> {status} {body[:120]!r}")
    run_id = json.loads(body)["run_id"]
    print(f"      run_id {run_id}")

    started, job = time.time(), {}
    while time.time() - started < args.max_minutes * 60:
        status, body = call("GET", f"{base}/runs/{run_id}", args.key)
        if status != 200:
            check(False, f"GET /runs/{run_id} -> {status} {body[:120]!r}")
        job = json.loads(body)
        if job["status"] in ("completed", "failed"):
            break
        print(f"      {job['status']} after {time.time() - started:.0f} s")
        time.sleep(args.poll_seconds)
    check(job.get("status") == "completed", f"run finished: {job.get('status')} {job.get('error') or ''} "
                                            f"({time.time() - started:.0f} s)")

    summary = job["summary"]
    check(SUMMARY_SECTIONS <= set(summary), "summary has all sections")
    print(f"      mean cost {summary['run'].get('mean_cost_over_horizon_test_seeds')}, "
          f"test cells passing {summary['run'].get('test_cells_passing')}")
    status, body = call("GET", f"{base}/runs/{run_id}/results.xlsx", args.key)
    check(status == 200 and body[:2] == b"PK", f"results.xlsx downloads ({len(body):,} bytes)")
    print("all checks passed")


if __name__ == "__main__":
    main()
