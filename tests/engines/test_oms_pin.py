"""Tests for ``oms_pin.py``: the installed OMSimulator package tree against ``OMSIMULATOR_PIN``.

Collaborators: ``agentic_fmi.oms_pin`` (under test) and ``agentic_fmi.config.OMSIMULATOR_PIN``.
Changes are made to a copy of the package tree in ``tmp_path``, never to the installed one, and
nothing here imports OMSimulator.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agentic_fmi import oms_pin
from agentic_fmi.config import OMSIMULATOR_PIN
from agentic_fmi.store.protocol import OmsVersion


@pytest.fixture
def tree_copy(tmp_path: Path) -> Path:
    """A copy of the installed ``OMSimulator/`` tree (bytecode caches included)."""
    source = oms_pin.package_dir()
    assert source is not None, "OMSimulator is a core dependency: run uv sync --locked"
    return Path(shutil.copytree(source, tmp_path / "OMSimulator", symlinks=True))


def test_installed_package_matches_the_pin() -> None:
    found = oms_pin.installed()
    assert found == OmsVersion(version=OMSIMULATOR_PIN.version, sha256=OMSIMULATOR_PIN.sha256)
    assert found == oms_pin.pinned()
    assert oms_pin.pin_problem(found, oms_pin.pinned()) is None


def test_digest_of_an_identical_copy_equals_the_pin(tree_copy: Path) -> None:
    assert oms_pin.package_digest(tree_copy) == OMSIMULATOR_PIN.sha256


def test_one_changed_byte_changes_the_digest(tree_copy: Path) -> None:
    library = tree_copy / "libOMSimulator.so"
    data = bytearray(library.read_bytes())
    data[len(data) // 2] ^= 0x01
    library.write_bytes(bytes(data))
    assert oms_pin.package_digest(tree_copy) != OMSIMULATOR_PIN.sha256


def test_renamed_or_added_files_change_the_digest(tree_copy: Path) -> None:
    (tree_copy / "cref.py").rename(tree_copy / "cref2.py")
    renamed = oms_pin.package_digest(tree_copy)
    assert renamed != OMSIMULATOR_PIN.sha256
    (tree_copy / "extra.py").write_text("")
    assert oms_pin.package_digest(tree_copy) not in (renamed, OMSIMULATOR_PIN.sha256)


def test_bytecode_caches_are_ignored(tree_copy: Path) -> None:
    for cache in ("__pycache__", "schema/__pycache__"):
        (tree_copy / cache).mkdir(parents=True, exist_ok=True)
        (tree_copy / cache / "junk.cpython-312.pyc").write_bytes(b"changes with the interpreter")
    assert oms_pin.package_digest(tree_copy) == OMSIMULATOR_PIN.sha256


def test_missing_package_or_metadata_is_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oms_pin, "package_dir", lambda: None)
    assert oms_pin.installed() is None
    message = oms_pin.pin_problem(None, oms_pin.pinned())
    assert message is not None
    assert "not installed" in message and "uv sync --locked" in message
    assert OMSIMULATOR_PIN.version in message


def test_mismatch_message_names_both_versions_and_digests() -> None:
    found = OmsVersion(version="3.0.0.post199", sha256="ab" * 32)
    message = oms_pin.pin_problem(found, oms_pin.pinned())
    assert message is not None
    for text in (found.version, found.sha256, OMSIMULATOR_PIN.version, OMSIMULATOR_PIN.sha256):
        assert text in message
    assert "uv sync --locked" in message and "config.OMSIMULATOR_PIN" in message
    assert len(message) <= 500  # fits Limits.message_max_chars uncut in a failed status
    same_tree_other_version = OmsVersion(version="3.0.1", sha256=OMSIMULATOR_PIN.sha256)
    assert oms_pin.pin_problem(same_tree_other_version, oms_pin.pinned()) is not None
