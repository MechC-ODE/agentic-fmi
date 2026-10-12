"""Output budgets (principle 2): measure a result's compact JSON and shrink it until it fits.

The mechanics (:func:`compact_data`, :func:`compact_json`, :func:`json_size`,
:func:`fit_to_budget`, :func:`json_budget`) serve every tool. The shrinking strategies of the
explore results sit here too, behind :func:`fit_page` (a page of FMUs, variables or runs cut to
the budget; ``next_offset`` continues where it now ends), :func:`fit_info` (``fmu_inspect``:
variable descriptions go first, then the variable lists are halved) and :func:`fit_report`
(``fmu_validate``: fewer problems, ``truncated``), so the MCP adapters only call them. The
strategies for results that only ``runs.py`` builds (``RunView``, ``RunResults``,
``Comparison``) stay beside those models there and use :func:`fit_to_budget`.

A result is measured as the compact JSON text a tool would send (no nulls, minified), against
``Limits.json_budget_bytes``, or ``Limits.json_hard_cap_bytes`` when the caller explicitly asked
for more than a default page or default points.

Collaborators: ``config.Limits``; the models it cuts (``fmu/catalog.py``, ``fmu/inspect.py``,
``fmu/validate.py``, ``runs.RunPage``); ``runs.py`` and ``mcp/server.py`` call it. Never loads FMU
binaries.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel

from agentic_fmi.config import Limits
from agentic_fmi.fmu.catalog import FmuPage
from agentic_fmi.fmu.inspect import FmuInfo, VariablePage
from agentic_fmi.fmu.validate import ValidationReport

if TYPE_CHECKING:
    from agentic_fmi.runs import RunPage

M = TypeVar("M", bound=BaseModel)
Page = TypeVar("Page", FmuPage, VariablePage, "RunPage")

INFO_LISTS = ("inputs", "outputs", "parameters")
"""The variable lists inside ``FmuInfo`` that are cut when the result is over budget."""


def compact_data(model: BaseModel) -> dict[str, Any]:
    """``model`` as JSON-ready data without nulls: what tools publish as structured content."""
    return model.model_dump(mode="json", exclude_none=True)


def compact_json(data: Mapping[str, Any]) -> str:
    """Minified JSON text of ``data`` (non-ASCII kept as is)."""
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def json_size(model: BaseModel) -> int:
    """Bytes of the compact JSON text a tool would send for ``model``."""
    return len(compact_json(compact_data(model)).encode("utf-8"))


def json_budget(limits: Limits, enlarged: bool) -> int:
    """The default JSON budget, or the hard cap if the caller asked for more than the default
    (a bigger page, more points)."""
    return limits.json_hard_cap_bytes if enlarged else limits.json_budget_bytes


def fit_to_budget(model: M, budget: int, smaller: Callable[[M], M | None]) -> M:
    """Apply ``smaller`` until the compact JSON of ``model`` fits ``budget`` or it gives up."""
    while json_size(model) > budget:
        reduced = smaller(model)
        if reduced is None:
            break
        model = reduced
    return model


def fit_page(page: Page, limits: Limits, requested: int | None) -> Page:
    """``page`` cut to the JSON budget; ``requested`` is the page size the caller asked for
    (``None``: the default), which picks the hard cap when it is above ``Limits.page_default``."""
    enlarged = requested is not None and requested > limits.page_default
    return fit_to_budget(page, json_budget(limits, enlarged), _fewer_items)


def fit_info(info: FmuInfo, limits: Limits) -> FmuInfo:
    """``info`` without variable descriptions, then with shorter variable lists, to fit."""
    return fit_to_budget(info, limits.json_budget_bytes, _smaller_info)


def fit_report(report: ValidationReport, limits: Limits) -> ValidationReport:
    """``report`` with fewer listed problems (``n_problems`` still counts all) to fit."""
    return fit_to_budget(report, limits.json_budget_bytes, _fewer_problems)


def _fewer_items(page: Page) -> Page | None:
    """Three quarters of the page's items; ``next_offset`` then continues where it now ends."""
    count = len(page.items)
    if count <= 1:
        return None
    keep = count * 3 // 4
    update = {"items": page.items[:keep], "limit": keep, "next_offset": page.offset + keep}
    return page.model_copy(update=update)


def _fewer_problems(report: ValidationReport) -> ValidationReport | None:
    count = len(report.problems)
    if count <= 1:
        return None
    return report.model_copy(
        update={"problems": report.problems[: count * 3 // 4], "truncated": True}
    )


def _smaller_info(info: FmuInfo) -> FmuInfo | None:
    """Drop the variable descriptions, then halve the variable lists."""
    lists = {name: getattr(info, name) for name in INFO_LISTS}
    if any(variable.description for variables in lists.values() for variable in variables):
        return info.model_copy(
            update={
                name: [v.model_copy(update={"description": None}) for v in variables]
                for name, variables in lists.items()
            }
        )
    longest = max(len(variables) for variables in lists.values())
    if longest <= 1:
        return None
    keep = longest // 2
    cut = {name: variables[:keep] for name, variables in lists.items()}
    return info.model_copy(update={**cut, "truncated": True})
