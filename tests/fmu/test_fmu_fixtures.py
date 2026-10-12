"""Tests for the shared ``workspace`` and ``fmu`` fixtures in ``tests/conftest.py``.

Collaborators: ``tests/conftest.py`` (the fixtures under test) and the cached Reference FMUs,
which these tests only read. They prove that tests get private copies and can't alter the cache.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest

FmuFactory = Callable[[str, str], Path]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_workspace_is_an_empty_resolved_directory(workspace: Path) -> None:
    """The workspace exists, is empty and is already symlink-free."""
    assert workspace.is_dir() and list(workspace.iterdir()) == []
    assert workspace == workspace.resolve()


def test_fmu_copies_into_the_workspace_and_leaves_the_cache_untouched(
    fmu: FmuFactory, workspace: Path, reference_fmus: Path
) -> None:
    """The copy has the cache's bytes, lives under ``<workspace>/<version>/``, and is private."""
    cached = reference_fmus / "3.0" / "BouncingBall.fmu"
    cache_hash = _sha256(cached)
    copy = fmu("3.0", "BouncingBall")
    assert copy == workspace / "3.0" / "BouncingBall.fmu"
    assert _sha256(copy) == cache_hash
    copy.write_bytes(b"mutated")
    assert _sha256(cached) == cache_hash


def test_fmu_returns_the_same_copy_when_called_twice_and_keeps_versions_apart(
    fmu: FmuFactory, workspace: Path
) -> None:
    """Repeat calls reuse the copy; the two FMI versions of one model get separate folders."""
    assert fmu("2.0", "BouncingBall") == fmu("2.0", "BouncingBall")
    assert fmu("2.0", "BouncingBall") != fmu("3.0", "BouncingBall")
    assert sorted(p.relative_to(workspace).as_posix() for p in workspace.rglob("*.fmu")) == [
        "2.0/BouncingBall.fmu",
        "3.0/BouncingBall.fmu",
    ]


def test_fmu_fails_clearly_for_a_name_that_is_not_cached(fmu: FmuFactory) -> None:
    """A typo in the model name fails the test with the missing path, not a copy error."""
    with pytest.raises(pytest.fail.Exception, match=r"no cached Reference FMU 3\.0/Nope\.fmu"):
        fmu("3.0", "Nope")
