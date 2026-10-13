"""Drive every framework through every workload in-process, and check the answers.

Run it with::

    .venv/bin/python benches/run.py

The point of this file is the *assertion*, not the timing (that is
``benches/bench.py``).  A cross-framework benchmark is worthless if a framework
is quietly answering the wrong thing: a 404 from a mis-registered route is
usually *faster* than a real handler, so a harness that only records durations
will happily publish a table in which the broken row wins.  Every request here
is therefore checked against the contract in ``benches/workloads.py`` before any
number derived from it is trusted.

This harness is a **single pass**: every framework is probed once, against an
application built for it inside this process.  Repeats belong to the timing
runner (``benches/bench.py``), which builds one fresh application in one fresh
interpreter per repeat.  Two properties make the comparison fair, and they are
split across the two runners deliberately:

* **Lifespan is driven for every framework**, here and in the timing runner.
  ``httpx.ASGITransport`` never sends the ``lifespan`` scope, but a deployed ASGI
  server always does, and Sanic refuses to serve without it.  Driving it
  everywhere is the deployed path; not driving it would have excluded Sanic for
  the wrong reason.
* **Every framework is probed on its own fresh application**, so nothing is
  checked against another framework's warm state.  The timing runner extends
  that to *per repeat, per process*, because what matters there is that no
  measurement inherits the previous measurement's warm state — a property this
  single-pass file neither needs nor claims.

Exit status is non-zero if any workload fails its contract, so this doubles as
the benchmark's own smoke test.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from typing import Any

import httpx

from asgi_lifespan import LifespanRunner
from framework_apps import FRAMEWORKS, Framework
from workloads import WORKLOAD_ORDER, WORKLOADS, Workload

BASE_URL = "http://bench.invalid"
REQUEST_TIMEOUT_S = 30.0

_WORKLOAD_BY_NAME = {workload.name: workload for workload in WORKLOADS}


@dataclass
class CheckResult:
    """Outcome of one (framework, workload) request."""

    framework: str
    workload: str
    ok: bool
    detail: str

    def __str__(self) -> str:
        return f"{self.framework}/{self.workload}: {self.detail}"


@dataclass
class FrameworkReport:
    """All check results for one framework."""

    key: str
    results: list[CheckResult] = field(default_factory=list)
    lifespan_note: str = ""

    @property
    def ok(self) -> bool:
        """Whether every workload passed."""
        return bool(self.results) and all(result.ok for result in self.results)

    @property
    def failures(self) -> list[CheckResult]:
        """Only the failing checks."""
        return [result for result in self.results if not result.ok]


def _check(workload: Workload, response: httpx.Response) -> dict[str, Any] | str:
    """Compare one response against the contract.

    Thin by design: the comparison itself lives on ``Workload.check``, so the
    harness that asserts and the harness that times run the *same* code and
    cannot drift apart.  Returns the parsed payload when the response satisfies
    the contract, or a human-readable description of the first mismatch when it
    does not.
    """
    return workload.check(response.status_code, response.text)


async def probe_framework(framework: Framework) -> FrameworkReport:
    """Build the app once and check every workload against it."""
    report = FrameworkReport(key=framework.key)

    if framework.app is None:  # pragma: no cover - guarded by the caller
        report.results.append(CheckResult(framework.key, "-", False, "no app factory registered"))
        return report

    app = framework.app()

    runner = LifespanRunner(app)
    try:
        await runner.startup()
        if not runner.supported:
            report.lifespan_note = runner.note

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url=BASE_URL, timeout=REQUEST_TIMEOUT_S
        ) as client:
            for name in WORKLOAD_ORDER:
                workload = _WORKLOAD_BY_NAME[name]
                try:
                    response = await client.request(workload.method, workload.path, json=workload.json_body)
                except Exception as exc:  # noqa: BLE001 - the failure *is* the result
                    report.results.append(
                        CheckResult(
                            framework.key,
                            name,
                            False,
                            f"{type(exc).__name__}: {exc}",
                        )
                    )
                    continue

                outcome = _check(workload, response)
                if isinstance(outcome, str):
                    report.results.append(CheckResult(framework.key, name, False, outcome))
                else:
                    report.results.append(
                        CheckResult(framework.key, name, True, f"HTTP {response.status_code} ok")
                    )
    finally:
        await runner.shutdown()

    return report


async def probe_all(keys: list[str] | None = None) -> list[FrameworkReport]:
    """Check every measurable framework, in registry order."""
    selected = [fw for fw in FRAMEWORKS if fw.measured and (keys is None or fw.key in keys)]
    return [await probe_framework(framework) for framework in selected]


def render(reports: list[FrameworkReport]) -> str:
    """Render the check matrix: one row per framework, one column per workload."""
    width = max((len(r.key) for r in reports), default=9)
    header = f"{'framework'.ljust(width)}  " + "  ".join(name.ljust(11) for name in WORKLOAD_ORDER)
    lines = [header, "-" * len(header)]
    for report in reports:
        cells = []
        for name in WORKLOAD_ORDER:
            result = next((r for r in report.results if r.workload == name), None)
            cells.append(("ok" if result and result.ok else "FAIL").ljust(11))
        lines.append(f"{report.key.ljust(width)}  " + "  ".join(cells))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Run the correctness harness and report failures loudly."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "frameworks",
        nargs="*",
        help="subset of framework keys to check (default: all measurable)",
    )
    args = parser.parse_args(argv)

    reports = asyncio.run(probe_all(args.frameworks or None))

    print(render(reports))

    excluded = [fw for fw in FRAMEWORKS if not fw.measured and not args.frameworks]
    if excluded:
        print("\nnot measured (excluded by design):")
        for framework in excluded:
            print(f"  - {framework.key}: {framework.not_measured_reason.splitlines()[0]}")

    notes = [(r.key, r.lifespan_note) for r in reports if r.lifespan_note]
    if notes:
        print("\nlifespan notes:")
        for key, note in notes:
            print(f"  - {key}: {note}")

    failures = [failure for report in reports for failure in report.failures]
    if failures:
        print(f"\n{len(failures)} contract failure(s):", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    total = sum(len(report.results) for report in reports)
    print(f"\nall {total} checks passed across {len(reports)} frameworks x {len(WORKLOADS)} workloads")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
