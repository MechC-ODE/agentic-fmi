"""The pane's fixtures (``pane/fixtures/store.ts``) match the protocol models (ADR 0010).

``scripts/pane_fixtures.py`` builds them from the pydantic models; this test regenerates them in
memory and compares with the committed file byte for byte (the drift test), checks that every
fixture document reads back with its model, that the worst-case ``live.json`` fits
``Limits.live_max_bytes``, that each golden pane command is a valid ``Command`` and that the
discovery cases are the ones ``config.load_config`` resolves.

Collaborators: ``scripts/pane_fixtures.py`` (loaded by path: scripts are not a package),
``agentic_fmi.store.protocol``, ``agentic_fmi.config``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import BaseModel, ValidationError

from agentic_fmi.config import Limits
from agentic_fmi.store.protocol import (
    BuildEvent,
    BuildRecord,
    Command,
    CommandOutcome,
    Index,
    LiveSnapshot,
    RunSpec,
    RunStatus,
    SystemSpec,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "pane_fixtures.py"
COMMITTED = ROOT / "pane" / "fixtures" / "store.ts"


@pytest.fixture(scope="module")
def generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pane_fixtures", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["pane_fixtures"] = module
    spec.loader.exec_module(module)
    return module


def test_committed_fixtures_match_the_models(generator: ModuleType) -> None:
    assert COMMITTED.read_text(encoding="utf-8") == generator.render(), (
        "pane/fixtures/store.ts is stale; run uv run python scripts/pane_fixtures.py"
    )


def _model_for(path: str) -> type[BaseModel]:
    name = path.rsplit("/", 1)[-1]
    if "/outcomes/" in path:
        return CommandOutcome
    if name == "spec.json":
        return SystemSpec if "/r2/" in path else RunSpec
    models: dict[str, type[BaseModel]] = {
        "index.json": Index,
        "status.json": RunStatus,
        "live.json": LiveSnapshot,
        "build.json": BuildRecord,
        "events.jsonl": BuildEvent,
    }
    return models[name]


def test_every_fixture_document_reads_back(generator: ModuleType) -> None:
    for path, text in generator.store_files().items():
        if path.endswith("/r4/spec.json"):
            continue  # a BuildSpec: covered by the build tests
        _model_for(path).model_validate_json(text)


def test_variants_are_what_a_newer_or_broken_writer_leaves(generator: ModuleType) -> None:
    found = generator.variants(generator.store_files())
    assert json.loads(found["status_protocol_2"])["protocol"] == 2
    with pytest.raises(ValidationError):
        RunStatus.model_validate_json(found["status_unknown_state"])
    with pytest.raises(ValueError):
        json.loads(found["live_torn"])


def test_worst_case_live_json_fits_the_cap(generator: ModuleType) -> None:
    limits = Limits()
    text = generator.worst_live()
    snapshot = LiveSnapshot.model_validate_json(text)
    assert len(snapshot.time) == limits.live_points
    assert len(snapshot.series) == limits.live_variables
    assert len(snapshot.values) == limits.live_max_columns
    assert len(text.encode("utf-8")) < limits.live_max_bytes


def test_golden_pane_commands_are_valid_commands(generator: ModuleType) -> None:
    commands = {
        button: Command.model_validate_json(text)
        for button, text in generator.pane_commands().items()
    }
    assert set(commands) == {"pause", "resume", "step", "set", "stop", "chart"}
    assert all(command.source == "pane" for command in commands.values())
    assert commands["step"].n == 1
    assert commands["set"].values == {"e": 0.6}
    assert commands["chart"].names == ["h"]  # r1 charts h and v; the ✕ on v leaves h


def test_discovery_cases_follow_load_config(generator: ModuleType) -> None:
    by_env = {row["env"]: row["store"] for row in generator.discovery()}
    assert by_env[None] == by_env[""] == by_env["   "] == "/ws/.agentic-fmi"
    assert by_env["store"] == "/ws/store"
    assert by_env["../shared/store"] == "/shared/store"
    assert by_env[" /lab/store "] == "/lab/store"
    assert by_env["~/afmi-store"] == "/home/dev/afmi-store"
