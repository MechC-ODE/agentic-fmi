"""Shared pytest fixtures for the Agentic FMI test suite.

Collaborators: pytest loads this file before the test modules under ``tests/``. The
``reference_fmus`` fixture points at the Reference FMUs cached by
``scripts/fetch-reference-fmus.py``. Tests read those files in place, never download them and
never modify them. The ``workspace`` and ``fmu`` fixtures give tests a temporary workspace and
copies of Reference FMUs inside it, for anything that writes or resolves paths.
"""

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
REFERENCE_FMUS_VERSION: str = "0.0.41"  # keep in sync with scripts/fetch-reference-fmus.py
REFERENCE_FMUS_DIR: Path = REPO_ROOT / ".cache" / "reference-fmus" / REFERENCE_FMUS_VERSION
COMPLETE_MARKER: str = ".complete"
SKIP_REASON: str = "Reference FMUs not cached; run: uv run python scripts/fetch-reference-fmus.py"


@pytest.fixture(scope="session")
def reference_fmus() -> Path:
    """Return the cached Reference FMUs directory, or skip when the cache is incomplete.

    The fetch script writes ``.complete`` only after a verified extraction, so a directory
    without that marker counts as not cached.
    """
    if not (REFERENCE_FMUS_DIR / COMPLETE_MARKER).is_file():
        pytest.skip(SKIP_REASON)
    return REFERENCE_FMUS_DIR


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Return an empty, symlink-free workspace directory inside the test's temp dir.

    Pass it to ``agentic_fmi.config.load_config`` for a ``Config`` whose allowed root is this
    directory. Nothing outside it is created by the fixture itself.
    """
    root = (tmp_path / "workspace").resolve()
    root.mkdir()
    return root


@pytest.fixture
def fmu(reference_fmus: Path, workspace: Path) -> Callable[[str, str], Path]:
    """Return a factory ``fmu(fmi_version, name)`` that copies a Reference FMU into ``workspace``.

    ``fmu("3.0", "BouncingBall")`` copies the cached ``3.0/BouncingBall.fmu`` to
    ``<workspace>/3.0/BouncingBall.fmu`` and returns that path, so tests can use both FMI
    versions of one model side by side. Calling it twice returns the same copy. The cache is
    only read; tests mutate the copy, never the cache.
    """

    def copy_fmu(fmi_version: str, name: str) -> Path:
        source = reference_fmus / fmi_version / f"{name}.fmu"
        if not source.is_file():
            pytest.fail(f"no cached Reference FMU {fmi_version}/{name}.fmu in {reference_fmus}")
        target = workspace / fmi_version / source.name
        target.parent.mkdir(exist_ok=True)
        if not target.exists():
            shutil.copy2(source, target)
        return target

    return copy_fmu
