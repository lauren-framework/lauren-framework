"""Time every framework through every workload, and emit one JSON document.

``benches/run.py`` answers *"does every framework answer correctly?"*; this file
answers *"how long does each one take?"*.  They are kept apart on purpose: the
damaging outcome of a cross-framework benchmark is not a wrong number, it is a
*fast* wrong number, so nothing is timed here that has not just passed the
contract in ``benches/workloads.py``.

How a number is produced
------------------------

1. **One process per measurement.**  ``benches/worker.py`` is spawned per
   (framework, workload, repeat).  It imports the framework, builds an
   application, drives the lifespan startup handshake, re-checks the workload's
   contract on *that* instance, discards a warmup pass, then times the workload.
   Isolation is not decoration: several frameworks in this field patch shared
   classes at import time, and Sanic cannot even start a second application in
   one interpreter, so a single process holding eight frameworks (or even two
   Sanic apps) does not produce comparable numbers.  See ``benches/worker.py``
   for the full account of that failure.
2. **Rounds, not blocks.**  Repeats are interleaved round-robin: round *r*
   measures every framework and workload before round *r+1* begins.  If the
   machine drifts — thermal throttling, a neighbour waking up — the drift lands
   on all frameworks instead of only the ones that happened to be measured late.
3. **Median plus spread.**  Each statistic is reported as the median over the
   repeats together with ``min``/``max``/``stdev``/``spread_pct``, so a reader
   can check a claimed difference against the noise before believing it.

What is measured
----------------

* **throughput** — completed requests divided by the elapsed wall time of one
  repeat (requests/second).
* **latency** — p50/p95/p99 of the per-request round trip as the in-process
  client observes it, pooled over the requests of a repeat.
* **startup** — separately, and only separately, import + build app + first
  ready request, in a fresh subprocess per repeat.  Measuring startup in a
  process that has already imported every framework would measure nothing.

What is deliberately *not* measured
-----------------------------------

Memory / RSS: out of scope for this comparison.  It needs a live server process
to mean anything, and this harness is deliberately in-process.

And the caveat that belongs above every number below: this is an **in-process**
measurement through ``httpx.ASGITransport``.  There is no socket, no server, no
TLS, no worker process.  It is repeatable and fair *between frameworks*, and it
is **not** a deployed-configuration number — do not read it as "requests per
second served by uvicorn".  The client itself is a large part of the measured
cost, which is why a hand-written ASGI baseline app is measured alongside the
frameworks and reported as a reference floor, not as a competitor.

Usage::

    .venv/bin/python benches/bench.py                    # full run, defaults
    .venv/bin/python benches/bench.py lauren fastapi     # subset
    .venv/bin/python benches/bench.py --repeat 9 --duration 1.0 --out /tmp/x.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from baseline_app import REFERENCE
from framework_apps import FRAMEWORKS, Framework, installed_versions
from stats import Stat
from workloads import WORKLOAD_ORDER, WORKLOADS, Workload

__all__ = ["main"]

SCHEMA = "lauren-framework-bench/1"
"""Version of the emitted document, so a reader can tell two document shapes apart."""

_HARNESS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _HARNESS_DIR.parent
_WORKER = _HARNESS_DIR / "worker.py"
_DEFAULT_OUT = _HARNESS_DIR / "results" / "frameworks-inprocess.json"

DEFAULT_REPEATS = 5
DEFAULT_WARMUP_S = 0.25
DEFAULT_DURATION_S = 0.5
DEFAULT_CONCURRENCY = 1
DEFAULT_STARTUP_REPEATS = 5

MEASURE_TIMEOUT_S = 300.0
STARTUP_TIMEOUT_S = 300.0

_WORKLOAD_BY_NAME = {workload.name: workload for workload in WORKLOADS}

#: Hashed into the artifact so a reader can tell which harness produced it.
_PROVENANCE_FILES = (
    "asgi_lifespan.py",
    "baseline_app.py",
    "bench.py",
    "framework_apps.py",
    "requirements-bench.txt",
    "run.py",
    "stats.py",
    "worker.py",
    "workloads.py",
)


# ---------------------------------------------------------------------------
# Talking to the worker processes
# ---------------------------------------------------------------------------


def _spawn(worker_args: Sequence[str], *, timeout: float) -> tuple[int, dict[str, Any] | None, str]:
    """Run one worker in a fresh interpreter and read its JSON sample.

    Returns the exit status, the parsed sample (``None`` on failure) and, on
    failure, a short tail of the worker's own diagnostics.
    """
    with tempfile.TemporaryDirectory(prefix="lauren-bench-") as tmp:
        out = Path(tmp) / "sample.json"
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                [sys.executable, str(_WORKER), *worker_args, "--out", str(out)],
                cwd=str(_HARNESS_DIR),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return 1, None, f"worker timed out after {timeout:g}s"

        process_ms = (time.perf_counter() - started) * 1000.0

        if completed.returncode != 0 or not out.exists():
            lines = [line for line in (completed.stderr or completed.stdout).splitlines() if line.strip()]
            detail = " | ".join(lines[-3:]) or f"exit {completed.returncode} with no output"
            return completed.returncode, None, detail

        sample: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
        sample["process_ms"] = process_ms
        return 0, sample, ""


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def measure_startup(framework: Framework, *, repeats: int) -> dict[str, Any]:
    """Cold-start one framework ``repeats`` times, each in a fresh subprocess.

    ``process_ms`` is the parent's wall clock around the whole subprocess, so it
    spans interpreter boot through *process exit* -- an upper bound on time to
    serve, which is what a user waits for, with each framework's teardown
    counted in at the end.  The phase breakdown comes from inside the child,
    where the interpreter is already running, and stops at the first ready
    request.
    """
    samples: list[dict[str, Any]] = []
    for attempt in range(1, repeats + 1):
        code, sample, detail = _spawn(
            ["--mode", "startup", "--framework", framework.key], timeout=STARTUP_TIMEOUT_S
        )
        if code != 0 or sample is None:
            raise RuntimeError(
                f"startup measurement for {framework.key} failed on attempt {attempt}/{repeats}: {detail}"
            )
        samples.append(sample)

    return {
        "repeats": len(samples),
        "process_ms": Stat.of([s["process_ms"] for s in samples]).as_dict(2),
        "phases_ms": {
            name: Stat.of([s[key] for s in samples]).as_dict(2)
            for name, key in (
                ("harness_import", "harness_import_ms"),
                ("build_app", "build_app_ms"),
                ("first_request", "first_request_ms"),
                ("child_total", "child_total_ms"),
            )
        },
        "lifespan_supported": all(bool(s["lifespan_supported"]) for s in samples),
        "raw": {
            "process_ms": [round(s["process_ms"], 2) for s in samples],
            "build_app_ms": [round(s["build_app_ms"], 2) for s in samples],
            "first_request_ms": [round(s["first_request_ms"], 2) for s in samples],
        },
    }


# ---------------------------------------------------------------------------
# Throughput and latency
# ---------------------------------------------------------------------------


def summarise(
    workload: Workload,
    samples: Sequence[dict[str, Any]],
    errors: Sequence[str],
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Turn one (framework, workload)'s repeat samples into a published entry.

    Failures are recorded next to the numbers rather than replacing the whole
    run: one handler that misbehaves must not cost the other 39 measurements
    their results, and it must not keep a number either.
    """
    entry: dict[str, Any] = {
        "describes": workload.describes,
        "request": {
            "method": workload.method,
            "path": workload.path,
            "json_body": workload.json_body,
        },
        "contract": {"status": workload.status, "body": workload.body},
    }

    if errors:
        entry["errors"] = list(errors)

    if not samples:
        entry["error"] = errors[-1] if errors else "no sample was produced"
        entry["repeats"] = 0
        entry["expected_repeats"] = args.repeat
        return entry

    throughputs = [float(sample["throughput_rps"]) for sample in samples]
    entry.update(
        {
            "repeats": len(samples),
            "expected_repeats": args.repeat,
            "incomplete": len(samples) != args.repeat,
            "concurrency": args.concurrency,
            "duration_s": args.duration,
            "warmup_s": args.warmup,
            "isolation": "one process, one application, one event loop per repeat",
            "requests_total": sum(int(sample["requests"]) for sample in samples),
            "warmup_requests_total": sum(int(sample["warmup_requests"]) for sample in samples),
            "child_process_ms": Stat.of([float(sample["process_ms"]) for sample in samples]).as_dict(2),
            "throughput_rps": Stat.of(throughputs).as_dict(2),
            "latency_ms": {
                "p50": Stat.of([float(s["p50_ms"]) for s in samples]).as_dict(4),
                "p95": Stat.of([float(s["p95_ms"]) for s in samples]).as_dict(4),
                "p99": Stat.of([float(s["p99_ms"]) for s in samples]).as_dict(4),
            },
            "raw": {
                "throughput_rps": [round(value, 2) for value in throughputs],
                "p50_ms": [round(float(s["p50_ms"]), 4) for s in samples],
                "p95_ms": [round(float(s["p95_ms"]), 4) for s in samples],
                "p99_ms": [round(float(s["p99_ms"]), 4) for s in samples],
                "requests": [int(s["requests"]) for s in samples],
                "elapsed_s": [round(float(s["elapsed_s"]), 6) for s in samples],
                "contract_ms": [round(float(s["contract_ms"]), 4) for s in samples],
            },
        }
    )
    return entry


def measure_rounds(
    frameworks: Sequence[Framework], args: argparse.Namespace
) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], dict[tuple[str, str], list[str]]]:
    """Measure every framework and workload, interleaved by round."""
    samples: dict[tuple[str, str], list[dict[str, Any]]] = {
        (framework.key, name): [] for framework in frameworks for name in WORKLOAD_ORDER
    }
    errors: dict[tuple[str, str], list[str]] = {
        (framework.key, name): [] for framework in frameworks for name in WORKLOAD_ORDER
    }

    for round_index in range(1, args.repeat + 1):
        print(f"-- round {round_index}/{args.repeat} --", flush=True)
        for framework in frameworks:
            cells: list[str] = []
            for name in WORKLOAD_ORDER:
                key = (framework.key, name)
                code, sample, detail = _spawn(
                    [
                        "--mode",
                        "measure",
                        "--framework",
                        framework.key,
                        "--workload",
                        name,
                        "--duration",
                        str(args.duration),
                        "--warmup",
                        str(args.warmup),
                        "--concurrency",
                        str(args.concurrency),
                    ],
                    timeout=MEASURE_TIMEOUT_S,
                )
                if code != 0 or sample is None:
                    errors[key].append(f"round {round_index}: {detail}")
                    cells.append(f"{name}=FAIL")
                    continue
                samples[key].append(sample)
                cells.append(f"{name}={float(sample['throughput_rps']):7.1f}")
            print(f"   {framework.key:11s} " + "  ".join(cells), flush=True)

    return samples, errors


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def _cpu_model() -> str:
    """Best-effort CPU description; the table is meaningless without it."""
    try:
        with Path("/proc/cpuinfo").open(encoding="utf-8") as handle:
            for line in handle:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def _sha256(path: Path) -> str:
    """SHA-256 of one file, or ``"missing"`` when it is not there."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "missing"


def _git_provenance() -> dict[str, Any]:
    """Record the commit the numbers were taken at, if this is a git tree."""

    def run(*args: str) -> str | None:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=str(_REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=30.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return completed.stdout.strip() if completed.returncode == 0 else None

    commit = run("rev-parse", "HEAD")
    if commit is None:
        return {"available": False}
    changed = [line for line in (run("status", "--porcelain") or "").splitlines() if line.strip()]
    return {
        "available": True,
        "commit": commit,
        "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(changed),
        # Named, not only flagged.  ``dirty: true`` on its own gives a reader no
        # way to tell whether the *harness* was edited while the numbers were
        # being taken -- the one thing that would invalidate them -- or whether
        # the tree merely carried the artifact being written.  Bounded so an
        # unrelated untracked directory cannot bloat the document.
        "dirty_paths": changed[:20],
    }


def environment() -> dict[str, Any]:
    """Everything a reader needs to decide how far these numbers travel."""
    return {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_model": _cpu_model(),
        "cpu_count_logical": os.cpu_count(),
        "installed_versions": installed_versions(),
        "harness_sha256": {f"benches/{name}": _sha256(_HARNESS_DIR / name) for name in _PROVENANCE_FILES},
    }


def methodology(args: argparse.Namespace) -> dict[str, Any]:
    """State the contract the numbers below were produced under."""
    return {
        "driver": "httpx.ASGITransport (in-process; no socket, no server process)",
        "isolation": (
            "each repeat runs in its own freshly spawned interpreter with its own "
            "application and event loop; nothing is shared between repeats or between frameworks"
        ),
        "ordering": "repeats are interleaved round-robin across frameworks and workloads",
        "lifespan": "the lifespan startup handshake is driven for every framework before the first request",
        "fresh_app_per_repeat": True,
        "repeats": args.repeat,
        "warmup_s": args.warmup,
        "duration_s": args.duration,
        "concurrency": args.concurrency,
        "startup_repeats": args.startup_repeats,
        "startup_scope": "import + build app + first ready request, in a fresh subprocess per repeat",
        "throughput_definition": "completed requests / elapsed wall time of one repeat",
        "latency_definition": "per-request round trip observed by the in-process client, pooled over a repeat",
        "latency_includes": "httpx + ASGITransport client overhead, not handler-only time",
        "percentile_method": "linear interpolation over the request latencies of each repeat",
        "spread_definition": "(max - min) / median * 100, over the per-repeat values of that statistic",
        "contract_enforced": (
            "every repeat re-checks its workload against benches/workloads.py on the "
            "application instance it is about to time"
        ),
        "not_measured": "memory / RSS is out of scope for this comparison",
        "caveat": (
            "in-process numbers are comparable between frameworks but are NOT a "
            "deployed-configuration number (uvicorn/gunicorn/granian over a socket)"
        ),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _run_all(frameworks: Sequence[Framework], args: argparse.Namespace) -> dict[str, Any]:
    """Measure every selected framework and build the JSON document."""
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "methodology": methodology(args),
        "environment": environment(),
        "git": _git_provenance(),
        "frameworks": [],
        "not_measured": [
            {
                "key": framework.key,
                "dist": framework.dist,
                "entrypoint": framework.entrypoint,
                "reason": framework.not_measured_reason,
            }
            for framework in FRAMEWORKS
            if not framework.measured
        ],
    }

    startup: dict[str, dict[str, Any]] = {}
    if args.startup_repeats > 0:
        for framework in frameworks:
            started = time.perf_counter()
            startup[framework.key] = measure_startup(framework, repeats=args.startup_repeats)
            print(
                f"   startup {framework.key:11s} "
                f"{startup[framework.key]['process_ms']['median']:7.0f} ms "
                f"(+/-{startup[framework.key]['process_ms']['spread_pct']:.1f}%)"
                f"  [{time.perf_counter() - started:.1f}s]",
                flush=True,
            )

    samples, errors = measure_rounds(frameworks, args)

    for framework in frameworks:
        entry: dict[str, Any] = {
            "key": framework.key,
            "dist": framework.dist,
            "version": "n/a — reference floor, not a framework"
            if framework is REFERENCE
            else framework.version,
            "entrypoint": framework.entrypoint,
            "serialization": framework.serialization,
            "di_mechanism": framework.di_mechanism,
            "validation": framework.validation,
        }
        if framework is REFERENCE:
            entry["reference"] = True
        if framework.key in startup:
            entry["startup"] = startup[framework.key]
        entry["workloads"] = {
            name: summarise(
                _WORKLOAD_BY_NAME[name], samples[(framework.key, name)], errors[(framework.key, name)], args
            )
            for name in WORKLOAD_ORDER
        }
        payload["frameworks"].append(entry)

    payload["reproduce"] = " ".join(["python", "benches/bench.py", *args.frameworks])
    return payload


def _summarise(payload: dict[str, Any]) -> str:
    """A short human table of the headline numbers, for the console."""
    width = max((len(entry["key"]) for entry in payload["frameworks"]), default=9)
    header = f"{'framework':{width}s} " + " ".join(f"{name:>13s}" for name in WORKLOAD_ORDER)
    lines = ["median throughput, req/s", header, "-" * len(header)]
    for entry in payload["frameworks"]:
        cells = []
        for name in WORKLOAD_ORDER:
            measured = entry["workloads"].get(name, {})
            if "throughput_rps" not in measured:
                cells.append(f"{'FAIL':>13s}")
            else:
                cell = f"{measured['throughput_rps']['median']:>13.1f}"
                cells.append(cell)
        lines.append(f"{entry['key']:{width}s} " + " ".join(cells))

    lines.append("")
    lines.append(f"{'startup, ms (median +/- spread)':{width}s}")
    for entry in payload["frameworks"]:
        measured = entry.get("startup")
        if measured is None:
            lines.append(f"{entry['key']:{width}s} not measured")
            continue
        lines.append(
            f"{entry['key']:{width}s} {measured['process_ms']['median']:7.1f} ms "
            f"+/-{measured['process_ms']['spread_pct']:.1f}%"
        )
    return "\n".join(lines)


def _failures(payload: dict[str, Any]) -> list[str]:
    """Every (framework, workload) that produced no complete set of repeats."""
    failures: list[str] = []
    for entry in payload["frameworks"]:
        for name in WORKLOAD_ORDER:
            measured = entry["workloads"][name]
            if "error" in measured:
                failures.append(f"{entry['key']}/{name}: {measured['error']}")
            elif measured.get("incomplete"):
                failures.append(
                    f"{entry['key']}/{name}: only {measured['repeats']}/{measured['expected_repeats']} "
                    f"repeats succeeded ({'; '.join(measured.get('errors', []))})"
                )
    return failures


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bench.py",
        description="Time every framework through every workload, and emit one JSON document.",
    )
    parser.add_argument(
        "frameworks",
        nargs="*",
        help="subset of framework keys to measure (default: all measurable)",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=DEFAULT_REPEATS,
        help=f"timed repeats per (framework, workload) (default: {DEFAULT_REPEATS})",
    )
    parser.add_argument(
        "--warmup",
        type=float,
        default=DEFAULT_WARMUP_S,
        help=f"discarded warmup seconds before each repeat (default: {DEFAULT_WARMUP_S})",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=DEFAULT_DURATION_S,
        help=f"timed seconds per repeat (default: {DEFAULT_DURATION_S})",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=(
            f"in-flight requests per repeat; 1 is a single sequential client (default: {DEFAULT_CONCURRENCY})"
        ),
    )
    parser.add_argument(
        "--startup-repeats",
        type=int,
        default=DEFAULT_STARTUP_REPEATS,
        help=(
            "fresh subprocesses per framework for the startup measurement; "
            f"0 disables it (default: {DEFAULT_STARTUP_REPEATS})"
        ),
    )
    parser.add_argument(
        "--no-baseline",
        action="store_true",
        help=(
            "skip the hand-written ASGI reference floor (benches/baseline_app.py), "
            "which is otherwise measured alongside the frameworks"
        ),
    )
    parser.add_argument(
        "--out",
        default=str(_DEFAULT_OUT),
        help=f"where to write the JSON document (default: {_DEFAULT_OUT})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the whole measurement and write the JSON document."""
    args = _parse_args(argv)

    if args.repeat < 1:
        print("error: --repeat must be at least 1", file=sys.stderr)
        return 2
    if args.duration <= 0:
        print("error: --duration must be positive", file=sys.stderr)
        return 2
    if args.concurrency < 1:
        print("error: --concurrency must be at least 1", file=sys.stderr)
        return 2
    if args.startup_repeats < 0:
        print("error: --startup-repeats cannot be negative", file=sys.stderr)
        return 2

    unknown = sorted(set(args.frameworks) - {framework.key for framework in [*FRAMEWORKS, REFERENCE]})
    if unknown:
        print(f"error: unknown framework key(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    not_measurable = sorted(
        framework.key
        for framework in FRAMEWORKS
        if framework.key in args.frameworks and not framework.measured
    )
    if not_measurable:
        print(
            f"error: cannot measure {', '.join(not_measurable)} in-process "
            "(see benches/feasibility-report.md)",
            file=sys.stderr,
        )
        return 2

    selected = [
        framework
        for framework in [*FRAMEWORKS, REFERENCE]
        if framework.measured and (not args.frameworks or framework.key in args.frameworks)
    ]
    if not args.no_baseline and all(framework is not REFERENCE for framework in selected):
        # The floor rides along so a reader can tell framework cost from the
        # harness's own client cost; it is never presented as a competitor.
        selected.append(REFERENCE)

    payload = _run_all(selected, args)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print()
    print(_summarise(payload))
    print()
    print(f"wrote {out}")

    failures = _failures(payload)
    if failures:
        print(f"\n{len(failures)} workload(s) did not produce a full set of repeats:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
