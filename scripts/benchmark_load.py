#!/usr/bin/env python3
"""Load benchmark for DART's ingestion API (`POST /api/v1/events`).

Sweeps a set of concurrency levels against the live docker-compose stack
(`dart-api` on localhost:8000), maintaining exactly `concurrency`
in-flight requests throughout each level via a worker-pool pattern (not
a single burst wave), and reports req/s plus p50/p95/p99 latency per
level.

Every request carries a fresh `idempotency_key` (uuid4) so the
idempotency fast-path is never exercised -- this measures real
ingestion throughput (idempotency claim + job persist + stream enqueue),
not a cache hit.

`target_url` in the benchmark payload points at the mock receiver via
its internal Docker network hostname (`mock-receiver`, not
`localhost`), since dart-worker -- not this script -- is what eventually
dispatches to it, and dart-worker resolves hostnames from inside the
compose network.

Usage:
    docker compose up -d --build
    python scripts/benchmark_load.py
    python scripts/benchmark_load.py --concurrency-levels 10 50 100 200 --requests-per-level 1000
"""

from __future__ import annotations

import argparse
import asyncio
import math
import statistics
import sys
import time
import uuid
from dataclasses import dataclass

import httpx

API_HEALTH_URL = "http://localhost:8000/healthz"
API_EVENTS_URL = "http://localhost:8000/api/v1/events"

# Internal Docker network hostname -- this is what dart-worker (running
# inside docker-compose, not on the host) resolves when it later
# dispatches the tasks this benchmark ingests. "localhost" here would be
# correct for this script but unreachable from the worker container.
DISPATCH_TARGET_URL = "http://mock-receiver:8000/webhook?behavior=success"
SIGNING_SECRET_ID = "benchmark_secret"

DEFAULT_CONCURRENCY_LEVELS = [10, 50, 100]
DEFAULT_REQUESTS_PER_LEVEL = 500
WARMUP_REQUESTS = 10
REQUEST_TIMEOUT_SECONDS = 10.0

# DART's own Redis connection pool ceiling (RedisSettings.max_connections
# default). Printed as a reference point alongside results, not enforced
# by this script -- the point of the sweep is to see whether/where
# throughput plateaus or errors appear around this line.
DART_REDIS_POOL_SIZE = 50


@dataclass(frozen=True)
class RequestResult:
    latency_ms: float
    status_code: int | None
    success: bool
    error: str | None = None


@dataclass(frozen=True)
class LevelSummary:
    concurrency: int
    total_requests: int
    successful: int
    failed: int
    wall_seconds: float
    req_per_sec: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    min_ms: float
    max_ms: float
    mean_ms: float


def _build_request_body() -> dict:
    return {
        "event_type": "benchmark.load_test",
        "target_url": DISPATCH_TARGET_URL,
        "payload": {"probe": "benchmark"},
        "idempotency_key": f"bench-{uuid.uuid4()}",
        "signing_secret_id": SIGNING_SECRET_ID,
    }


async def _send_one(client: httpx.AsyncClient) -> RequestResult:
    body = _build_request_body()
    start = time.perf_counter()
    try:
        response = await client.post(
            API_EVENTS_URL, json=body, timeout=REQUEST_TIMEOUT_SECONDS
        )
        latency_ms = (time.perf_counter() - start) * 1000
        success = response.status_code in (200, 202)
        return RequestResult(
            latency_ms=latency_ms,
            status_code=response.status_code,
            success=success,
            error=None if success else f"HTTP {response.status_code}",
        )
    except httpx.HTTPError as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        return RequestResult(
            latency_ms=latency_ms,
            status_code=None,
            success=False,
            error=str(exc),
        )


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (matches numpy's default method).
    `sorted_values` must already be sorted ascending."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]

    k = (len(sorted_values) - 1) * (pct / 100)
    lower = math.floor(k)
    upper = math.ceil(k)
    if lower == upper:
        return sorted_values[int(k)]
    fraction = k - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction


async def _run_warmup(client: httpx.AsyncClient, count: int) -> None:
    """Throwaway requests to absorb first-connection overhead before
    measurement starts. Results are discarded, not added to stats."""
    tasks = [asyncio.create_task(_send_one(client)) for _ in range(count)]
    await asyncio.gather(*tasks, return_exceptions=True)


async def _run_concurrency_level(concurrency: int, total_requests: int) -> LevelSummary:
    """Maintains exactly `concurrency` in-flight requests for the
    duration of the run via a worker-pool draining a shared queue --
    steady-state load, not a single burst of `concurrency` requests."""
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=200)
    async with httpx.AsyncClient(limits=limits) as client:
        await _run_warmup(client, WARMUP_REQUESTS)

        queue: asyncio.Queue[int] = asyncio.Queue()
        for i in range(total_requests):
            queue.put_nowait(i)

        results: list[RequestResult] = []

        async def worker() -> None:
            while True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                # Single-threaded asyncio: list.append() has no `await`
                # inside it, so this is safe without an explicit lock.
                results.append(await _send_one(client))

        wall_start = time.perf_counter()
        workers = [asyncio.create_task(worker()) for _ in range(concurrency)]
        await asyncio.gather(*workers)
        wall_seconds = time.perf_counter() - wall_start

    latencies_ms = sorted(r.latency_ms for r in results)
    successful = sum(1 for r in results if r.success)
    failed = len(results) - successful

    return LevelSummary(
        concurrency=concurrency,
        total_requests=len(results),
        successful=successful,
        failed=failed,
        wall_seconds=wall_seconds,
        req_per_sec=(len(results) / wall_seconds) if wall_seconds > 0 else 0.0,
        p50_ms=_percentile(latencies_ms, 50),
        p95_ms=_percentile(latencies_ms, 95),
        p99_ms=_percentile(latencies_ms, 99),
        min_ms=latencies_ms[0] if latencies_ms else 0.0,
        max_ms=latencies_ms[-1] if latencies_ms else 0.0,
        mean_ms=statistics.fmean(latencies_ms) if latencies_ms else 0.0,
    )


async def _check_api_reachable() -> bool:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(API_HEALTH_URL, timeout=5.0)
            return response.status_code == 200
    except httpx.HTTPError:
        return False


def _print_summary(summary: LevelSummary) -> None:
    error_rate_pct = (
        (summary.failed / summary.total_requests * 100) if summary.total_requests else 0.0
    )
    pool_note = (
        "  <-- exceeds DART_REDIS_POOL_SIZE"
        if summary.concurrency > DART_REDIS_POOL_SIZE
        else ""
    )
    print(f"\n=== Concurrency: {summary.concurrency}{pool_note} ===")
    print(f"  Total requests      : {summary.total_requests}")
    print(f"  Successful          : {summary.successful}")
    print(f"  Failed              : {summary.failed} ({error_rate_pct:.2f}%)")
    print(f"  Wall time           : {summary.wall_seconds:.3f}s")
    print(f"  Throughput          : {summary.req_per_sec:.2f} req/s")
    print(f"  Latency min / mean  : {summary.min_ms:.2f}ms / {summary.mean_ms:.2f}ms")
    print(
        f"  Latency p50/p95/p99 : {summary.p50_ms:.2f}ms / "
        f"{summary.p95_ms:.2f}ms / {summary.p99_ms:.2f}ms"
    )
    print(f"  Latency max         : {summary.max_ms:.2f}ms")


def _print_final_table(summaries: list[LevelSummary]) -> None:
    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    header = (
        f"{'Concurrency':>11} | {'Req/s':>9} | {'p50 (ms)':>9} | "
        f"{'p95 (ms)':>9} | {'p99 (ms)':>9} | {'Errors':>7}"
    )
    print(header)
    print("-" * len(header))
    for s in summaries:
        print(
            f"{s.concurrency:>11} | {s.req_per_sec:>9.2f} | {s.p50_ms:>9.2f} | "
            f"{s.p95_ms:>9.2f} | {s.p99_ms:>9.2f} | {s.failed:>7}"
        )
    print("=" * 78)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DART ingestion API load benchmark.")
    parser.add_argument(
        "--concurrency-levels",
        type=int,
        nargs="+",
        default=DEFAULT_CONCURRENCY_LEVELS,
        help=f"Concurrency levels to sweep (default: {DEFAULT_CONCURRENCY_LEVELS}).",
    )
    parser.add_argument(
        "--requests-per-level",
        type=int,
        default=DEFAULT_REQUESTS_PER_LEVEL,
        help=f"Total requests sent at each concurrency level (default: {DEFAULT_REQUESTS_PER_LEVEL}).",
    )
    return parser.parse_args()


async def main() -> int:
    args = _parse_args()

    print(f"Checking dart-api reachability at {API_HEALTH_URL} ...")
    if not await _check_api_reachable():
        print(
            f"ERROR: dart-api is not reachable at {API_HEALTH_URL}. "
            "Start the stack first: docker compose up -d --build",
            file=sys.stderr,
        )
        return 1

    print("dart-api is reachable. Starting benchmark.\n")
    print(f"Concurrency levels : {args.concurrency_levels}")
    print(f"Requests per level : {args.requests_per_level}")
    print(f"DART Redis pool size (reference): {DART_REDIS_POOL_SIZE}")

    summaries: list[LevelSummary] = []
    for concurrency in args.concurrency_levels:
        summary = await _run_concurrency_level(concurrency, args.requests_per_level)
        summaries.append(summary)
        _print_summary(summary)

    _print_final_table(summaries)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
