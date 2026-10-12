"""The Phase 1 exit demo, in process: inspect BouncingBall, run it twice, compare, plot, read.

Collaborators: the ``session`` fixture (``tests/mcp/conftest.py``: the real server and real worker
processes behind an in-memory MCP client) and the ``fmu`` fixture (a private BouncingBall copy).
Every number asserted here comes from runs that really happened; the apex of the first rebound
of a ball dropped from 1 m is ``e ** 2`` metres.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp.types import ImageContent, TextContent
from mcp_support import PNG_SIGNATURE, Session, text_of

BALL = "3.0/BouncingBall.fmu"
FmuFactory = Callable[[str, str], Path]


def drop_ball(session: Session, e: float) -> dict[str, Any]:
    """Run BouncingBall for 3 s with restitution ``e`` and wait for the end."""
    return session.ok("run_start", fmu=BALL, start_values={"e": e}, stop_time=3.0, wait_s=30)


@pytest.fixture
def two_runs(session: Session, fmu: FmuFactory) -> tuple[dict[str, Any], dict[str, Any]]:
    fmu("3.0", "BouncingBall")
    return drop_ball(session, 0.5), drop_ball(session, 0.8)


def test_inspect_then_two_runs_finish_with_summaries(session: Session, fmu: FmuFactory) -> None:
    fmu("3.0", "BouncingBall")
    info = session.ok("fmu_inspect", fmu=BALL)
    assert info["fmi_version"] == "3.0"
    assert info["runnability"]["runnable"] is True
    assert {p["name"] for p in info["parameters"]} >= {"e", "g"}
    first, second = drop_ball(session, 0.5), drop_ball(session, 0.8)
    assert (first["run_id"], second["run_id"]) == ("r1", "r2")
    for view in (first, second):
        assert view["state"] == "finished"
        assert view["fmu"] == BALL
        assert view["progress"] == 1.0
        assert "run_plot" in view["hint"]
        by_name = {v["name"]: v for v in view["summary"]["variables"]}
        assert by_name["h"]["max"] == 1.0
        assert by_name["h"]["unit"] == "m"
        assert view["summary"]["t_end"] == 3.0


def test_compare_reports_the_e_difference_and_height_metrics(
    session: Session, two_runs: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    comparison = session.ok("run_compare", run_ids=["r1", "r2"])
    assert comparison["baseline"] == "r1"
    differences = {d["field"]: d["values"] for d in comparison["spec_differences"]}
    assert differences["start_values.e"] == {"r1": 0.5, "r2": 0.8}
    assert "fmu.sha256" not in differences
    by_name = {v["name"]: v for v in comparison["variables"]}
    assert {"h", "v"} <= set(by_name)
    heights = {m["run_id"]: m for m in by_name["h"]["runs"]}
    assert heights["r1"]["max"] == heights["r2"]["max"] == 1.0
    assert heights["r1"]["min"] == pytest.approx(0.0, abs=0.01)
    assert heights["r2"]["max_abs_diff"] > 0.1
    assert heights["r2"]["rms_diff"] > 0.05
    assert "max_abs_diff" not in heights["r1"]


def test_results_show_the_rebound_apex_and_respect_max_points(
    session: Session, two_runs: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    apexes = {}
    for run_id, e in (("r1", 0.5), ("r2", 0.8)):
        window = session.ok(
            "run_results", run_id=run_id, variables=["h"], start_time=0.47, stop_time=1.6
        )
        (series,) = window["series"]
        apexes[e] = max(y for y in series["y"] if y is not None)
        assert series["unit"] == "m"
    assert apexes[0.5] == pytest.approx(0.25, abs=0.01)
    assert apexes[0.8] == pytest.approx(0.64, abs=0.01)

    whole = session.ok("run_results", run_id="r1", variables=["h", "v"], max_points=20)
    assert whole["n_points_total"] == 751
    assert whole["t_start"] == 0.0
    assert whole["t_end"] == 3.0
    assert [s["name"] for s in whole["series"]] == ["h", "v"]
    for series in whole["series"]:
        assert 4 <= len(series["t"]) == len(series["y"]) <= 20
        assert series["t"][0] == 0.0
        assert series["t"][-1] == 3.0
    default = session.ok("run_results", run_id="r1")
    assert default["n_points_total"] == 751
    assert all(len(s["t"]) <= 100 for s in default["series"])
    assert len(default["series"]) == 3  # h, v and h_ft, the FMU's outputs


def test_plot_is_one_png_with_no_structured_content(
    session: Session, two_runs: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    result = session.call("run_plot", run_ids=["r1", "r2"], variables=["h"], title="e: 0.5 vs 0.8")
    assert not result.is_error, text_of(result)
    assert result.structured_content is None
    images = [c for c in result.content if isinstance(c, ImageContent)]
    assert len(images) == 1
    assert images[0].mime_type == "image/png"
    png = base64.b64decode(images[0].data)
    assert png.startswith(PNG_SIGNATURE)
    assert len(png) <= 200_000
    captions = [c.text for c in result.content if isinstance(c, TextContent)]
    assert len(captions) == 1
    assert captions[0].startswith("h for r1, r2") and "plots/h__r1+r2.png" in captions[0]
    saved = session.config.store_dir / "runs" / "r1" / "plots" / "h__r1+r2.png"
    assert saved.read_bytes() == png
    assert list(saved.parent.glob("*.tmp-*")) == []
