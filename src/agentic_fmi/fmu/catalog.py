"""Find FMUs in the workspace and resolve FMU arguments to safe, absolute paths.

Every tool that takes an ``fmu`` argument resolves it here first, so the allowed-roots rule
lives in one place. Reads only directory listings, zip directories and ``modelDescription.xml``.

Collaborators: ``config.Config`` (workspace, allowed roots, limits), ``fmu/inspect.py`` (parsing
and the quick runnability check for list entries), ``mcp/server.py`` (``fmu_list``).

``fmu/inspect.py`` imports this module for :func:`resolve_fmu`, so :func:`list_fmus` imports
``inspect`` inside the function to avoid an import cycle.
"""

from __future__ import annotations

import difflib
import fnmatch
import os
from pathlib import Path, PurePosixPath

from pydantic import BaseModel

from agentic_fmi.config import Config
from agentic_fmi.errors import UserError

SKIP_DIRS = frozenset(
    {
        ".git",
        ".agentic-fmi",
        ".venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
    }
)
"""Directory names never descended into while scanning."""

MAX_SCAN_DEPTH = 8
"""Directory depth below the workspace that the scan reaches."""

MAX_CLOSE_MATCHES = 3
"""Suggestions offered when an FMU path is not found."""

GLOB_CHARS = frozenset("*?[")
"""A query containing one of these is an ``fnmatch`` glob; anything else is a substring."""


class FmuEntry(BaseModel):
    """One FMU in ``fmu_list``. Fields other than ``path`` are null when the XML can't be read."""

    path: str
    """Workspace-relative POSIX path; pass it back as the ``fmu`` argument."""
    size_bytes: int
    model_name: str | None = None
    fmi_version: str | None = None
    interfaces: list[str] = []
    runnable: bool | None = None
    """Quick check: a binary for this platform and an interface FMPy can run."""
    problem: str | None = None
    """Why the entry couldn't be read or isn't supported (FMI 1.0, corrupt zip)."""


class FmuPage(BaseModel):
    """A page of :class:`FmuEntry` sorted by path."""

    total: int
    offset: int
    limit: int
    items: list[FmuEntry]
    next_offset: int | None = None


def relative_path(config: Config, path: Path) -> str:
    """The workspace-relative POSIX path of ``path``, or its absolute POSIX path if it lies in
    another allowed root."""
    try:
        return path.relative_to(config.workspace).as_posix()
    except ValueError:
        return path.as_posix()


def matches(text: str, query: str) -> bool:
    """Case-insensitive match of ``text``: ``fnmatch`` glob if ``query`` has ``* ? [``, else
    substring."""
    haystack, needle = text.lower(), query.lower()
    if GLOB_CHARS & set(needle):
        return fnmatch.fnmatchcase(haystack, needle)
    return needle in haystack


def page_window(
    config: Config, total: int, offset: int, limit: int | None, what: str
) -> tuple[int, int]:
    """Validate ``offset`` and ``limit`` for a list of ``total`` items; return ``(offset, size)``.

    ``limit`` defaults to ``Limits.page_default`` and is capped at ``Limits.page_max``.

    Raises:
        UserError: negative offset, ``limit`` below 1, or an offset past the end (says ``total``).
    """
    limits = config.limits
    size = limits.page_default if limit is None else limit
    if size < 1:
        raise UserError(
            f"limit must be at least 1 (got {limit}); the maximum is {limits.page_max}."
        )
    if offset < 0:
        raise UserError(f"offset must be 0 or more (got {offset}).")
    if offset > 0 and offset >= total:
        raise UserError(
            f"offset {offset} is past the end: there are {total} {what}. "
            f"Use an offset below {total}, or 0 to start over."
        )
    return offset, min(size, limits.page_max)


def resolve_fmu(config: Config, fmu: str) -> Path:
    """Resolve an ``fmu`` argument to an absolute, symlink-free path inside an allowed root.

    A relative ``fmu`` is taken relative to ``config.workspace``. The result is the fully
    resolved path of an existing file, so symlinks pointing outside the workspace are caught.
    Containment is checked before existence, so a path outside the roots is refused without
    revealing whether it exists.

    Raises:
        UserError: the file doesn't exist (with close matches among known FMUs), isn't a
            ``.fmu`` file, or resolves outside ``config.allowed_roots`` ("copy it into the
            workspace").
    """
    if not fmu or not fmu.strip():
        raise UserError("The fmu argument is empty. Pass a path from fmu_list.")
    candidate = Path(fmu)
    if not candidate.is_absolute():
        candidate = config.workspace / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise UserError(
            f"Can't resolve FMU path {fmu!r}: {exc}. Call fmu_list for valid paths."
        ) from exc
    if not any(resolved.is_relative_to(root) for root in config.allowed_roots):
        raise UserError(
            f"FMU path {fmu!r} is outside the workspace ({config.workspace}), possibly through "
            "a symlink. Copy the FMU into the workspace and pass its workspace-relative path."
        )
    if candidate.suffix.lower() != ".fmu":
        raise UserError(
            f"{fmu!r} is not a .fmu file. Pass an FMU archive; call fmu_list to see the "
            "FMUs in the workspace."
        )
    if resolved.is_dir():
        raise UserError(
            f"{fmu!r} is a directory. Extracted FMUs are not supported; pass the .fmu archive."
        )
    if not resolved.is_file():
        raise UserError(_not_found_message(config, fmu))
    return resolved


def _not_found_message(config: Config, fmu: str) -> str:
    """The text for a missing FMU, naming close matches among the workspace's FMUs."""
    known = [relative_path(config, path) for path in find_fmus(config)]
    wanted_name = PurePosixPath(fmu.replace("\\", "/")).name
    same_name = [rel for rel in known if PurePosixPath(rel).name == wanted_name]
    close = difflib.get_close_matches(fmu, known, n=MAX_CLOSE_MATCHES, cutoff=0.4)
    suggestions = list(dict.fromkeys(same_name + close))[:MAX_CLOSE_MATCHES]
    text = f"FMU {fmu!r} was not found in the workspace."
    if suggestions:
        text += f" Close matches: {', '.join(suggestions)}."
    return text + " Call fmu_list to see all FMUs; paths are relative to the workspace."


def find_fmus(config: Config) -> list[Path]:
    """All ``*.fmu`` files under the workspace, sorted, skipping :data:`SKIP_DIRS`, symlinks that
    leave the allowed roots, and anything deeper than :data:`MAX_SCAN_DEPTH`."""
    root = config.workspace
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        directory = Path(dirpath)
        depth = len(directory.relative_to(root).parts)
        dirnames[:] = [] if depth >= MAX_SCAN_DEPTH else [d for d in dirnames if d not in SKIP_DIRS]
        found.extend(
            directory / name
            for name in filenames
            if name.lower().endswith(".fmu") and _usable(config, directory / name)
        )
    return sorted(found, key=lambda path: path.relative_to(root).as_posix())


def _usable(config: Config, path: Path) -> bool:
    """False for a symlink that is broken or points outside the allowed roots."""
    if not path.is_symlink():
        return True
    target = path.resolve()
    return target.is_file() and any(target.is_relative_to(root) for root in config.allowed_roots)


def list_fmus(
    config: Config, pattern: str | None = None, offset: int = 0, limit: int | None = None
) -> FmuPage:
    """Page through the workspace's FMUs.

    ``pattern`` is a case-insensitive substring or ``fnmatch`` glob on the relative path.
    ``limit`` defaults to ``Limits.page_default`` and is capped at ``Limits.page_max``. Only the
    entries on the page are parsed.
    """
    paths = find_fmus(config)
    if pattern:
        paths = [path for path in paths if _path_matches(config, path, pattern)]
    start, size = page_window(config, len(paths), offset, limit, "FMUs")
    page = paths[start : start + size]
    end = start + len(page)
    return FmuPage(
        total=len(paths),
        offset=start,
        limit=size,
        items=[_entry(config, path) for path in page],
        next_offset=end if end < len(paths) else None,
    )


def _path_matches(config: Config, path: Path, pattern: str) -> bool:
    """Whether ``pattern`` matches the relative path or, for globs, just the file name."""
    relative = relative_path(config, path)
    return matches(relative, pattern) or matches(path.name, pattern)


def _entry(config: Config, path: Path) -> FmuEntry:
    """Parse one FMU into a list entry; read failures become ``problem`` text."""
    from agentic_fmi.fmu import inspect as fmu_inspect  # cycle: inspect imports this module

    relative = relative_path(config, path)
    try:
        size = path.stat().st_size
        md = fmu_inspect.read_description(path)
    except OSError as exc:
        return FmuEntry(path=relative, size_bytes=0, problem=f"can't read the file: {exc.strerror}")
    except UserError as exc:
        return FmuEntry(path=relative, size_bytes=size, problem=str(exc))
    blocker = fmu_inspect.platform_blocker(path, md)
    return FmuEntry(
        path=relative,
        size_bytes=size,
        model_name=md.modelName,
        fmi_version=md.fmiVersion,
        interfaces=list(fmu_inspect.interfaces_of(md)),
        runnable=blocker is None,
        problem=blocker,
    )
