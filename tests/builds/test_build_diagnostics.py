"""Build diagnostics without running a build: the pythonfmu log parser on captured output, the
diagnostics text and its caps, the tool's environment and argv.

Collaborators: ``agentic_fmi.build_diagnostics`` (the parser and the text) and
``agentic_fmi.build`` (the environment and argv), both under test, and
``agentic_fmi.config.Limits``. The log fixtures are real pythonfmu 0.7.0 output captured in the
dev container (CPython 3.12) and re-rooted at a temporary ``work/`` directory, which is where
the worker builds.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentic_fmi import build, build_diagnostics
from agentic_fmi.build_diagnostics import Diagnostic
from agentic_fmi.config import Limits

SITE = "/home/dev/.venvs/agentic-fmi/lib/python3.12/site-packages"
STDLIB = "/home/dev/.local/share/uv/python/cpython-3.12.13-linux-x86_64-gnu/lib/python3.12"

HEAD = f"""Traceback (most recent call last):
  File "<frozen runpy>", line 198, in _run_module_as_main
  File "<frozen runpy>", line 88, in _run_code
  File "{SITE}/pythonfmu/__main__.py", line 59, in <module>
    cli_main()
  File "{SITE}/pythonfmu/__main__.py", line 55, in cli_main
    execute(**options)
"""
IMPORT_FRAMES = f"""  File "{SITE}/pythonfmu/builder.py", line 245, in build_FMU
    model_class = get_model_class(script_file)
                  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "{SITE}/pythonfmu/builder.py", line 76, in get_model_class
    module = importlib.import_module(modulename)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "{STDLIB}/importlib/__init__.py", line 90, in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "<frozen importlib._bootstrap>", line 1387, in _gcd_import
  File "<frozen importlib._bootstrap>", line 1360, in _find_and_load
  File "<frozen importlib._bootstrap>", line 1331, in _find_and_load_unlocked
  File "<frozen importlib._bootstrap>", line 935, in _load_unlocked
"""
COMPILE_FRAMES = """  File "<frozen importlib._bootstrap_external>", line 995, in exec_module
  File "<frozen importlib._bootstrap_external>", line 1133, in get_code
  File "<frozen importlib._bootstrap_external>", line 1063, in source_to_code
  File "<frozen importlib._bootstrap>", line 488, in _call_with_frames_removed
"""
EXEC_FRAMES = """  File "<frozen importlib._bootstrap_external>", line 999, in exec_module
  File "<frozen importlib._bootstrap>", line 488, in _call_with_frames_removed
"""

SYNTAX_SOURCE = """from pythonfmu import Fmi2Slave


class AfmiSyntax(Fmi2Slave):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.k = (2.0 +
"""
SYNTAX_LOG = (
    HEAD
    + IMPORT_FRAMES
    + COMPILE_FRAMES
    + """  File "{work}/src/afmi_bad.py", line 7
    self.k = (2.0 +
             ^
SyntaxError: '(' was never closed
"""
)

NAME_LOG = (
    """{work}/src/afmi_bad.py:4: UserWarning: gain is experimental
  warnings.warn("gain is experimental")
"""
    + HEAD
    + IMPORT_FRAMES
    + EXEC_FRAMES
    + """  File "{work}/src/afmi_bad.py", line 5, in <module>
    LIMIT = undefined_limit * 2
            ^^^^^^^^^^^^^^^
NameError: name 'undefined_limit' is not defined
"""
)

INIT_LOG = (
    HEAD
    + f"""  File "{SITE}/pythonfmu/builder.py", line 287, in build_FMU
    model_identifier, xml = get_model_description(
                            ^^^^^^^^^^^^^^^^^^^^^^
  File "{SITE}/pythonfmu/builder.py", line 183, in get_model_description
    instance = getattr(fmu_interface, class_name)(instance_name="dummyInstance")
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
"""
    + """  File "{work}/tmp/pythonfmu_wvqrii7e/afmi_bad.py", line 11, in __init__
    self.k = helper(1.0)
             ^^^^^^^^^^^
  File "{work}/tmp/pythonfmu_wvqrii7e/afmi_bad.py", line 5, in helper
    return value / 0
           ~~~~~~^~~
ZeroDivisionError: float division by zero
"""
)

NO_CLASS_LOG = (
    HEAD
    + f"""  File "{SITE}/pythonfmu/builder.py", line 245, in build_FMU
    model_class = get_model_class(script_file)
                  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "{SITE}/pythonfmu/builder.py", line 91, in get_model_class
    raise ValueError(...) from None
"""
    + """ValueError: No child class of Fmi2Slave found in module {work}/src/afmi_bad.py
"""
)

TWO_CLASSES_LOG = (
    HEAD
    + """ValueError: Non-unique Fmi2Slave-derived class in module {work}/src/afmi_bad.py. Found \
[<class 'afmi_bad.A'>, <class 'afmi_bad.B'>].
"""
)

SOURCE_REL = "models/afmi_bad.py"


@pytest.fixture
def work(tmp_path: Path) -> Path:
    """A worker's ``work/`` directory with the source copy ``src/afmi_bad.py``."""
    path = tmp_path / "runs" / "r1" / "work"
    (path / "src").mkdir(parents=True)
    (path / "src" / "afmi_bad.py").write_text(SYNTAX_SOURCE, encoding="utf-8")
    return path


def _parse(log: str, work: Path) -> list[Diagnostic]:
    return build_diagnostics.parse_pythonfmu_log(
        log.format(work=work), work / "src" / "afmi_bad.py", SOURCE_REL
    )


def _no_internals(text: str, work: Path) -> None:
    for internal in (str(work), "/work/", "/tmp", "site-packages", "importlib", "<frozen", "\x1b"):
        assert internal not in text


def test_a_syntax_error_gets_the_line_and_the_column_in_the_original_source(work: Path) -> None:
    (error,) = _parse(SYNTAX_LOG, work)
    assert error.format() == f"{SOURCE_REL}:7:18: error: SyntaxError: '(' was never closed"
    # Column 18 is the "(" of "        self.k = (2.0 +" (Python printed the line unindented).
    assert SYNTAX_SOURCE.splitlines()[6].index("(") + 1 == 18


def test_a_runtime_error_points_at_the_source_frame_and_warnings_follow(work: Path) -> None:
    error, warning = _parse(NAME_LOG, work)
    assert error.format() == (
        f"{SOURCE_REL}:5: error: NameError: name 'undefined_limit' is not defined"
    )
    assert warning.format() == f"{SOURCE_REL}:4: warning: UserWarning: gain is experimental"


def test_frames_in_pythonfmus_temporary_copy_map_to_the_source(work: Path) -> None:
    (error,) = _parse(INIT_LOG, work)
    assert error.format() == f"{SOURCE_REL}:5: error: ZeroDivisionError: float division by zero"


def test_errors_without_a_source_frame_name_the_file_without_temporary_paths(work: Path) -> None:
    (error,) = _parse(NO_CLASS_LOG, work)
    assert error.path == SOURCE_REL and error.line is None
    assert error.message.startswith(
        f"ValueError: No child class of Fmi2Slave found in module {SOURCE_REL} ("
    )
    assert "do_step" in error.message
    (error,) = _parse(TWO_CLASSES_LOG, work)
    assert f"in module {SOURCE_REL}. Found" in error.message
    _no_internals(error.format(), work)


def test_ansi_escapes_are_removed(work: Path) -> None:
    colored = NAME_LOG.replace("NameError:", "\x1b[1;35mNameError\x1b[0m:")
    error, _ = _parse(colored, work)
    assert error.message == "NameError: name 'undefined_limit' is not defined"


def test_output_without_a_traceback_has_no_error(work: Path) -> None:
    assert _parse("building...\nAll done\n", work) == []


def test_the_failure_text_has_the_fixed_shape(work: Path) -> None:
    text = build_diagnostics.format_failure(
        _parse(NAME_LOG, work),
        backend="pythonfmu",
        log_path=".agentic-fmi/runs/r1/build.log",
        limits=Limits(),
    )
    assert text.splitlines() == [
        "Build failed (pythonfmu): 1 error, 1 warning.",
        f"{SOURCE_REL}:5: error: NameError: name 'undefined_limit' is not defined",
        f"{SOURCE_REL}:4: warning: UserWarning: gain is experimental",
        "Full log: .agentic-fmi/runs/r1/build.log",
        "Fix the source and call fmu_build again.",
    ]
    _no_internals(text, work)


def test_a_flood_of_errors_stays_within_the_line_and_character_caps() -> None:
    limits = Limits()
    flood = [
        Diagnostic("error", f"undefined name 'v{i}' " + "x" * 150, SOURCE_REL, i + 1)
        for i in range(200)
    ]
    text = build_diagnostics.format_failure(flood, backend="omc", log_path="b.log", limits=limits)
    lines = text.splitlines()
    assert len(text) <= limits.build_diagnostics_max_chars
    assert lines[0] == "Build failed (omc): 200 errors."
    shown = [line for line in lines if ": error: " in line]
    assert 1 <= len(shown) <= limits.build_diagnostics_max_lines
    assert lines[-3] == f"and {200 - len(shown)} more (see the full log)"
    assert lines[-2:] == ["Full log: b.log", "Fix the source and call fmu_build again."]


def test_one_huge_message_is_cut_with_an_ellipsis() -> None:
    limits = Limits()
    huge = [Diagnostic("error", "y" * 10_000, SOURCE_REL, 1), Diagnostic("warning", "w")]
    text = build_diagnostics.format_failure(
        huge, backend="pythonfmu", log_path="b.log", limits=limits
    )
    assert len(text) <= limits.build_diagnostics_max_chars
    assert text.splitlines()[1].endswith("…")
    assert "and 1 more" in text


def test_a_headline_replaces_the_count_line() -> None:
    text = build_diagnostics.format_failure(
        [], backend="pythonfmu", log_path="b.log", limits=Limits(), headline="Build failed: X."
    )
    assert text.splitlines()[0] == "Build failed: X."


@pytest.mark.parametrize(
    ("diagnostic", "line"),
    [
        (Diagnostic("error", "bad", "m.mo", 3, 7), "m.mo:3:7: error: bad"),
        (Diagnostic("warning", "odd", "m.mo", 3), "m.mo:3: warning: odd"),
        (Diagnostic("error", "bad", "m.mo"), "m.mo: error: bad"),
        (Diagnostic("error", "bad"), "error: bad"),
    ],
)
def test_diagnostic_lines(diagnostic: Diagnostic, line: str) -> None:
    assert diagnostic.format() == line


def test_the_tool_gets_an_allowlisted_environment(tmp_path: Path) -> None:
    environ = {
        "PATH": "/usr/bin",
        "HOME": "/home/dev",
        "LANG": "C.UTF-8",
        "OPENMODELICAHOME": "/usr",
        "PYTHONPATH": "/evil",
        "AWS_SECRET_ACCESS_KEY": "secret",
        "LD_PRELOAD": "/evil.so",
        "TMPDIR": "/tmp",
    }
    env = build.build_env(tmp_path, environ)
    assert env == {
        "PATH": "/usr/bin",
        "HOME": "/home/dev",
        "LANG": "C.UTF-8",
        "OPENMODELICAHOME": "/usr",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "TMPDIR": str(tmp_path / "tmp"),
    }


def test_pythonfmu_runs_isolated_without_bytecode_and_never_deploys(tmp_path: Path) -> None:
    argv = build.pythonfmu_argv(tmp_path / "src" / "a.py", tmp_path / "out")
    assert argv[:6] == [sys.executable, "-I", "-B", "-m", "pythonfmu", "build"]
    assert "deploy" not in argv
    assert argv[6:] == ["-f", str(tmp_path / "src" / "a.py"), "-d", str(tmp_path / "out")]
