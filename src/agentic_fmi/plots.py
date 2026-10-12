"""Render PNG plots with matplotlib's object API (``Figure`` + ``FigureCanvasAgg``).

Never import ``matplotlib.pyplot``: it keeps global state and isn't thread-safe, and MCP sync
tools run on worker threads. One subplot per variable, one line per run, shared time axis.

Collaborators: ``runs.py`` (``run_plot``) builds the panels from result files and saves the
PNG under ``runs/<id>/plots/``; ``analysis/downsample.py`` bounds the points per line;
``config.Limits`` supplies the default panel and line caps.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from agentic_fmi.analysis.downsample import lttb_indices
from agentic_fmi.config import Limits
from agentic_fmi.errors import UserError

_WIDTH_IN = 9.0
_PANEL_HEIGHT_IN = 2.5
_TITLE_HEIGHT_IN = 0.5
_START_DPI = 100
_MIN_DPI = 40
_DPI_FACTOR = 0.8
"""Each re-render uses this fraction of the previous dpi until the PNG fits."""
_MAX_LINE_POINTS = 2000
_DEFAULT_LIMITS = Limits()


@dataclass(frozen=True)
class Line:
    """One run's trace of one variable."""

    label: str
    """Legend text, e.g. ``r3 (e=0.5)``."""
    t: np.ndarray
    y: np.ndarray


@dataclass(frozen=True)
class Panel:
    """One subplot: a variable across runs."""

    variable: str
    unit: str | None
    lines: Sequence[Line]


def render_png(
    panels: Sequence[Panel],
    title: str | None,
    max_bytes: int,
    *,
    max_panels: int = _DEFAULT_LIMITS.plot_max_variables,
    max_lines: int = _DEFAULT_LIMITS.plot_max_runs,
) -> bytes:
    """Draw the panels stacked vertically and return PNG bytes of at most ``max_bytes``.

    Starts at about 900 px wide and 250 px per panel at 100 dpi; if the PNG is too large,
    re-renders at a lower dpi. Lines are downsampled to at most 2000 points each. The caps
    ``max_panels`` (variables) and ``max_lines`` (runs per panel) default to the
    ``config.Limits`` values.

    Raises:
        ValueError: no panels, or the PNG can't be brought under ``max_bytes``.
        UserError: more than ``max_panels`` panels or ``max_lines`` lines in a panel.
    """
    if not panels:
        raise ValueError("render_png needs at least one panel")
    _check_caps(panels, max_panels, max_lines)
    figure = _draw(panels, title)
    dpi = _START_DPI
    png = _encode(figure, dpi)
    while len(png) > max_bytes:
        if int(dpi * _DPI_FACTOR) < _MIN_DPI:
            raise ValueError(
                f"The plot is {len(png)} bytes even at {dpi} dpi, over the {max_bytes} byte "
                "limit; plot fewer runs or variables."
            )
        dpi = int(dpi * _DPI_FACTOR)
        png = _encode(figure, dpi)
    return png


def _check_caps(panels: Sequence[Panel], max_panels: int, max_lines: int) -> None:
    if len(panels) > max_panels:
        raise UserError(
            f"A plot shows at most {max_panels} variables, got {len(panels)}. "
            "Split them over several plots."
        )
    most_lines = max(len(panel.lines) for panel in panels)
    if most_lines > max_lines:
        raise UserError(
            f"A plot shows at most {max_lines} runs, got {most_lines}. Plot fewer runs per call."
        )


def _draw(panels: Sequence[Panel], title: str | None) -> Figure:
    height = _PANEL_HEIGHT_IN * len(panels) + (_TITLE_HEIGHT_IN if title else 0.0)
    figure = Figure(figsize=(_WIDTH_IN, height), dpi=_START_DPI, layout="constrained")
    FigureCanvasAgg(figure)
    axes = figure.subplots(len(panels), 1, sharex=True, squeeze=False)[:, 0]
    for axis, panel in zip(axes, panels, strict=True):
        for line in panel.lines:
            t, y = _bounded(line)
            axis.plot(t, y, label=line.label, linewidth=1.2)
        axis.set_ylabel(f"{panel.variable} [{panel.unit}]" if panel.unit else panel.variable)
        axis.grid(True, alpha=0.3)
    if panels[0].lines:
        axes[0].legend(loc="best", fontsize="small")
    axes[-1].set_xlabel("time [s]")
    if title:
        figure.suptitle(title, fontsize="medium")
    return figure


def _bounded(line: Line) -> tuple[np.ndarray, np.ndarray]:
    """The line's points, reduced to at most ``_MAX_LINE_POINTS`` with LTTB."""
    t = np.asarray(line.t, dtype=np.float64)
    y = np.asarray(line.y, dtype=np.float64)
    if t.size <= _MAX_LINE_POINTS:
        return t, y
    keep = lttb_indices(t, y, _MAX_LINE_POINTS)
    return t[keep], y[keep]


def _encode(figure: Figure, dpi: int) -> bytes:
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=dpi)
    return buffer.getvalue()
