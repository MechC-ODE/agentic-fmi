"""OMSimulator stays out of every process but a system run's worker (ADR 0002, ADR 0009).

Collaborators: the whole ``agentic_fmi`` package, imported in a fresh interpreter, and its
sources, parsed with ``ast``. ``import OMSimulator`` loads ``libOMSimulator.so``, so only
``engines/oms_engine.py`` may import it, and only inside a function.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import agentic_fmi

PACKAGE_DIR = Path(agentic_fmi.__file__).resolve().parent
ENGINE = PACKAGE_DIR / "engines" / "oms_engine.py"

IMPORT_EVERYTHING = """
import json, pkgutil, importlib, sys
import agentic_fmi
from agentic_fmi import oms_pin
names = [m.name for m in pkgutil.walk_packages(agentic_fmi.__path__, "agentic_fmi.")
         if not m.name.endswith("__main__")]
for name in names:
    importlib.import_module(name)
oms_pin.installed()
maps = open("/proc/self/maps").read()
print(json.dumps({"modules": names, "imported": "OMSimulator" in sys.modules,
                  "mapped": "libOMSimulator" in maps}))
"""


def _imports_omsimulator(node: ast.AST) -> bool:
    if isinstance(node, ast.Import):
        return any(alias.name.split(".")[0] == "OMSimulator" for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        return (node.module or "").split(".")[0] == "OMSimulator"
    return False


def test_importing_every_module_leaves_omsimulator_unloaded() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", IMPORT_EVERYTHING],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert {"agentic_fmi.worker", "agentic_fmi.engines.oms_engine"} <= set(report["modules"])
    assert "agentic_fmi.mcp.server" in report["modules"]
    assert report["imported"] is False
    assert report["mapped"] is False


def test_only_the_oms_engine_imports_omsimulator_and_only_lazily() -> None:
    importers = {
        path.relative_to(PACKAGE_DIR).as_posix()
        for path in PACKAGE_DIR.rglob("*.py")
        if any(map(_imports_omsimulator, ast.walk(ast.parse(path.read_text(encoding="utf-8")))))
    }
    assert importers == {"engines/oms_engine.py"}
    module = ast.parse(ENGINE.read_text(encoding="utf-8"))
    assert not any(map(_imports_omsimulator, module.body))  # never at module level
