"""Compare runs: what differs in their specs, and how their variables differ over time.

Works on the specs of single-FMU runs (``RunSpec``) and of system runs (``SystemSpec``, ADR
0009). A system run's differences are named ``parameters.<component>.<variable>`` (the
effective value, whichever layer set it), ``master.step_size``, ``experiment.*``,
``connections``, ``components.<name>.sha256`` and so on; they are listed parameters first, so
legends label runs by what was tuned.

Collaborators: ``runs.py`` (``run_compare``) passes specs and result arrays; ``summary.round_sig``
rounds the numbers.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence

import numpy as np
from pydantic import BaseModel

from agentic_fmi.analysis.summary import TIME_COLUMN, round_sig
from agentic_fmi.errors import UserError
from agentic_fmi.store.protocol import RunSpec, SystemSpec

SimulationSpec = RunSpec | SystemSpec
"""The specs of runs that have a time series."""

Leaf = str | float | int | bool | None

_IGNORED_FIELDS = frozenset(
    {"run_id", "created_at", "estimated_steps", "fmu.size_bytes", "outputs", "idle_limit_s"}
)
"""Spec fields that never count as a difference: identity, timestamps, values derived from other
fields, the recorded variable list (a missing variable is a note instead) and a live run's idle
limit (it can only end a run, not change its trajectory). ``live`` is compared: a live run's
result also depends on its applied commands (``commands/`` and ``outcomes/``, ADR 0010)."""


class SpecDifference(BaseModel):
    """One spec field whose value differs between runs, e.g. ``start_values.e``."""

    field: str
    values: dict[str, str | float | int | bool | None]
    """Run ID -> value (``None`` if absent in that run)."""


class RunMetrics(BaseModel):
    """One variable in one run."""

    run_id: str
    min: float | None = None
    max: float | None = None
    final: float | None = None
    max_abs_diff: float | None = None
    """Against the baseline run on the overlapping time range; ``None`` for the baseline."""
    rms_diff: float | None = None


class VariableComparison(BaseModel):
    name: str
    unit: str | None = None
    runs: list[RunMetrics]


class Comparison(BaseModel):
    """The ``run_compare`` result. The first run is the baseline."""

    baseline: str
    runs: list[str]
    spec_differences: list[SpecDifference]
    variables: list[VariableComparison]
    notes: list[str] = []
    """E.g. different FMUs, different time ranges, a variable missing in some runs."""


def diff_specs(specs: Sequence[SimulationSpec]) -> list[SpecDifference]:
    """Fields that differ across specs, flattened to dotted paths (``experiment.stop_time``,
    ``start_values.e``, ``inputs.u``, ``fmu.sha256``, ``fmi_type``; for a system run
    ``parameters.PI.Kp``, ``master.step_size``, ``connections``, ``components.PI.sha256``).
    ``run_id``, ``created_at`` and ``versions`` are ignored unless the versions differ (then
    ``versions.numpy`` and so on are listed). Also ignored: ``estimated_steps``,
    ``fmu.size_bytes`` and ``outputs`` (see ``_IGNORED_FIELDS``). An input signal or a list is
    one compact JSON text."""
    flat = [_flatten(spec) for spec in specs]
    paths = dict.fromkeys(path for fields in flat for path in fields)
    differences: list[SpecDifference] = []
    for path in paths:
        values = [fields.get(path) for fields in flat]
        if any(not _same(values[0], other) for other in values[1:]):
            differences.append(
                SpecDifference(
                    field=path,
                    values={s.run_id: value for s, value in zip(specs, values, strict=True)},
                )
            )
    return differences


def _flatten(spec: SimulationSpec) -> dict[str, Leaf]:
    """Dotted path -> scalar. An input signal or any list is one JSON-text leaf."""
    flat = _flatten_system(spec) if isinstance(spec, SystemSpec) else _flatten_run(spec)
    return {path: value for path, value in flat.items() if path not in _IGNORED_FIELDS}


def _flatten_run(spec: RunSpec) -> dict[str, Leaf]:
    flat: dict[str, Leaf] = {}
    for key, value in spec.model_dump(mode="json").items():
        if isinstance(value, dict):
            flat.update({f"{key}.{sub}": _leaf(item) for sub, item in value.items()})
        else:
            flat[key] = _leaf(value)
    return flat


def _flatten_system(spec: SystemSpec) -> dict[str, Leaf]:
    """What can differ between system runs, parameters first (legends use the first few).

    A parameter is its effective value only: the layer that set it doesn't change the result.
    ``connections`` is one JSON text, sorted so the order of the file doesn't matter. Each
    component is its FMU's sha256; paths, sizes and the equal ``experiment.output_interval``
    (it is ``master.step_size``) are left out.
    """
    flat: dict[str, Leaf] = {"kind": spec.kind}
    flat.update({f"parameters.{ref}": item.value for ref, item in spec.parameters.items()})
    flat["master.step_size"] = spec.master.step_size
    flat["experiment.start_time"] = spec.experiment.start_time
    flat["experiment.stop_time"] = spec.experiment.stop_time
    flat["connections"] = json.dumps(
        sorted(f"{c.from_}->{c.to}" for c in spec.connections), separators=(",", ":")
    )
    flat["name"] = spec.name
    flat.update({f"components.{name}.sha256": c.fmu.sha256 for name, c in spec.components.items()})
    flat["system.sha256"] = spec.system.sha256
    flat["paramset.sha256"] = None if spec.paramset is None else spec.paramset.sha256
    flat["omsimulator.version"] = spec.omsimulator.version
    flat["omsimulator.sha256"] = spec.omsimulator.sha256
    flat["timeout_s"] = spec.timeout_s
    flat["force"] = spec.force
    flat["live"] = spec.live
    flat.update({f"versions.{key}": value for key, value in spec.versions.model_dump().items()})
    return flat


def _leaf(value: object) -> Leaf:
    if value is None or isinstance(value, str | bool | int | float):
        return value
    return json.dumps(value, separators=(",", ":"))


def _same(a: object, b: object) -> bool:
    """Equal values; ``True`` and ``1`` count as different, ``1`` and ``1.0`` as the same."""
    return a == b and isinstance(a, bool) == isinstance(b, bool)


def series_difference(
    base_t: np.ndarray, base_y: np.ndarray, other_t: np.ndarray, other_y: np.ndarray
) -> tuple[float | None, float | None]:
    """Max absolute and RMS difference of ``other`` against ``base`` on ``base``'s grid,
    restricted to the overlapping time range. Duplicate times are collapsed to the last value
    (the post-event value) before ``np.interp``. ``(None, None)`` if there is no overlap."""
    base_t, base_y = _collapse(base_t, base_y)
    other_t, other_y = _collapse(other_t, other_y)
    if base_t.size == 0 or other_t.size == 0:
        return None, None
    overlap = (base_t >= other_t[0]) & (base_t <= other_t[-1])
    difference = np.interp(base_t[overlap], other_t, other_y) - base_y[overlap]
    difference = difference[np.isfinite(difference)]
    if difference.size == 0:
        return None, None
    return float(np.max(np.abs(difference))), float(np.sqrt(np.mean(difference**2)))


def _collapse(t: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Keep the last sample of each run of equal times, as floats."""
    if t.size == 0:
        return t.astype(np.float64), y.astype(np.float64)
    keep = np.append(t[1:] != t[:-1], True)
    return t[keep].astype(np.float64), y[keep].astype(np.float64)


def compare_runs(
    specs: Sequence[SimulationSpec],
    results: Mapping[str, np.ndarray],
    variables: Sequence[str],
    units: Mapping[str, str | None] | None = None,
    digits: int = 6,
) -> Comparison:
    """Build the comparison. Runs may have different time grids, lengths and FMUs.

    Raises:
        UserError: no specs, or a run in ``specs`` has no entry in ``results``.
    """
    if not specs:
        raise UserError("Nothing to compare: pass at least one run.")
    run_ids = [spec.run_id for spec in specs]
    missing = [run_id for run_id in run_ids if run_id not in results]
    if missing:
        raise UserError(f"Run {', '.join(missing)} has no result to compare yet.")
    notes = _grid_notes(specs, results)
    unit_of = units or {}
    comparisons: list[VariableComparison] = []
    for name in variables:
        present = [run_id for run_id in run_ids if name in (results[run_id].dtype.names or ())]
        if len(present) < len(run_ids):
            notes.append(_missing_note(name, run_ids, present))
        if present:
            runs = [_metrics(run_id, name, present[0], results, digits) for run_id in present]
            comparisons.append(VariableComparison(name=name, unit=unit_of.get(name), runs=runs))
    return Comparison(
        baseline=run_ids[0],
        runs=run_ids,
        spec_differences=diff_specs(specs),
        variables=comparisons,
        notes=notes,
    )


def _metrics(
    run_id: str, name: str, base_id: str, results: Mapping[str, np.ndarray], digits: int
) -> RunMetrics:
    """Statistics of one variable in one run; differences are against ``base_id``'s series."""
    values = results[run_id][name].astype(np.float64)
    finite = values[np.isfinite(values)]
    metrics = RunMetrics(
        run_id=run_id,
        min=round_sig(finite.min(), digits) if finite.size else None,
        max=round_sig(finite.max(), digits) if finite.size else None,
        final=round_sig(values[-1], digits) if values.size else None,
    )
    if run_id == base_id:
        return metrics
    base = results[base_id]
    max_abs, rms = series_difference(
        base[TIME_COLUMN], base[name], results[run_id][TIME_COLUMN], results[run_id][name]
    )
    return metrics.model_copy(
        update={
            "max_abs_diff": None if max_abs is None else round_sig(max_abs, digits),
            "rms_diff": None if rms is None else round_sig(rms, digits),
        }
    )


def _missing_note(name: str, run_ids: Sequence[str], present: Sequence[str]) -> str:
    absent = ", ".join(run_id for run_id in run_ids if run_id not in present)
    if not present:
        return f"Variable '{name}' is not in any of the runs."
    return f"Variable '{name}' is missing in {absent}; differences use the first run that has it."


def _fmus(spec: SimulationSpec) -> tuple[tuple[str, str], ...]:
    """The FMUs a run used, as (component, sha256) pairs; a single FMU has an empty component."""
    if isinstance(spec, SystemSpec):
        return tuple(sorted((name, c.fmu.sha256) for name, c in spec.components.items()))
    return (("", spec.fmu.sha256),)


def _grid_notes(specs: Sequence[SimulationSpec], results: Mapping[str, np.ndarray]) -> list[str]:
    """Notes about runs that aren't directly comparable: other FMU, range or time grid."""
    base = specs[0]
    base_time = results[base.run_id][TIME_COLUMN]
    notes: list[str] = []
    if len({_fmus(spec) for spec in specs}) > 1:
        field = "fmu.sha256" if all(isinstance(s, RunSpec) for s in specs) else "components"
        notes.append(f"The runs use different FMUs (see spec_differences {field}).")
    for spec in specs[1:]:
        time = results[spec.run_id][TIME_COLUMN]
        if time.size == 0 or base_time.size == 0:
            continue
        if not (_close(time[0], base_time[0]) and _close(time[-1], base_time[-1])):
            notes.append(
                f"{spec.run_id} covers t={round_sig(time[0])}..{round_sig(time[-1])} but "
                f"{base.run_id} covers t={round_sig(base_time[0])}..{round_sig(base_time[-1])}; "
                "differences use the overlap."
            )
        elif time.size != base_time.size or not np.array_equal(time, base_time):
            notes.append(
                f"{spec.run_id} has a different time grid than {base.run_id}; it is linearly "
                f"interpolated onto the grid of {base.run_id}."
            )
    return notes


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)
