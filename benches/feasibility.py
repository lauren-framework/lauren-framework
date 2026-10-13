"""In-process ASGI feasibility probe for the cross-framework benchmark.

Run it with::

    .venv/bin/python benches/feasibility.py

It answers one question for every framework in ``benches/framework_apps.py``:
*can we drive this framework's request handling in-process, with no socket, no
server process and no external load generator?*

That question comes first because the benchmark's chosen methodology is an
in-process ``httpx.ASGITransport`` harness, and the methodology is only honest
if every row of the results table took the same path.  A framework that needs
its own server to answer a request simply cannot be compared this way, and
pretending otherwise would produce a table that looks precise and means
nothing.  So: measure what is measurable, and *state* what is not.

How a verdict is reached
------------------------
* **MEASURED** — build the framework's idiomatic minimal app, push one ``GET /``
  through it with the same transport the benchmark uses, and require a ``200``
  whose JSON body is ``{"ok": true}``.  Importing cleanly is not enough; the
  request has to come back correct.  Lifespan startup is driven first (see
  ``benches/asgi_lifespan.py``), because that is what a deployed ASGI server
  does and what Sanic requires before it will serve anything.
* **NOT MEASURED** — build the framework's app object and hand it to
  ``ASGITransport`` as well, then record the exact error it raises.  The
  exclusion is therefore demonstrated, not asserted.

Exit status is non-zero if a framework claimed as measurable fails its smoke
request, so this can be used as a CI gate.

The report it writes is deterministic
-------------------------------------
``benches/feasibility-report.md`` is committed next to the probe, so it must not
carry anything that changes for a reason other than a change in the facts.  It
records no wall clock: the provenance line is a SHA-256 over the probe's own
inputs, which moves when the probe moves and stays put when the probe is merely
re-run -- or when the report itself is committed.  Re-running this file on an
unchanged tree rewrites the report byte for byte, which is what lets it be a CI
gate without leaving a diff behind.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

# Sibling imports: running this file as a script puts its own directory first on
# ``sys.path``, so no path juggling is needed here.
from asgi_lifespan import LifespanRunner
from framework_apps import FRAMEWORKS, Framework

_HERE = Path(__file__).resolve().parent

#: The one request every measurable framework has to answer.
SMOKE_PATH = "/"
SMOKE_JSON: dict[str, Any] = {"ok": True}
TIMEOUT_S = 10.0

_REPORT_PATH = _HERE / "feasibility-report.md"

#: Hashed into the report so a reader can tell which probe produced it.  The
#: report is committed, so its provenance has to be a pure function of these
#: files rather than of the wall clock -- see ``_probe_inputs_digest``.
_PROBE_INPUTS = ("asgi_lifespan.py", "feasibility.py", "framework_apps.py")


@dataclass
class Verdict:
    """Outcome of probing one framework."""

    key: str
    claimed_measurable: bool
    worked: bool = False
    status: int | None = None
    body: str = ""
    error: str = ""
    surprise: str = ""
    lifespan: str = ""

    @property
    def ok(self) -> bool:
        """Whether the observed behaviour matches the registry's claim."""
        return self.worked == self.claimed_measurable and not self.surprise


@dataclass
class DriveResult:
    """One smoke request, plus how the app felt about lifespan startup."""

    status: int
    body: str
    lifespan: str = ""


async def _drive(app: Any, *, lifespan: bool = True) -> DriveResult:
    """Send one ``GET /`` through ``app`` via the benchmark's transport.

    Lifespan startup is driven first when ``lifespan`` is set, because that is
    what a deployed ASGI server does and what Sanic requires before it will
    serve anything.
    """
    runner = LifespanRunner(app) if lifespan else None
    try:
        if runner is not None:
            await runner.startup()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://bench.invalid", timeout=TIMEOUT_S
        ) as client:
            response = await client.get(SMOKE_PATH)
        note = "" if runner is None or runner.supported else runner.note
        return DriveResult(status=response.status_code, body=response.text, lifespan=note)
    finally:
        if runner is not None:
            await runner.shutdown()


async def probe(framework: Framework) -> Verdict:
    """Probe one framework and return its verdict."""
    verdict = Verdict(key=framework.key, claimed_measurable=framework.measured)

    if not framework.measured:
        await _probe_excluded(framework, verdict)
        return verdict

    try:
        app = framework.app()
    except Exception as exc:  # noqa: BLE001 - the failure *is* the evidence
        verdict.error = f"building the app raised {type(exc).__name__}: {exc}"
        return verdict

    try:
        result = await _drive(app)
    except Exception as exc:  # noqa: BLE001 - the failure *is* the evidence
        verdict.error = f"ASGITransport raised {type(exc).__name__}: {exc}"
        return verdict

    verdict.status = result.status
    verdict.body = result.body
    verdict.lifespan = result.lifespan
    if result.status != 200:
        verdict.error = f"expected HTTP 200, got {result.status}"
        return verdict
    try:
        payload = json.loads(result.body)
    except ValueError as exc:
        verdict.error = f"response body is not JSON: {exc}"
        return verdict
    if payload != SMOKE_JSON:
        verdict.error = f"expected body {SMOKE_JSON!r}, got {payload!r}"
        return verdict

    verdict.worked = True
    return verdict


async def _probe_excluded(framework: Framework, verdict: Verdict) -> None:
    """Demonstrate *why* an excluded framework cannot be driven in-process."""
    if framework.raw_object is None:
        verdict.error = "no app object to probe"
        return

    # Robyn parses ``sys.argv`` while it is being constructed; hide ours so the
    # negative probe cannot fail for the wrong reason.
    saved_argv = sys.argv
    sys.argv = [saved_argv[0]]
    try:
        obj = framework.raw_object()
    except Exception as exc:  # noqa: BLE001 - the failure *is* the evidence
        verdict.error = f"constructing the app object raised {type(exc).__name__}: {exc}"
        return
    finally:
        sys.argv = saved_argv

    try:
        result = await _drive(obj, lifespan=False)
    except Exception as exc:  # noqa: BLE001 - the failure *is* the evidence
        verdict.error = f"ASGITransport rejected it: {type(exc).__name__}: {exc}"
        return

    # Reaching here means the framework *is* drivable after all: the registry's
    # exclusion is wrong and somebody has to look at it.
    verdict.worked = True
    verdict.status = result.status
    verdict.body = result.body
    verdict.surprise = (
        "expected to be undrivable, but ASGITransport served it a response — "
        "the registry's not_measured_reason needs revisiting"
    )


def _probe_inputs_digest() -> str:
    """SHA-256 over the probe's own inputs, in sorted-name order.

    The report is committed next to the probe, so anything volatile in it -- a
    wall-clock stamp above all -- turns every read-only re-run into a spurious
    one-line diff.  Hashing the inputs keeps the provenance honest *and*
    reproducible: the digest changes when the probe changes, and never because
    the probe was merely re-run or because the report was committed.
    """
    digest = hashlib.sha256()
    for name in sorted(_PROBE_INPUTS):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update((_HERE / name).read_bytes())
        except OSError:  # pragma: no cover - only if a sibling file is missing
            digest.update(b"missing")
        digest.update(b"\0")
    return digest.hexdigest()


def _render_report(verdicts: list[Verdict]) -> str:
    """Render the committed feasibility report.

    Deterministic by construction: the same verdicts, the same environment and
    the same probe inputs always render the same bytes, so re-running the
    read-only probe leaves the working tree clean.
    """
    inputs = ", ".join(f"`{name}`" for name in sorted(_PROBE_INPUTS))
    lines: list[str] = [
        "# In-process ASGI feasibility",
        "",
        "Generated by `benches/feasibility.py` — do not edit by hand.",
        "",
        f"* Probe inputs: {inputs} — sha256 `{_probe_inputs_digest()}`",
        f"* Python: {platform.python_version()}",
        f"* Platform: {platform.platform()}",
        "",
        "The cross-framework benchmark drives every application in-process through",
        "`httpx.ASGITransport`. This probe establishes, for each candidate framework,",
        "that such a driver exists — and records the exact error for the candidates",
        "where it does not.",
        "",
        "There is deliberately no wall-clock stamp: the report is a pure function of",
        "the inputs and the environment above, so re-running the probe on an unchanged",
        "tree rewrites this file byte for byte. That is what lets it be a CI gate",
        "without leaving a spurious diff behind.",
        "",
        "| Framework | Version | Verdict | ASGI entry point | JSON serialization |",
        "| --- | --- | --- | --- | --- |",
    ]

    for verdict in verdicts:
        framework = next(fw for fw in FRAMEWORKS if fw.key == verdict.key)
        if verdict.claimed_measurable and verdict.worked:
            mark = "MEASURED"
        elif not verdict.claimed_measurable and not verdict.worked and not verdict.surprise:
            mark = "NOT MEASURED"
        else:
            mark = "**UNEXPECTED**"
        lines.append(
            f"| `{framework.key}` | {framework.version} | {mark} | "
            f"{framework.entrypoint} | {framework.serialization} |"
        )

    lines += ["", "## Evidence", ""]

    for verdict in verdicts:
        framework = next(fw for fw in FRAMEWORKS if fw.key == verdict.key)
        lines.append(f"### `{framework.key}`")
        lines.append("")
        if verdict.worked and not verdict.surprise:
            lines.append(f"* `GET /` -> HTTP {verdict.status}")
            lines.append(f"* body: `{verdict.body}`")
            if verdict.lifespan:
                lines.append(f"* lifespan: {verdict.lifespan}")
        elif framework.measured:
            lines.append(f"* **FAILED**: {verdict.error}")
        else:
            lines.append(f"* {verdict.error}")
            lines.append("")
            lines.append(f"* Rationale: {framework.not_measured_reason}")
        if verdict.surprise:
            lines.append(f"* **SURPRISE**: {verdict.surprise}")
        lines.append("")

    # Trailing blank lines are stripped to a single newline: the repo's
    # end-of-file-fixer hook enforces exactly one, and a generated file must be
    # byte-identical to what the hook would leave behind.
    return "\n".join(lines).rstrip("\n") + "\n"


async def _run_all() -> list[Verdict]:
    return [await probe(framework) for framework in FRAMEWORKS]


def main(argv: list[str] | None = None) -> int:
    """Run the probe, print a table, write the report, return an exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if any verdict contradicts the registry",
    )
    args = parser.parse_args(argv)

    verdicts = asyncio.run(_run_all())

    width = max(len(v.key) for v in verdicts)
    print(f"{'framework'.ljust(width)}  version        verdict        evidence")
    print(f"{'-' * width}  -------------  -------------  ------------------------------------")
    for verdict in verdicts:
        framework = next(fw for fw in FRAMEWORKS if fw.key == verdict.key)
        if verdict.worked and not verdict.surprise:
            state, detail = "MEASURED", f"HTTP {verdict.status} {verdict.body}"
        elif framework.measured:
            state, detail = "FAILED", verdict.error
        elif verdict.surprise:
            state, detail = "UNEXPECTED", verdict.surprise
        else:
            state, detail = "NOT MEASURED", verdict.error
        print(f"{verdict.key.ljust(width)}  {framework.version:<13}  {state:<13}  {detail}")

    rendered = _render_report(verdicts)
    if _REPORT_PATH.exists() and _REPORT_PATH.read_text(encoding="utf-8") == rendered:
        # Rewriting an identical file would only churn its mtime; saying so
        # instead makes it obvious that a clean tree after a re-run is the
        # expected outcome and not a missed write.
        print(f"\n{_REPORT_PATH.relative_to(_HERE.parent)} is already up to date")
    else:
        _REPORT_PATH.write_text(rendered, encoding="utf-8")
        print(f"\nwrote {_REPORT_PATH.relative_to(_HERE.parent)}")

    if not all(v.ok for v in verdicts):
        print("\nat least one verdict contradicts the registry", file=sys.stderr)
        return 1 if args.check else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
