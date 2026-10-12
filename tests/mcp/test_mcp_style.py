"""Response styles through the in-memory MCP client: the ``response_style`` tool, its file in the
store, and what the run, build and live-control tools return in ninja mode.

Collaborators: ``mcp_support.Session`` (the ``session`` fixture, and ``open_session`` for a second
server on the same store), the ``fmu`` and ``workspace`` fixtures, and
``skills/fmu-authoring/examples/pi_controller.py`` (built by a real worker with pythonfmu, about a
second). Runs are real workers; the session's cleanup stops any a test leaves.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp_support import Session, open_session

from agentic_fmi.config import Config
from agentic_fmi.store import runstore

BALL = "3.0/BouncingBall.fmu"
FmuFactory = Callable[[str, str], Path]
EXAMPLE = Path(__file__).resolve().parents[2] / "skills" / "fmu-authoring" / "examples"
NINJA_RULE = "reply with this line only; give details only if the user asks."


def _ninja(session: Session) -> None:
    assert session.ok("response_style", style="ninja")["style"] == "ninja"


def _preferences(session: Session) -> Path:
    return session.config.store_dir / runstore.PREFERENCES_FILE


def test_the_default_is_talkative_and_the_tool_reports_it(session: Session) -> None:
    assert session.ok("response_style") == {
        "style": "talkative",
        "line": "Talkative mode on: full summaries.",
    }
    assert not _preferences(session).exists()  # asking writes nothing


def test_the_style_is_set_reported_and_kept_in_the_store(
    session: Session, mcp_config: Config
) -> None:
    assert session.ok("response_style", style="ninja") == {
        "style": "ninja",
        "line": "🥷 Ninja mode on: status lines only.",
    }
    assert session.ok("response_style")["style"] == "ninja"
    assert _preferences(session).read_text(encoding="utf-8") == (
        '{"protocol":1,"response_style":"ninja"}\n'
    )
    with open_session(mcp_config) as later:  # a new server on the same store keeps the choice
        assert later.ok("response_style")["style"] == "ninja"
        assert later.ok("response_style", style="talkative")["style"] == "talkative"
    assert session.ok("response_style")["style"] == "talkative"


def test_an_unknown_style_is_refused_by_the_schema(session: Session) -> None:
    message = session.error("response_style", style="loud")
    assert "ninja" in message and "talkative" in message
    assert not _preferences(session).exists()


@pytest.mark.parametrize("text", ["{not json", '{"protocol":1,"response_style":"loud"}', ""])
def test_a_corrupt_file_means_talkative_and_never_fails_a_tool(
    session: Session, fmu: FmuFactory, text: str
) -> None:
    fmu("3.0", "BouncingBall")
    _preferences(session).write_text(text, encoding="utf-8")
    assert session.ok("response_style")["style"] == "talkative"
    view = session.ok("run_start", fmu=BALL, stop_time=1.0, wait_s=30)
    assert view["state"] == "finished" and "summary" in view
    assert session.ok("response_style", style="ninja")["style"] == "ninja"  # and it is repaired


def test_a_preferences_path_that_cannot_be_written_is_a_clear_error(session: Session) -> None:
    _preferences(session).mkdir()
    assert session.ok("response_style")["style"] == "talkative"
    message = session.error("response_style", style="ninja")
    assert "File system problem" in message and "writable" in message


def test_talkative_runs_keep_their_summary_and_hint(session: Session, fmu: FmuFactory) -> None:
    fmu("3.0", "BouncingBall")
    view = session.ok("run_start", fmu=BALL, stop_time=1.0, wait_s=30)
    assert view["state"] == "finished"
    assert view["summary"]["variables"], view
    assert view["hint"].startswith("Use run_plot or run_results on 'r1'")
    assert session.ok("run_status", run_id="r1")["summary"] == view["summary"]


def test_ninja_run_start_and_run_status_return_one_line(session: Session, fmu: FmuFactory) -> None:
    fmu("3.0", "BouncingBall")
    _ninja(session)
    view = session.ok("run_start", fmu=BALL, stop_time=1.0, wait_s=30)
    assert view["state"] == "finished" and view["t"] == 1.0
    assert "summary" not in view and "build" not in view
    assert view["hint"] == f"🥷 r1 done: BouncingBall, t=1 s. Ninja mode: {NINJA_RULE}"
    status = session.ok("run_status", run_id="r1")
    assert "summary" not in status
    assert status["hint"] == view["hint"]
    # The results tools are not styled: the data is still there when the user asks for it.
    series = session.ok("run_results", run_id="r1", variables=["h"])["series"]
    assert series[0]["name"] == "h" and series[0]["y"]
    assert session.ok("run_list")["items"][0]["run_id"] == "r1"


def test_ninja_keeps_the_error_code_and_message_but_not_the_log_tail(
    session: Session, fmu: FmuFactory
) -> None:
    fmu("3.0", "BouncingBall")
    bad: dict[str, Any] = {"fmu": BALL, "start_values": {"e": "abc"}, "stop_time": 1.0}
    talkative = session.ok("run_start", wait_s=30, **bad)
    assert talkative["state"] == "failed" and talkative["error"]["message"]
    _ninja(session)
    ninja = session.ok("run_start", wait_s=30, **bad)
    assert ninja["error"]["code"] == "simulation_error" and ninja["error"]["message"]
    assert "log_tail" not in ninja["error"]
    assert ninja["hint"].startswith("🥷 r2 failed: simulation_error. ")
    assert "log_tail" not in session.ok("run_status", run_id="r1")["error"]


def test_ninja_run_stop_says_where_the_run_stopped(session: Session, fmu: FmuFactory) -> None:
    fmu("3.0", "BouncingBall")
    _ninja(session)
    started = session.ok("run_start", fmu=BALL, stop_time=1000.0, output_interval=0.001, wait_s=0)
    session.wait_for(started["run_id"], {"running"})
    stopped = session.ok("run_stop", run_id=started["run_id"])
    assert stopped["state"] == "stopped" and "summary" not in stopped
    assert stopped["hint"].startswith("🥷 r1 stopped at t=")


def test_ninja_live_control_returns_status_lines_and_keeps_requested_values(
    session: Session, fmu: FmuFactory
) -> None:
    fmu("3.0", "BouncingBall")
    _ninja(session)
    paused = session.ok("run_start", fmu=BALL, live=True, stop_time=3.0, wait_s=30)
    assert paused["state"] == "paused"
    assert paused["hint"].startswith("🥷 r1 in: BouncingBall paused at t=0 (live). Ninja mode")
    asked = session.ok("run_status", run_id="r1", values=["h"])
    assert [value["name"] for value in asked["values"]] == ["h"]
    assert asked["hint"].startswith("🥷 r1 in: BouncingBall paused at t=0 (live).")
    plain = session.ok("run_status", run_id="r1")
    assert "values" not in plain
    stepped = session.ok("run_control", run_id="r1", action="step", n=2)
    assert stepped["hint"].startswith("🥷 r1 in: BouncingBall paused at t=")
    ended = session.ok("run_control", run_id="r1", action="stop")
    assert ended["state"] == "stopped" and "summary" not in ended
    assert ended["hint"].startswith("🥷 r1 stopped at t=")


def test_ninja_mode_does_not_soften_a_refusal(session: Session, fmu: FmuFactory) -> None:
    fmu("3.0", "BouncingBall")
    refused: dict[str, Any] = {"fmu": BALL, "start_values": {"ee": 0.5}}
    start_before = session.error("run_start", **refused)
    status_before = session.error("run_status", run_id="r99")
    assert "Close matches: e." in start_before and "Call run_list" in status_before
    _ninja(session)
    assert session.error("run_start", **refused) == start_before
    assert session.error("run_status", run_id="r99") == status_before


def test_ninja_fmu_build_returns_the_built_line_without_the_facts(
    session: Session, workspace: Path
) -> None:
    (workspace / "models").mkdir()
    shutil.copy2(EXAMPLE / "pi_controller.py", workspace / "models" / "pi_controller.py")
    _ninja(session)
    view = session.ok("fmu_build", source="models/pi_controller.py", wait_s=60)
    assert view["kind"] == "build" and view["state"] == "finished", view
    assert "build" not in view
    assert view["fmu"] == "generated/pi_controller/pi_controller.fmu"
    assert view["hint"] == f"🥷 pi_controller built. Ninja mode: {NINJA_RULE}"
    assert (workspace / view["fmu"]).is_file()
    assert "build" not in session.ok("run_status", run_id=view["run_id"])
    # Talkative again: the facts come back.
    session.ok("response_style", style="talkative")
    assert session.ok("run_status", run_id=view["run_id"])["build"]["sha256"]


def test_a_failed_build_is_still_an_error_with_its_diagnostics(
    session: Session, workspace: Path
) -> None:
    (workspace / "models").mkdir()
    (workspace / "models" / "afmi_broken.py").write_text(
        "from pythonfmu import Fmi2Slave\n\n\nclass AfmiBroken(Fmi2Slave):\n    x = (2.0 +\n",
        encoding="utf-8",
    )
    _ninja(session)
    message = session.error("fmu_build", source="models/afmi_broken.py", wait_s=60)
    assert "models/afmi_broken.py:5:" in message and "SyntaxError" in message
