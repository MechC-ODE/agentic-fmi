"""Tests for ``agentic_fmi.fmu.inspect``: parsing, runnability, the resolved experiment, the step
guard, variable checks and paging, on all 15 cached Reference FMUs and on synthetic archives.

Collaborators: the ``fmu`` and ``workspace`` fixtures (``tests/conftest.py``: every Reference FMU is
copied into a temporary workspace first), and ``config``, ``write_fmu``, ``fmi1_xml`` and
``fmi2_xml`` (``tests/fmu/conftest.py``). FMPy is used for XML only; no FMU binary is loaded.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fmpy import read_model_description

from agentic_fmi.config import Config, Limits
from agentic_fmi.errors import UserError
from agentic_fmi.fmu import inspect as fmu_inspect
from agentic_fmi.fmu.inspect import FmiType

FmuFactory = Callable[[str, str], Path]
WriteFmu = Callable[[str, dict[str, str]], Path]
Fmi2Xml = Callable[..., str]

ALL_FMUS = [
    *(("2.0", name) for name in ("BouncingBall", "Dahlquist", "Feedthrough", "Resource")),
    ("2.0", "Stair"),
    ("2.0", "VanDerPol"),
    *(("3.0", name) for name in ("BouncingBall", "Clocks", "Dahlquist", "Feedthrough")),
    *(("3.0", name) for name in ("Resource", "Roberts", "Stair", "StateSpace", "VanDerPol")),
]
FMI_TYPES: tuple[FmiType, FmiType] = ("CoSimulation", "ModelExchange")


def _ids(fmus: list[tuple[str, str]]) -> list[str]:
    return [f"{version}-{name}" for version, name in fmus]


# inspect_fmu ---------------------------------------------------------------------------------


@pytest.mark.parametrize(("version", "name"), ALL_FMUS, ids=_ids(ALL_FMUS))
def test_inspect_works_on_every_reference_fmu(
    config: Config, fmu: FmuFactory, version: str, name: str
) -> None:
    """Each of the 15 Reference FMUs inspects, with consistent hash, counts and JSON size."""
    path = fmu(version, name)
    info = fmu_inspect.inspect_fmu(config, f"{version}/{name}.fmu")
    assert info.path == f"{version}/{name}.fmu"
    assert info.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert info.size_bytes == path.stat().st_size
    expected = read_model_description(str(path))
    assert (info.fmi_version, info.model_name) == (version, expected.modelName)
    assert info.counts["total"] == sum(n for key, n in info.counts.items() if key != "total")
    assert info.runnability.current_platform in info.runnability.platforms
    assert info.runnability.runnable is (name != "Clocks")
    assert len(info.model_dump_json()) <= config.limits.json_budget_bytes


def test_inspect_bouncing_ball_30_facts(config: Config, fmu: FmuFactory) -> None:
    """BouncingBall 3.0: interfaces, default experiment, parameters (tunable first), outputs."""
    fmu("3.0", "BouncingBall")
    info = fmu_inspect.inspect_fmu(config, "3.0/BouncingBall.fmu")
    assert info.interfaces == ["CoSimulation", "ModelExchange"]
    assert info.default_experiment is not None
    assert (info.default_experiment.start_time, info.default_experiment.stop_time) == (0.0, 3.0)
    assert info.default_experiment.step_size == 0.01
    assert [(p.name, p.variability) for p in info.parameters] == [("e", "tunable"), ("g", "fixed")]
    e = info.parameters[0]
    assert (e.type, e.start, e.min, e.max) == ("Float64", "0.7", "0.5", "1")
    assert [o.name for o in info.outputs] == ["h", "v", "h_ft"]
    assert info.outputs[0].unit == "m"
    assert info.inputs == [] and info.truncated is False
    assert info.generation_tool is not None and "Reference FMUs" in info.generation_tool


def test_inspect_clocks_is_not_runnable_because_scheduled_execution_only(
    config: Config, fmu: FmuFactory
) -> None:
    """FMI 3.0 Clocks declares only ScheduledExecution, so FMPy can't run it."""
    fmu("3.0", "Clocks")
    info = fmu_inspect.inspect_fmu(config, "3.0/Clocks.fmu")
    assert info.interfaces == ["ScheduledExecution"]
    assert info.runnability.runnable is False
    assert info.runnability.interfaces == []
    assert info.runnability.reasons == ["ScheduledExecution only"]


def test_inspect_roberts_cs_is_blocked_with_pointer_to_model_exchange(
    config: Config, fmu: FmuFactory
) -> None:
    """Roberts 3.0: the CS default experiment is refused, ME passes, so the FMU is runnable."""
    fmu("3.0", "Roberts")
    runnability = fmu_inspect.inspect_fmu(config, "3.0/Roberts.fmu").runnability
    cs, me = runnability.interfaces
    assert (cs.fmi_type, cs.runnable) == ("CoSimulation", False)
    assert cs.estimated_steps == pytest.approx(1e11)
    assert cs.reason is not None and "fmi_type='ModelExchange'" in cs.reason
    assert (me.fmi_type, me.runnable, me.reason) == ("ModelExchange", True, None)
    assert runnability.runnable is True and runnability.reasons == []


def test_inspect_does_not_modify_the_fmu(config: Config, fmu: FmuFactory) -> None:
    """Inspecting leaves the archive byte-identical."""
    path = fmu("2.0", "Feedthrough")
    before = path.read_bytes()
    fmu_inspect.inspect_fmu(config, "2.0/Feedthrough.fmu")
    assert path.read_bytes() == before


def test_inspect_caps_lists_and_description(
    config: Config, write_fmu: WriteFmu, fmi2_xml: Fmi2Xml
) -> None:
    """More than 25 variables per list are cut and flagged; long texts are truncated."""
    xml = fmi2_xml(
        inputs=30,
        outputs=30,
        parameters=30,
        description="d" * 5000,
        variable_description="v" * 400,
    )
    write_fmu("big.fmu", {"modelDescription.xml": xml})
    info = fmu_inspect.inspect_fmu(config, "big.fmu")
    cap = config.limits.inspect_max_items
    assert [len(info.inputs), len(info.outputs), len(info.parameters)] == [cap, cap, cap]
    assert info.truncated is True
    assert info.counts == {"input": 30, "output": 30, "parameter": 30, "total": 90}
    assert info.description is not None
    assert len(info.description) == config.limits.description_max_chars
    assert info.description.endswith("...")
    assert all(len(v.description or "") <= 120 for v in info.inputs)
    assert info.runnability.runnable is False
    assert info.runnability.reasons and "no binaries" in info.runnability.reasons[0]


def test_inspect_rejects_fmi1_with_actionable_error(
    config: Config, write_fmu: WriteFmu, fmi1_xml: str
) -> None:
    """A synthesized FMI 1.0 archive raises UserError saying FMI 1.0 is not supported."""
    write_fmu("old.fmu", {"modelDescription.xml": fmi1_xml})
    with pytest.raises(UserError, match=r"FMI 1\.0 is not supported.*FMI 2\.0 or 3\.0"):
        fmu_inspect.inspect_fmu(config, "old.fmu")


def test_inspect_rejects_unknown_fmi_version(config: Config, write_fmu: WriteFmu) -> None:
    """An FMI version other than 2.x/3.x is a UserError that names the supported ones."""
    xml = '<fmiModelDescription fmiVersion="4.0" modelName="x"/>'
    write_fmu("future.fmu", {"modelDescription.xml": xml})
    with pytest.raises(UserError, match=r"'4\.0'.*2\.0 and 3\.0"):
        fmu_inspect.inspect_fmu(config, "future.fmu")


@pytest.mark.parametrize(
    ("members", "message"),
    [
        ({}, "no modelDescription.xml"),
        ({"modelDescription.xml": "<fmiModelDescription"}, "not well-formed XML"),
        ({"modelDescription.xml": "<fmiModelDescription modelName='x'/>"}, "no fmiVersion"),
        (
            {"modelDescription.xml": '<fmiModelDescription fmiVersion="2.0" modelName="x"/>'},
            "violates the FMI schema.*fmu_validate",
        ),
    ],
    ids=["no-xml", "malformed-xml", "no-version", "schema-violation"],
)
def test_inspect_reports_unreadable_descriptions(
    config: Config, write_fmu: WriteFmu, members: dict[str, str], message: str
) -> None:
    """Each way the XML can be unusable becomes a UserError, never a raw exception."""
    write_fmu("bad.fmu", members)
    with pytest.raises(UserError, match=message):
        fmu_inspect.inspect_fmu(config, "bad.fmu")


def test_inspect_rejects_a_file_that_is_not_a_zip(config: Config, workspace: Path) -> None:
    """A non-zip file with a .fmu name raises a UserError about the archive."""
    (workspace / "junk.fmu").write_bytes(b"junk")
    with pytest.raises(UserError, match="not a zip archive"):
        fmu_inspect.inspect_fmu(config, "junk.fmu")


# resolve_experiment --------------------------------------------------------------------------


def _description(fmu: FmuFactory, version: str, name: str) -> Any:
    return read_model_description(str(fmu(version, name)))


@pytest.mark.parametrize(
    ("version", "fmi_type", "interval"),
    [
        ("3.0", "CoSimulation", 0.004),
        ("2.0", "CoSimulation", 0.01),
        ("3.0", "ModelExchange", 0.004),
        ("2.0", "ModelExchange", 0.004),
    ],
)
def test_resolve_experiment_bouncing_ball_defaults_match_fmpy(
    fmu: FmuFactory, version: str, fmi_type: FmiType, interval: float
) -> None:
    """The defaults reproduce FMPy's rows: 751 (3.0 CS), 301 (2.0 CS), 751/751 for ME grids."""
    md = _description(fmu, version, "BouncingBall")
    experiment = fmu_inspect.resolve_experiment(md, fmi_type)
    assert (experiment.start_time, experiment.stop_time) == (0.0, 3.0)
    assert experiment.output_interval == interval
    assert experiment.relative_tolerance is None and experiment.step_size is None


def test_resolve_experiment_uses_roberts_default_experiment(fmu: FmuFactory) -> None:
    """Roberts: start 1e-5, stop 1e8, tolerance 1e-4 from the FMU; intervals double to <= 1000."""
    md = _description(fmu, "3.0", "Roberts")
    cs = fmu_inspect.resolve_experiment(md, "CoSimulation")
    me = fmu_inspect.resolve_experiment(md, "ModelExchange")
    assert (cs.start_time, cs.stop_time, cs.relative_tolerance) == (1e-5, 1e8, 1e-4)
    assert cs.output_interval == pytest.approx(1e-3 * 2**27)
    assert me.output_interval == pytest.approx(1e5)


def test_resolve_experiment_without_default_experiment_uses_fmpy_fallbacks(
    write_fmu: WriteFmu, fmi2_xml: Fmi2Xml, config: Config
) -> None:
    """No DefaultExperiment: start 0, stop 1 and FMPy's auto_interval (0.002 for a 1 s run)."""
    xml = fmi2_xml(inputs=1).replace('<DefaultExperiment startTime="0" stopTime="1"/>', "")
    path = write_fmu("plain.fmu", {"modelDescription.xml": xml})
    md = fmu_inspect.read_description(path)
    experiment = fmu_inspect.resolve_experiment(md, "CoSimulation")
    assert (experiment.start_time, experiment.stop_time) == (0.0, 1.0)
    assert experiment.output_interval == pytest.approx(0.002)
    assert fmu_inspect.resolve_experiment(md, "CoSimulation", start_time=5.0).stop_time == 6.0


def test_resolve_experiment_overrides_win_and_step_size_is_model_exchange_only(
    fmu: FmuFactory,
) -> None:
    """Caller values replace the defaults; ``step_size`` is kept for ME and dropped for CS."""
    md = _description(fmu, "2.0", "BouncingBall")
    me = fmu_inspect.resolve_experiment(
        md,
        "ModelExchange",
        start_time=1.0,
        stop_time=2.0,
        output_interval=0.05,
        relative_tolerance=1e-6,
        step_size=0.001,
    )
    assert (me.start_time, me.stop_time, me.output_interval) == (1.0, 2.0, 0.05)
    assert (me.relative_tolerance, me.step_size) == (1e-6, 0.001)
    cs = fmu_inspect.resolve_experiment(md, "CoSimulation", step_size=0.001)
    assert cs.step_size is None
    coarse = fmu_inspect.resolve_experiment(md, "ModelExchange", stop_time=100.0, step_size=0.01)
    assert coarse.output_interval == pytest.approx(0.16)  # 0.01 doubled until <= 1000 intervals


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"stop_time": 0.0}, r"stop_time \(0\) must be greater than start_time \(0\)"),
        ({"start_time": 5.0}, "must be greater than start_time"),
        ({"stop_time": float("nan")}, "must be greater than start_time"),
        ({"output_interval": 0.0}, "output_interval must be a positive number"),
        ({"output_interval": -1.0}, "output_interval must be a positive number"),
        ({"relative_tolerance": 0.0}, "relative_tolerance must be a positive number"),
        ({"step_size": -0.1}, "step_size must be a positive number"),
    ],
)
def test_resolve_experiment_rejects_invalid_values(
    fmu: FmuFactory, overrides: dict[str, float], message: str
) -> None:
    """Empty horizons and non-positive intervals or tolerances are UserErrors with the value."""
    md = _description(fmu, "2.0", "BouncingBall")
    with pytest.raises(UserError, match=message):
        fmu_inspect.resolve_experiment(md, "ModelExchange", **overrides)


# step estimate and run gate ------------------------------------------------------------------


def test_estimate_internal_steps(fmu: FmuFactory) -> None:
    """CS with fixedInternalStepSize counts internal steps; everything else counts intervals."""
    ball = _description(fmu, "3.0", "BouncingBall")
    cs = fmu_inspect.resolve_experiment(ball, "CoSimulation")
    assert fmu_inspect.estimate_internal_steps(ball, "CoSimulation", cs) == pytest.approx(3000)
    me = fmu_inspect.resolve_experiment(ball, "ModelExchange")
    assert fmu_inspect.estimate_internal_steps(ball, "ModelExchange", me) == pytest.approx(750)
    old = _description(fmu, "2.0", "BouncingBall")
    old_cs = fmu_inspect.resolve_experiment(old, "CoSimulation")
    assert fmu_inspect.estimate_internal_steps(old, "CoSimulation", old_cs) == pytest.approx(300)
    roberts = _description(fmu, "3.0", "Roberts")
    rob = fmu_inspect.resolve_experiment(roberts, "CoSimulation")
    assert fmu_inspect.estimate_internal_steps(roberts, "CoSimulation", rob) == pytest.approx(1e11)


def test_check_run_refuses_roberts_cs_default_and_points_to_model_exchange(
    fmu: FmuFactory,
) -> None:
    """Roberts 3.0 CS (about 1e11 steps) is refused; the message names the estimate and options."""
    path = fmu("3.0", "Roberts")
    md = read_model_description(str(path))
    experiment = fmu_inspect.resolve_experiment(md, "CoSimulation")
    with pytest.raises(UserError) as caught:
        fmu_inspect.check_run(path, md, "CoSimulation", experiment, Limits(), force=False)
    text = str(caught.value)
    assert "1e+11" in text and "limit of 1e+08" in text
    assert "fmi_type='ModelExchange'" in text and "shorten stop_time" in text
    assert "force=true" in text and "timeout_s" in text


def test_check_run_force_returns_the_estimate_for_roberts_cs(fmu: FmuFactory) -> None:
    """``force`` lets the guarded run through and still reports the estimate."""
    path = fmu("3.0", "Roberts")
    md = read_model_description(str(path))
    experiment = fmu_inspect.resolve_experiment(md, "CoSimulation")
    steps = fmu_inspect.check_run(path, md, "CoSimulation", experiment, Limits(), force=True)
    assert steps == pytest.approx(1e11)


def test_check_run_refuses_a_tiny_output_interval_in_model_exchange(fmu: FmuFactory) -> None:
    """The guard also covers ME: a 1e-9 s output interval means too many intervals."""
    path = fmu("3.0", "BouncingBall")
    md = read_model_description(str(path))
    experiment = fmu_inspect.resolve_experiment(md, "ModelExchange", output_interval=1e-9)
    with pytest.raises(UserError, match="raise output_interval"):
        fmu_inspect.check_run(path, md, "ModelExchange", experiment, Limits(), force=False)


@pytest.mark.parametrize(("version", "name"), ALL_FMUS, ids=_ids(ALL_FMUS))
def test_check_run_passes_every_default_experiment_except_roberts_cs_and_clocks(
    fmu: FmuFactory, version: str, name: str
) -> None:
    """Every declared interface of every Reference FMU runs, except Roberts CS and Clocks."""
    path = fmu(version, name)
    md = read_model_description(str(path))
    for fmi_type in FMI_TYPES:
        if name == "Clocks":
            with pytest.raises(UserError, match="ScheduledExecution only"):
                fmu_inspect.check_run(
                    path,
                    md,
                    fmi_type,
                    fmu_inspect.resolve_experiment(md, fmi_type),
                    Limits(),
                    False,
                )
            continue
        experiment = fmu_inspect.resolve_experiment(md, fmi_type)
        if (version, name, fmi_type) == ("3.0", "Roberts", "CoSimulation"):
            with pytest.raises(UserError, match="ModelExchange"):
                fmu_inspect.check_run(path, md, fmi_type, experiment, Limits(), force=False)
            continue
        steps = fmu_inspect.check_run(path, md, fmi_type, experiment, Limits(), force=False)
        assert steps is not None and 0 < steps <= Limits().max_internal_steps


def test_check_run_rejects_an_undeclared_interface(
    config: Config, write_fmu: WriteFmu, fmi2_xml: Fmi2Xml
) -> None:
    """Asking for ModelExchange from a CS-only FMU fails; the message lists what is offered."""
    xml = fmi2_xml(inputs=1)
    path = write_fmu("cs.fmu", {"modelDescription.xml": xml, "binaries/linux64/Synth.so": ""})
    md = fmu_inspect.read_description(path)
    experiment = fmu_inspect.resolve_experiment(md, "ModelExchange")
    with pytest.raises(UserError, match=r"doesn't declare fmi_type='ModelExchange'.*CoSimulation"):
        fmu_inspect.check_run(path, md, "ModelExchange", experiment, Limits(), force=False)


def test_check_run_rejects_fmu_without_binary_for_this_platform(
    write_fmu: WriteFmu, fmi2_xml: Fmi2Xml
) -> None:
    """Source-only and wrong-platform FMUs are refused with the platform named."""
    sources = write_fmu("src.fmu", {"modelDescription.xml": fmi2_xml(inputs=1), "sources/a.c": ""})
    other = write_fmu(
        "mac.fmu",
        {"modelDescription.xml": fmi2_xml(inputs=1), "binaries/darwin64/Synth.dylib": ""},
    )
    for path, message in ((sources, "source-only"), (other, "no binary for linux64")):
        md = fmu_inspect.read_description(path)
        experiment = fmu_inspect.resolve_experiment(md, "CoSimulation")
        with pytest.raises(UserError, match=message):
            fmu_inspect.check_run(path, md, "CoSimulation", experiment, Limits(), force=False)


# helpers -------------------------------------------------------------------------------------


def test_default_fmi_type_and_interfaces(fmu: FmuFactory) -> None:
    """CS wins when both are declared; ScheduledExecution only has no usable default."""
    ball = read_model_description(str(fmu("2.0", "BouncingBall")))
    assert fmu_inspect.interfaces_of(ball) == ["CoSimulation", "ModelExchange"]
    assert fmu_inspect.default_fmi_type(ball) == "CoSimulation"
    clocks = read_model_description(str(fmu("3.0", "Clocks")))
    assert fmu_inspect.interfaces_of(clocks) == ["ScheduledExecution"]
    with pytest.raises(UserError, match="ScheduledExecution only"):
        fmu_inspect.default_fmi_type(clocks)


def test_variable_types_maps_names_to_fmpy_types(fmu: FmuFactory) -> None:
    """FMI 2.0 uses ``Real``; FMI 3.0 uses ``Float64``."""
    old = read_model_description(str(fmu("2.0", "BouncingBall")))
    new = read_model_description(str(fmu("3.0", "BouncingBall")))
    assert fmu_inspect.variable_types(old)["h"] == "Real"
    assert fmu_inspect.variable_types(new)["h"] == "Float64"
    feedthrough = read_model_description(str(fmu("3.0", "Feedthrough")))
    assert {"Int32", "Boolean", "String", "Binary"} <= set(
        fmu_inspect.variable_types(feedthrough).values()
    )


def test_sha256_file_matches_hashlib_for_a_multi_chunk_file(tmp_path: Path) -> None:
    """The streaming hash equals hashlib's for a file larger than one chunk."""
    path = tmp_path / "big.bin"
    data = b"0123456789abcdef" * (200 * 1024)
    path.write_bytes(data)
    assert fmu_inspect.sha256_file(path) == hashlib.sha256(data).hexdigest()


def test_require_variables_accepts_known_names(fmu: FmuFactory) -> None:
    """Known names, with and without a causality filter, pass silently."""
    md = read_model_description(str(fmu("2.0", "BouncingBall")))
    fmu_inspect.require_variables(md, ["e", "g"], purpose="start_values")
    fmu_inspect.require_variables(
        md, iter(["e"]), purpose="start_values", causalities=["parameter"]
    )


def test_require_variables_unknown_name_has_close_matches_and_next_step(fmu: FmuFactory) -> None:
    """The documented message: unknown name, close matches, how to list names."""
    md = read_model_description(str(fmu("2.0", "BouncingBall")))
    with pytest.raises(UserError) as caught:
        fmu_inspect.require_variables(md, ["e", "hh"], purpose="start_values")
    assert str(caught.value) == (
        "Unknown variable 'hh' in start_values. Close matches: h. Call fmu_variables to list names."
    )


def test_require_variables_mentions_every_unknown_name(fmu: FmuFactory) -> None:
    """With several unknown names, the first is explained and the others are listed."""
    md = read_model_description(str(fmu("2.0", "BouncingBall")))
    with pytest.raises(UserError, match=r"Unknown variable 'zzz'.*Also unknown: 'yyy'"):
        fmu_inspect.require_variables(md, ["zzz", "yyy"], purpose="outputs")


def test_require_variables_checks_causality(fmu: FmuFactory) -> None:
    """A known name with a disallowed causality is refused, naming the allowed ones."""
    md = read_model_description(str(fmu("2.0", "BouncingBall")))
    with pytest.raises(UserError, match=r"'h' in inputs has causality 'output'.*accepts input"):
        fmu_inspect.require_variables(md, ["h"], purpose="inputs", causalities=["input"])


# list_variables ------------------------------------------------------------------------------


def test_list_variables_returns_all_with_total_and_no_next_offset(
    config: Config, fmu: FmuFactory
) -> None:
    """Unfiltered, a small model fits on one page in modelDescription order."""
    fmu("3.0", "BouncingBall")
    page = fmu_inspect.list_variables(config, "3.0/BouncingBall.fmu")
    assert page.fmu == "3.0/BouncingBall.fmu"
    assert (page.total, page.offset, page.limit, page.next_offset) == (9, 0, 50, None)
    assert [v.name for v in page.items][:3] == ["time", "h", "der(h)"]


def test_list_variables_filters_by_causality_variability_and_type(
    config: Config, fmu: FmuFactory
) -> None:
    """Each filter narrows ``total``; type is case-insensitive."""
    fmu("3.0", "Feedthrough")
    where = "3.0/Feedthrough.fmu"
    inputs = fmu_inspect.list_variables(config, where, causality="input")
    assert inputs.total == 16 and {v.causality for v in inputs.items} == {"input"}
    fixed = fmu_inspect.list_variables(config, where, variability="fixed")
    assert [v.name for v in fixed.items] == ["Float64_fixed_parameter"] and fixed.total == 1
    booleans = fmu_inspect.list_variables(config, where, type="boolean")
    assert {v.type for v in booleans.items} == {"Boolean"} and booleans.total == 2
    both = fmu_inspect.list_variables(config, where, causality="input", type="Int32")
    assert [v.name for v in both.items] == ["Int32_input"]
    none = fmu_inspect.list_variables(config, where, causality="local")
    assert (none.total, none.items) == (0, [])


def test_list_variables_name_query_is_substring_or_glob_case_insensitive(
    config: Config, fmu: FmuFactory
) -> None:
    """A plain query is a substring; ``*`` makes it an anchored glob."""
    fmu("3.0", "Feedthrough")
    where = "3.0/Feedthrough.fmu"
    substring = fmu_inspect.list_variables(config, where, name="INT32")
    assert [v.name for v in substring.items] == [
        "Int32_input",
        "Int32_output",
        "UInt32_input",
        "UInt32_output",
    ]
    glob = fmu_inspect.list_variables(config, where, name="float64_*_input")
    assert [v.name for v in glob.items] == ["Float64_continuous_input", "Float64_discrete_input"]
    assert fmu_inspect.list_variables(config, where, name="no-such-name").total == 0


def test_list_variables_pages_with_default_50_and_max_200(
    config: Config, write_fmu: WriteFmu, fmi2_xml: Fmi2Xml
) -> None:
    """Default page 50, ``limit`` capped at 200, ``next_offset`` walks the rest, total is stable."""
    write_fmu("many.fmu", {"modelDescription.xml": fmi2_xml(inputs=260)})
    first = fmu_inspect.list_variables(config, "many.fmu")
    assert (first.total, first.limit, len(first.items), first.next_offset) == (260, 50, 50, 50)
    big = fmu_inspect.list_variables(config, "many.fmu", limit=1000)
    assert (big.limit, len(big.items), big.next_offset) == (200, 200, 200)
    rest = fmu_inspect.list_variables(config, "many.fmu", offset=big.next_offset or 0, limit=200)
    assert (len(rest.items), rest.next_offset, rest.total) == (60, None, 260)
    assert big.items[0].name == "i0" and rest.items[-1].name == "i259"


def test_list_variables_filters_before_paging(
    config: Config, write_fmu: WriteFmu, fmi2_xml: Fmi2Xml
) -> None:
    """``total`` counts matches across pages; offsets index the filtered list."""
    write_fmu("mix.fmu", {"modelDescription.xml": fmi2_xml(inputs=3, outputs=70)})
    page = fmu_inspect.list_variables(config, "mix.fmu", causality="output", offset=60, limit=50)
    assert (page.total, len(page.items), page.next_offset) == (70, 10, None)
    assert page.items[0].name == "o60"


def test_list_variables_errors_are_actionable(config: Config, fmu: FmuFactory) -> None:
    """An offset past the end names the total; bad filter values list the valid ones."""
    fmu("3.0", "BouncingBall")
    with pytest.raises(UserError, match=r"offset 99 is past the end: there are 9 matching"):
        fmu_inspect.list_variables(config, "3.0/BouncingBall.fmu", offset=99)
    with pytest.raises(UserError, match=r"Unknown causality 'bogus'.*parameter"):
        fmu_inspect.list_variables(config, "3.0/BouncingBall.fmu", causality="bogus")
    with pytest.raises(UserError, match=r"Unknown variability 'bogus'.*tunable"):
        fmu_inspect.list_variables(config, "3.0/BouncingBall.fmu", variability="bogus")
    with pytest.raises(UserError, match="not found"):
        fmu_inspect.list_variables(config, "missing.fmu")


def test_list_variables_declared_type_supplies_unit_min_and_max(
    config: Config, write_fmu: WriteFmu
) -> None:
    """A variable without its own unit/min/max inherits them from its declared type."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<fmiModelDescription fmiVersion="2.0" modelName="T" guid="{t}">
  <CoSimulation modelIdentifier="T"/>
  <UnitDefinitions><Unit name="m"/></UnitDefinitions>
  <TypeDefinitions>
    <SimpleType name="Length"><Real quantity="Length" unit="m" min="0" max="10"/></SimpleType>
  </TypeDefinitions>
  <ModelVariables>
    <ScalarVariable name="x" valueReference="0" causality="input" variability="continuous">
      <Real declaredType="Length" start="1"/>
    </ScalarVariable>
  </ModelVariables>
  <ModelStructure/>
</fmiModelDescription>
"""
    write_fmu("typed.fmu", {"modelDescription.xml": xml})
    variable = fmu_inspect.list_variables(config, "typed.fmu").items[0]
    assert (variable.unit, variable.min, variable.max, variable.start) == ("m", "0", "10", "1")
