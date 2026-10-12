"""Do exactly one measurement, in an interpreter of its own.

Spawned by ``benches/bench.py`` — one process per measurement — and runnable by
hand when a number looks surprising::

    .venv/bin/python benches/worker.py --mode startup --framework lauren --out /tmp/s.json
    .venv/bin/python benches/worker.py --mode measure --framework lauren \\
        --workload hello --duration 0.5 --warmup 0.25 --out /tmp/m.json

Two modes, because "how fast is it?" and "how long until it can serve?" cannot
be measured in the same warm process:

``startup``
    Import the framework, build the application with all five routes, drive the
    lifespan startup handshake and get an HTTP 200 from the first real request.
    This is "import + build app + first ready request".

``measure``
    Check the workload's contract, discard a warmup pass, then time the workload
    for ``--duration`` seconds.  This is the throughput and latency number.

Why one process *per measurement*
---------------------------------

Because not every framework tolerates a second application in one interpreter.
Sanic is the clear case: its ``touchup`` scheme compiles patched handler methods
into a table keyed per app, and standing up a second Sanic app in the same
process leaves applications unable to start at all — a ``KeyError`` raised out of
``sanic.touchup.schemes.base`` when it looks up a method the table no longer
holds.  *Which* key is missing varies with what the app registered:
``'connection_task'``, ``'handle_exception'`` and ``'http1_response_header'``
have all been observed from that one line, so the failure is something to
recognise by its traceback rather than by its message.  An earlier revision of
this harness built one fresh app per repeat inside a single process, and the
first workload measured there failed on its *second* repeat; a process that
builds two Sanic apps leaves *both* of them unable to start.  That is a property
of the harness, not of Sanic, and publishing it as a benchmark result would be a
slander; so the harness gives every framework the same isolated process rather
than special-casing one.

Isolation buys a second, larger property for free: no repeat can inherit warm
state from another, no framework can be affected by another framework's
import-time patches, and every measurement gets a fresh event loop and a fresh
application.  The cost — one interpreter boot per repeat — is real, and it is
stated on the results page rather than hidden.

Why the timings go to a file
----------------------------

Frameworks print their own banners (Sanic's MOTD to stdout, startup logs from
several others).  A measurement must neither corrupt nor be corrupted by
anybody else's output, so the sample travels through ``--out`` and stdout is
left to the frameworks.

Exit codes: ``0`` measured, ``2`` the request is unusable (unknown framework,
bad arguments), ``3`` the application did not satisfy the contract, ``4`` an
unexpected error.  The parent treats any non-zero status as a failed repeat and
records the reason rather than a number.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from stats import percentile

__all__ = ["main"]

#: Started as early as an import can: everything interesting happens after it.
_T0 = time.perf_counter()

BASE_URL = "http://bench.invalid"
"""Sentinel origin; ``ASGITransport`` never opens a socket, so nothing resolves it."""

REQUEST_TIMEOUT_S = 60.0


class ContractError(RuntimeError):
    """The application did not answer the workload's contract, so nothing was timed."""


async def drive(
    client: Any,
    workload: Any,
    *,
    duration_s: float,
    concurrency: int,
) -> tuple[int, float, list[float]]:
    """Issue requests for ``duration_s`` seconds; return count, elapsed, latencies.

    The loop is time-boxed rather than request-boxed so that every framework is
    measured over the same slice of wall time no matter how fast it is.  The
    status is re-checked on every response — cheap, and it means a framework
    cannot quietly start failing halfway through a run and still keep its numbers.
    """
    latencies: list[float] = []
    deadline = time.perf_counter() + duration_s

    async def worker() -> None:
        while time.perf_counter() < deadline:
            started = time.perf_counter()
            response = await client.request(workload.method, workload.path, json=workload.json_body)
            latencies.append(time.perf_counter() - started)
            if response.status_code != workload.status:
                raise ContractError(f"{workload.name}: HTTP {response.status_code} mid-measurement")

    started_at = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    return len(latencies), time.perf_counter() - started_at, latencies


async def _drive_workload(
    client: Any,
    workload: Any,
    *,
    duration_s: float,
    warmup_s: float,
    concurrency: int,
) -> dict[str, float]:
    """Check the contract, warm up, and time one workload on one application."""
    started = time.perf_counter()
    response = await client.request(workload.method, workload.path, json=workload.json_body)
    contract_ms = (time.perf_counter() - started) * 1000.0

    outcome = workload.check(response.status_code, response.text)
    if isinstance(outcome, str):
        raise ContractError(outcome)

    warmup_requests = 0
    if warmup_s > 0:
        warmup_requests, _, _ = await drive(client, workload, duration_s=warmup_s, concurrency=concurrency)

    requests, elapsed_s, latencies = await drive(
        client, workload, duration_s=duration_s, concurrency=concurrency
    )
    if not latencies:
        raise ContractError("no request completed within the measurement window")

    return {
        "throughput_rps": requests / elapsed_s if elapsed_s else 0.0,
        "p50_ms": percentile(latencies, 50) * 1000.0,
        "p95_ms": percentile(latencies, 95) * 1000.0,
        "p99_ms": percentile(latencies, 99) * 1000.0,
        "latency_min_ms": min(latencies) * 1000.0,
        "latency_max_ms": max(latencies) * 1000.0,
        "requests": requests,
        "elapsed_s": elapsed_s,
        "warmup_requests": warmup_requests,
        "contract_ms": contract_ms,
    }


def _resolve(key: str) -> Any:
    """Look up a participant: a framework from the registry, or the floor.

    The baseline in ``benches/baseline_app.py`` is not in the framework registry
    — it is not a framework — but it is measured by this same worker, so it is
    resolved here rather than duplicated.
    """
    from baseline_app import REFERENCE
    from framework_apps import BY_KEY

    if key == REFERENCE.key:
        return REFERENCE

    framework = BY_KEY.get(key)
    if framework is None or not framework.measured or framework.app is None:
        raise LookupError(f"{key!r} is not measurable in-process")
    return framework


async def _measure(args: argparse.Namespace) -> dict[str, Any]:
    """Run ``measure`` mode: contract, warmup, timed run.  All in one app."""
    import httpx

    from asgi_lifespan import LifespanRunner
    from workloads import WORKLOADS

    workload = next((w for w in WORKLOADS if w.name == args.workload), None)
    if workload is None:
        raise LookupError(f"unknown workload {args.workload!r}")

    framework = _resolve(args.framework)

    # Fresh application, fresh event loop: this process will never serve twice.
    app = framework.app()
    lifespan = LifespanRunner(app)
    try:
        await lifespan.startup()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url=BASE_URL, timeout=REQUEST_TIMEOUT_S
        ) as client:
            measured = await _drive_workload(
                client,
                workload,
                duration_s=args.duration,
                warmup_s=args.warmup,
                concurrency=args.concurrency,
            )
    finally:
        await lifespan.shutdown()

    measured["lifespan_supported"] = lifespan.supported
    return measured


async def _startup(args: argparse.Namespace) -> dict[str, Any]:
    """Run ``startup`` mode: import, build, first ready request."""
    import httpx

    from asgi_lifespan import LifespanRunner

    framework = _resolve(args.framework)

    t_harness_imported = time.perf_counter()
    app = framework.app()
    t_app_built = time.perf_counter()

    lifespan = LifespanRunner(app)
    try:
        await lifespan.startup()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url=BASE_URL, timeout=REQUEST_TIMEOUT_S
        ) as client:
            response = await client.get("/")
    finally:
        await lifespan.shutdown()
    t_ready = time.perf_counter()

    try:
        body = json.loads(response.text)
    except ValueError:
        body = None

    if response.status_code != 200 or body != {"ok": True}:
        raise ContractError(
            f"first request was not the hello contract (HTTP {response.status_code}, body {body!r})"
        )

    return {
        "harness_import_ms": (t_harness_imported - _T0) * 1000.0,
        "build_app_ms": (t_app_built - t_harness_imported) * 1000.0,
        "first_request_ms": (t_ready - t_app_built) * 1000.0,
        "child_total_ms": (t_ready - _T0) * 1000.0,
        "first_request_status": response.status_code,
        "lifespan_supported": lifespan.supported,
        "lifespan_note": lifespan.note,
        "entrypoint": framework.entrypoint,
    }


def _write(path: Path, payload: dict[str, Any]) -> None:
    """Write the sample where the parent will look for it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="worker.py",
        description="Take one measurement, in an interpreter of its own.",
    )
    parser.add_argument("--mode", choices=("startup", "measure"), required=True)
    parser.add_argument("--framework", required=True, help="registry key to measure")
    parser.add_argument("--out", required=True, help="path to write the JSON sample to")
    parser.add_argument("--workload", default="hello", help="workload name (measure mode)")
    parser.add_argument("--duration", type=float, default=0.5, help="timed seconds")
    parser.add_argument("--warmup", type=float, default=0.25, help="discarded warmup seconds")
    parser.add_argument("--concurrency", type=int, default=1, help="in-flight requests")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Take one measurement and write it to ``--out``."""
    args = _parse_args(argv)

    # Frameworks log banners at import and at startup; none of that belongs in a
    # measurement, and the sample travels through a file, not through stdout.
    logging.disable(logging.CRITICAL)

    try:
        if args.mode == "startup":
            payload: dict[str, Any] = asyncio.run(_startup(args))
        else:
            payload = asyncio.run(_measure(args))
    except ContractError as exc:
        print(f"error: {args.framework}: {exc}", file=sys.stderr)
        return 3
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - reported to the parent as a failed repeat
        import traceback

        traceback.print_exc()
        print(f"error: {args.framework}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 4

    payload["framework"] = args.framework
    payload["mode"] = args.mode
    if args.mode == "measure":
        payload["workload"] = args.workload
        payload["warmup_s"] = args.warmup
        payload["duration_s"] = args.duration
        payload["concurrency"] = args.concurrency
        payload["harness_import_ms"] = None
    _write(Path(args.out), payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
