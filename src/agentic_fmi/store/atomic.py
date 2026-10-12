"""Crash-safe file primitives for the run store: atomic JSON replace, single-line appends, and
an advisory lock. They know nothing about the run-store layout.

Collaborators: ``store/runstore.py`` builds every read and write on these helpers; the worker
uses them through ``runstore``. Linux only (``fcntl``), like the rest of the run store.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import IO, NoReturn, TypeVar

from pydantic import BaseModel, ValidationError

from agentic_fmi.errors import ProtocolError
from agentic_fmi.store.protocol import MAX_EVENT_LINE_BYTES, PROTOCOL_VERSION

M = TypeVar("M", bound=BaseModel)

_MAX_REPORTED_PROBLEMS = 3
"""Validation problems quoted in a ``ProtocolError`` message."""


class UnsupportedProtocolError(ProtocolError):
    """The document's ``protocol`` is not 1. Callers that repair damaged files (the index
    rebuild) must let it through, because the file is intact and a newer writer owns it."""


def _tmp_path(path: Path) -> Path:
    """The temporary sibling of ``path``: ``<name>.tmp-<pid>-<thread id>``."""
    return path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")


@contextmanager
def atomic_open(path: Path) -> Iterator[IO[bytes]]:
    """Yield a binary handle on a temporary file next to ``path``; replace ``path`` on success.

    On a clean exit the data is flushed and ``os.fsync``-ed before ``os.replace`` moves the
    temporary file into place, so a reader sees the old file or the new one, never a partial
    one. On any exception the temporary file is removed and ``path`` is left untouched.
    """
    tmp = _tmp_path(path)
    try:
        with tmp.open("wb") as handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with suppress(FileNotFoundError):
            tmp.unlink()
        raise


def dump_json(model: BaseModel) -> str:
    """Validate ``model`` again and return it as compact JSON without a trailing newline.

    ``model_copy(update=...)`` skips validation, so this is where a NaN or a bad run ID is
    caught. ``json.dumps(..., allow_nan=False)`` is the second guard: JSON never carries NaN
    or Infinity.

    Raises:
        ValueError: the model is invalid or holds a non-finite float.
    """
    checked = type(model).model_validate(model.model_dump())
    return json.dumps(
        checked.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def write_json(path: Path, model: BaseModel) -> None:
    """Atomically replace ``path`` with ``model`` as compact UTF-8 JSON plus a newline.

    Steps: re-validate the model (``model_copy(update=...)`` skips validation, so this is where a
    NaN or a bad run ID is caught), dump with ``json.dumps(..., allow_nan=False)``, write to
    ``<name>.tmp-<pid>-<thread id>`` in the same directory, flush and ``os.fsync``, then
    ``os.replace`` onto ``path``. A reader never sees a partial file. The temporary file is
    removed if any step fails.

    Raises:
        ValueError: the model is invalid or holds a non-finite float.
        OSError: the directory is missing or not writable.
    """
    data = (dump_json(model) + "\n").encode("utf-8")
    with atomic_open(path) as handle:
        handle.write(data)


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(f"{name} is not valid JSON")


def _load_document(path: Path) -> dict[str, object]:
    """Read ``path`` as a JSON object, or raise ``ProtocolError`` naming the file."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise ProtocolError(f"{path} is missing.") from None
    except OSError as error:
        raise ProtocolError(f"{path} cannot be read: {error.strerror or error}.") from error
    try:
        document = json.loads(raw, parse_constant=_reject_constant)
    except ValueError as error:
        raise ProtocolError(f"{path} is not valid JSON ({error}).") from error
    if not isinstance(document, dict):
        raise ProtocolError(f"{path} is not a JSON object.")
    return document


def _describe(error: ValidationError) -> str:
    """The first few validation problems as ``field: message`` without echoing input values."""
    problems = [
        f"{'.'.join(str(part) for part in problem['loc']) or 'document'}: {problem['msg']}"
        for problem in error.errors(include_url=False, include_input=False)
    ]
    shown = "; ".join(problems[:_MAX_REPORTED_PROBLEMS])
    extra = len(problems) - _MAX_REPORTED_PROBLEMS
    return shown + (f"; and {extra} more" if extra > 0 else "")


def read_json(path: Path, model: type[M]) -> M:
    """Read ``path`` and validate it as ``model``.

    Raises:
        ProtocolError: the file is missing, is not JSON, fails validation, or has a ``protocol``
            other than 1 (the message names the file and says "unsupported protocol version"
            for the last case, and the exception is an :class:`UnsupportedProtocolError`).
            Callers that poll may catch it and retry.
    """
    document = _load_document(path)
    found = document.get("protocol")
    if found != PROTOCOL_VERSION or isinstance(found, bool):
        raise UnsupportedProtocolError(
            f"{path} has unsupported protocol version {found!r}; this build reads "
            f"protocol {PROTOCOL_VERSION}. Use a matching Agentic FMI version."
        )
    try:
        return model.model_validate(document)
    except ValidationError as error:
        raise ProtocolError(f"{path} is invalid: {_describe(error)}.") from error


def append_line(path: Path, line: str, max_bytes: int = MAX_EVENT_LINE_BYTES) -> None:
    """Append ``line`` plus ``\\n`` with a single ``os.write`` on an ``O_APPEND`` descriptor.

    Several processes may append to the same file; one ``write`` of at most ``max_bytes`` on a
    local Linux filesystem is not interleaved with others.

    Raises:
        ValueError: ``line`` contains a newline or encodes to more than ``max_bytes`` with it.
    """
    if "\n" in line:
        raise ValueError("an appended line must not contain a newline")
    data = (line + "\n").encode("utf-8")
    if len(data) > max_bytes:
        raise ValueError(f"line is {len(data)} bytes with its newline; the limit is {max_bytes}")
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        written = os.write(descriptor, data)
    finally:
        os.close(descriptor)
    if written != len(data):
        raise OSError(f"short write to {path}: {written} of {len(data)} bytes")


@contextmanager
def locked(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive ``fcntl.flock`` on ``lock_path`` (created if missing) for the block.

    ``flock`` locks belong to the open file description, so two threads of one process that
    each open the file exclude each other, as do two processes. Use it only around short
    read-modify-write sections (``index.json``); never hold it while waiting for a worker.
    """
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)  # closing the only descriptor of this description drops the lock
