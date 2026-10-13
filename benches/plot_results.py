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

Mirrored into the assets repository
-----------------------------------

The README shows these same four files, but it cannot reference them here.  The
README doubles as the PyPI long description (``pyproject.toml`` sets
``readme = "README.md"``), so every image in it must be an absolute URL, and an
absolute URL into this repository only resolves on the default branch -- a
reader looking at a feature branch would get a broken image.  The README
therefore points at a copy kept in the org's separate assets repository,
``lauren-framework/lauren-assets``, under ``framework/benchmarks/``.  That copy
is not written by this script: a regeneration only rewrites ``docs/assets/``, so
the four files have to be copied there byte-for-byte, committed and pushed
afterwards, or the README keeps showing the previous run.  Copy them as they
are -- no re-encoding, no re-rendering -- so the figure the README shows and the
figure the benchmark page shows stay the same drawing.

What the error bars mean
------------------------

Every statistic in the artifact carries ``spread_pct = (max - min) / median * 100``
over the repeats.  The bars here draw that same range directly -- from ``median``
down to ``min`` and up to ``max`` -- rather than a symmetric +/- of the percentage,
because the range is what the reader is being asked to weigh a difference against.

The capped latency axis
-----------------------

The latency panels share one y-axis, and a single slow outlier -- one framework's
``validation`` p99, far above every other range in the artifact -- would otherwise
set the scale for all five workloads and squash every other bar into the bottom of
the canvas.  Error bars are therefore capped at the tallest range that is not a far
outlier (Tukey's fence; see ``_LATENCY_FAR_OUTLIER_FACTOR``), and a bar whose
range crosses that cap is still drawn, and still honest: its error bar stops at
the cap, a break marker sits on the cap, and the range's true maximum is written
above the marker.  The caption names the cap and the clipped maximum as well, so
a reader who never looks at the marker still learns that something was cut.  The
cap only ever cuts an error-bar tip -- never a bar's own height -- and the note is
derived from the ceiling the figure draws, so it cannot drift from the picture.
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

# A lone slow outlier must not set the scale for the whole figure.  The shared
# latency axis is capped at the tallest upper error bound that is not a *far*
# outlier, so one 10.5 ms spike cannot squash the other four workloads into the
# bottom tenth of the canvas.  Tukey's fence is used rather than a percentile
# because a percentile cap is only as definite as the convention behind it: on
# the current artifact the 99th percentile of these 135 bounds is 2.69 ms under
# the linear interpolation this file uses (also numpy's default) but 7.70 ms
# under the exclusive one (``statistics.quantiles(bounds, n=100)[98]``).  A 5 ms
# swing that decides whether the waste is removed is not a basis for a limit.
# The fence's own value moves with the convention as well (2.771 ms vs 2.777 ms
# here), but its *decision* does not: both conventions keep the same 134 of 135
# bounds and the same 2.70 ms ceiling, so it is used instead.
_LATENCY_FAR_OUTLIER_FACTOR = 3.0
# Space kept above the ceiling, so the true value of a clipped bar can be written
# beside its break marker instead of on top of the bar.
_LATENCY_CEILING_HEADROOM = 1.08

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


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """Linear-interpolated percentile of an already-sorted list."""
    last = len(sorted_values) - 1
    position = fraction * last
    lower = int(position)
    upper = min(lower + 1, last)
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (position - lower)


def _latency_bars(artifact: dict[str, Any]) -> list[tuple[float, float]]:
    """``(median, upper error bound)`` for every latency bar the figure draws."""
    bars: list[tuple[float, float]] = []
    for participant in _participants(artifact):
        for workload in _workload_order(artifact):
            for percentile in _PERCENTILES:
                point = _point(participant["workloads"][workload]["latency_ms"][percentile])
                bars.append((point.median, point.median + point.up))
    return bars


def _latency_ceiling(artifact: dict[str, Any]) -> float:
    """The tallest upper error bound the shared latency axis has to show.

    Anything above it is a far outlier -- still drawn, but clipped, with its true
    value written at the break so the reader can see it rather than guess at it.
    The ceiling never falls below the tallest *bar*: a bar whose own height is cut
    off would be worse than a tall axis, so the body is always fully visible and
    only error-bar tips can be clipped.
    """
    bars = _latency_bars(artifact)
    bounds = sorted(bound for _, bound in bars)
    quartile_one = _percentile(bounds, 0.25)
    quartile_three = _percentile(bounds, 0.75)
    fence = quartile_three + _LATENCY_FAR_OUTLIER_FACTOR * (quartile_three - quartile_one)
    inliers = [bound for bound in bounds if bound <= fence]
    return max(max(inliers or bounds), max(median for median, _ in bars))


def _latency_cap_note(artifact: dict[str, Any], ceiling: float) -> str:
    """The caption clause naming the error bars the axis cap cuts off.

    Empty when nothing is clipped, so an artifact that fits the axis is never
    described as if it had been cut -- and derived from the same ceiling the
    figure draws, so the sentence cannot drift from the picture.
    """
    cut = sorted(bound for _, bound in _latency_bars(artifact) if bound > ceiling)
    if not cut:
        return ""
    one = len(cut) == 1
    ranges = ("one range" if one else f"{len(cut)} ranges").capitalize()
    return (
        f"  {ranges} {'is' if one else 'are'} cut off at the {ceiling:.1f} ms cap, because "
        f"{'it' if one else 'they'} would otherwise flatten the rest of the figure; the true "
        f"maximum ({cut[-1]:.1f} ms) is written above the break."
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
    """p50/p95/p99 round trip per participant, one panel per workload.

    The shared y-axis is capped so one far outlier cannot set the scale for every
    workload.  A range that crosses the cap is still drawn, but clipped at it and
    marked with a break, with its true maximum written above the marker; the
    caption says the same thing in words (see ``_latency_ceiling``).
    """
    participants = _participants(artifact)
    workloads = _workload_order(artifact)
    colors = _colors_for(artifact)
    reference = _reference_key(artifact)
    # The panels share one y-axis, so this single number is the scale of the whole
    # figure -- and the headroom above it is where a clipped bar's value is written.
    ceiling = _latency_ceiling(artifact)
    axis_top = ceiling * _LATENCY_CEILING_HEADROOM

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
                top = point.median + point.up
                clipped = top > ceiling
                column = participant_index + percentile_offsets[percentile]
                ax.bar(
                    column,
                    point.median,
                    width=bar_width,
                    # A far outlier stops at the ceiling; the marker and the
                    # annotation below are what say so, and by how much.
                    yerr=[[point.down], [min(point.up, ceiling - point.median)]],
                    capsize=1.0,
                    error_kw={"ecolor": theme.spine, "elinewidth": 0.6, "alpha": 0.9},
                    color=colors[key] if not is_reference else _REFERENCE_COLOR,
                    edgecolor=theme.spine if not is_reference else "#6E6E6E",
                    linewidth=0.5,
                    hatch=_REFERENCE_HATCH if is_reference else None,
                    zorder=3,
                )
                if clipped:
                    ax.plot(
                        [column],
                        [ceiling],
                        marker="^",
                        markersize=4.5,
                        linestyle="none",
                        color=theme.spine,
                        zorder=4,
                    )
                    ax.annotate(
                        f"{top:.1f}",
                        (column, ceiling),
                        xytext=(0, 3),
                        textcoords="offset points",
                        ha="center",
                        va="bottom",
                        fontsize=7,
                        color=theme.text,
                        zorder=5,
                    )

        ax.set_title(name, color=theme.text, fontsize=11, pad=6)
        ax.set_xticks(centers)
        ax.set_xticklabels(
            [str(participant["key"]) for participant in participants],
            rotation=45,
            ha="right",
        )
        ax.set_ylim(0.0, axis_top)
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
    # Colour is the participant's, not the percentile's: the three bars in a group
    # differ by *position*, so the percentile key is drawn as an unfilled swatch
    # rather than painted in a colour that appears nowhere on the figure, and the
    # colours the figure does use are given the key they were missing.
    percentile_handles = [
        plt.Rectangle((0, 0), 1, 1, facecolor="none", edgecolor=theme.text, linewidth=0.8)
        for _ in _PERCENTILES
    ]
    fig.legend(
        percentile_handles,
        [f"{percentile} (median)" for percentile in _PERCENTILES],
        loc="upper center",
        bbox_to_anchor=(0.5, 0.955),
        ncol=3,
        frameon=False,
        labelcolor=theme.text,
        fontsize=9,
    )
    # Swatches copied from the bars themselves -- same fill, edge and hatch -- so
    # the key cannot drift from what is drawn.
    participant_handles = [
        plt.Rectangle(
            (0, 0),
            1,
            1,
            facecolor=colors[key],
            edgecolor="#6E6E6E" if key == reference else theme.spine,
            linewidth=0.5,
            hatch=_REFERENCE_HATCH if key == reference else None,
        )
        for key in (str(participant["key"]) for participant in participants)
    ]
    fig.legend(
        participant_handles,
        [_legend_label(participant, reference) for participant in participants],
        loc="upper center",
        bbox_to_anchor=(0.5, 0.925),
        ncol=len(participants),
        frameon=False,
        labelcolor=theme.text,
        fontsize=8.5,
    )
    fig.text(
        0.5,
        0.008,
        f"Each panel is one workload; within a group the bars are p50, p95 and p99, left to right.  "
        f"Colour marks the participant and the hatched group is the reference floor.\n"
        f"Error bars span the min-max range over the {_repeats(artifact)} repeats."
        + _latency_cap_note(artifact, ceiling),
        ha="center",
        va="bottom",
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
