"""The command inbox of a live run (ADR 0010): ``runs/<id>/commands/`` and ``runs/<id>/outcomes/``.

Layout and rules (protocol v1, additive; [docs/protocol.md](../../../docs/protocol.md#inbox))::

    runs/<id>/commands/<ms13>-<server|pane>-<rand8>.json   Command   server or pane, write-once
    runs/<id>/outcomes/<same id>.json                      CommandOutcome   worker only

- Names follow ``protocol.COMMAND_ID_PATTERN`` plus ``.json``. The worker ignores every other
  entry (the server's temporary files start with a dot), every non-regular file and symlinks.
- The server creates a command exclusively: it writes ``commands/.<id>.tmp-<pid>-<thread>``,
  fsyncs it and ``os.link``\\ s it to the final name (``FileExistsError`` if taken), then unlinks
  the temporary file. On a file system without hard links (the ``os.link`` call fails with
  ``EPERM``, ``ENOTSUP``, ``ENOSYS``, ``EXDEV`` or ``EMLINK``) it creates the final name with
  ``O_CREAT | O_EXCL`` and writes the bytes in place instead, which a reader can see torn for a
  moment, like a pane file (the grace below covers it). The pane writes the final name directly
  with ``$.fs.write``, which is not atomic: a reader can see an empty or torn file.
- The worker applies each file at most once, in name order. A file that doesn't parse yet is
  retried until ``Timing.inbox_grace_s`` after the worker first saw it, then rejected; while it
  waits, later names wait behind it (strict name order). A lower name that shows up after a
  higher one was applied is still applied: nothing is ever skipped by comparing a name with
  ``status.last_cmd``. A file over ``protocol.MAX_COMMAND_BYTES`` is rejected unread.
- Exactly one :class:`~agentic_fmi.store.protocol.CommandOutcome` per command file the worker
  found, written atomically to ``outcomes/<id>.json``; a reader finds it in O(1).

Collaborators: ``store/protocol.py`` (``Command``, ``CommandOutcome``, the ID grammar),
``store/atomic.py`` (atomic writes, validated reads); ``live.py`` (the worker's gate reads the
inbox and writes outcomes) and ``control.py`` (the server writes commands and waits for their
outcomes). Linux only, like the rest of the store.
"""

from __future__ import annotations

import errno
import json
import os
import re
import secrets
import stat
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, NoReturn, cast

from pydantic import ValidationError

from agentic_fmi.store.atomic import dump_json, read_json, write_json
from agentic_fmi.store.protocol import (
    COMMAND_ID_PATTERN,
    MAX_COMMAND_BYTES,
    PROTOCOL_VERSION,
    Command,
    CommandOutcome,
)

COMMANDS_DIR: Final = "commands"
OUTCOMES_DIR: Final = "outcomes"
COMMAND_FILE: Final = re.compile(COMMAND_ID_PATTERN.removesuffix("$") + r"\.json$", re.ASCII)
"""A command file name: the ID grammar plus ``.json`` (ASCII digits only)."""
_COMMAND_ID: Final = re.compile(COMMAND_ID_PATTERN, re.ASCII)
_NO_HARD_LINKS: Final = frozenset(
    {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS, errno.EXDEV, errno.EMLINK}
)
"""``os.link`` errors that mean the file system can't make hard links (exFAT, some SMB or FUSE
mounts), not that the name is taken (``EEXIST``)."""

Source = Literal["server", "pane"]


def new_command_id(source: Source, now_ms: int | None = None) -> str:
    """A fresh command ID: ``<13-digit Unix ms>-<source>-<8 random lowercase hex digits>``
    (``secrets.token_hex(4)``); ``now_ms`` defaults to the current time."""
    millis = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    return f"{millis:013d}-{source}-{secrets.token_hex(4)}"


def write_command(run_path: Path, command: Command) -> Path:
    """Create ``commands/<command.id>.json`` exclusively (server side) and return its path.

    Writes the validated JSON (``atomic.dump_json``) to a dot-prefixed temporary file in
    ``commands/`` (created if missing), fsyncs it, ``os.link``\\ s it to the final name and
    unlinks the temporary file, so the worker never sees a partial server command. Where the
    file system refuses hard links (:data:`_NO_HARD_LINKS`), the final name is created with
    ``O_CREAT | O_EXCL``, written once and fsynced instead: the worker may then briefly see a
    partial file, which its grace for unparsable files covers.

    Raises:
        FileExistsError: a file with this name exists already (pick a new ID).
        ValueError: the command encodes to more than ``protocol.MAX_COMMAND_BYTES``.
        OSError: the run directory is missing or not writable.
    """
    data = (dump_json(command) + "\n").encode("utf-8")
    if len(data) > MAX_COMMAND_BYTES:
        raise ValueError(f"the command is {len(data)} bytes; the limit is {MAX_COMMAND_BYTES}")
    directory = run_path / COMMANDS_DIR
    directory.mkdir(exist_ok=True)
    final = directory / f"{command.id}.json"
    temporary = directory / f".{command.id}.tmp-{os.getpid()}-{threading.get_ident()}"
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _publish(temporary, final, data)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()
    return final


def _publish(temporary: Path, final: Path, data: bytes) -> None:
    """Give ``final`` the content of ``temporary`` without ever replacing a file: a hard link,
    else (no hard links here) an exclusive create of ``final`` with ``data``.

    Raises:
        FileExistsError: ``final`` exists (not a missing capability, so no fallback).
        OSError: anything else the file system refuses.
    """
    try:
        os.link(temporary, final)
    except OSError as error:
        if error.errno not in _NO_HARD_LINKS:
            raise
        _create_exclusively(final, data)


def _create_exclusively(final: Path, data: bytes) -> None:
    """Create ``final`` with ``O_CREAT | O_EXCL``, write ``data`` and fsync. A failed write
    removes the file it created, so no partial command stays behind."""
    descriptor = os.open(final, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC, 0o666)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with suppress(FileNotFoundError):
            final.unlink()
        raise


def _invalid(reason: str) -> NoReturn:
    raise ValueError(reason)


def _first_problem(error: ValidationError) -> str:
    """The first validation problem as ``field: message``, without echoing input values."""
    problems = error.errors(include_url=False, include_input=False)
    if not problems:
        return "invalid"
    first = problems[0]
    where = ".".join(str(part) for part in first["loc"]) or "document"
    return f"{where}: {first['msg']}"


def parse_command(name: str, data: bytes) -> Command:
    """The :class:`Command` in a command file named ``name`` holding ``data``.

    Checks, in order: ``name`` matches :data:`COMMAND_FILE`; ``data`` is at most
    ``MAX_COMMAND_BYTES``; UTF-8; one JSON object (no NaN or Infinity) with ``protocol`` 1; valid
    as a ``Command``; ``id`` equals the name's stem. Never raises anything but ``ValueError``,
    whatever the bytes (fuzz-tested).

    Raises:
        ValueError: with a one-line reason that goes into the rejected outcome's message.
    """
    if not COMMAND_FILE.match(name):
        raise ValueError(f"{name!r} is not a command file name (<ms13>-<server|pane>-<hex8>.json)")
    if len(data) > MAX_COMMAND_BYTES:
        raise ValueError(f"the file is {len(data)} bytes; the limit is {MAX_COMMAND_BYTES}")
    try:
        document = json.loads(data.decode("utf-8"), parse_constant=_invalid)
    except UnicodeDecodeError:
        raise ValueError("the file is not UTF-8 text") from None
    except (ValueError, RecursionError):
        raise ValueError("the file is empty or not complete JSON") from None
    if not isinstance(document, dict):
        raise ValueError("the file is not a JSON object")
    found = document.get("protocol")
    if found != PROTOCOL_VERSION or isinstance(found, bool):
        raise ValueError(f"unsupported protocol version {found!r}; this worker reads protocol 1")
    try:
        command = Command.model_validate(document)
    except ValidationError as error:
        raise ValueError(f"not a valid command ({_first_problem(error)})") from None
    except Exception as error:  # anything pydantic or a converter may raise on odd input
        raise ValueError(f"not a valid command ({type(error).__name__})") from None
    if command.id != name.removesuffix(".json"):
        raise ValueError(f"its id {command.id} differs from the file name {name}")
    return command


@dataclass(frozen=True)
class Pending:
    """A command file ready to be handled: parsed, or unreadable past its grace."""

    id: str
    source: Source
    command: Command | None
    """``None`` when the file could not be read as a command; ``problem`` says why."""
    problem: str | None = None


class _TooLarge(ValueError):
    """A command file over the size limit: rejected unread, without a grace."""


class InboxReader:
    """The worker's view of one run's ``commands/`` (single reader, the simulation thread).

    Remembers the IDs it has handed out and when it first saw each unreadable file, so every
    file is returned at most once and a torn pane file gets its grace. Holds no lock: only the
    worker's gate calls it.
    """

    def __init__(
        self, run_path: Path, grace_s: float, now: Callable[[], float] = time.monotonic
    ) -> None:
        self.run_path = run_path
        self.grace_s = grace_s
        self._now = now
        self._directory = run_path / COMMANDS_DIR
        self._done: set[str] = set()
        self._first_seen: dict[str, float] = {}

    def poll(self) -> list[Pending]:
        """List ``commands/`` (``os.scandir``; a missing directory is empty) and return, in name
        order, the not yet returned files up to the first one that is still within its grace
        (an empty or unparsable file seen for less than ``grace_s``). Each returned file is
        marked done; the caller must write its outcome. Never raises for odd entries: wrong
        names, directories, symlinks (opened with ``O_NOFOLLOW``) and unreadable files are
        skipped or returned as unreadable."""
        ready: list[Pending] = []
        for name in self._new_names():
            pending = self._take(name, final=False)
            if pending is None:
                break  # strict name order: later names wait behind a file in its grace
            ready.append(pending)
        return ready

    def remaining(self) -> list[Pending]:
        """Every file not yet returned, unreadable ones included regardless of their grace: the
        worker's final sweep, which rejects them because the run has ended."""
        return [pending for name in self._new_names() if (pending := self._take(name, True))]

    def _new_names(self) -> list[str]:
        """Regular command files not yet returned, in name order."""
        try:
            with os.scandir(self._directory) as entries:
                names = [
                    entry.name
                    for entry in entries
                    if COMMAND_FILE.match(entry.name)
                    and entry.name not in self._done
                    and entry.is_file(follow_symlinks=False)
                ]
        except OSError:  # missing (no command yet) or unreadable: nothing to handle now
            return []
        return sorted(names)

    def _take(self, name: str, final: bool) -> Pending | None:
        """The file as a :class:`Pending` (marked done), or ``None`` while it is in its grace."""
        stem = name.removesuffix(".json")
        source = cast(Source, stem.split("-")[1])
        try:
            command = parse_command(name, self._read(name))
        except _TooLarge as error:
            problem: str | None = str(error)
        except (OSError, ValueError) as error:
            first_seen = self._first_seen.setdefault(name, self._now())
            if not final and self._now() - first_seen < self.grace_s:
                return None
            waited = "" if final else f" after {self.grace_s:g} s"
            problem = f"unreadable{waited}: {_reason(error)}"
        else:
            problem = None
        self._done.add(name)
        self._first_seen.pop(name, None)
        if problem is not None:
            return Pending(id=stem, source=source, command=None, problem=problem)
        return Pending(id=stem, source=source, command=command)

    def _read(self, name: str) -> bytes:
        """The bytes of a regular file, opened without following a symlink.

        Raises:
            _TooLarge: over ``MAX_COMMAND_BYTES`` (not read).
            OSError: missing, a symlink, not a regular file, or unreadable.
        """
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        descriptor = os.open(self._directory / name, flags)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise OSError(f"{name} is not a regular file")
            if info.st_size > MAX_COMMAND_BYTES:
                raise _TooLarge(
                    f"the file is {info.st_size} bytes; the limit is {MAX_COMMAND_BYTES}"
                )
            return os.read(descriptor, MAX_COMMAND_BYTES + 1)
        finally:
            os.close(descriptor)


def _reason(error: Exception) -> str:
    if isinstance(error, OSError) and error.strerror:
        return f"it can't be read ({error.strerror})"
    return str(error)


def write_outcome(run_path: Path, outcome: CommandOutcome) -> None:
    """Atomically write ``outcomes/<outcome.id>.json`` (worker only; created once per command).

    Raises:
        ValueError: the outcome is invalid.
        OSError: the run directory is missing or not writable.
    """
    directory = run_path / OUTCOMES_DIR
    directory.mkdir(exist_ok=True)
    write_json(directory / f"{outcome.id}.json", outcome)


def read_outcome(run_path: Path, command_id: str) -> CommandOutcome | None:
    """The outcome of ``command_id``, or ``None`` if the worker hasn't written it yet. O(1): one
    file read, no listing.

    Raises:
        ValueError: ``command_id`` is not a command ID (it never names a file).
        ProtocolError: the outcome file exists but is invalid or of another protocol version.
    """
    if not _COMMAND_ID.match(command_id):
        raise ValueError(f"{command_id!r} is not a command ID")
    path = run_path / OUTCOMES_DIR / f"{command_id}.json"
    if not path.is_file():
        return None
    return read_json(path, CommandOutcome)
