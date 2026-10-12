"""The command inbox (ADR 0010, ``store/inbox.py``): names, the server's exclusive create, tolerant
parsing (fuzzed), strict name order with a grace for torn pane files, each file handed out at
most once (a late lower name included), oversized and odd entries, outcomes, and the final sweep.

Collaborators: ``agentic_fmi.store.inbox`` (under test), ``agentic_fmi.store.protocol``,
``live_support`` (command builders). Pure file-system tests: no worker, no FMU.
"""

from __future__ import annotations

import errno
import json
import os
import random
import string
from contextlib import suppress
from pathlib import Path

import pytest
from live_support import command

from agentic_fmi.errors import ProtocolError
from agentic_fmi.store import inbox
from agentic_fmi.store.protocol import (
    COMMAND_ID_PATTERN,
    MAX_COMMAND_BYTES,
    CommandOutcome,
    utc_now,
)


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _write(run_path: Path, name: str, data: bytes | str) -> Path:
    path = run_path / inbox.COMMANDS_DIR / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return path


def _pane_name(millis: int, tag: str = "0000000a") -> str:
    return f"{millis:013d}-pane-{tag}.json"


def _command_bytes(name: str, action: str = "pause", **extra: object) -> bytes:
    stem = name.removesuffix(".json")
    document = {
        "protocol": 1,
        "id": stem,
        "source": stem.split("-")[1],
        "issued_at": "2026-10-11T00:00:00Z",
        "action": action,
        **extra,
    }
    return json.dumps(document).encode()


def test_new_ids_match_the_grammar_and_sort_by_time() -> None:
    import re

    first = inbox.new_command_id("server", now_ms=1_700_000_000_000)
    second = inbox.new_command_id("pane", now_ms=1_700_000_000_001)
    assert re.match(COMMAND_ID_PATTERN, first) and re.match(COMMAND_ID_PATTERN, second)
    assert first < second
    assert inbox.new_command_id("pane", now_ms=5).startswith("0000000000005-pane-")


def test_write_command_is_exclusive_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    sent = command("server", "step", n=3)
    path = inbox.write_command(tmp_path, sent)
    assert path.name == f"{sent.id}.json"
    assert sorted(os.listdir(path.parent)) == [path.name]
    assert inbox.parse_command(path.name, path.read_bytes()) == sent
    with pytest.raises(FileExistsError):
        inbox.write_command(tmp_path, sent)
    assert sorted(os.listdir(path.parent)) == [path.name]


def _link_fails_with(monkeypatch: pytest.MonkeyPatch, code: int) -> list[tuple[str, str]]:
    """Make ``os.link`` fail with ``code`` (a file system without hard links); returns its calls."""
    calls: list[tuple[str, str]] = []

    def refuse(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        calls.append((os.fspath(source), os.fspath(target)))
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(os, "link", refuse)
    return calls


@pytest.mark.parametrize("code", sorted(inbox._NO_HARD_LINKS))
def test_write_command_falls_back_to_an_exclusive_create_without_hard_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    calls = _link_fails_with(monkeypatch, code)
    sent = command("server", "step", n=3)
    path = inbox.write_command(tmp_path, sent)
    assert len(calls) == 1  # hard link first
    assert sorted(os.listdir(path.parent)) == [path.name]  # the temporary file is gone
    assert inbox.parse_command(path.name, path.read_bytes()) == sent
    with pytest.raises(FileExistsError):  # still exclusive: it never replaces a command
        inbox.write_command(tmp_path, sent)
    assert inbox.parse_command(path.name, path.read_bytes()) == sent
    assert sorted(os.listdir(path.parent)) == [path.name]


def test_write_command_does_not_fall_back_for_other_link_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _link_fails_with(monkeypatch, errno.EIO)
    sent = command("server", "pause")
    with pytest.raises(OSError, match="Input/output error"):
        inbox.write_command(tmp_path, sent)
    assert os.listdir(tmp_path / inbox.COMMANDS_DIR) == []  # nothing published, nothing left


def test_a_failed_fallback_write_leaves_no_partial_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _link_fails_with(monkeypatch, errno.EPERM)

    real_fsync = os.fsync
    fsyncs: list[int] = []

    def full_disk_in_place(descriptor: int) -> None:
        fsyncs.append(descriptor)
        if len(fsyncs) == 1:  # the temporary file is fine; the in-place copy is not
            return real_fsync(descriptor)
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "fsync", full_disk_in_place)
    with pytest.raises(OSError, match="No space"):
        inbox.write_command(tmp_path, command("server", "pause"))
    assert os.listdir(tmp_path / inbox.COMMANDS_DIR) == []


def test_write_command_refuses_an_oversized_command(tmp_path: Path) -> None:
    values = {f"v{i:04d}": float(i) for i in range(400)}
    with pytest.raises(ValueError, match="limit"):
        inbox.write_command(tmp_path, command("server", "set", values=values))
    assert not (tmp_path / inbox.COMMANDS_DIR).exists()


def test_parse_command_accepts_a_valid_file_and_names_each_problem() -> None:
    name = _pane_name(1_700_000_000_000)
    assert inbox.parse_command(name, _command_bytes(name)).action == "pause"
    problems = {
        "not a command file name": ("x.json", _command_bytes(name)),
        "empty or not complete JSON": (name, b'{"protocol": 1, "id'),
        "not UTF-8": (name, b"\xff\xfe"),
        "not a JSON object": (name, b"[1, 2]"),
        "unsupported protocol": (
            name,
            _command_bytes(name).replace(b'"protocol": 1', b'"protocol": 2'),
        ),
        "not a valid command": (name, _command_bytes(name, action="jump")),
        "differs from the file name": (_pane_name(1_700_000_000_001), _command_bytes(name)),
        "limit": (name, b" " * (MAX_COMMAND_BYTES + 1)),
    }
    for expected, (file_name, data) in problems.items():
        with pytest.raises(ValueError, match=expected):
            inbox.parse_command(file_name, data)


def test_parse_command_survives_fuzzed_bytes() -> None:
    """Whatever the bytes, ``parse_command`` returns a command or raises ``ValueError``."""
    rng = random.Random(20261011)
    name = _pane_name(1_700_000_000_000)
    valid = _command_bytes(name, action="set", values={"e": 0.5})
    alphabet = string.printable.encode() + bytes(range(256))
    samples: list[bytes] = [b"", b"null", b"NaN", b"[" * 3000 + b"]" * 3000, b'{"a":' * 2000]
    samples += [b'{"protocol": 1, "values": {"e": 1e999}}', b'{"protocol": true}']
    for _ in range(2000):
        data = bytearray(valid)
        for _ in range(rng.randint(1, 8)):
            if not data:
                break
            position = rng.randrange(len(data))
            choice = rng.random()
            if choice < 0.4:
                data[position] = rng.choice(alphabet)
            elif choice < 0.7:
                del data[position:]
            else:
                data[position:position] = bytes(rng.choice(alphabet) for _ in range(5))
        samples.append(bytes(data))
    for data in samples:
        with suppress(ValueError):  # a command or ValueError; never anything else
            inbox.parse_command(name, data)


def test_poll_returns_each_file_once_in_name_order_and_ignores_other_entries(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    reader = inbox.InboxReader(tmp_path, grace_s=2.0, now=clock)
    assert reader.poll() == []  # no commands/ yet
    late, early = _pane_name(1_700_000_000_002), _pane_name(1_700_000_000_001)
    _write(tmp_path, late, _command_bytes(late, action="resume"))
    _write(tmp_path, early, _command_bytes(early))
    _write(tmp_path, "notes.txt", "hello")
    _write(tmp_path, ".1700000000000-server-0000000a.json.tmp-1-2", "{")
    (tmp_path / inbox.COMMANDS_DIR / _pane_name(1_700_000_000_000, "000000dd")).mkdir()
    os.symlink(
        tmp_path / "elsewhere.json",
        tmp_path / inbox.COMMANDS_DIR / _pane_name(1_699_999_999_999, "0000000b"),
    )
    handed = reader.poll()
    assert [p.id for p in handed] == [early.removesuffix(".json"), late.removesuffix(".json")]
    assert [p.command.action for p in handed if p.command] == ["pause", "resume"]
    assert reader.poll() == [] and reader.remaining() == []


def test_a_late_lower_name_is_still_handed_out(tmp_path: Path) -> None:
    reader = inbox.InboxReader(tmp_path, grace_s=2.0, now=FakeClock())
    higher, lower = _pane_name(1_700_000_000_009), _pane_name(1_700_000_000_001)
    _write(tmp_path, higher, _command_bytes(higher))
    assert [p.id for p in reader.poll()] == [higher.removesuffix(".json")]
    _write(tmp_path, lower, _command_bytes(lower, action="resume"))
    [pending] = reader.poll()
    assert pending.id == lower.removesuffix(".json") and pending.command is not None
    assert pending.command.action == "resume"


def test_a_torn_file_is_retried_within_its_grace_and_holds_later_names_back(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    reader = inbox.InboxReader(tmp_path, grace_s=2.0, now=clock)
    torn, later = _pane_name(1_700_000_000_001), _pane_name(1_700_000_000_002)
    whole = _command_bytes(torn, action="step", n=2)
    _write(tmp_path, torn, whole[:10])
    _write(tmp_path, later, _command_bytes(later))
    assert reader.poll() == []  # the torn file is in its grace; the later one waits behind it
    clock.now += 1.0
    _write(tmp_path, torn, whole)  # the pane's write completes
    handed = reader.poll()
    assert [p.id for p in handed] == [torn.removesuffix(".json"), later.removesuffix(".json")]
    assert handed[0].command is not None and handed[0].command.n == 2


def test_an_unparsable_file_is_rejected_after_its_grace(tmp_path: Path) -> None:
    clock = FakeClock()
    reader = inbox.InboxReader(tmp_path, grace_s=2.0, now=clock)
    bad, later = _pane_name(1_700_000_000_001), _pane_name(1_700_000_000_002)
    _write(tmp_path, bad, "")
    _write(tmp_path, later, _command_bytes(later))
    assert reader.poll() == []
    clock.now += 1.9
    assert reader.poll() == []
    clock.now += 0.2
    handed = reader.poll()
    assert [p.id for p in handed] == [bad.removesuffix(".json"), later.removesuffix(".json")]
    assert handed[0].command is None
    assert handed[0].problem is not None and "unreadable after 2 s" in handed[0].problem
    assert handed[1].command is not None


def test_an_oversized_file_is_rejected_unread_at_once(tmp_path: Path) -> None:
    reader = inbox.InboxReader(tmp_path, grace_s=2.0, now=FakeClock())
    big = _pane_name(1_700_000_000_001)
    _write(tmp_path, big, b"{" + b" " * MAX_COMMAND_BYTES + b"}")
    [pending] = reader.poll()
    assert pending.command is None and pending.problem is not None and "limit" in pending.problem


def test_remaining_hands_out_every_file_once_regardless_of_grace(tmp_path: Path) -> None:
    reader = inbox.InboxReader(tmp_path, grace_s=2.0, now=FakeClock())
    torn, later = _pane_name(1_700_000_000_001), _pane_name(1_700_000_000_002)
    _write(tmp_path, torn, "{")
    _write(tmp_path, later, _command_bytes(later))
    assert reader.poll() == []
    left = reader.remaining()
    assert [(p.id, p.command is None) for p in left] == [
        (torn.removesuffix(".json"), True),
        (later.removesuffix(".json"), False),
    ]
    assert reader.remaining() == [] and reader.poll() == []


def test_outcomes_round_trip_and_are_found_by_id(tmp_path: Path) -> None:
    sent = command("pane", "pause")
    assert inbox.read_outcome(tmp_path, sent.id) is None
    outcome = CommandOutcome(
        run_id="r1",
        id=sent.id,
        source="pane",
        action="pause",
        status="applied",
        t=0.5,
        at=utc_now(),
        message="already paused",
    )
    inbox.write_outcome(tmp_path, outcome)
    assert inbox.read_outcome(tmp_path, sent.id) == outcome
    (tmp_path / inbox.OUTCOMES_DIR / f"{sent.id}.json").write_text("{")
    with pytest.raises(ProtocolError):
        inbox.read_outcome(tmp_path, sent.id)
    with pytest.raises(ValueError, match="not a command ID"):
        inbox.read_outcome(tmp_path, "../status")
