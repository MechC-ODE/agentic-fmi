"""Phase 0 smoke tests: the package imports, the ``afmi`` CLI runs, the cached Reference FMUs
parse, and the OMSimulator Python API resolves.

Collaborators: ``agentic_fmi`` and ``agentic_fmi.cli.main`` (the code under test), the ``afmi``
console script in the interpreter's bin directory, FMPy's ``read_model_description`` (it reads
``modelDescription.xml`` only, never an FMU binary), and the ``reference_fmus`` fixture from
``tests/conftest.py``. No simulation runs in this module.
"""

import shutil
import subprocess
import sys
import zipfile
from importlib.metadata import version
from pathlib import Path

import pytest
from fmpy import read_model_description

import agentic_fmi
from agentic_fmi.cli import main

CONSOLE_SCRIPT_TIMEOUT_SECONDS = 60
BOUNCING_BALL = "BouncingBall.fmu"


def test_package_imports_and_version_matches_installed_metadata() -> None:
    """The package imports and reports the version its installed metadata declares."""
    assert isinstance(agentic_fmi.__version__, str)
    assert agentic_fmi.__version__ != ""
    assert agentic_fmi.__version__ == version("agentic-fmi")


def test_version_flag_prints_afmi_version_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``main(["--version"])`` prints ``afmi <version>`` and exits with status 0."""
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out == f"afmi {agentic_fmi.__version__}\n"


def test_no_arguments_prints_the_help_and_returns_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``main([])`` returns 0 and prints the usage with the ``mcp`` and ``worker`` commands."""
    assert main([]) == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: afmi")
    assert "mcp" in out and "worker" in out


def test_afmi_console_script_runs_version_flag() -> None:
    """The installed ``afmi`` script, found next to ``sys.executable``, runs and exits 0."""
    bin_dir = Path(sys.executable).parent
    script = shutil.which("afmi", path=str(bin_dir))
    assert script is not None, f"no afmi console script in {bin_dir}; run: uv sync"
    result = subprocess.run(
        [script, "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=CONSOLE_SCRIPT_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [f"afmi {agentic_fmi.__version__}"]


@pytest.mark.parametrize("fmi_version", ["2.0", "3.0"])
def test_bouncing_ball_model_description_parses(reference_fmus: Path, fmi_version: str) -> None:
    """FMPy parses BouncingBall's XML, reports its FMI version, and ``e`` is a tunable parameter."""
    fmu = reference_fmus / fmi_version / BOUNCING_BALL
    assert fmu.is_file(), f"missing {fmu}; re-run scripts/fetch-reference-fmus.py"
    description = read_model_description(fmu)
    assert description.fmiVersion == fmi_version
    coefficients = [v for v in description.modelVariables if v.name == "e"]
    assert len(coefficients) == 1, f"expected one variable named 'e', found {len(coefficients)}"
    assert coefficients[0].causality == "parameter"
    assert coefficients[0].variability == "tunable"


def test_every_cached_fmu_is_a_zip_with_model_description(reference_fmus: Path) -> None:
    """Each cached ``*.fmu`` is an intact zip archive that contains ``modelDescription.xml``.

    Only the archive directory and the CRC check are read. No FMU binary is loaded.
    """
    fmus = sorted(reference_fmus.rglob("*.fmu"))
    assert fmus, f"no *.fmu files under {reference_fmus}"
    problems: list[str] = []
    for fmu in fmus:
        name = fmu.relative_to(reference_fmus).as_posix()
        if not zipfile.is_zipfile(fmu):
            problems.append(f"{name}: not a zip archive")
            continue
        with zipfile.ZipFile(fmu) as archive:
            corrupt = archive.testzip()
            if corrupt is not None:
                problems.append(f"{name}: corrupt member {corrupt!r}")
            if "modelDescription.xml" not in archive.namelist():
                problems.append(f"{name}: no modelDescription.xml")
    assert problems == [], "\n".join(problems)


@pytest.mark.oms
def test_omsimulator_ssp_api_resolves() -> None:
    """``import OMSimulator`` works and ``SSP`` and ``CRef`` resolve. Nothing is simulated.

    OMSimulator is a core dependency (ADR 0009); skipped only in a broken environment.
    """
    oms = pytest.importorskip(
        "OMSimulator", reason="OMSimulator not installed; run: uv sync --locked"
    )
    from OMSimulator import SSP, CRef

    assert oms.__name__ == "OMSimulator"
    assert SSP is not None
    assert CRef is not None
