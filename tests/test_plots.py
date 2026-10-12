"""Tests for ``plots.py``: PNG size and caps, no pyplot import, and rendering from threads."""

from __future__ import annotations

import struct
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from agentic_fmi.errors import UserError
from agentic_fmi.plots import Line, Panel, render_png

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_BYTES = 200_000


def _png_size(png: bytes) -> tuple[int, int]:
    """Width and height from the IHDR chunk."""
    assert png.startswith(PNG_MAGIC)
    width, height = struct.unpack(">II", png[16:24])
    return width, height


def _line(label: str, n: int = 500, seed: int = 0, noise: float = 0.0) -> Line:
    rng = np.random.default_rng(seed)
    t = np.linspace(0.0, 10.0, n)
    return Line(label, t, np.sin(t + seed) + noise * rng.standard_normal(n))


def _panels(n_panels: int, n_lines: int, n_points: int = 500, noise: float = 0.0) -> list[Panel]:
    return [
        Panel(
            f"var{i}",
            "m" if i % 2 == 0 else None,
            [_line(f"r{j + 1} (e=0.{j})", n_points, 10 * i + j, noise) for j in range(n_lines)],
        )
        for i in range(n_panels)
    ]


def test_single_panel_renders_a_small_png_of_the_documented_size() -> None:
    png = render_png(_panels(1, 1), None, MAX_BYTES)
    assert png.startswith(PNG_MAGIC)
    assert len(png) <= MAX_BYTES
    assert _png_size(png) == (900, 250)


def test_size_grows_with_panels_and_title() -> None:
    assert _png_size(render_png(_panels(3, 1), None, MAX_BYTES)) == (900, 750)
    assert _png_size(render_png(_panels(3, 1), "Bouncing ball", MAX_BYTES))[1] == 800


def test_largest_allowed_plot_with_noisy_data_fits_the_byte_limit() -> None:
    png = render_png(_panels(6, 6, n_points=3_000, noise=0.5), "6 runs x 6 variables", MAX_BYTES)
    assert len(png) <= MAX_BYTES
    assert png.startswith(PNG_MAGIC)


def test_lower_dpi_is_used_when_the_first_render_is_too_big() -> None:
    panels = _panels(6, 6, n_points=3_000, noise=0.5)
    full = render_png(panels, None, 10_000_000)
    smaller = render_png(panels, None, len(full) // 2)
    assert len(smaller) <= len(full) // 2
    assert _png_size(smaller)[0] < _png_size(full)[0]


def test_impossible_limit_raises_value_error() -> None:
    with pytest.raises(ValueError, match="fewer runs or variables"):
        render_png(_panels(2, 2), None, 100)


def test_no_panels_raises_value_error() -> None:
    with pytest.raises(ValueError, match="at least one panel"):
        render_png([], None, MAX_BYTES)


def test_more_than_six_variables_is_a_user_error() -> None:
    with pytest.raises(UserError, match="at most 6 variables"):
        render_png(_panels(7, 1), None, MAX_BYTES)


def test_more_than_six_runs_is_a_user_error() -> None:
    with pytest.raises(UserError, match="at most 6 runs"):
        render_png(_panels(1, 7), None, MAX_BYTES)


def test_caps_can_be_overridden_by_keyword() -> None:
    with pytest.raises(UserError, match="at most 1 variables"):
        render_png(_panels(2, 1), None, MAX_BYTES, max_panels=1)
    with pytest.raises(UserError, match="at most 1 runs"):
        render_png(_panels(1, 2), None, MAX_BYTES, max_lines=1)
    assert render_png(_panels(7, 1), None, 10_000_000, max_panels=7).startswith(PNG_MAGIC)


def test_huge_series_are_downsampled_before_drawing() -> None:
    t = np.linspace(0.0, 100.0, 2_000_000)
    panel = Panel("x", "m", [Line("r1", t, np.sin(t))])
    assert len(render_png([panel], None, MAX_BYTES)) <= MAX_BYTES


def test_odd_data_does_not_break_rendering() -> None:
    t = np.array([0.0, 1.0, 1.0, 2.0, 3.0])
    lines = [
        Line("nan", t, np.array([0.0, np.nan, 1.0, np.nan, 2.0])),
        Line("all nan", t, np.full(5, np.nan)),
        Line("event", t, np.array([1.0, 1.0, 5.0, 5.0, 5.0])),
        Line("bool", t, np.array([True, True, False, False, True])),
        Line("empty", np.empty(0), np.empty(0)),
        Line("inf", t, np.array([0.0, np.inf, 1.0, -np.inf, 2.0])),
    ]
    assert render_png([Panel("x", None, lines)], "odd", MAX_BYTES).startswith(PNG_MAGIC)


def test_panel_without_lines_renders() -> None:
    assert render_png([Panel("x", "m", [])], None, MAX_BYTES).startswith(PNG_MAGIC)


def test_pyplot_is_never_imported() -> None:
    code = textwrap.dedent(
        """
        import sys
        import numpy as np
        from agentic_fmi.plots import Line, Panel, render_png

        t = np.linspace(0.0, 1.0, 100)
        png = render_png([Panel("x", "m", [Line("r1", t, t)])], "title", 200_000)
        assert png.startswith(b"\\x89PNG")
        assert "matplotlib.pyplot" not in sys.modules, "pyplot was imported"
        print("ok")
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"


def test_four_threads_render_concurrently_and_match_a_serial_render() -> None:
    panels = _panels(2, 2, n_points=500, noise=0.2)
    expected = render_png(panels, "threads", MAX_BYTES)

    def render_many(_: int) -> list[bytes]:
        return [render_png(panels, "threads", MAX_BYTES) for _ in range(3)]

    with ThreadPoolExecutor(max_workers=4) as pool:
        batches = list(pool.map(render_many, range(4)))
    assert all(png == expected for batch in batches for png in batch)


def test_threads_with_different_content_each_get_their_own_picture() -> None:
    def render_one(index: int) -> bytes:
        return render_png(_panels(1 + index % 3, 2), f"plot {index}", MAX_BYTES)

    with ThreadPoolExecutor(max_workers=4) as pool:
        pngs = list(pool.map(render_one, range(12)))
    for index, png in enumerate(pngs):
        assert _png_size(png) == (900, 250 * (1 + index % 3) + 50)
