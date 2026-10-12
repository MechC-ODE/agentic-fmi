"""Helpers for the MCP tool tests: a synchronous in-memory MCP session over the real server, and
cleanup that leaves no worker process behind.

Collaborators: ``tests/mcp/conftest.py`` (the fixtures that use them), ``agentic_fmi.mcp.server``
(``build_server``, run in this process), the MCP SDK's in-memory ``Client``, and
``agentic_fmi.store.runstore`` (to find runs a test left running). An anyio blocking portal runs
the client's event loop in a helper thread, so tests call tools with plain function calls.
Workers are real processes; the server never loads FMU binaries in this process.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio.from_thread
import pytest
from mcp import Client, StdioServerParameters
from mcp.types import CallToolResult, TextContent, Tool

from agentic_fmi.config import Config, Timing
from agentic_fmi.mcp.server import build_server
from agentic_fmi.store import runstore
from agentic_fmi.store.protocol import TERMINAL_STATES
from agentic_fmi.supervisor import is_alive

PNG_SIGNATURE = bytes.fromhex("89504e470d0a1a0a")

FAST_TIMING = Timing(heartbeat_stale_s=4.0, poll_interval_s=0.05)
"""Defaults except a 4 s stale threshold (eight heartbeat intervals, so a loaded machine doesn't
take a live worker over) and a quick poll, so supervision tests stay short."""


@dataclass
class Session:
    """A connected MCP client with helpers that fail the test on the wrong outcome."""

    portal: anyio.from_thread.BlockingPortal
    client: Client
    config: Config

    def call(self, tool: str, **arguments: Any) -> CallToolResult:
        """Call a tool and return the raw result, error or not."""
        return self.portal.call(self.client.call_tool, tool, arguments)

    def ok(self, tool: str, **arguments: Any) -> dict[str, Any]:
        """Call a tool that must succeed; return its structured content."""
        result = self.call(tool, **arguments)
        assert not result.is_error, text_of(result)
        assert result.structured_content is not None
        return result.structured_content

    def error(self, tool: str, **arguments: Any) -> str:
        """Call a tool that must fail with ``is_error``; return the message the model sees."""
        result = self.call(tool, **arguments)
        assert result.is_error, f"{tool} should have failed: {text_of(result)[:200]}"
        return text_of(result)

    def tools(self) -> list[Tool]:
        return self.portal.call(self.client.list_tools).tools

    def wait_for(self, run_id: str, states: set[str], timeout_s: float = 30.0) -> dict[str, Any]:
        """Poll ``run_status`` until the run is in one of ``states``."""
        limit = time.monotonic() + timeout_s
        while True:
            view = self.ok("run_status", run_id=run_id)
            if view["state"] in states:
                return view
            if time.monotonic() > limit:
                pytest.fail(f"{run_id} not in {sorted(states)} within {timeout_s} s: {view}")
            time.sleep(0.05)

    def stop_everything(self) -> None:
        """Stop and, if needed, kill every run still queued or running, then have the server
        reap its exited workers, so none outlives the test, not even as a zombie."""
        entries = runstore.read_index(self.config.store_dir).runs
        for entry in entries:
            if entry.state in TERMINAL_STATES:
                continue
            with suppress(Exception):
                self.call("run_stop", run_id=entry.run_id)
        kill_leftover_workers(self.config.store_dir)
        self.call("run_list")  # reconciles the runs still open
        if entries:  # reconcile reaps every exited worker whose run ended; run_list alone
            self.call("run_status", run_id=entries[-1].run_id)  # doesn't when none is open


def text_of(result: CallToolResult) -> str:
    """The text of a result's first content block."""
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def kill_leftover_workers(store_dir: Path, timeout_s: float = 10.0) -> None:
    """SIGKILL the process group of any worker a test left alive (or that is still exiting after
    its last write), then wait until none of them runs any more, so the reconciliation that
    follows can reap them: an exited worker nobody polls stays a zombie of this process until
    garbage collection."""
    workers: list[tuple[int, int | None]] = []
    for run_id in runstore.list_run_ids(store_dir):
        with suppress(Exception):
            status = runstore.read_status(store_dir / runstore.RUNS_DIR / run_id)
            if status.pid is not None and is_alive(status.pid, status.pid_start_time):
                os.killpg(status.pid, signal.SIGKILL)
                workers.append((status.pid, status.pid_start_time))
    deadline = time.monotonic() + timeout_s
    while any(is_alive(*worker) for worker in workers) and time.monotonic() < deadline:
        time.sleep(0.01)


@contextmanager
def open_session(config: Config) -> Iterator[Session]:
    """An in-memory client on a fresh server for ``config``; leftovers are stopped on exit."""
    server = build_server(config)
    with (
        anyio.from_thread.start_blocking_portal() as portal,
        portal.wrap_async_context_manager(Client(server)) as client,
    ):
        session = Session(portal, client, config)
        try:
            yield session
        finally:
            session.stop_everything()


@contextmanager
def open_stdio_session(config: Config) -> Iterator[Session]:
    """A client on a real ``afmi mcp --workspace`` subprocess over stdio; leftovers are stopped
    on exit, and the server process ends when the client disconnects."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agentic_fmi", "mcp", "--workspace", str(config.workspace)],
    )
    with (
        anyio.from_thread.start_blocking_portal() as portal,
        portal.wrap_async_context_manager(Client(params)) as client,
    ):
        session = Session(portal, client, config)
        try:
            yield session
        finally:
            session.stop_everything()
