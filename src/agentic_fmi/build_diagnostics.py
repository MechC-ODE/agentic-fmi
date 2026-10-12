"""Turn a build tool's output into diagnostics: the lines ``fmu_build`` shows when a build fails.

Parsers: :func:`parse_pythonfmu_log` reads a Python traceback and warnings;
:func:`parse_omc_log` reads omc's ``@@afmi`` marker lines and its error-buffer messages. Both map
the private source copy under ``runs/<id>/work/`` back to the workspace-relative path the caller
wrote, so no temporary path reaches the model. :func:`format_failure` renders any list of
:class:`Diagnostic` as the fixed text ``Build failed (<backend>): N errors, M warnings.`` plus up
to ``Limits.build_diagnostics_max_lines`` lines ``path:line[:col]: severity: message`` and the
log path, within ``Limits.build_diagnostics_max_chars``.

Pure functions over text: no file is written and no process is started (a SyntaxError's column is
the one read, from the source copy). Collaborators: ``build.py`` (calls the parsers on
``build.log`` and writes the ``.mos`` whose markers :data:`OMC_MARKER` names), ``config.Limits``
(the caps) and ``store/protocol.py`` (``Backend``). ADR 0008 fixes the line format.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from agentic_fmi.config import Limits
from agentic_fmi.store.protocol import Backend

OMC_MARKER: Final = "@@afmi "
"""Prefix of the ``key=value`` lines the generated ``.mos`` prints with ``print()`` (after
``echo(false)``), so the result is parsed from markers, not from statement echoes: ``omc=``,
``msl=``, ``load=true|false``, ``fmu=<path or empty>``. Everything else in the log is tool
output (``getErrorString()`` messages)."""

MAX_OMC_MESSAGE_CHARS: Final = 400
"""An omc message (its continuation lines joined) is cut to this length."""

FIX_HINT: Final = "Fix the source and call fmu_build again."
MIN_CUT_CHARS: Final = 40
"""A diagnostics line cut to fit the character cap keeps at least this much, else it is left
out (and counted in "and N more")."""

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-_]")
_TRACEBACK = "Traceback (most recent call last):"
_FRAME = re.compile(r'^  File "(?P<path>[^"]+)", line (?P<line>\d+)(?:, in .*)?$')
_EXCEPTION = re.compile(r"^(?P<type>[A-Za-z_][\w.]*)(?::|$)")
_SYNTAX_ERRORS = frozenset({"SyntaxError", "IndentationError", "TabError"})
_CARET = re.compile(r"^\s*\^+\s*$")
_WARNING = re.compile(
    r"^(?P<path>/[^:]+):(?P<line>\d+): (?P<category>[A-Za-z_]\w*Warning): (?P<message>.*)$"
)
_DISPLAY_INDENT = 4
"""Python prints a traceback's source line with its own indentation stripped and four spaces."""
_OMC_MARKER = re.compile(rf"^{re.escape(OMC_MARKER)}(?P<key>[a-z_]+)=(?P<value>.*)$")
_OMC_MESSAGE = re.compile(
    r"^(?:\[(?P<file>.+?):(?P<line>\d+):(?P<column>\d+)-\d+:\d+:(?:writable|readonly)\] )?"
    r"(?P<severity>Error|Warning|Notification): (?P<text>.*)$"
)
_OMC_NETWORK_NOISE = re.compile(r"Curl error for URL |Failed to download package index ")
_OMC_LOAD_FOLLOWUPS = re.compile(r"Failed to load package |Class \S+ not found in scope <top>")


@dataclass(frozen=True)
class Diagnostic:
    """One tool message, mapped onto the user's source.

    ``path`` is the workspace-relative source path when the message points into the source,
    another non-temporary path (``~`` for the home directory) for library files, or ``None``.
    Temporary paths (``runs/<id>/work/...``) never appear: they are mapped to the source.
    """

    severity: Literal["error", "warning"]
    message: str
    """One line: continuation lines are joined with spaces; ANSI escapes removed."""
    path: str | None = None
    line: int | None = None
    column: int | None = None

    def format(self) -> str:
        """The fixed diagnostics line (ADR 0008): ``path:line[:col]: severity: message``,
        ``path: severity: message`` without a line, ``severity: message`` without a path."""
        if self.path is None:
            return f"{self.severity}: {self.message}"
        where = self.path
        if self.line is not None:
            where += f":{self.line}"
            if self.column is not None:
                where += f":{self.column}"
        return f"{where}: {self.severity}: {self.message}"


@dataclass(frozen=True)
class OmcLog:
    """:func:`parse_omc_log`'s reading of an omc build log."""

    fmu: str | None
    """The path ``buildModelFMU`` returned, or ``None`` for an empty or missing ``fmu=`` marker."""
    loaded: bool
    """``load=`` marker: whether ``loadFile`` of the source succeeded."""
    omc_version: str | None
    msl_version: str | None
    diagnostics: list[Diagnostic]
    """Errors first, then warnings; notifications and known noise (package-index download
    errors, and after a failed ``loadFile`` the "Failed to load package" and "Class ... not
    found" follow-ups) are dropped."""


@dataclass
class _OmcMessage:
    """One message of omc's error buffer: its header fields and its text lines."""

    severity: str
    """``Error``, ``Warning`` or ``Notification``."""
    file: str | None
    line: int
    """0 when the message has no location."""
    column: int
    parts: list[str]
    """The first line's text, then the stripped continuation lines."""


def parse_pythonfmu_log(text: str, source_copy: Path, source_rel: str) -> list[Diagnostic]:
    """Diagnostics from a failed pythonfmu build's output (a Python traceback, and warnings).

    The error is the exception line (``NameError: name 'x' is not defined``; a SyntaxError's
    ``File "...", line N`` header gives the line and its caret line the column). Its location
    is the innermost traceback frame inside ``source_copy`` (or pythonfmu's temporary copy of
    it under the same ``work/``), reported as ``source_rel``; frames of pythonfmu, importlib
    and the standard library are dropped. Without a frame in the source (pythonfmu's "No child
    class of Fmi2Slave found"), the path is ``source_rel`` with no line. Python warnings
    (``file:line: XWarning: text``) pointing into the source become warnings. ANSI escapes are
    removed and no temporary path survives.
    """
    lines = _ANSI.sub("", text).splitlines()
    warnings = [
        Diagnostic(
            "warning", f"{m['category']}: {m['message'].strip()}", source_rel, int(m["line"])
        )
        for m in map(_WARNING.match, lines)
        if m is not None and _is_source(m["path"], source_copy)
    ]
    error = _traceback_error(lines, source_copy, source_rel)
    return ([error] if error is not None else []) + warnings


def parse_omc_log(text: str, source_copy: Path, source_rel: str, home: str) -> OmcLog:
    """Read an omc build log: the :data:`OMC_MARKER` lines and the messages.

    Messages match ``[<file>:<l1>:<c1>-<l2>:<c2>:<writable|readonly>] <Error|Warning|
    Notification>: <text>`` with the bracket optional and indented continuation lines appended
    (spike (f)). ``source_copy`` maps to ``source_rel``, a ``home`` prefix to ``~``, other
    paths under ``work/`` to nothing (message-only). Marker lines end a message and are read
    only at the start of a line. Duplicates are merged and messages are cut to
    :data:`MAX_OMC_MESSAGE_CHARS`. See :class:`OmcLog` for what is dropped.
    """
    markers: dict[str, str] = {}
    messages: list[_OmcMessage] = []
    current: _OmcMessage | None = None
    for line in _ANSI.sub("", text).splitlines():
        marker = _OMC_MARKER.match(line)
        match = _OMC_MESSAGE.match(line)
        if marker is not None:
            markers[marker["key"]] = marker["value"].strip()
            current = None
        elif match is not None:
            current = _OmcMessage(
                severity=match["severity"],
                file=match["file"],
                line=int(match["line"] or 0),
                column=int(match["column"] or 0),
                parts=[match["text"].strip()],
            )
            messages.append(current)
        elif current is not None and line[:1] in (" ", "\t") and line.strip():
            current.parts.append(line.strip())
        else:
            current = None
    loaded = markers.get("load") == "true"
    found = (_omc_diagnostic(m, loaded, source_copy, source_rel, home) for m in messages)
    unique = dict.fromkeys(d for d in found if d is not None)
    return OmcLog(
        fmu=markers.get("fmu") or None,
        loaded=loaded,
        omc_version=(markers.get("omc") or "").removeprefix("OpenModelica ") or None,
        msl_version=markers.get("msl") or None,
        diagnostics=sorted(unique, key=lambda d: d.severity != "error"),
    )


def format_failure(
    diagnostics: Sequence[Diagnostic],
    *,
    backend: Backend,
    log_path: str,
    limits: Limits,
    headline: str | None = None,
) -> str:
    """The text of a failed build, at most ``limits.build_diagnostics_max_chars``.

    A count line (``headline`` or "Build failed (omc): 3 errors, 1 warning."), up to
    ``limits.build_diagnostics_max_lines`` :meth:`Diagnostic.format` lines, errors first (a
    line that would break the character cap is cut with ``…``), "and N more" when lines were
    left out, then ``Full log: <log_path>`` (the workspace-relative store path of
    ``build.log``) and "Fix the source and call fmu_build again." A 200-error flood stays
    within the cap.
    """
    cap = limits.build_diagnostics_max_chars
    errors = [d for d in diagnostics if d.severity == "error"]
    warnings = [d for d in diagnostics if d.severity == "warning"]
    head = _clip(headline or _count_line(backend, len(errors), len(warnings)), cap // 2)
    footer = [f"Full log: {log_path}", FIX_HINT]
    ordered = errors + warnings
    room = cap - len(head) - sum(len(line) + 1 for line in footer) - len(_more(len(ordered))) - 1
    shown: list[str] = []
    for diagnostic in ordered[: limits.build_diagnostics_max_lines]:
        line = diagnostic.format()
        if len(line) + 1 > room:
            if room >= MIN_CUT_CHARS:
                shown.append(line[: room - 2] + "…")
            break
        shown.append(line)
        room -= len(line) + 1
    left = len(ordered) - len(shown)
    text = "\n".join([head, *shown, *([_more(left)] if left else []), *footer])
    return _clip(text, cap)


def _traceback_error(lines: list[str], source_copy: Path, source_rel: str) -> Diagnostic | None:
    """The error of the last traceback in ``lines``, located at its innermost source frame."""
    starts = [i for i, line in enumerate(lines) if line.startswith(_TRACEBACK)]
    if not starts:
        return None
    block = lines[starts[-1] + 1 :]
    frame_at: int | None = None
    position = 0
    while position < len(block) and (block[position].startswith(" ") or not block[position]):
        match = _FRAME.match(block[position])
        if match is not None and _is_source(match["path"], source_copy):
            frame_at = position
        position += 1
    rest = [line.strip() for line in block[position:] if line.strip()]
    header = _EXCEPTION.match(rest[0]) if rest else None
    if header is None:
        return None
    message = _without_work_paths(" ".join(rest), source_copy, source_rel)
    if "No child class of Fmi2Slave" in message:
        message += " (a subclass that leaves do_step undefined is abstract and doesn't count)"
    if frame_at is None:
        return Diagnostic("error", message, source_rel)
    frame = _FRAME.match(block[frame_at])
    assert frame is not None
    line = int(frame["line"])
    column = None
    if header["type"] in _SYNTAX_ERRORS:
        column = _syntax_column(block, frame_at, source_copy, line)
    return Diagnostic("error", message, source_rel, line, column)


def _syntax_column(block: list[str], frame_at: int, source_copy: Path, line: int) -> int | None:
    """The 1-based column of a SyntaxError: its caret line under the displayed source line,
    plus the indentation Python stripped from it (read from the source copy)."""
    if frame_at + 2 >= len(block) or not _CARET.match(block[frame_at + 2]):
        return None
    try:
        original = source_copy.read_text(encoding="utf-8").splitlines()[line - 1]
    except (OSError, IndexError, UnicodeDecodeError):
        return None
    stripped = len(original) - len(original.lstrip(" \f"))
    return block[frame_at + 2].index("^") - _DISPLAY_INDENT + stripped + 1


def _is_source(path: str, source_copy: Path) -> bool:
    """True for the source copy and for pythonfmu's temporary copy of it under ``work/``."""
    work = source_copy.parent.parent
    return path == str(source_copy) or (
        path.startswith(f"{work}/") and Path(path).name == source_copy.name
    )


def _without_work_paths(text: str, source_copy: Path, source_rel: str) -> str:
    """``text`` with paths under ``work/`` replaced: the source's copies by ``source_rel``,
    anything else by ``<build dir>`` (trailing punctuation is kept)."""

    def replace(match: re.Match[str]) -> str:
        path = match.group()
        trimmed = path.rstrip(".,;:)]")
        shown = source_rel if trimmed.endswith(f"/{source_copy.name}") else "<build dir>"
        return shown + path[len(trimmed) :]

    work = re.escape(str(source_copy.parent.parent))
    return re.sub(work + r"/[^\s'\"]*", replace, text)


def _omc_diagnostic(
    message: _OmcMessage, loaded: bool, source_copy: Path, source_rel: str, home: str
) -> Diagnostic | None:
    """The diagnostic for one omc message, or ``None`` for a notification or known noise
    (package-index download errors; after a failed ``loadFile``, its follow-ups)."""
    if message.severity == "Notification":
        return None
    text = " ".join(message.parts)
    if _OMC_NETWORK_NOISE.match(text) or (not loaded and _OMC_LOAD_FOLLOWUPS.match(text)):
        return None
    path = _omc_location(message.file, source_copy, source_rel, home)
    located = path is not None and message.line > 0
    return Diagnostic(
        "error" if message.severity == "Error" else "warning",
        _clip(_omc_text(text, source_copy, source_rel, home), MAX_OMC_MESSAGE_CHARS),
        path,
        message.line if located else None,
        message.column if located and message.column > 0 else None,
    )


def _omc_location(file: str | None, source_copy: Path, source_rel: str, home: str) -> str | None:
    """The path to show for a message's file: the source's workspace-relative path, ``~/...``
    for files under ``home``, ``None`` for any other file in the build directory."""
    if file is None:
        return None
    if file == str(source_copy):
        return source_rel
    if file.startswith(f"{source_copy.parent.parent}/"):
        return None
    return _under_home(file, home)


def _omc_text(text: str, source_copy: Path, source_rel: str, home: str) -> str:
    """``text`` without temporary paths (:func:`_without_work_paths`) or ``home``."""
    return _under_home(_without_work_paths(text, source_copy, source_rel), home)


def _under_home(text: str, home: str) -> str:
    return text.replace(home, "~") if home.strip("/") else text


def _count_line(backend: Backend, errors: int, warnings: int) -> str:
    counted = _plural(errors, "error") + (f", {_plural(warnings, 'warning')}" if warnings else "")
    return f"Build failed ({backend}): {counted}."


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _more(count: int) -> str:
    return f"and {count} more (see the full log)"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
