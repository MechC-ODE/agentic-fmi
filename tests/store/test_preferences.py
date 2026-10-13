"""``preferences.json``: the response style is stored atomically, and anything unreadable means
the default (a preference never fails a tool).

Collaborators: ``agentic_fmi.store.runstore`` (``read_preferences``, ``write_preferences``) and
``agentic_fmi.store.protocol`` (``Preferences``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import Preferences


def test_a_missing_file_means_ninja(tmp_path: Path) -> None:
    assert runstore.read_preferences(tmp_path / "store") == Preferences()
    assert Preferences().response_style == "ninja"


def test_the_style_round_trips_and_creates_the_store(tmp_path: Path) -> None:
    store = tmp_path / "store"
    runstore.write_preferences(store, Preferences(response_style="talkative"))
    assert runstore.read_preferences(store).response_style == "talkative"
    assert (store / "preferences.json").read_text(encoding="utf-8") == (
        '{"protocol":1,"response_style":"talkative"}\n'
    )
    assert not [p for p in store.iterdir() if ".tmp-" in p.name]  # the write was atomic


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        "[]",
        '{"protocol":1,"response_style":"loud"}',
        '{"protocol":2,"response_style":"talkative"}',
        '{"response_style":"talkative"}',
        "",
    ],
)
def test_an_unreadable_file_means_ninja(tmp_path: Path, text: str) -> None:
    (tmp_path / "preferences.json").write_text(text, encoding="utf-8")
    assert runstore.read_preferences(tmp_path).response_style == "ninja"


def test_unknown_fields_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "preferences.json").write_text(
        '{"protocol":1,"response_style":"talkative","later":true}', encoding="utf-8"
    )
    assert runstore.read_preferences(tmp_path).response_style == "talkative"
