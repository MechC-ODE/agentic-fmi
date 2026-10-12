"""Build FMI 2.0 FMUs from one source file: pythonfmu for ``.py``, OpenModelica ``omc`` for ``.mo``.

A build is a job in the run store (ADR 0008): ``runs.start_build`` validates the request here
(:func:`check_request`, no subprocess), writes a :class:`BuildSpec` with a private copy of the
hashed source bytes and spawns the ordinary worker, which calls :func:`execute_build`. One
module branches on the file extension; there is no backend class, protocol or registry.

Server side (imported by ``runs.py``; never runs a tool, never imports user code):
:func:`check_request`, :func:`resolve_source`, :func:`check_name`, :func:`build_spec`.

Worker side (called by ``worker.py`` inside the worker's process group):
:func:`execute_build` runs the tool in ``runs/<id>/work/`` with an allowlisted environment
(:func:`build_env`), argv only, stdin from ``/dev/null`` and output to ``runs/<id>/build.log``;
decides success from the tool's own result (pythonfmu: exit status 0 and one ``.fmu``; omc:
the path ``buildModelFMU`` returned, never the exit status, which is 0 on failure); checks the
FMU's ``modelDescription.xml``; then commits ``generated/<name>/<name>.fmu`` and
``build.json`` (each through a temporary file in the same directory and ``os.replace``) and
the job's own ``runs/<id>/build.json``. A failure, stop or timeout leaves ``generated/``
untouched. ``build_diagnostics.py`` (``parse_pythonfmu_log``, ``parse_omc_log``,
``format_failure``) turns tool output into the diagnostics text ``path:line[:col]: error:
message``.

pythonfmu names its output after the slave class (``PIController.fmu``); the worker renames it
to ``<name>.fmu`` (default: the file stem) when it commits.

Security (ADR 0008): building executes the source (pythonfmu imports it; omc may evaluate
functions and compile external C). This is isolation, not a sandbox: the tool runs as the user,
with network access (omc alone gets an unroutable proxy, :data:`OMC_OFFLINE_ENV`, so its own
libcurl can't fetch a package index or library; compiled model code isn't restricted). The
trust boundary is the user reviewing the file Claude wrote (the Write prompt) and approving
the ``fmu_build`` call. ruff TID251 bans importing ``pythonfmu`` in
``src/``; it only ever runs as ``sys.executable -I -B -m pythonfmu build``. Never call
``pythonfmu deploy``, ``installPackage`` or ``updatePackageIndex``.

Collaborators: ``config`` (``Config``, ``Limits``, ``Timing``), ``errors.UserError``,
``build_diagnostics.py`` (the log parsers and ``format_failure``), ``store/protocol.py``
(``BuildSpec``, ``BuildRecord``), ``store/runstore.py`` (layout, ``write_build_record``),
``store/atomic.py`` (``atomic_open``, ``locked``), ``fmu/inspect.py``
(``read_description``, ``sha256_file``) and ``fmu/catalog.py`` (``relative_path``). Callers:
``runs.start_build`` (server) and ``worker.py``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import keyword
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Final

from agentic_fmi.build_diagnostics import (
    OMC_MARKER,
    Diagnostic,
    OmcLog,
    format_failure,
    parse_omc_log,
    parse_pythonfmu_log,
)
from agentic_fmi.config import Config, Limits, Timing
from agentic_fmi.errors import ProtocolError, UserError
from agentic_fmi.fmu.catalog import relative_path
from agentic_fmi.fmu.inspect import ModelDescription, interfaces_of, read_description, sha256_file
from agentic_fmi.store import runstore
from agentic_fmi.store.atomic import atomic_open, locked
from agentic_fmi.store.protocol import (
    BUILD_NAME_PATTERN,
    Backend,
    BuildExperiment,
    BuildRecord,
    BuildSpec,
    BuildSummary,
    BuildVersions,
    BuiltFmu,
    SourceRef,
    Versions,
    utc_now,
)

BACKENDS: Final[Mapping[str, Backend]] = {".py": "pythonfmu", ".mo": "omc"}
"""Source suffix (compared in lower case) -> build tool. Anything else is refused."""

PYTHONFMU_OPTIONS: Final[Mapping[str, str]] = {"fmi_version": "2.0", "fmu_type": "cs"}
"""Recorded in the spec and ``build.json``; pythonfmu 0.7 writes FMI 2.0 Co-Simulation only."""

OMC_OPTIONS: Final[Mapping[str, str]] = {
    "fmi_version": "2.0",
    "fmu_type": "me_cs",
    "fmi_flags": "s:cvode",
}
"""``buildModelFMU(..., version="2.0", fmuType="me_cs")`` after
``setCommandLineOptions("--fmiFlags=s:cvode")``: without CVode the CS interface integrates with
forward Euler (spike (f): 8e-3 against 3e-7 max error). Not a tool parameter yet."""

PROMISED_INTERFACES: Final[Mapping[Backend, frozenset[str]]] = {
    "pythonfmu": frozenset({"CoSimulation"}),
    "omc": frozenset({"CoSimulation", "ModelExchange"}),
}
"""The interfaces a built FMU must declare, per backend; anything less fails the build."""

SLAVE_MARKER: Final = b"Fmi2Slave"
"""A ``.py`` source without this text is refused before any subprocess (it can't define a
pythonfmu slave, and refusing it keeps arbitrary scripts from being run by a build)."""

WINDOWS_RESERVED: Final = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)
"""Build names refused (case-insensitively): the workspace may sit on a Windows bind mount."""

ENV_ALLOWLIST: Final = ("PATH", "HOME", "LANG")
"""Variables passed from the worker's environment to the build tool, if set. ``HOME`` must be
writable: omc installs the Modelica library cache into ``~/.openmodelica`` on first use."""

ENV_PREFIXES: Final = ("OPENMODELICA",)
"""Variable-name prefixes also passed through (``OPENMODELICAHOME``, ``OPENMODELICALIBRARY``)."""

ENV_FIXED: Final[Mapping[str, str]] = {
    "PYTHONNOUSERSITE": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONHASHSEED": "0",
}
"""Set for every build (``-I`` already ignores ``PYTHON*``; these cover tools that spawn their
own Python). ``TMPDIR`` is set to ``runs/<id>/work/tmp``."""

OMC_INSTALL_HINT: Final = (
    "OpenModelica's omc is not on PATH, so .mo sources can't be built. The dev container "
    "installs it by default: rebuild the image with WITH_OPENMODELICA=1 (docker compose build). "
    "On another Ubuntu host add the OpenModelica apt repository "
    "(https://openmodelica.org/download/download-linux) and run: "
    "sudo apt install --no-install-recommends omc omlibrary. Then call fmu_build again."
)
"""The ``UserError`` text for a ``.mo`` build without ``omc`` on ``PATH`` (no subprocess)."""

OFFLINE_PROXY: Final = "http://127.0.0.1:9"
"""Nothing listens on the loopback discard port, so a connection is refused at once and a
download fails fast instead of hanging."""

OMC_OFFLINE_ENV: Final[Mapping[str, str]] = {
    "http_proxy": OFFLINE_PROXY,
    "https_proxy": OFFLINE_PROXY,
    "HTTP_PROXY": OFFLINE_PROXY,
    "HTTPS_PROXY": OFFLINE_PROXY,
    "no_proxy": "",
    "NO_PROXY": "",
}
"""Added to omc's environment only (never pythonfmu's), so omc's own libcurl can't reach the
network. On a fresh ``HOME`` omc tries to download the package index: that attempt now fails
with "Couldn't connect to server" and harmless ``Error:`` lines, which :func:`parse_omc_log`
drops. Nothing is ever fetched, so a library that isn't installed is an error, not a download."""

OMC_SCRIPT: Final = "build.mos"
"""The script omc runs, written into ``work/``."""

WORK_SRC, WORK_OUT, WORK_TMP = "src", "out", "tmp"
"""``work/`` subdirectories: the source copy the tool reads, its output, its ``TMPDIR``."""

TOOL_POLL_S: Final = 0.1
"""How often the worker checks for a stop request while the build tool runs."""

LOG_READ_MAX_BYTES: Final = 1 << 20
"""The end of ``build.log`` parsed for diagnostics (a traceback is always at the end)."""

SOURCE_ARG_MAX_CHARS: Final = 200
"""Longest echo of the caller's ``source`` argument in a refusal."""

_NAME = re.compile(BUILD_NAME_PATTERN, re.ASCII)
_MODELICA_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
_MODELICA_SKIPPED = re.compile(r'/\*.*?\*/|//[^\n]*|"(?:[^"\\\n]|\\.)*"', re.DOTALL)
"""Comments and one-line string literals, blanked before the class definitions are searched."""
_MODELICA_CLASS = re.compile(
    r"\b(?P<kind>model|block|class|package|function|record|connector|type)\s+(?P<name>[A-Za-z_]\w*)",
    re.ASCII,
)


@dataclass(frozen=True)
class BuildRequest:
    """A validated ``fmu_build`` request; :func:`build_spec` turns it into the job's spec."""

    source: Path
    """Absolute, symlink-resolved source path inside the workspace."""
    source_rel: str
    """Workspace-relative POSIX path, as recorded in ``SourceRef.path``."""
    data: bytes
    """The source bytes read once; hashed into the spec and copied to ``runs/<id>/source/``."""
    sha256: str
    backend: Backend
    name: str
    overwrite: bool


@dataclass(frozen=True)
class BuildOutcome:
    """What :func:`execute_build` reports to the worker. Exactly one case holds."""

    record: BuildRecord | None = None
    """Success: the FMU, ``generated/<name>/build.json`` and ``runs/<id>/build.json`` exist."""
    failure: str | None = None
    """``build_failed``: the diagnostics text (:func:`format_failure`); nothing was written."""
    stopped: bool = False
    """A stop request arrived before the commit; nothing was written."""


@dataclass(frozen=True)
class _ToolResult:
    """A finished tool run, read by its backend's rules."""

    fmu: Path | None
    """The FMU the tool reported, if it reported success."""
    diagnostics: list[Diagnostic]
    headline: str | None = None
    """The count line to use when the log names no error."""
    versions: dict[str, str | None] = field(default_factory=dict)
    """``BuildVersions`` fields only the tool knows (``pythonfmu``, ``omc``, ``msl``)."""


# --- server side: validation before any job or subprocess ------------------------------------


def check_request(
    config: Config,
    source: str,
    *,
    name: str | None = None,
    overwrite: bool = False,
) -> BuildRequest:
    """Validate an ``fmu_build`` request and read the source once. Runs no subprocess.

    Order: :func:`resolve_source`; the suffix picks the backend (:data:`BACKENDS`); a regular
    file of at most ``Limits.build_source_max_bytes``, decodable as UTF-8; ``.py``: contains
    :data:`SLAVE_MARKER` and :func:`module_name_problem` is ``None``; ``.mo``:
    :func:`modelica_class_problem` is ``None`` and :func:`find_omc` finds ``omc`` (else
    :data:`OMC_INSTALL_HINT`); then the name (default :func:`default_name`) passes
    :func:`check_name` (without the "already being built" rule, which ``runs.start_build``
    applies under its admission lock).

    Raises:
        UserError: every refusal, with the fix. A path outside the workspace is refused with
            the same text whether or not it exists.
    """
    path = resolve_source(config, source)
    rel = relative_path(config, path)
    backend = BACKENDS.get(path.suffix.lower())
    if backend is None:
        raise UserError(
            f"{rel} is not a .py or .mo file. fmu_build builds a pythonfmu slave (.py) or a "
            "Modelica model (.mo); pass one of those."
        )
    data = _read_source(path, rel, config.limits.build_source_max_bytes)
    if backend == "pythonfmu":
        _check_python_source(path, rel, data)
    else:
        _check_modelica_source(path, rel, data)
    chosen = default_name(path) if name is None else name
    check_name(config, chosen, overwrite=overwrite, building=())
    return BuildRequest(
        source=path,
        source_rel=rel,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        backend=backend,
        name=chosen,
        overwrite=overwrite,
    )


def resolve_source(config: Config, source: str) -> Path:
    """Resolve ``source`` (workspace-relative or absolute) to a symlink-free path.

    Containment in ``config.allowed_roots`` is checked on the resolved path before existence
    (as ``fmu.catalog.resolve_fmu`` does), so a symlink pointing outside is caught and an
    outside path is refused without revealing whether it exists. Sources inside
    ``config.store_dir`` or ``config.generated_dir`` are refused.

    Raises:
        UserError: empty, outside the workspace, inside the store or ``generated/``, missing,
            a directory, or not a regular file.
    """
    if not source or not source.strip():
        raise UserError(
            "The source argument is empty. Pass the workspace-relative path of the .py or .mo "
            "file to build."
        )
    shown = repr(source[:SOURCE_ARG_MAX_CHARS])
    candidate = Path(source)
    if not candidate.is_absolute():
        candidate = config.workspace / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise UserError(f"Can't resolve the source path {shown}: {exc}. Check the path.") from exc
    if not any(resolved.is_relative_to(root) for root in config.allowed_roots):
        raise UserError(
            f"Source {shown} is outside the workspace ({config.workspace}), possibly through a "
            "symlink. Write the file inside the workspace and pass its workspace-relative path."
        )
    rel = relative_path(config, resolved)
    for owned, label in ((config.store_dir, "the run store"), (config.generated_dir, "generated/")):
        if resolved.is_relative_to(owned.resolve()):
            raise UserError(
                f"{rel} is inside {label}, which Agentic FMI writes itself. Keep sources "
                "elsewhere in the workspace (for example models/) and pass that path."
            )
    if resolved.is_dir():
        raise UserError(f"{rel} is a directory. Pass one .py or .mo file.")
    if not resolved.exists():
        raise UserError(
            f"Source {rel} was not found. Paths are relative to the workspace "
            f"({config.workspace}); check the file name."
        )
    if not resolved.is_file():
        raise UserError(f"{rel} is not a regular file. Pass a .py or .mo file.")
    return resolved


def default_name(source: Path) -> str:
    """The default build name: the file stem (``pi_controller.py`` -> ``pi_controller``)."""
    return source.stem


def check_name(config: Config, name: str, *, overwrite: bool, building: Collection[str]) -> None:
    """Refuse a build name that can't be used now.

    Rules: matches ``protocol.BUILD_NAME_PATTERN``; not in :data:`WINDOWS_RESERVED`
    (case-insensitive); no entry of ``generated/`` equals it case-insensitively without being
    equal (case-only collision on a case-insensitive bind mount); not in ``building``
    (case-insensitive); and if ``generated/<name>/`` exists, it must hold a readable
    ``build.json`` (else "not made by fmu_build; choose another name") and ``overwrite`` must
    be true (else "pass overwrite=true", naming its source and sha256).

    Raises:
        UserError: any rule broken, with the fix.
    """
    if not _NAME.fullmatch(name):
        raise UserError(
            f"The build name {name[:80]!r} is invalid: use 1 to 64 letters, digits and "
            "underscores, starting with a letter. Pass name='...' (it defaults to the file "
            "name)."
        )
    if name.upper() in WINDOWS_RESERVED:
        raise UserError(
            f"The build name {name!r} is reserved on Windows, where the workspace may live. "
            "Pass another name='...'."
        )
    if any(other.lower() == name.lower() for other in building):
        raise UserError(
            f"A build of {name!r} is already queued or running (run_list shows it). Wait for "
            "it with run_status or cancel it with run_stop, or pass another name='...'."
        )
    problem = _output_problem(config.generated_dir, name, overwrite=overwrite)
    if problem is not None:
        raise UserError(problem)


def module_name_problem(stem: str) -> str | None:
    """Why ``<stem>.py`` can't be a pythonfmu slave module, or ``None``.

    pythonfmu imports the source by module name, so the stem must be an identifier and not a
    keyword, must not be in ``sys.stdlib_module_names``, must not be ``pythonfmu``, and
    ``importlib.util.find_spec(stem)`` must be ``None`` (an installed module such as ``fmpy`` or
    ``numpy`` would be imported instead of the source, at build time or in the FMU's host).
    """
    if not (stem.isascii() and stem.isidentifier()):
        return (
            f"its name {stem!r} is not a Python module name (ASCII letters, digits and "
            "underscores, not starting with a digit)."
        )
    if keyword.iskeyword(stem):
        return f"its name {stem!r} is a Python keyword."
    if stem == "pythonfmu":
        return "its name 'pythonfmu' is the build tool's own package."
    if stem in sys.stdlib_module_names:
        return (
            f"its name {stem!r} is a standard-library module, which Python would import "
            "instead of the file."
        )
    try:
        installed = importlib.util.find_spec(stem) is not None
    except (ImportError, ValueError):
        installed = True
    if installed:
        return (
            f"its name {stem!r} is an installed module, which Python would import instead of "
            "the file (at build time and where the FMU runs)."
        )
    return None


def modelica_class_problem(stem: str, text: str) -> str | None:
    """Why ``<stem>.mo`` (whose content is ``text``) can't be built, or ``None``.

    The stem names the class to build (``MSD.mo`` -> ``model MSD``, written into the generated
    ``.mos``), so it must be a plain Modelica identifier (``[A-Za-z_][A-Za-z0-9_]*``)
    and the file must define a ``model`` or ``block`` of that name. This is a
    cheap search over the text without comments and one-line strings, not a parse: it doesn't
    check that the class is top-level (omc reports a nested one when it builds) and it can't
    see syntax errors (omc does).
    """
    if not _MODELICA_IDENTIFIER.fullmatch(stem):
        return (
            f"its name {stem!r} is not a Modelica class name (ASCII letters, digits and "
            "underscores, not starting with a digit) and fmu_build builds the class named like "
            "the file. Rename the file."
        )
    defined: dict[str, set[str]] = {}
    for match in _MODELICA_CLASS.finditer(_MODELICA_SKIPPED.sub(" ", text)):
        defined.setdefault(match["name"], set()).add(match["kind"])
    kinds = defined.get(stem, set())
    if kinds & {"model", "block"}:
        return None
    if kinds:
        return (
            f"it defines {sorted(kinds)[0]} {stem}, not a model or block, and fmu_build builds "
            f"the class named like the file. Make {stem} a model."
        )
    others = [name for name, found in defined.items() if found & {"model", "block"}][:5]
    found_text = f"only {', '.join(others)}" if others else "no model or block"
    return (
        f"it defines {found_text}, but fmu_build builds the model named like the file "
        f"({stem!r}). Rename the model to {stem} or the file to <model name>.mo, then call "
        "fmu_build again."
    )


def find_omc() -> str | None:
    """Absolute path of ``omc`` on ``PATH`` (``shutil.which``), or ``None``."""
    return shutil.which("omc")


def build_spec(config: Config, request: BuildRequest, run_id: str) -> BuildSpec:
    """The job's spec: options from :data:`PYTHONFMU_OPTIONS` or :data:`OMC_OPTIONS`,
    ``fmu_path`` ``generated/<name>/<name>.fmu``, ``timeout_s`` ``Timing.build_timeout_s``,
    ``Versions.current()``, stamped now."""
    options = PYTHONFMU_OPTIONS if request.backend == "pythonfmu" else OMC_OPTIONS
    output = config.generated_dir / request.name / f"{request.name}.fmu"
    return BuildSpec(
        run_id=run_id,
        created_at=utc_now(),
        workspace=str(config.workspace),
        source=SourceRef(
            path=request.source_rel, sha256=request.sha256, size_bytes=len(request.data)
        ),
        name=request.name,
        fmu_path=relative_path(config, output),
        backend=request.backend,
        options=dict(options),
        overwrite=request.overwrite,
        timeout_s=config.timing.build_timeout_s,
        versions=Versions.current(),
    )


def _read_source(path: Path, rel: str, limit: int) -> bytes:
    """The source bytes, read once: at most ``limit`` bytes of UTF-8 text."""
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        raise UserError(f"Can't read {rel}: {exc.strerror or exc}. Check its permissions.") from exc
    if len(data) > limit:
        raise UserError(
            f"{rel} is larger than {limit // 1024} KiB, the fmu_build source limit. Keep the "
            "model in one smaller file (move tables or data into parameters)."
        )
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UserError(
            f"{rel} is not UTF-8 text (invalid byte at offset {exc.start}). Save it as UTF-8."
        ) from exc
    return data


def _check_python_source(path: Path, rel: str, data: bytes) -> None:
    if SLAVE_MARKER not in data:
        raise UserError(
            f"{rel} defines no pythonfmu slave: the text Fmi2Slave doesn't appear in it. A .py "
            "source must define one class derived from pythonfmu.Fmi2Slave; fmu_build doesn't "
            "run other scripts."
        )
    problem = module_name_problem(path.stem)
    if problem is not None:
        raise UserError(
            f"{rel} can't be built: pythonfmu imports it by its file name, and {problem} Rename "
            "the file (for example add _fmu to the name), then call fmu_build again."
        )


def _check_modelica_source(path: Path, rel: str, data: bytes) -> None:
    problem = modelica_class_problem(path.stem, data.decode("utf-8"))
    if problem is not None:
        raise UserError(f"{rel} can't be built: {problem}")
    if find_omc() is None:
        raise UserError(OMC_INSTALL_HINT)


def _output_problem(generated: Path, name: str, *, overwrite: bool) -> str | None:
    """Why ``generated/<name>/`` can't take this build now, or ``None``: a case-only clash, a
    directory not made by fmu_build, or an earlier build without ``overwrite``."""
    try:
        entries = os.listdir(generated)
    except FileNotFoundError:
        return None
    except NotADirectoryError:
        return "generated/ in the workspace is not a directory. Move it away, then build again."
    clash = next((e for e in entries if e.lower() == name.lower() and e != name), None)
    if clash is not None:
        return (
            f"generated/{clash} differs from {name!r} only by case, which collides on a "
            f"case-insensitive file system. Pass name='{clash}' (with overwrite=true) or another "
            "name."
        )
    if name not in entries:
        return None
    directory = generated / name
    try:
        record = runstore.read_build_record(directory) if directory.is_dir() else None
    except ProtocolError:
        record = None
    if record is None:
        return (
            f"generated/{name} exists but was not made by fmu_build (no readable build.json), "
            "so it is left alone. Pass another name='...'."
        )
    if not overwrite:
        return (
            f"generated/{name}/ already holds a build of {record.source.path} (FMU sha256 "
            f"{record.fmu.sha256[:12]}…). Pass overwrite=true to replace it, or another "
            "name='...'."
        )
    return None


# --- worker side: run the tool, parse, commit -------------------------------------------------


def execute_build(
    spec: BuildSpec,
    run_path: Path,
    limits: Limits,
    timing: Timing,
    *,
    should_stop: Callable[[], bool],
    before_commit: Callable[[], bool],
) -> BuildOutcome:
    """Run one build job in the worker; see the module docstring for the rules.

    Steps:
    1. Re-hash ``runs/<id>/source/<file>``; a mismatch with ``spec.source.sha256`` is a bug or
       tampering: raise (the worker records ``internal_error``).
    2. Create ``work/`` (and ``work/tmp`` for ``TMPDIR``), write the hashed bytes to
       ``work/src/``.
    3. Start the tool with ``subprocess.Popen``: argv from :func:`pythonfmu_argv`, or
       ``[omc, "build.mos"]`` with :func:`omc_script` written to ``work/``; ``cwd=work``,
       ``env=build_env(...)`` (omc: ``offline=True``), ``stdin=DEVNULL``, stdout and stderr to
       ``build.log``, ``close_fds=True``, no ``start_new_session`` and no ``preexec_fn``: the
       tool stays in the worker's process group, so the watchdog's and the supervisor's group
       kills reach it.
    4. Wait in short slices; when ``should_stop()`` turns true, send SIGTERM to the worker's own
       group if the worker leads it (the worker's handler only sets its flag), else to the tool
       alone; wait up to ``timing.stop_grace_s``, then SIGKILL the tool, and return
       ``BuildOutcome(stopped=True)``.
    5. Decide success (module docstring); on failure return ``BuildOutcome(failure=...)`` with
       :func:`format_failure` over the parsed log.
    6. Check the FMU with ``fmu.inspect.read_description``: FMI 2.0 and the interfaces the
       backend promises (pythonfmu: CoSimulation; omc: ModelExchange and CoSimulation).
    7. Call ``before_commit()`` (the worker disarms its watchdog there); if it returns False,
       return ``stopped``. Under the store lock, re-check the output directory as
       :func:`check_name` does (a directory taken meanwhile is a ``failure``), then write the
       FMU, ``generated/<name>/build.json`` and ``runs/<id>/build.json``. If a write fails, a
       ``generated/<name>/`` this commit created is removed again, so the name stays free.
    8. ``finally``: remove ``work/``.

    Raises:
        Exception: only for bugs and store failures; the worker records ``internal_error``.
    """
    data = _verified_source(spec, run_path)
    work = run_path / runstore.WORK_DIR
    log_path = run_path / runstore.BUILD_LOG_FILE
    log_shown = _shown_path(log_path, Path(spec.workspace))
    try:
        source_copy = _prepare_work(work, Path(spec.source.path).name, data)
        argv = _tool_argv(spec, source_copy, work)
        started = time.monotonic()
        code = _run_tool(argv, work, log_path, timing, should_stop, offline=spec.backend == "omc")
        if code is None:
            return BuildOutcome(stopped=True)
        duration = time.monotonic() - started
        result = _tool_result(spec, code, _read_log(log_path), source_copy, work)
        if result.fmu is None:
            text = format_failure(
                result.diagnostics,
                backend=spec.backend,
                log_path=log_shown,
                limits=limits,
                headline=result.headline,
            )
            return BuildOutcome(failure=text)
        md, problem = _checked_fmu(result.fmu, spec.backend)
        if problem is not None or md is None:
            return BuildOutcome(failure=_plain_failure(spec, problem, log_shown, limits))
        if not before_commit():
            return BuildOutcome(stopped=True)
        built = _Built(result.fmu, md, result, duration)
        return _commit(spec, run_path, built, log_shown, limits)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def build_env(work: Path, environ: Mapping[str, str], *, offline: bool = False) -> dict[str, str]:
    """The tool's environment: :data:`ENV_ALLOWLIST` and :data:`ENV_PREFIXES` variables taken
    from ``environ`` if present, :data:`ENV_FIXED`, ``TMPDIR=<work>/tmp``, and, with
    ``offline`` (omc only), :data:`OMC_OFFLINE_ENV`. Nothing else: any proxy variable the worker
    inherited is dropped."""
    env = {
        key: value
        for key, value in environ.items()
        if key in ENV_ALLOWLIST or key.startswith(ENV_PREFIXES)
    }
    env.update(ENV_FIXED)
    env["TMPDIR"] = str(work / WORK_TMP)
    if offline:
        env.update(OMC_OFFLINE_ENV)
    return env


def pythonfmu_argv(source_copy: Path, out_dir: Path) -> list[str]:
    """``[sys.executable, "-I", "-B", "-m", "pythonfmu", "build", "-f", source_copy, "-d",
    out_dir]``: isolated mode (no user site, no cwd on ``sys.path``), no bytecode files. No
    project files, no ``--doc`` and default FMI options."""
    return [
        sys.executable,
        "-I",
        "-B",
        "-m",
        "pythonfmu",
        "build",
        "-f",
        str(source_copy),
        "-d",
        str(out_dir),
    ]


def omc_script(source_copy: Path, class_name: str, modelica_path: str) -> str:
    """The ``.mos`` omc runs, and the only one: ``echo(false)``; ``setModelicaPath`` pinned to
    ``modelica_path`` (``$HOME/.openmodelica/libraries``); ``loadModel(Modelica)``;
    ``loadFile`` of the private copy; ``setCommandLineOptions("--fmiFlags=s:cvode")``;
    ``buildModelFMU(<class>, version="2.0", fmuType="me_cs")``. It prints the
    :data:`OMC_MARKER` lines and the ``getErrorString()`` text after each step. Paths are
    escaped as Modelica string literals; ``class_name`` passed :func:`modelica_class_problem`.

    Each message buffer is read into a variable first (``afmi_msg := getErrorString();
    print(afmi_msg);``): omc 1.27.1 prints nothing for ``print(getErrorString())`` after some
    failures (an under-determined model's multi-line error), but does print the variable.
    """
    marker = OMC_MARKER
    options = OMC_OPTIONS
    drain = "afmi_msg := getErrorString(); print(afmi_msg);"
    lines = [
        "echo(false);",
        f'print("{marker}omc=" + getVersion() + "\\n");',
        f"setModelicaPath({_modelica_string(modelica_path)});",
        drain,
        "afmi_ok := loadModel(Modelica);",
        f'print("{marker}msl=" + getVersion(Modelica) + "\\n");',
        drain,
        f'afmi_ok := loadFile({_modelica_string(str(source_copy))}, encoding="UTF-8");',
        f'print("{marker}load=" + String(afmi_ok) + "\\n");',
        drain,
        f'setCommandLineOptions("--fmiFlags={options["fmi_flags"]}");',
        drain,
        f"afmi_fmu := buildModelFMU({class_name}, "
        f'version="{options["fmi_version"]}", fmuType="{options["fmu_type"]}");',
        f'print("{marker}fmu=" + afmi_fmu + "\\n");',
        drain,
    ]
    return "\n".join(lines) + "\n"


# --- worker helpers ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Built:
    """A successful tool run whose FMU passed the checks, ready to commit."""

    fmu: Path
    md: ModelDescription
    result: _ToolResult
    duration_s: float


@dataclass(frozen=True)
class _Written:
    """The sha256 and size of the FMU file as written to ``generated/``."""

    sha256: str
    size_bytes: int


def _verified_source(spec: BuildSpec, run_path: Path) -> bytes:
    """The private source copy's bytes, if they still hash to ``spec.source.sha256``."""
    path = run_path / runstore.SOURCE_DIR / Path(spec.source.path).name
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != spec.source.sha256:
        raise RuntimeError(
            f"the private source copy {path.name} doesn't match spec.json (sha256 "
            f"{digest[:12]}…, expected {spec.source.sha256[:12]}…); call fmu_build again"
        )
    return data


def _prepare_work(work: Path, file_name: str, data: bytes) -> Path:
    """A fresh ``work/`` with ``src/<file_name>`` (the hashed bytes), ``out/`` and ``tmp/``."""
    shutil.rmtree(work, ignore_errors=True)
    for sub in (WORK_SRC, WORK_OUT, WORK_TMP):
        (work / sub).mkdir(parents=True)
    source_copy = work / WORK_SRC / file_name
    source_copy.write_bytes(data)
    return source_copy


def _tool_argv(spec: BuildSpec, source_copy: Path, work: Path) -> list[str]:
    if spec.backend == "pythonfmu":
        return pythonfmu_argv(source_copy, work / WORK_OUT)
    omc = find_omc()
    if omc is None:
        raise RuntimeError(OMC_INSTALL_HINT)
    libraries = Path(os.environ.get("HOME", "~")).expanduser() / ".openmodelica" / "libraries"
    script = omc_script(source_copy, source_copy.stem, str(libraries))
    (work / OMC_SCRIPT).write_text(script, encoding="utf-8")
    return [omc, OMC_SCRIPT]


def _run_tool(
    argv: list[str],
    work: Path,
    log_path: Path,
    timing: Timing,
    should_stop: Callable[[], bool],
    *,
    offline: bool,
) -> int | None:
    """Run the tool to its end and return its exit status, or ``None`` if a stop request
    ended it. It is never left running, whatever happens here."""
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            argv,
            cwd=work,
            env=build_env(work, os.environ, offline=offline),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            close_fds=True,
        )
    try:
        while True:
            try:
                code: int | None = process.wait(timeout=TOOL_POLL_S)
            except subprocess.TimeoutExpired:
                code = None
            if should_stop():  # checked after an exit too: a group SIGTERM ends the tool first
                _stop_tool(process, timing.stop_grace_s)
                return None
            if code is not None:
                return code
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def _stop_tool(process: subprocess.Popen[bytes], grace_s: float) -> None:
    """SIGTERM the worker's own group if it leads one (reaching the tool's children too; the
    worker's handler only sets its stop flag), else the tool alone; SIGKILL it after the grace."""
    if process.poll() is not None:
        return
    if os.getpgrp() == os.getpid():
        os.killpg(os.getpgrp(), signal.SIGTERM)
    else:
        process.terminate()
    try:
        process.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _read_log(path: Path) -> str:
    """The last :data:`LOG_READ_MAX_BYTES` of ``build.log`` as text."""
    try:
        with path.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            handle.seek(max(size - LOG_READ_MAX_BYTES, 0))
            data = handle.read()
    except FileNotFoundError:
        return ""
    return data.decode("utf-8", errors="replace")


def _tool_result(
    spec: BuildSpec, code: int, log: str, source_copy: Path, work: Path
) -> _ToolResult:
    if spec.backend == "pythonfmu":
        return _pythonfmu_result(code, log, source_copy, spec.source.path, work / WORK_OUT)
    return _omc_result(code, log, source_copy, spec.source.path, work)


def _pythonfmu_result(
    code: int, log: str, source_copy: Path, source_rel: str, out: Path
) -> _ToolResult:
    """Success is exit status 0 and exactly one ``.fmu`` in ``out``."""
    diagnostics = parse_pythonfmu_log(log, source_copy, source_rel)
    fmus = sorted(out.glob("*.fmu"))
    versions: dict[str, str | None] = {"pythonfmu": _package_version("pythonfmu")}
    if code == 0 and len(fmus) == 1:
        return _ToolResult(fmus[0], _warnings(diagnostics), versions=versions)
    headline = None
    if not any(d.severity == "error" for d in diagnostics):
        headline = (
            f"Build failed (pythonfmu): it exited with status {code} after writing "
            f"{len(fmus)} FMUs, and the log names no Python error."
        )
    return _ToolResult(None, diagnostics, headline, versions)


def _omc_result(code: int, log: str, source_copy: Path, source_rel: str, work: Path) -> _ToolResult:
    """Success is the path ``buildModelFMU`` returned, naming a file (never the exit status,
    which is 0 on failure); the errors a successful build printed become warnings."""
    parsed = parse_omc_log(log, source_copy, source_rel, os.environ.get("HOME", ""))
    versions: dict[str, str | None] = {"omc": parsed.omc_version, "msl": parsed.msl_version}
    if parsed.fmu:
        candidate = Path(parsed.fmu)
        candidate = candidate if candidate.is_absolute() else work / candidate
        if candidate.is_file():
            return _ToolResult(candidate, _as_warnings(parsed.diagnostics), versions=versions)
    headline = None
    if not any(d.severity == "error" for d in parsed.diagnostics):
        headline = f"Build failed (omc): {_omc_stage(parsed, code)}, and omc reported no error."
    return _ToolResult(None, parsed.diagnostics, headline, versions)


def _omc_stage(parsed: OmcLog, code: int) -> str:
    """Where a build that named no error stopped, for the count line."""
    if parsed.omc_version is None:
        return f"omc ended with status {code} before it ran the script"
    return "buildModelFMU returned no FMU" if parsed.loaded else "loadFile failed"


def _checked_fmu(path: Path, backend: Backend) -> tuple[ModelDescription | None, str | None]:
    """The FMU's model description, or why it fails the checks (FMI 2.0, promised interfaces)."""
    try:
        md = read_description(path)
    except UserError as exc:
        return None, f"the built FMU can't be read: {exc}"
    if md.fmiVersion != "2.0":
        return None, f"the built FMU declares FMI {md.fmiVersion}, not 2.0."
    missing = sorted(PROMISED_INTERFACES[backend] - set(interfaces_of(md)))
    if missing:
        return None, f"the built FMU lacks the {' and '.join(missing)} interface."
    return md, None


def _plain_failure(spec: BuildSpec, problem: str | None, log_shown: str, limits: Limits) -> str:
    """The diagnostics text of a failure that has no tool message, only ``problem``."""
    headline = f"Build failed ({spec.backend}): {problem or 'unknown reason.'}"
    return format_failure(
        [], backend=spec.backend, log_path=log_shown, limits=limits, headline=headline
    )


def _commit(
    spec: BuildSpec, run_path: Path, built: _Built, log_shown: str, limits: Limits
) -> BuildOutcome:
    """Under the store lock: re-check ``generated/<name>/``, then replace the FMU, then its
    ``build.json``, then write ``runs/<id>/build.json``."""
    directory = (Path(spec.workspace) / spec.fmu_path).parent
    with locked(run_path.parent.parent / runstore.LOCK_FILE):
        problem = _output_problem(directory.parent, spec.name, overwrite=spec.overwrite)
        if problem is not None:
            return BuildOutcome(failure=_plain_failure(spec, problem, log_shown, limits))
        record = _write_outputs(spec, run_path, built, limits)
    return BuildOutcome(record=record)


def _write_outputs(spec: BuildSpec, run_path: Path, built: _Built, limits: Limits) -> BuildRecord:
    """Write the FMU and both ``build.json`` files. A directory this call created is removed
    again if a write fails: without a ``build.json`` it would be "not made by fmu_build" and
    block every later build of the name. A directory that already held a build stays as it was
    found (the FMU is replaced atomically, so it is whole either way)."""
    target = Path(spec.workspace) / spec.fmu_path
    directory = target.parent
    replaced = sha256_file(target) if target.is_file() else None
    created = not directory.exists()
    directory.mkdir(parents=True, exist_ok=True)
    try:
        written = _copy_hashed(built.fmu, target)
        record = _record(spec, built, written, replaced, limits)
        runstore.write_build_record(directory, record)
        runstore.write_build_record(run_path, record)
    except BaseException:
        if created:
            shutil.rmtree(directory, ignore_errors=True)
        raise
    return record


def _copy_hashed(source: Path, target: Path) -> _Written:
    """Copy ``source`` to ``target`` atomically (temporary file in ``target``'s directory, then
    ``os.replace``) and return the sha256 and size of the bytes written."""
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as reader, atomic_open(target) as writer:
        while chunk := reader.read(1 << 20):
            digest.update(chunk)
            writer.write(chunk)
            size += len(chunk)
    return _Written(digest.hexdigest(), size)


def _record(
    spec: BuildSpec, built: _Built, written: _Written, replaced: str | None, limits: Limits
) -> BuildRecord:
    md = built.md
    interface = md.coSimulation if md.coSimulation is not None else md.modelExchange
    current = Versions.current()
    tool_versions = {key: value for key, value in built.result.versions.items() if value}
    return BuildRecord(
        run_id=spec.run_id,
        name=spec.name,
        built_at=utc_now(),
        backend=spec.backend,
        source=spec.source,
        options=spec.options,
        versions=BuildVersions(
            agentic_fmi=current.agentic_fmi,
            python=current.python,
            platform=current.platform,
            **tool_versions,
        ),
        fmu=BuiltFmu(
            path=spec.fmu_path,
            sha256=written.sha256,
            size_bytes=written.size_bytes,
            fmi_version="2.0",
            model_name=md.modelName,
            model_identifier=interface.modelIdentifier,
            interfaces=[i for i in interfaces_of(md) if i != "ScheduledExecution"],
        ),
        duration_s=round(built.duration_s, 3),
        warnings=[d.format() for d in built.result.diagnostics][
            : limits.build_diagnostics_max_lines
        ],
        replaced_sha256=replaced,
        summary=_summary(md, limits),
    )


def _summary(md: ModelDescription, limits: Limits) -> BuildSummary:
    """Variable counts and the first input, output and parameter names (tunable first)."""
    variables = list(md.modelVariables)
    cap = limits.inspect_max_items
    counts = dict(Counter(v.causality for v in variables))
    counts["total"] = len(variables)
    parameters = sorted(
        (v for v in variables if v.causality in ("parameter", "structuralParameter")),
        key=lambda v: {"tunable": 0, "fixed": 1}.get(v.variability, 2),
    )
    return BuildSummary(
        counts=counts,
        inputs=[v.name for v in variables if v.causality == "input"][:cap],
        outputs=[v.name for v in variables if v.causality == "output"][:cap],
        parameters=[v.name for v in parameters][:cap],
        default_experiment=_experiment(md),
    )


def _experiment(md: ModelDescription) -> BuildExperiment | None:
    experiment = md.defaultExperiment
    if experiment is None:
        return None
    return BuildExperiment(
        start_time=_finite(experiment.startTime),
        stop_time=_finite(experiment.stopTime),
        step_size=_finite(experiment.stepSize),
        tolerance=_finite(experiment.tolerance),
    )


def _finite(text: str | None) -> float | None:
    """A finite float from an XML attribute, else ``None``."""
    try:
        value = float(text) if text is not None else math.nan
    except ValueError:
        return None
    return value if math.isfinite(value) else None


# --- small helpers ----------------------------------------------------------------------------


def _modelica_string(text: str) -> str:
    """``text`` as a Modelica string literal."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _as_warnings(diagnostics: Sequence[Diagnostic]) -> list[Diagnostic]:
    """Every diagnostic as a warning: on a successful omc build its ``Error:`` lines (offline
    library index, initialization hints) are not failures."""
    return [replace(d, severity="warning") for d in diagnostics]


def _warnings(diagnostics: Sequence[Diagnostic]) -> list[Diagnostic]:
    return [d for d in diagnostics if d.severity == "warning"]


def _shown_path(path: Path, workspace: Path) -> str:
    """``path`` relative to the workspace (POSIX), or absolute if the store is elsewhere."""
    try:
        return path.relative_to(workspace).as_posix()
    except ValueError:
        return path.as_posix()


def _package_version(name: str) -> str | None:
    """An installed distribution's version from its metadata (nothing is imported)."""
    try:
        return version(name)
    except PackageNotFoundError:
        return None
