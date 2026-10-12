"""The 18 MCP tools through the in-memory client: registration, the explore tools, output size
budgets, failure paths, and the proof that the server never loads FMU binaries.

Collaborators: ``mcp_support.Session`` (a synchronous client on the real server, via the
``session`` fixture), the ``fmu`` and ``synth_fmu`` fixtures, and ``agentic_fmi.mcp.server.TOOLS``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp_support import Session, text_of

from agentic_fmi.mcp.server import TOOLS

BALL = "3.0/BouncingBall.fmu"
FmuFactory = Callable[[str, str], Path]
SynthFmu = Callable[..., Path]
MAPS = Path("/proc/self/maps")
BUDGET = 8_192
HARD_CAP = 16_384
ONE = {"kind": "constant", "value": 1}
TOOLS_LIST_BUDGET = 32_000
"""The `tools/list` payload was 36.4 KB before the schemas were trimmed, 25.4 KB with fmu_build,
and 29.5 KB with the three system tools; ADR 0009 raised the cap from 26 KB to 32 KB. Phase 4
(ADR 0010) fits run_control by trimming descriptions: 31.1 KB with 17 tools. response_style
(ninja mode) makes it 31.8 KB with 18."""


def test_all_eighteen_tools_are_listed_in_order(session: Session) -> None:
    tools = session.tools()
    assert [tool.name for tool in tools] == [fn.__name__ for fn, _ in TOOLS]
    assert len(tools) == 18
    for tool in tools:
        assert tool.description and len(tool.description) <= 2048
        assert tool.annotations is not None
    by_name = {tool.name: tool for tool in tools}
    assert by_name["run_plot"].output_schema is None
    # fmu_build and run_control return a RunView without publishing its schema again
    # (tools/list budget).
    assert by_name["fmu_build"].output_schema is None
    assert by_name["run_control"].output_schema is None
    unpublished = {"run_plot", "fmu_build", "run_control"}
    assert all(t.output_schema is not None for t in tools if t.name not in unpublished)
    assert by_name["fmu_inspect"].annotations.read_only_hint is True
    assert by_name["run_start"].annotations.read_only_hint is False
    assert by_name["response_style"].annotations.read_only_hint is False  # it writes preferences
    assert by_name["response_style"].annotations.destructive_hint is False
    assert len(by_name["response_style"].description or "") <= 300


def test_tools_list_payload_stays_lean(session: Session) -> None:
    payload = [t.model_dump(by_alias=True, exclude_none=True) for t in session.tools()]
    size = len(json.dumps(payload))
    assert size < TOOLS_LIST_BUDGET, f"tools/list is {size} bytes"
    assert '"title": "' not in json.dumps(payload)  # pydantic's title annotations are stripped


def test_optional_parameters_are_the_published_interface(session: Session) -> None:
    props = {t.name: set(t.input_schema["properties"]) for t in session.tools()}
    required = {t.name: set(t.input_schema.get("required", [])) for t in session.tools()}
    assert props["fmu_variables"] == {
        "fmu",
        "causality",
        "variability",
        "name",
        "type",
        "offset",
        "limit",
    }
    assert props["run_start"] >= {"fmu", "start_values", "inputs", "outputs", "wait_s", "force"}
    assert required["run_start"] == set()  # exactly one of fmu or system (ADR 0009)
    assert required["run_compare"] == {"run_ids"}
    assert required["workspace_info"] == set()
    assert props["fmu_build"] == {"source", "name", "overwrite", "wait_s"}  # ADR 0008
    assert required["fmu_build"] == {"source"}
    assert props["run_start"] >= {"system", "paramset"}  # ADR 0009
    assert props["system_check"] == required["system_check"] == {"system"}
    assert props["system_import"] == {"source", "name"}
    assert required["system_import"] == {"source"}
    assert props["system_export"] == {"system", "dest", "paramset"}
    assert required["system_export"] == {"system", "dest"}
    # ADR 0010: live runs are ordinary runs plus one control tool.
    assert "live" in props["run_start"]
    assert props["run_status"] == {"run_id", "values"}
    assert required["run_status"] == {"run_id"}
    assert props["run_control"] == {"run_id", "action", "n", "dt", "values", "speed", "wait_s"}
    assert required["run_control"] == {"run_id", "action"}
    # Ninja mode: one optional parameter.
    assert props["response_style"] == {"style"}
    assert required["response_style"] == set()


def test_workspace_info_reports_the_workspace_and_limits(session: Session, fmu: FmuFactory) -> None:
    fmu("3.0", "BouncingBall")
    info = session.ok("workspace_info")
    assert info["workspace"] == str(session.config.workspace)
    assert info["store_dir"].endswith(".agentic-fmi")
    assert info["fmu_count"] == 1
    assert info["active_runs"] == []
    assert info["max_workers"] == 4
    assert info["limits"]["results_default_points"] == 100
    assert set(info["versions"]) >= {"agentic_fmi", "fmpy", "python", "mcp"}


def test_fmu_list_inspect_variables_and_validate(session: Session, fmu: FmuFactory) -> None:
    fmu("3.0", "BouncingBall")
    fmu("2.0", "Dahlquist")
    page = session.ok("fmu_list")
    assert page["total"] == 2
    assert [item["path"] for item in page["items"]] == [
        "2.0/Dahlquist.fmu",
        "3.0/BouncingBall.fmu",
    ]
    assert session.ok("fmu_list", pattern="ball")["total"] == 1
    inspected = session.ok("fmu_inspect", fmu=BALL)
    assert inspected["model_name"] == "BouncingBall"
    assert inspected["path"] == BALL
    parameters = session.ok("fmu_variables", fmu=BALL, causality="parameter")
    assert [v["name"] for v in parameters["items"]] == ["g", "e"]
    reals = session.ok("fmu_variables", fmu=BALL, type="float64", name="h")
    assert {v["name"] for v in reals["items"]} == {"h", "der(h)", "h_ft"}
    assert session.ok("fmu_variables", fmu=BALL, type="Boolean")["total"] == 0
    report = session.ok("fmu_validate", fmu=BALL)
    assert report["valid"] is True


@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        ("fmu_inspect", {"fmu": "3.0/BouncingBal.fmu"}, "Close matches: 3.0/BouncingBall.fmu"),
        ("fmu_inspect", {"fmu": "../outside.fmu"}, "outside the workspace"),
        ("fmu_inspect", {"fmu": "3.0/notes.txt"}, "not a .fmu file"),
        ("fmu_validate", {"fmu": "nothing.fmu"}, "fmu_list"),
        ("fmu_variables", {"fmu": BALL, "offset": 99}, "past the end"),
        ("fmu_variables", {"fmu": BALL, "causality": "sideways"}, "parameter"),
        ("fmu_list", {"offset": 5}, "past the end"),
        ("fmu_list", {"limit": 0}, "greater than or equal to 1"),
        ("run_status", {"run_id": "r99"}, "Call run_list"),
        ("run_status", {"run_id": "banana"}, "should match pattern"),
        ("run_stop", {"run_id": "r99"}, "Call run_list"),
        ("run_results", {"run_id": "r99"}, "Call run_list"),
        ("run_compare", {"run_ids": ["r1", "r2"]}, "Call run_list"),
        ("run_compare", {"run_ids": ["r1"]}, "at least 2"),
        ("run_plot", {"run_ids": ["r98"]}, "Call run_list"),
        ("run_plot", {"run_ids": []}, "at least 1"),
        ("run_plot", {"run_ids": ["r1"], "title": "t" * 121}, "at most 120 characters"),
        ("run_list", {"state": "sleeping"}, "queued"),
        ("run_list", {"offset": 3}, "past the end"),
        ("run_start", {"fmu": "3.0/missing.fmu"}, "was not found"),
        ("run_start", {"fmu": BALL, "wait_s": 61}, "less than or equal to 60"),
        ("run_start", {"fmu": BALL, "timeout_s": 10**9}, "timeout_s"),
        ("run_start", {"fmu": BALL, "stop_time": -1.0}, "stop_time"),
        ("run_start", {"fmu": BALL, "start_values": {"ee": 0.5}}, "Close matches: e."),
        ("run_start", {"fmu": BALL, "outputs": ["hh"]}, "Close matches: h"),
        ("run_start", {"fmu": BALL, "inputs": {"e": ONE}}, "start_values"),
        ("run_start", {"fmu": BALL, "inputs": {"uu": ONE}}, "no inputs"),
        ("run_start", {"fmu": BALL, "fmi_type": "ModelExchange", "solver": "RK4"}, "CVode"),
    ],
)
def test_every_failure_path_is_an_error_with_actionable_text(
    session: Session, fmu: FmuFactory, tool: str, arguments: dict[str, Any], expected: str
) -> None:
    (fmu("3.0", "BouncingBall").parent / "notes.txt").write_text("not an fmu")
    message = session.error(tool, **arguments)
    assert expected in message
    assert "Traceback" not in message
    assert len(message) < 1_500


def test_string_outputs_and_scheduled_execution_fmus_are_refused(
    session: Session, fmu: FmuFactory
) -> None:
    fmu("3.0", "Feedthrough")
    message = session.error("run_start", fmu="3.0/Feedthrough.fmu", outputs=["String_output"])
    assert "String variable" in message and "numeric, Boolean and enumeration" in message
    fmu("3.0", "Clocks")
    assert "ScheduledExecution" in session.error("run_start", fmu="3.0/Clocks.fmu")


def test_a_corrupt_archive_is_an_error_and_listed_with_a_problem(
    session: Session, workspace: Path
) -> None:
    (workspace / "broken.fmu").write_bytes(b"not a zip")
    message = session.error("run_start", fmu="broken.fmu")
    assert "not a valid FMU" in message
    assert "not a valid FMU" in session.error("fmu_inspect", fmu="broken.fmu")
    page = session.ok("fmu_list")
    assert page["items"][0]["problem"]


def test_text_and_structured_content_agree_and_carry_no_nulls(
    session: Session, fmu: FmuFactory
) -> None:
    fmu("3.0", "BouncingBall")
    result = session.call("fmu_inspect", fmu=BALL)
    text = text_of(result)
    assert json.loads(text) == result.structured_content
    assert text == json.dumps(result.structured_content, separators=(",", ":"), ensure_ascii=False)
    assert "null" not in text


def test_pages_are_cut_to_the_budget_and_continue_at_next_offset(
    session: Session, synth_fmu: SynthFmu
) -> None:
    synth_fmu("synthetic/Synth.fmu", variables=300)
    seen: list[str] = []
    offset = 0
    while offset is not None:
        result = session.call("fmu_variables", fmu="synthetic/Synth.fmu", offset=offset)
        assert not result.is_error, text_of(result)
        assert len(text_of(result).encode()) <= BUDGET
        page = result.structured_content
        assert page is not None and page["total"] == 300
        assert len(page["items"]) <= page["limit"]
        seen += [v["name"] for v in page["items"]]
        offset = page.get("next_offset")
    assert len(seen) == 300 and len(set(seen)) == 300  # nothing lost, nothing repeated
    first = session.ok("fmu_variables", fmu="synthetic/Synth.fmu")
    assert len(first["items"]) < 50  # 50 long-description variables don't fit 8 KB
    wide = session.call("fmu_variables", fmu="synthetic/Synth.fmu", limit=200)
    assert BUDGET < len(text_of(wide).encode()) <= HARD_CAP
    assert len(wide.structured_content["items"]) > len(first["items"])


def test_inspect_of_a_wide_fmu_fits_the_budget_and_says_it_was_cut(
    session: Session, synth_fmu: SynthFmu
) -> None:
    synth_fmu("Synth.fmu", variables=300)
    result = session.call("fmu_inspect", fmu="Synth.fmu")
    assert not result.is_error, text_of(result)
    assert len(text_of(result).encode()) <= BUDGET
    info = result.structured_content
    assert info is not None
    assert info["truncated"] is True
    assert info["counts"]["total"] == 300


def test_fmu_list_page_is_cut_to_the_budget(session: Session, synth_fmu: SynthFmu) -> None:
    for i in range(60):
        synth_fmu(f"collection/with/a/rather/long/path/segment/model_{i:03d}.fmu", variables=2)
    page = session.ok("fmu_list")
    assert page["total"] == 60
    assert len(page["items"]) <= 50
    assert page["next_offset"] == len(page["items"])
    assert len(json.dumps(page, separators=(",", ":"))) <= BUDGET


def test_server_never_loads_fmu_binaries(session: Session, fmu: FmuFactory) -> None:
    if not MAPS.is_file():
        pytest.skip("needs /proc/self/maps (Linux)")

    def mapped() -> set[str]:
        return {line.strip() for line in MAPS.read_text().splitlines() if "/binaries/" in line}

    before = mapped()
    fmu("3.0", "BouncingBall")
    fmu("3.0", "Roberts")
    session.ok("workspace_info")
    session.ok("fmu_list")
    session.ok("fmu_inspect", fmu=BALL)
    session.ok("fmu_variables", fmu=BALL)
    session.ok("fmu_validate", fmu=BALL)
    first = session.ok("run_start", fmu=BALL, stop_time=1.0, wait_s=30)
    second = session.ok("run_start", fmu=BALL, stop_time=1.0, start_values={"e": 0.9}, wait_s=30)
    assert first["state"] == second["state"] == "finished"
    session.ok("run_status", run_id="r1")
    session.ok("run_list")
    session.ok("run_results", run_id="r1")
    session.ok("run_compare", run_ids=["r1", "r2"])
    assert not session.call("run_plot", run_ids=["r1", "r2"]).is_error
    session.error("run_start", fmu="3.0/Roberts.fmu")  # refused by the step guard
    started = session.ok("run_start", fmu=BALL, stop_time=100.0, output_interval=0.001, wait_s=0)
    session.ok("run_stop", run_id=started["run_id"])
    new = mapped() - before
    assert not new, f"FMU binaries were mapped into the server process: {sorted(new)}"
