"""Pin and verify the installed OMSimulator package without importing it (ADR 0009).

``import OMSimulator`` loads ``libOMSimulator.so`` at once (spike (g)), and its sdist installs
binaries and Python code fetched without a hash, so the lock file vouches for neither. This
module hashes the installed package tree like an FMU and compares it with
``config.OMSIMULATOR_PIN``. It detects a changed install; it does not prove authenticity.

Two callers, the same functions:
- the server (``runs.py``, at ``run_start(system=...)``): refuse early with a clear message, and
  record the identity in ``SystemSpec.omsimulator``;
- the worker (``worker.py``), before it imports ``engines.oms_engine``: fail closed with
  ``failed{oms_changed}`` if the tree no longer matches the spec.

Never imports OMSimulator (ruff TID251 enforces it outside ``engines/``): it locates the package
with ``importlib.util.find_spec`` (which, for a top-level package, executes nothing) and reads
the version with ``importlib.metadata``.

Collaborators: ``config.OMSIMULATOR_PIN``, ``store/protocol.OmsVersion``.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import os
from pathlib import Path

from agentic_fmi.config import OMSIMULATOR_PIN
from agentic_fmi.store.protocol import OmsVersion

PACKAGE = "OMSimulator"
"""The import name (and distribution name) of the pinned package."""

SKIPPED_DIRS = frozenset({"__pycache__"})
"""Directories left out of the digest: bytecode caches change with every interpreter."""


def package_dir() -> Path | None:
    """The installed ``OMSimulator/`` package directory, or ``None`` if it isn't installed.

    Uses ``importlib.util.find_spec("OMSimulator").submodule_search_locations[0]``, resolved;
    never imports the package.
    """
    try:
        spec = importlib.util.find_spec(PACKAGE)
    except (ImportError, ValueError):
        return None
    locations = list(spec.submodule_search_locations or ()) if spec is not None else []
    return Path(locations[0]).resolve() if locations else None


def package_digest(directory: Path) -> str:
    """The sha256 that ``config.OMSIMULATOR_PIN.sha256`` pins, over every regular file below
    ``directory`` except those under a :data:`SKIPPED_DIRS` directory.

    Exactly this (the pinned value was computed so: 49 files, about 13 ms)::

        entries = POSIX paths relative to directory, from os.walk(directory, followlinks=False),
                  pruning SKIPPED_DIRS; files only
        digest = sha256()
        for rel in sorted(entries):
            digest.update(rel.encode("utf-8") + b"\\0")
            digest.update(sha256(file bytes).digest())
        return digest.hexdigest()

    A symlinked file is hashed by its target's bytes; a symlinked directory is not followed.

    Raises:
        OSError: a file can't be read.
    """
    entries: list[str] = []
    for root, dirs, files in os.walk(directory, followlinks=False):
        dirs[:] = [name for name in dirs if name not in SKIPPED_DIRS]
        base = Path(root)
        entries.extend((base / name).relative_to(directory).as_posix() for name in files)
    digest = hashlib.sha256()
    for rel in sorted(entries):
        digest.update(rel.encode("utf-8") + b"\0")
        with (directory / rel).open("rb") as handle:
            digest.update(hashlib.file_digest(handle, "sha256").digest())
    return digest.hexdigest()


def installed() -> OmsVersion | None:
    """The installed package's identity: ``importlib.metadata.version("OMSimulator")`` and
    :func:`package_digest` of :func:`package_dir`; ``None`` if the package or its metadata is
    missing, or a file of it can't be read."""
    directory = package_dir()
    if directory is None:
        return None
    try:
        version = importlib.metadata.version(PACKAGE)
        digest = package_digest(directory)
    except (importlib.metadata.PackageNotFoundError, OSError):
        return None
    return OmsVersion(version=version, sha256=digest)


def pinned() -> OmsVersion:
    """``config.OMSIMULATOR_PIN`` as the protocol's :class:`OmsVersion`."""
    return OmsVersion(version=OMSIMULATOR_PIN.version, sha256=OMSIMULATOR_PIN.sha256)


def pin_problem(found: OmsVersion | None, expected: OmsVersion) -> str | None:
    """Why ``found`` may not be used where ``expected`` is required, or ``None`` if it matches.

    The text names both versions and prints both full digests, then the fix: reinstall the
    pinned version (``uv sync --locked`` with the uv cache, ADR 0009), or, after reviewing a
    deliberate upgrade, update ``config.OMSIMULATOR_PIN``. ``found=None`` says OMSimulator is
    not installed and how to install it.
    """
    if found is None:
        return (
            f"OMSimulator {expected.version} is required for system runs, but it is not "
            "installed (or its package can't be read) in the environment of Agentic FMI. "
            "Install it with `uv sync --locked` in the agentic-fmi project, then try again."
        )
    if found == expected:
        return None
    return (
        "The installed OMSimulator package does not match the pinned one, so it is not "
        f"loaded: found version {found.version}, sha256 {found.sha256}; expected version "
        f"{expected.version}, sha256 {expected.sha256}. Reinstall the pinned version with "
        "`uv sync --locked` (it reuses the uv cache), or, after reviewing a deliberate "
        "upgrade, update config.OMSIMULATOR_PIN."
    )
