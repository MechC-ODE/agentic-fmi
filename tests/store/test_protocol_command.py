"""The ``chart`` action of ``store.protocol.Command`` (ADR 0010, the pane's chart picker): which
arguments each action takes, and the rules for ``names`` (required for ``chart`` only, an empty
list allowed, at most 16 names, each once).

Collaborators: ``agentic_fmi.store.protocol`` (``Command``). Pure model tests: no store, no FMU.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from agentic_fmi.store.protocol import Command

ID = "1760000000000-pane-0a1b2c3d"


def _command(action: str, **arguments: Any) -> Command:
    return Command.model_validate(
        {
            "protocol": 1,
            "id": ID,
            "source": "pane",
            "issued_at": "2026-10-12T10:00:00Z",
            "action": action,
            **arguments,
        }
    )


def test_a_chart_command_carries_the_names_to_chart_in_order() -> None:
    assert _command("chart", names=["v", "h"]).names == ["v", "h"]


def test_a_chart_command_may_chart_nothing() -> None:
    assert _command("chart", names=[]).names == []


def test_chart_needs_names_and_only_chart_takes_them() -> None:
    with pytest.raises(ValidationError, match="names is required for chart"):
        _command("chart")
    for action, arguments in {
        "pause": {},
        "stop": {},
        "resume": {"speed": 2.0},
        "step": {"n": 2},
        "set": {"values": {"e": 0.6}},
    }.items():
        assert _command(action, **arguments).names is None
        with pytest.raises(ValidationError, match="allowed only there"):
            _command(action, names=["h"], **arguments)


def test_chart_takes_no_other_argument() -> None:
    for extra in ({"n": 1}, {"values": {"e": 0.6}}, {"speed": 2.0}):
        with pytest.raises(ValidationError):
            _command("chart", names=["h"], **extra)


def test_names_are_unique_and_at_most_sixteen() -> None:
    with pytest.raises(ValidationError, match="must not repeat"):
        _command("chart", names=["h", "v", "h"])
    assert len(_command("chart", names=[f"x{i}" for i in range(16)]).names or []) == 16
    with pytest.raises(ValidationError, match="at most 16"):
        _command("chart", names=[f"x{i}" for i in range(17)])


def test_a_chart_command_round_trips_through_json() -> None:
    sent = _command("chart", names=["h"])
    assert Command.model_validate_json(sent.model_dump_json()) == sent
    assert '"names":["h"]' in sent.model_dump_json(exclude_none=True)
