"""Proof that the ``fmu/`` package never loads FMU binaries into the calling process.

Collaborators: every public function of ``agentic_fmi.fmu.{catalog,inspect,validate}``, the
``fmu`` and ``config`` fixtures. After running them on all 15 Reference FMUs in this process, the
memory map (``/proc/self/maps``) must hold no new file under a ``/binaries/`` directory, which is
where an extracted FMU's shared library would live. Linux only: it skips where ``/proc`` is absent.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from pathlib import Path

import pytest
from fmpy import read_model_description

from agentic_fmi.config import Config, Limits
from agentic_fmi.errors import UserError
from agentic_fmi.fmu import catalog
from agentic_fmi.fmu import inspect as fmu_inspect
from agentic_fmi.fmu.validate import validate_fmu

FmuFactory = Callable[[str, str], Path]
MAPS = Path("/proc/self/maps")
ALL_FMUS = [
    *(("2.0", name) for name in ("BouncingBall", "Dahlquist", "Feedthrough", "Resource")),
    ("2.0", "Stair"),
    ("2.0", "VanDerPol"),
    *(("3.0", name) for name in ("BouncingBall", "Clocks", "Dahlquist", "Feedthrough")),
    *(("3.0", name) for name in ("Resource", "Roberts", "Stair", "StateSpace", "VanDerPol")),
]


def _mapped_binaries() -> set[str]:
    """Memory-map lines that mention a ``/binaries/`` path."""
    return {line.strip() for line in MAPS.read_text().splitlines() if "/binaries/" in line}


def _exercise(config: Config, fmu: FmuFactory, version: str, name: str) -> None:
    """Call every public ``fmu/`` function on one Reference FMU."""
    path = fmu(version, name)
    relative = f"{version}/{name}.fmu"
    md = fmu_inspect.read_description(path)
    fmu_inspect.inspect_fmu(config, relative)
    fmu_inspect.list_variables(config, relative, name="e")
    validate_fmu(config, relative)
    fmu_inspect.sha256_file(path)
    fmu_inspect.interfaces_of(md)
    fmu_inspect.variable_types(md)
    fmu_inspect.check_runnability(path, md, Limits())
    fmu_inspect.platform_blocker(path, md)
    declared = {"CoSimulation": md.coSimulation, "ModelExchange": md.modelExchange}
    for fmi_type, interface in declared.items():
        if interface is None:
            continue
        experiment = fmu_inspect.resolve_experiment(md, fmi_type)
        fmu_inspect.estimate_internal_steps(md, fmi_type, experiment)
        # Roberts CS is refused by design; that path must not load anything either.
        with contextlib.suppress(UserError):
            fmu_inspect.check_run(path, md, fmi_type, experiment, Limits(), force=False)
    fmu_inspect.require_variables(md, [v.name for v in md.modelVariables], purpose="test")


@pytest.mark.skipif(not MAPS.is_file(), reason="needs /proc/self/maps (Linux)")
def test_fmu_package_loads_no_native_fmu_code(config: Config, fmu: FmuFactory) -> None:
    """Running all ``fmu/`` functions on all 15 FMUs maps no file from a ``binaries/`` folder."""
    assert "python" in MAPS.read_text().lower()  # the map is readable and describes this process
    before = _mapped_binaries()
    for version, name in ALL_FMUS:
        _exercise(config, fmu, version, name)
    assert catalog.list_fmus(config).total == len(ALL_FMUS)
    assert read_model_description(str(fmu("3.0", "BouncingBall"))).modelName == "BouncingBall"
    new = _mapped_binaries() - before
    assert not new, f"FMU binaries were mapped into the test process: {sorted(new)}"
