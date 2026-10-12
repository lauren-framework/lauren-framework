"""Turn the committed results artifact into the two figures the comparison page shows.

Run it with::

    .venv/bin/python benches/plot_results.py

The page ``docs/comparisons/performance-benchmarks.md`` is built on one rule:
every figure on it must trace back to ``benches/results/frameworks-inprocess.json``.
These charts are no exception.  This script reads *only* that artifact -- the
medians it plots come from ``throughput_rps.median`` and ``latency_ms.p*.median``,
and the error bars come from the ``min``/``max`` that ``spread_pct`` is computed
from -- so a chart can never disagree with the tables beside it.  No measured
value is written down here; if a number is not in the artifact it is not in the
picture.

Two figures, each rendered twice
--------------------------------

* ``bench-throughput-{light,dark}.svg`` -- median requests/second per participant
  across the five workloads, one bar per participant per workload.
* ``bench-latency-{light,dark}.svg`` -- the p50/p95/p99 round trip per participant,
  as small multiples: one panel per workload, three bars per participant.

Each figure is rendered once for the light theme and once for the dark one, and
the page swaps between them with MkDocs Material's ``#only-light`` / ``#only-dark``
classes.  The SVG background is transparent, so a figure sits on whatever the
page background is; only the text, grid and spines change between the two, which
is what keeps them legible on both.

Reproducible output
-------------------

Regenerating must not dirty the tree, or the artifact and the figures it drives
would drift apart on every run.  Three things make the bytes stable: a fixed
``svg.hashsalt`` (matplotlib otherwise salts its element ids per process), a
dateless ``metadata`` block, and stripping the trailing space matplotlib leaves
at the end of every continued line of path data.  That last one is not
cosmetic: the repository's ``trailing-whitespace`` pre-commit hook rewrites
those lines, so left alone a fresh regeneration would differ from the committed
figure by exactly the spaces the hook removes and ``git status`` would never
come back clean.  With all three, two runs on an unchanged artifact produce
byte-identical files -- and those bytes are what the hook would leave behind.

Tight margins
-------------

Each figure is saved with ``bbox_inches="tight"``, so the canvas is the
drawing's own bounding box plus a hairline ``pad_inches`` rather than the whole
``figsize`` rectangle.  ``tight_layout`` has already taken most of the slack out
of the axes, but a band still survives around the artists, and these figures are
placed inline in a document -- the README and the benchmark page -- where every
pixel of that band reads as blank space around the chart.  Cropping to the
artists removes it.  The pad is kept small but non-zero so the ink never touches
the canvas edge.

What the error bars mean
------------------------

Every statistic in the artifact carries ``spread_pct = (max - min) / median * 100``
over the repeats.  The bars here draw that same range directly -- from ``median``
down to ``min`` and up to ``max`` -- rather than a symmetric +/- of the percentage,
because the range is what the reader is being asked to weigh a difference against.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402  (backend must be set first)
from matplotlib.axes import Axes  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

__all__ = [
    "latency_figure",
    "load_artifact",
    "main",
    "render",
    "throughput_figure",
]

_HARNESS_DIR = Path(__file__).resolve().parent
_ROOT = _HARNESS_DIR.parent
_DEFAULT_ARTIFACT = _HARNESS_DIR / "results" / "frameworks-inprocess.json"
_DEFAULT_OUT_DIR = _ROOT / "docs" / "assets"

# The figures are placed inline in a document, so each canvas is cropped to the
# drawing (``bbox_inches="tight"``) and given only this hairline pad, rather than
# the slack the ``figsize`` rectangle leaves around the axes.
_PAD_INCHES = 0.02

# A fixed salt keeps the SVG element ids stable between runs; without it
# matplotlib salts them per process and the file changes every regeneration.
matplotlib.rcParams["svg.hashsalt"] = "lauren-framework-bench"
# Keep text as text rather than glyph outlines: the figures live in docs, so
# they should stay selectable, searchable and legible to a screen reader (and
# they are a fraction of the size).
matplotlib.rcParams["svg.fonttype"] = "none"

_PERCENTILES: tuple[str, ...] = ("p50", "p95", "p99")

# One colour per measured participant, in artifact order (lauren first).  All are
# mid-saturation so they read on both the light and the dark page background.
_FRAMEWORK_COLORS: tuple[str, ...] = (
    "#5E35B1",  # lauren -- the theme's deep purple, so it is easy to find
    "#1E88E5",
    "#00897B",
    "#F4511E",
    "#8E24AA",
    "#6D4C41",
    "#D81B60",
    "#00ACC1",
)

# The floor is a reference, not a competitor: grey and hatched, so it never
# reads as "another framework" that finished on the podium.
_REFERENCE_COLOR = "#9E9E9E"
_REFERENCE_HATCH = "//"

_PERCENTILE_COLORS: dict[str, str] = {
    "p50": "#4C72B0",
    "p95": "#DD8452",
    "p99": "#C44E52",
}


@dataclass(frozen=True)
class Theme:
    """The handful of colours that differ between the light and dark renderings.

    The background is deliberately absent: it stays transparent so the figure
    inherits the page, and only the ink changes.
    """

    name: str
    text: str
    grid: str
    spine: str


_THEMES: dict[str, Theme] = {
    "light": Theme(name="light", text="#1A1A1A", grid="#D9D9D9", spine="#8A8A8A"),
    "dark": Theme(name="dark", text="#E8E8E8", grid="#4A4A55", spine="#9A9AA5"),
}


@dataclass(frozen=True)
class Point:
    """One plotted statistic: the median, and how far it moved either side."""

    median: float
    down: float
    """``median - min`` -- the lower error-bar arm."""

    up: float
    """``max - median`` -- the upper error-bar arm."""


def _point(stat: dict[str, float]) -> Point:
    """Turn one ``{median, min, max, ...}`` block into a plottable point."""
    median = float(stat["median"])
    return Point(
        median=median,
        down=median - float(stat["min"]),
        up=float(stat["max"]) - median,
    )


def load_artifact(path: Path) -> dict[str, Any]:
    """Read the results artifact.

    Raises:
        SystemExit: the file is missing or is not the schema this script expects.
    """
    if not path.exists():
        raise SystemExit(
            f"error: no results artifact at {path}\nrun `nox -s bench_frameworks` first, or pass --artifact"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data.get("frameworks"), list) or not data["frameworks"]:
        raise SystemExit(f"error: {path} has no 'frameworks' list to plot")
    return data


def _participants(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    """The participants, in the order the artifact recorded them."""
    return list(artifact["frameworks"])


def _workload_order(artifact: dict[str, Any]) -> list[str]:
    """The workloads, in the order the artifact recorded them."""
    return list(artifact["frameworks"][0]["workloads"].keys())


def _reference_key(artifact: dict[str, Any]) -> str:
    """The key of the hand-written floor.

    Identified structurally -- it is the one participant with no distribution --
    rather than by name, so it stays correct if the floor is ever renamed.
    """
    for participant in _participants(artifact):
        if not participant.get("dist"):
            return str(participant["key"])
    return ""


def _repeats(artifact: dict[str, Any]) -> int:
    """How many repeats the artifact summarises, for the caption."""
    return int(artifact.get("methodology", {}).get("repeats", 0))


def _not_measured_keys(artifact: dict[str, Any]) -> list[str]:
    """Keys of the frameworks the artifact explicitly did *not* measure."""
    entries = artifact.get("not_measured", [])
    if isinstance(entries, dict):
        return list(entries)
    return [entry["key"] for entry in entries]


def _style_axes(ax: Axes, theme: Theme, *, grid_axis: str = "y") -> None:
    """Apply the theme's ink to one axes, leaving the background transparent."""
    ax.set_facecolor("none")
    ax.tick_params(colors=theme.text, labelsize=9)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(theme.spine)
    ax.grid(True, axis=grid_axis, color=theme.grid, linewidth=0.6, alpha=0.8)
    ax.set_axisbelow(True)


def _colors_for(artifact: dict[str, Any]) -> dict[str, str]:
    """Map each participant key to its bar colour."""
    reference = _reference_key(artifact)
    colors: dict[str, str] = {}
    index = 0
    for participant in _participants(artifact):
        key = str(participant["key"])
        if key == reference:
            colors[key] = _REFERENCE_COLOR
        else:
            colors[key] = _FRAMEWORK_COLORS[index % len(_FRAMEWORK_COLORS)]
            index += 1
    return colors


def _legend_label(participant: dict[str, Any], reference: str) -> str:
    """The legend text for one participant.

    The floor is labelled as a reference so the legend cannot be read as a
    ranking that includes it.
    """
    key = str(participant["key"])
    if key == reference:
        return f"{key} (reference floor)"
    return key


def throughput_figure(artifact: dict[str, Any], theme: Theme) -> Figure:
    """Median throughput per participant across the workloads."""
    participants = _participants(artifact)
    workloads = _workload_order(artifact)
    colors = _colors_for(artifact)
    reference = _reference_key(artifact)

    fig, ax = plt.subplots(figsize=(11.5, 5.6))
    fig.patch.set_alpha(0)

    count = len(participants)
    group_width = 0.82
    bar_width = group_width / count
    centers = list(range(len(workloads)))

    for index, participant in enumerate(participants):
        key = str(participant["key"])
        offset = (index - (count - 1) / 2.0) * bar_width
        points = [_point(participant["workloads"][name]["throughput_rps"]) for name in workloads]
        xs = [center + offset for center in centers]
        is_reference = key == reference
        ax.bar(
            xs,
            [point.median for point in points],
            width=bar_width,
            yerr=[
                [point.down for point in points],
                [point.up for point in points],
            ],
            capsize=1.4,
            error_kw={"ecolor": theme.spine, "elinewidth": 0.7, "alpha": 0.9},
            label=_legend_label(participant, reference),
            color=colors[key],
            edgecolor=theme.spine if not is_reference else "#6E6E6E",
            linewidth=0.5,
            hatch=_REFERENCE_HATCH if is_reference else None,
            zorder=3,
        )

    ax.set_xticks(centers)
    ax.set_xticklabels(workloads)
    ax.set_ylabel("requests / second", color=theme.text)
    ax.set_title(
        "Median throughput by workload (higher is better)",
        color=theme.text,
        fontsize=12,
        pad=10,
    )
    _style_axes(ax, theme)
    ax.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, -0.12),
        ncol=5,
        frameon=False,
        labelcolor=theme.text,
        fontsize=8.5,
    )
    fig.text(
        0.5,
        0.005,
        f"Error bars span the min-max range across the {_repeats(artifact)} repeats.",
        ha="center",
        fontsize=8,
        color=theme.text,
        alpha=0.85,
    )
    fig.tight_layout(rect=(0, 0.10, 1, 1))
    return fig


def latency_figure(artifact: dict[str, Any], theme: Theme) -> Figure:
    """p50/p95/p99 round trip per participant, one panel per workload."""
    participants = _participants(artifact)
    workloads = _workload_order(artifact)
    colors = _colors_for(artifact)
    reference = _reference_key(artifact)

    columns = 2
    rows = (len(workloads) + columns - 1) // columns
    fig, axes = plt.subplots(rows, columns, figsize=(14.0, 12.0), sharey=True)
    fig.patch.set_alpha(0)
    flat = list(axes.flat) if hasattr(axes, "flat") else [axes]

    count = len(participants)
    bar_width = 0.24
    percentile_offsets = {
        percentile: (index - (len(_PERCENTILES) - 1) / 2.0) * bar_width
        for index, percentile in enumerate(_PERCENTILES)
    }
    centers = list(range(count))

    for panel_index, name in enumerate(workloads):
        ax = flat[panel_index]
        for participant_index, participant in enumerate(participants):
            key = str(participant["key"])
            is_reference = key == reference
            for percentile in _PERCENTILES:
                stat = participant["workloads"][name]["latency_ms"][percentile]
                point = _point(stat)
                ax.bar(
                    participant_index + percentile_offsets[percentile],
                    point.median,
                    width=bar_width,
                    yerr=[[point.down], [point.up]],
                    capsize=1.0,
                    error_kw={"ecolor": theme.spine, "elinewidth": 0.6, "alpha": 0.9},
                    color=colors[key] if not is_reference else _REFERENCE_COLOR,
                    edgecolor=theme.spine if not is_reference else "#6E6E6E",
                    linewidth=0.5,
                    hatch=_REFERENCE_HATCH if is_reference else None,
                    zorder=3,
                )

        ax.set_title(name, color=theme.text, fontsize=11, pad=6)
        ax.set_xticks(centers)
        ax.set_xticklabels(
            [str(participant["key"]) for participant in participants],
            rotation=45,
            ha="right",
        )
        _style_axes(ax, theme)

    for panel_index in range(len(workloads), len(flat)):
        flat[panel_index].set_visible(False)

    for row in range(rows):
        flat[row * columns].set_ylabel("round-trip latency (ms)", color=theme.text)

    fig.suptitle(
        "In-process round-trip latency by workload (lower is better)",
        color=theme.text,
        fontsize=12,
    )
    handles = [
        plt.Rectangle((0, 0), 1, 1, color=_PERCENTILE_COLORS[percentile]) for percentile in _PERCENTILES
    ]
    fig.legend(
        handles,
        [f"{percentile} (median)" for percentile in _PERCENTILES],
        loc="upper center",
        bbox_to_anchor=(0.5, 0.945),
        ncol=3,
        frameon=False,
        labelcolor=theme.text,
        fontsize=9,
    )
    fig.text(
        0.5,
        0.01,
        f"Each panel is one workload; bars are the median of {_repeats(artifact)} repeats, "
        "error bars span the min-max range. The hatched group is the reference floor.",
        ha="center",
        fontsize=8,
        color=theme.text,
        alpha=0.85,
    )
    fig.tight_layout(rect=(0, 0.03, 1, 0.93))
    return fig


def _strip_trailing_whitespace(path: Path) -> None:
    """Trim the trailing space matplotlib leaves on continued path-data lines.

    Whitespace is only ever a separator inside an SVG ``d`` attribute, so
    dropping it changes nothing the renderer sees -- but the repository's
    ``trailing-whitespace`` pre-commit hook drops it too, and a file that the
    hook would rewrite is a file that shows up as a diff after every
    regeneration.  Normalising here keeps the working tree clean.
    """
    text = path.read_text(encoding="utf-8")
    trimmed = "\n".join(line.rstrip() for line in text.split("\n"))
    if trimmed != text:
        path.write_text(trimmed, encoding="utf-8")


def render(artifact_path: Path, out_dir: Path) -> list[Path]:
    """Write both figures, light and dark, and return the paths written."""
    artifact = load_artifact(artifact_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    builders = (("throughput", throughput_figure), ("latency", latency_figure))
    written: list[Path] = []
    for figure_name, builder in builders:
        for theme_name, theme in _THEMES.items():
            fig = builder(artifact, theme)
            destination = out_dir / f"bench-{figure_name}-{theme_name}.svg"
            fig.savefig(
                destination,
                format="svg",
                transparent=True,
                metadata={"Date": None},
                bbox_inches="tight",
                pad_inches=_PAD_INCHES,
            )
            plt.close(fig)
            _strip_trailing_whitespace(destination)
            written.append(destination)
    return written


def main(argv: list[str] | None = None) -> int:
    """Render the figures from the artifact and print where they landed."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--artifact",
        default=str(_DEFAULT_ARTIFACT),
        help=f"results artifact to read (default: {_DEFAULT_ARTIFACT})",
    )
    parser.add_argument(
        "--out-dir",
        default=str(_DEFAULT_OUT_DIR),
        help=f"directory to write the SVGs into (default: {_DEFAULT_OUT_DIR})",
    )
    args = parser.parse_args(argv)

    artifact = load_artifact(Path(args.artifact))
    missing = [
        key
        for key in _not_measured_keys(artifact)
        if key in {str(participant["key"]) for participant in _participants(artifact)}
    ]
    if missing:  # pragma: no cover - a guard, not a path the artifact takes
        print(
            f"error: not-measured framework(s) present in the results: {', '.join(missing)}",
            file=sys.stderr,
        )
        return 1

    written = render(Path(args.artifact), Path(args.out_dir))
    for path in written:
        print(f"wrote {path}")
    print(
        f"\n{len(written)} figure(s) from {args.artifact} "
        f"({len(_participants(artifact))} participants, {len(_workload_order(artifact))} workloads)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
