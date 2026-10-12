"""The output-budget helpers in ``budget.py``: compact JSON, the budget choice, and the cuts of
pages, ``FmuInfo`` and validation reports.

Collaborators: ``agentic_fmi.budget`` (under test), the result models it cuts
(``runs.RunPage``, ``fmu.inspect.FmuInfo``, ``fmu.validate.ValidationReport``) built by hand, and
``config.Limits``. The tools' end-to-end budget behaviour is covered in ``tests/mcp/``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agentic_fmi import budget
from agentic_fmi.config import Limits
from agentic_fmi.fmu.inspect import FmuInfo, Runnability, VariableInfo
from agentic_fmi.fmu.validate import ValidationReport
from agentic_fmi.runs import RunListItem, RunPage
from agentic_fmi.store.protocol import RunState

LIMITS = Limits()


def _run_page(count: int, offset: int = 0, name_chars: int = 100) -> RunPage:
    items = [
        RunListItem(
            run_id=f"r{offset + i + 1}",
            state=RunState.FINISHED,
            fmu_name="M" * name_chars,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        for i in range(count)
    ]
    return RunPage(total=offset + count + 10, offset=offset, limit=count, items=items)


def _variables(count: int, description: bool) -> list[VariableInfo]:
    return [
        VariableInfo(
            name=f"variable_{i:02d}_" + "x" * 50,
            causality="parameter",
            variability="tunable",
            type="Float64",
            start="0.5",
            description="d" * 120 if description else None,
        )
        for i in range(count)
    ]


def _info(per_list: int, description: bool) -> FmuInfo:
    return FmuInfo(
        path="m.fmu",
        sha256="0" * 64,
        size_bytes=1,
        fmi_version="3.0",
        model_name="M",
        interfaces=["CoSimulation"],
        runnability=Runnability(
            runnable=True, current_platform="linux64", platforms=["linux64"], interfaces=[]
        ),
        counts={"total": 3 * per_list},
        inputs=_variables(per_list, description),
        outputs=_variables(per_list, description),
        parameters=_variables(per_list, description),
    )


def test_compact_json_is_minified_without_nulls() -> None:
    page = RunPage(total=1, offset=0, limit=50, items=[])
    text = budget.compact_json(budget.compact_data(page))
    assert text == '{"total":1,"offset":0,"limit":50,"items":[]}'
    assert budget.json_size(page) == len(text)


def test_the_budget_is_the_hard_cap_only_when_more_than_the_default_was_asked() -> None:
    assert budget.json_budget(LIMITS, enlarged=False) == LIMITS.json_budget_bytes
    assert budget.json_budget(LIMITS, enlarged=True) == LIMITS.json_hard_cap_bytes


def test_a_page_that_fits_is_returned_as_is() -> None:
    page = _run_page(5, name_chars=10)
    assert budget.fit_page(page, LIMITS, None) is page


def test_a_page_is_cut_to_the_budget_and_continues_at_next_offset() -> None:
    page = _run_page(50, offset=20)
    assert budget.json_size(page) > LIMITS.json_budget_bytes
    cut = budget.fit_page(page, LIMITS, None)
    assert budget.json_size(cut) <= LIMITS.json_budget_bytes
    assert 0 < len(cut.items) < 50
    assert cut.items == page.items[: len(cut.items)]
    assert cut.limit == len(cut.items)
    assert cut.next_offset == 20 + len(cut.items)


def test_a_page_asked_for_beyond_the_default_may_use_the_hard_cap() -> None:
    page = _run_page(150)
    narrow = budget.fit_page(page, LIMITS, 50)
    wide = budget.fit_page(page, LIMITS, 150)
    assert budget.json_size(narrow) <= LIMITS.json_budget_bytes
    assert LIMITS.json_budget_bytes < budget.json_size(wide) <= LIMITS.json_hard_cap_bytes
    assert len(wide.items) > len(narrow.items)


def test_one_item_that_is_over_budget_is_kept() -> None:
    page = _run_page(1, name_chars=LIMITS.json_budget_bytes)
    assert budget.fit_page(page, LIMITS, None) is page


def test_validation_problems_are_cut_but_still_counted() -> None:
    report = ValidationReport(fmu="m.fmu", valid=False, n_problems=100, problems=["p" * 300] * 100)
    cut = budget.fit_report(report, LIMITS)
    assert budget.json_size(cut) <= LIMITS.json_budget_bytes
    assert 0 < len(cut.problems) < 100 and cut.truncated is True
    assert cut.n_problems == 100


def test_inspect_drops_descriptions_before_it_cuts_variables() -> None:
    info = _info(per_list=10, description=True)
    assert budget.json_size(info) > LIMITS.json_budget_bytes
    fitted = budget.fit_info(info, LIMITS)
    assert budget.json_size(fitted) <= LIMITS.json_budget_bytes
    assert fitted.truncated is False and len(fitted.inputs) == 10
    assert all(v.description is None for v in fitted.inputs + fitted.outputs + fitted.parameters)


def test_inspect_halves_the_variable_lists_when_descriptions_are_not_enough() -> None:
    info = _info(per_list=25, description=True)
    fitted = budget.fit_info(info, LIMITS)
    assert budget.json_size(fitted) <= LIMITS.json_budget_bytes
    assert fitted.truncated is True
    assert 0 < len(fitted.inputs) < 25 and len(fitted.inputs) == len(fitted.parameters)
    assert fitted.counts == info.counts
