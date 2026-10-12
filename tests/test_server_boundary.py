"""The server side never imports the worker side (ADR 0002, docs/architecture.md).

Collaborators: ``agentic_fmi.mcp.server`` and ``agentic_fmi.control``, imported in a fresh
interpreter. ``control.py`` imports ``live.py`` for its set checks, so ``live.py`` must reach
``engines/`` for typing only; a runtime import there would put the engine seam (and the next
import after it) into the MCP process.
"""

from __future__ import annotations

import json
import subprocess
import sys

IMPORT_SERVER = """
import json, sys
import agentic_fmi.mcp.server, agentic_fmi.control
banned = ("agentic_fmi.engines", "agentic_fmi.worker", "OMSimulator")
print(json.dumps(sorted(m for m in sys.modules if m.startswith(banned))))
"""


def test_the_server_imports_no_engine_worker_or_omsimulator_module() -> None:
    result = subprocess.run(
        [sys.executable, "-c", IMPORT_SERVER], capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout.strip().splitlines()[-1]) == []
