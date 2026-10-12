"""Fixtures for the ``fmu_build`` tests: a fast-timing ``Config``, a ``Supervisor`` whose workers
(and any build tool left behind) are killed at teardown, and a writer for tiny slave sources.

Collaborators: the shared ``workspace`` fixture (``tests/conftest.py``), ``build_support``
(sources and process helpers, importable because pytest puts this directory on ``sys.path``),
``agentic_fmi.config`` and ``agentic_fmi.supervisor.Supervisor`` (real workers run real
``python -I -B -m pythonfmu build`` subprocesses).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from build_support import SLAVE, TEST_TIMING, kill_leftovers

from agentic_fmi.config import Config, load_config
from agentic_fmi.supervisor import Supervisor

WriteSource = Callable[..., Path]


@pytest.fixture
def config(workspace: Path) -> Config:
    """The server configuration for the test workspace, with ``TEST_TIMING``."""
    return replace(load_config(workspace), timing=TEST_TIMING)


@pytest.fixture
def supervisor(config: Config) -> Iterator[Supervisor]:
    """A ``Supervisor``; at teardown every worker it spawned is killed and reaped, and any
    process still naming the workspace (a stray build tool) is killed."""
    made = Supervisor(config)
    yield made
    kill_leftovers(made, config.workspace)


@pytest.fixture
def write_source(workspace: Path) -> WriteSource:
    """``write_source(rel, text=None, cls="AfmiGain", k=2.0)`` writes a source file into the
    workspace (default: a valid gain slave) and returns its path."""

    def write(rel: str, text: str | None = None, cls: str = "AfmiGain", k: float = 2.0) -> Path:
        path = workspace / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SLAVE.format(cls=cls, k=k) if text is None else text, encoding="utf-8")
        return path

    return write
