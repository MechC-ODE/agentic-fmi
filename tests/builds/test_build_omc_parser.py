"""The omc side of ``fmu_build`` that needs no omc process: the log parser on captured output,
the generated ``.mos`` script, omc's offline environment, the class-name pre-check and the
install hint.

Collaborators: ``agentic_fmi.build`` and ``agentic_fmi.build_diagnostics`` (under test),
``agentic_fmi.runs.start_build`` (the refusals) and the fixtures in
``tests/builds/conftest.py``. The log fixtures are real omc 1.27.1 output captured in the dev
container (``@@afmi`` marker lines and the text of ``getErrorString()``) and re-rooted at a
temporary ``work/`` directory, which is where the worker builds. These tests run without omc:
only ``tests/builds/test_build_omc.py`` (marker ``omc``) starts it.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agentic_fmi import build, build_diagnostics, runs
from agentic_fmi.build_diagnostics import Diagnostic
from agentic_fmi.config import Config, Limits
from agentic_fmi.errors import UserError
from agentic_fmi.store import runstore
from agentic_fmi.supervisor import Supervisor

WriteSource = Callable[..., Path]

HOME = "/home/dev"
SOURCE_REL = "models/Sem.mo"

HEAD = """@@afmi omc=OpenModelica 1.27.1~2-g6db4671
@@afmi msl=4.1.0
"""

SYNTAX_LOG = (
    HEAD
    + """@@afmi load=false
[{work}/src/Sem.mo:5:1-5:7:writable] Error: Expected token of type SEMICOLON, \
got 'Sem' of type END_IDENT
@@afmi fmu=
Error: Failed to load package Sem (default) using MODELICAPATH \
/home/dev/.openmodelica/libraries.
Error: Class Sem not found in scope <top>.
"""
)

UNDECLARED_LOG = (
    HEAD
    + """@@afmi load=true
@@afmi fmu=
[{work}/src/Sem.mo:4:3-4:14:writable] Error: Variable y not found in scope Sem.
"""
)

UNDER_DETERMINED_LOG = (
    HEAD
    + """@@afmi load=true
@@afmi fmu=
Error: Too few equations, under-determined system. The model has 1 equation(s) and \
2 variable(s).
[{work}/src/Sem.mo:3:3-3:9:writable] Warning: Variable z does not have any remaining \
equation to be solved in.
  The original equations were:
  Equation 1: der(x) = -x
"""
)

OFFLINE_SUCCESS_LOG = """@@afmi omc=OpenModelica 1.27.1~2-g6db4671
[/usr/bin/../share/omlibrary/cache/index.json:0:0-0:0:readonly] Notification: Cached \
libraries were found and will be installed into /home/dev/.openmodelica/libraries/.
[/home/dev/.openmodelica/libraries/Modelica 4.1.0+maint.om/package.mo:0:0-0:0:readonly] \
Notification: Package installed successfully (SHA 7a4bf7de77a3986e8eb1e88cbb515d646f78f834).
Error: Curl error for URL https://libraries.openmodelica.org/index/v1/index.json: \
Couldn't connect to server
Error: Failed to download package index \
https://libraries.openmodelica.org/index/v1/index.json to file \
/home/dev/.openmodelica/libraries/index.json.
@@afmi msl=4.1.0
@@afmi load=true
Warning: The initial conditions are not fully specified. For more information set \
-d=initialization.
@@afmi fmu={work}/Sem.fmu
"""


@pytest.fixture
def work(tmp_path: Path) -> Path:
    """A worker's ``work/`` directory with the source copy ``src/Sem.mo``."""
    path = tmp_path / "runs" / "r1" / "work"
    (path / "src").mkdir(parents=True)
    (path / "src" / "Sem.mo").write_text("model Sem\nend Sem;\n", encoding="utf-8")
    return path


def _parse(log: str, work: Path, home: str = HOME) -> build_diagnostics.OmcLog:
    return build_diagnostics.parse_omc_log(
        log.format(work=work), work / "src" / "Sem.mo", SOURCE_REL, home
    )


def _lines(parsed: build_diagnostics.OmcLog) -> list[str]:
    return [d.format() for d in parsed.diagnostics]


def test_a_syntax_error_is_mapped_to_the_source_and_its_follow_ups_are_dropped(
    work: Path,
) -> None:
    parsed = _parse(SYNTAX_LOG, work)
    assert (parsed.loaded, parsed.fmu) == (False, None)
    assert parsed.omc_version == "1.27.1~2-g6db4671" and parsed.msl_version == "4.1.0"
    assert _lines(parsed) == [
        f"{SOURCE_REL}:5:1: error: Expected token of type SEMICOLON, got 'Sem' of type END_IDENT"
    ]


def test_an_undeclared_variable_has_its_line_and_column(work: Path) -> None:
    parsed = _parse(UNDECLARED_LOG, work)
    assert (parsed.loaded, parsed.fmu) == (True, None)
    assert _lines(parsed) == [f"{SOURCE_REL}:4:3: error: Variable y not found in scope Sem."]


def test_a_message_without_a_location_is_kept_and_continuation_lines_are_joined(
    work: Path,
) -> None:
    error, warning = _parse(UNDER_DETERMINED_LOG, work).diagnostics
    assert error == Diagnostic(
        "error",
        "Too few equations, under-determined system. The model has 1 equation(s) and 2 "
        "variable(s).",
    )
    assert error.format().startswith("error: Too few equations")
    assert warning.format() == (
        f"{SOURCE_REL}:3:3: warning: Variable z does not have any remaining equation to be "
        "solved in. The original equations were: Equation 1: der(x) = -x"
    )


def test_errors_come_first_even_when_a_warning_was_printed_first(work: Path) -> None:
    log = UNDER_DETERMINED_LOG.replace(
        "Error: Too few", "[{work}/src/Sem.mo:2:1-2:5:writable] Warning: Odd.\nError: Too few"
    )
    severities = [d.severity for d in _parse(log, work).diagnostics]
    assert severities == ["error", "warning", "warning"]


def test_curl_and_notification_noise_on_a_successful_build_is_dropped(work: Path) -> None:
    parsed = _parse(OFFLINE_SUCCESS_LOG, work)
    assert parsed.fmu == f"{work}/Sem.fmu" and parsed.loaded
    assert _lines(parsed) == [
        "warning: The initial conditions are not fully specified. For more information set "
        "-d=initialization."
    ]


def test_a_real_error_survives_a_noisy_log_and_load_follow_ups_only_go_after_a_failed_load(
    work: Path,
) -> None:
    noisy = OFFLINE_SUCCESS_LOG.replace(
        "@@afmi fmu=", "Error: Class Foo not found in scope Sem.\n@@afmi fmu="
    ).replace(
        "Warning: The initial", "Error: Class Bar not found in scope <top>.\nWarning: The initial"
    )
    texts = [d.message for d in _parse(noisy, work).diagnostics]
    assert "Class Bar not found in scope <top>." in texts  # loaded: it is a real error
    assert "Class Foo not found in scope Sem." in texts
    assert not any("Curl" in t or "download" in t for t in texts)


def test_no_temporary_or_home_path_survives(work: Path) -> None:
    log = (
        HEAD
        + """@@afmi load=true
@@afmi fmu=
[/home/dev/.openmodelica/libraries/Modelica 4.1.0+maint.om/Blocks/package.mo:10:3-10:9:\
readonly] Error: Bad block in /home/dev/.openmodelica/libraries.
[{work}/MSD_info.json:1:1-1:2:writable] Error: Odd in {work}/src/Sem.mo and {work}/tmp/x.c.
"""
    )
    library, local = _parse(log, work).diagnostics
    assert library.path == "~/.openmodelica/libraries/Modelica 4.1.0+maint.om/Blocks/package.mo"
    assert library.message == "Bad block in ~/.openmodelica/libraries."
    assert local.path is None and local.line is None  # a file of the build directory
    assert local.message == f"Odd in {SOURCE_REL} and <build dir>."
    for diagnostic in (library, local):
        assert str(work) not in diagnostic.format() and HOME not in diagnostic.format()


def test_an_empty_home_is_never_replaced(work: Path) -> None:
    (error,) = _parse(UNDECLARED_LOG, work, home="").diagnostics
    assert error.format() == f"{SOURCE_REL}:4:3: error: Variable y not found in scope Sem."


def test_repeated_messages_are_merged_and_long_ones_are_cut(work: Path) -> None:
    repeated = UNDECLARED_LOG + UNDECLARED_LOG.splitlines()[-1].format(work=work) + "\n"
    assert len(_parse(repeated, work).diagnostics) == 1
    long = UNDECLARED_LOG.replace("not found", "x" * 1000)
    (error,) = _parse(long, work).diagnostics
    assert len(error.message) == build_diagnostics.MAX_OMC_MESSAGE_CHARS and error.message.endswith(
        "…"
    )


def test_markers_are_read_only_at_the_start_of_a_line(work: Path) -> None:
    log = UNDECLARED_LOG.replace("not found in scope Sem.", "got @@afmi fmu=/etc/passwd")
    assert _parse(log, work).fmu is None


def test_a_log_without_markers_has_no_version_and_no_fmu(work: Path) -> None:
    parsed = _parse("Segmentation fault\n", work)
    assert (parsed.omc_version, parsed.msl_version, parsed.fmu, parsed.loaded) == (
        None,
        None,
        None,
        False,
    )
    assert parsed.diagnostics == []


def test_a_flood_of_two_hundred_errors_stays_within_the_caps(work: Path) -> None:
    flood = HEAD + "@@afmi load=true\n@@afmi fmu=\n"
    flood += "".join(
        f"[{{work}}/src/Sem.mo:{n}:3-{n}:14:writable] Error: Variable v{n} not found in scope "
        f"Sem. {'x' * 150}\n"
        for n in range(1, 201)
    )
    parsed = _parse(flood, work)
    assert len(parsed.diagnostics) == 200
    limits = Limits()
    text = build_diagnostics.format_failure(
        parsed.diagnostics, backend="omc", log_path=".agentic-fmi/runs/r1/build.log", limits=limits
    )
    lines = text.splitlines()
    assert len(text) <= limits.build_diagnostics_max_chars
    assert lines[0] == "Build failed (omc): 200 errors."
    shown = [line for line in lines if ": error: " in line]
    assert 1 <= len(shown) <= limits.build_diagnostics_max_lines
    assert shown[0].startswith(f"{SOURCE_REL}:1:3: error: Variable v1 not found")
    assert lines[-3] == f"and {200 - len(shown)} more (see the full log)"


def test_a_successful_builds_errors_become_warnings_not_a_failure(work: Path) -> None:
    fmu = work / "Sem.fmu"
    fmu.write_bytes(b"zip")
    log = OFFLINE_SUCCESS_LOG.replace("Warning: The", "Error: The")
    result = build._omc_result(0, log.format(work=work), work / "src" / "Sem.mo", SOURCE_REL, work)
    assert result.fmu == fmu
    assert [d.severity for d in result.diagnostics] == ["warning"]
    assert result.versions == {"omc": "1.27.1~2-g6db4671", "msl": "4.1.0"}


@pytest.mark.parametrize(
    ("log", "stage"),
    [
        ("Segmentation fault\n", "omc ended with status 139 before it ran the script"),
        (HEAD + "@@afmi load=false\n@@afmi fmu=\n", "loadFile failed"),
        (HEAD + "@@afmi load=true\n@@afmi fmu=\n", "buildModelFMU returned no FMU"),
    ],
)
def test_a_failure_without_an_error_message_still_says_where_it_stopped(
    work: Path, log: str, stage: str
) -> None:
    result = build._omc_result(139, log, work / "src" / "Sem.mo", SOURCE_REL, work)
    assert result.fmu is None
    assert result.headline == f"Build failed (omc): {stage}, and omc reported no error."


def test_a_returned_path_that_is_no_file_is_a_failure(work: Path) -> None:
    log = HEAD + f"@@afmi load=true\n@@afmi fmu={work}/Missing.fmu\n"
    result = build._omc_result(0, log, work / "src" / "Sem.mo", SOURCE_REL, work)
    assert result.fmu is None


# --- the script ---------------------------------------------------------------------------------


def test_the_script_loads_builds_and_marks_in_order(tmp_path: Path) -> None:
    source = tmp_path / "work" / "src" / "MSD.mo"
    libraries = "/home/dev/.openmodelica/libraries"
    lines = build.omc_script(source, "MSD", libraries).splitlines()
    assert lines[0] == "echo(false);"
    expected = [
        'print("@@afmi omc=',
        f'setModelicaPath("{libraries}");',
        "loadModel(Modelica);",
        'print("@@afmi msl=',
        f'loadFile("{source}"',
        'print("@@afmi load=',
        'setCommandLineOptions("--fmiFlags=s:cvode");',
        'buildModelFMU(MSD, version="2.0", fmuType="me_cs");',
        'print("@@afmi fmu=',
    ]
    positions = [next(i for i, line in enumerate(lines) if piece in line) for piece in expected]
    assert positions == sorted(positions)
    drains = [line for line in lines if line == "afmi_msg := getErrorString(); print(afmi_msg);"]
    assert len(drains) == 5  # after the path, MSL, the load, the options and the build
    script = "\n".join(lines)
    for forbidden in ("installPackage", "updatePackageIndex", "system("):
        assert forbidden not in script


def test_script_paths_are_escaped_as_modelica_strings(tmp_path: Path) -> None:
    odd = tmp_path / 'a "b"\\c\nd' / "M.mo"
    script = build.omc_script(odd, "M", '/x"y')
    assert 'setModelicaPath("/x\\"y");' in script
    assert (
        'loadFile("' + str(odd).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        in script
    )
    assert len(script.splitlines()) == 15  # the newline in the path didn't add a line


# --- the environment ------------------------------------------------------------------------


def test_only_omc_gets_the_unroutable_proxy_and_inherited_proxies_are_dropped(
    tmp_path: Path,
) -> None:
    environ = {
        "PATH": "/usr/bin",
        "HOME": "/home/dev",
        "http_proxy": "http://corporate:3128",
        "HTTPS_PROXY": "http://corporate:3128",
        "no_proxy": "pypi.org",
    }
    plain = build.build_env(tmp_path, environ)
    assert not {k for k in plain if "proxy" in k.lower()}
    offline = build.build_env(tmp_path, environ, offline=True)
    assert offline == {
        **plain,
        "http_proxy": "http://127.0.0.1:9",
        "https_proxy": "http://127.0.0.1:9",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "no_proxy": "",
        "NO_PROXY": "",
    }


# --- refusals before any process --------------------------------------------------------------


@pytest.fixture
def no_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that starts a process (the worker or a build tool)."""

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail(f"a process was started: {args[0] if args else kwargs}")

    monkeypatch.setattr(subprocess, "Popen", forbidden)


def _refused(config: Config, supervisor: Supervisor, source: str) -> str:
    with pytest.raises(UserError) as caught:
        runs.start_build(config, supervisor, source)
    assert runstore.list_run_ids(config.store_dir) == []
    return str(caught.value)


def test_a_modelica_source_without_omc_gets_the_install_hint_and_no_process(
    config: Config,
    supervisor: Supervisor,
    write_source: WriteSource,
    monkeypatch: pytest.MonkeyPatch,
    no_processes: None,
) -> None:
    write_source("models/Tiny.mo", "model Tiny\n  Real x;\nequation\n  x = 1;\nend Tiny;\n")
    monkeypatch.setattr(build, "find_omc", lambda: None)
    message = _refused(config, supervisor, "models/Tiny.mo")
    assert message == build.OMC_INSTALL_HINT
    assert "sudo apt install --no-install-recommends omc omlibrary" in message
    assert "WITH_OPENMODELICA=1" in message and "call fmu_build again" in message


def test_omc_is_found_on_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = tmp_path / "omc"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert build.find_omc() == str(fake)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert build.find_omc() is None


@pytest.mark.parametrize(
    ("file", "text", "problem"),
    [
        (
            "Other.mo",
            "model MSD\nend MSD;\n",
            "it defines only MSD, but fmu_build builds the model",
        ),
        ("Empty.mo", "// nothing here\n", "it defines no model or block"),
        ("Pack.mo", "package Pack\nend Pack;\n", "it defines package Pack, not a model or block"),
        ("Hidden.mo", '// model Hidden\n/* block Hidden */\nx = "model Hidden";\n', "no model"),
        ("1bad.mo", "model bad\nend bad;\n", "not a Modelica class name"),
        ("bad-name.mo", "model bad\nend bad;\n", "not a Modelica class name"),
    ],
)
def test_the_class_must_be_a_model_or_block_named_like_the_file(
    config: Config,
    supervisor: Supervisor,
    write_source: WriteSource,
    monkeypatch: pytest.MonkeyPatch,
    no_processes: None,
    file: str,
    text: str,
    problem: str,
) -> None:
    write_source(f"models/{file}", text)
    monkeypatch.setattr(build, "find_omc", lambda: "/usr/bin/omc")
    message = _refused(config, supervisor, f"models/{file}")
    assert message.startswith(f"models/{file} can't be built: ")
    assert problem in message


@pytest.mark.parametrize(
    "text",
    [
        "model Good\nend Good;\n",
        "block Good\nend Good;\n",
        "within Lib;\nencapsulated partial model Good\n  Real x;\nend Good;\n",
        "model Other\nend Other;\nmodel Good = Other(x = 1);\n",
        '/* c */ model\n  Good\n  "doc" end Good;\n',
        "model Good // model Wrong\nend Good;\n",
    ],
)
def test_valid_class_definitions_pass_the_pre_check(text: str) -> None:
    assert build.modelica_class_problem("Good", text) is None


def test_a_valid_modelica_request_reads_and_hashes_the_source_without_a_process(
    config: Config,
    write_source: WriteSource,
    monkeypatch: pytest.MonkeyPatch,
    no_processes: None,
) -> None:
    monkeypatch.setattr(build, "find_omc", lambda: "/usr/bin/omc")
    path = write_source("models/MSD.mo", "model MSD\n  Real x;\nequation\n  x = 1;\nend MSD;\n")
    request = build.check_request(config, "models/MSD.mo")
    assert (request.backend, request.name, request.source) == ("omc", "MSD", path)
    spec = build.build_spec(config, request, "r1")
    assert spec.fmu_path == "generated/MSD/MSD.fmu"
    assert spec.options == {"fmi_version": "2.0", "fmu_type": "me_cs", "fmi_flags": "s:cvode"}
