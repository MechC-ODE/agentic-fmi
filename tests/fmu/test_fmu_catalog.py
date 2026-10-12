"""Tests for ``agentic_fmi.fmu.catalog``: path resolution inside the workspace, the FMU scan and
``list_fmus`` paging and filtering.

Collaborators: the ``workspace``, ``fmu`` and ``reference_fmus`` fixtures (``tests/conftest.py``),
and ``config``, ``write_fmu`` and ``fmi2_xml`` (``tests/fmu/conftest.py``). Reference FMUs are
copied into the temporary workspace first. Only ``modelDescription.xml`` is read.
"""

from __future__ import annotations

import dataclasses
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from agentic_fmi.config import Config, Limits
from agentic_fmi.errors import UserError
from agentic_fmi.fmu import catalog

FmuFactory = Callable[[str, str], Path]
WriteFmu = Callable[[str, dict[str, str]], Path]
Fmi2Xml = Callable[..., str]


def _symlink(link: Path, target: Path) -> None:
    """Create ``link`` -> ``target``; skip the test where symlinks aren't available."""
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks not available: {exc}")


@pytest.fixture
def synth_fmu(write_fmu: WriteFmu, fmi2_xml: Fmi2Xml) -> Callable[[str], Path]:
    """Return ``synth_fmu(name)``: a tiny valid FMI 2.0 archive in the workspace."""
    return lambda name: write_fmu(name, {"modelDescription.xml": fmi2_xml(inputs=1)})


# resolve_fmu ---------------------------------------------------------------------------------


def test_resolve_relative_and_absolute_paths_give_the_same_resolved_file(
    config: Config, synth_fmu: Callable[[str], Path]
) -> None:
    """A workspace-relative and an absolute argument resolve to one absolute path."""
    created = synth_fmu("models/a.fmu")
    assert catalog.resolve_fmu(config, "models/a.fmu") == created.resolve()
    assert catalog.resolve_fmu(config, str(created)) == created.resolve()


def test_resolve_missing_fmu_suggests_close_matches(config: Config, fmu: FmuFactory) -> None:
    """A mistyped name lists the closest existing FMUs and points at fmu_list."""
    fmu("3.0", "BouncingBall")
    with pytest.raises(
        UserError, match=r"not found.*Close matches: 3\.0/BouncingBall\.fmu.*fmu_list"
    ):
        catalog.resolve_fmu(config, "3.0/BouncingBal.fmu")


def test_resolve_missing_fmu_suggests_same_name_in_another_folder(
    config: Config, fmu: FmuFactory
) -> None:
    """A right file name in the wrong folder still finds the real path."""
    fmu("2.0", "Stair")
    with pytest.raises(UserError, match=r"Close matches: 2\.0/Stair\.fmu"):
        catalog.resolve_fmu(config, "Stair.fmu")


def test_resolve_rejects_empty_argument(config: Config) -> None:
    """An empty argument is a UserError that points at fmu_list."""
    with pytest.raises(UserError, match="fmu_list"):
        catalog.resolve_fmu(config, "  ")


def test_resolve_rejects_other_file_types(config: Config, workspace: Path) -> None:
    """Only ``.fmu`` archives resolve."""
    (workspace / "notes.txt").write_text("not an fmu")
    with pytest.raises(UserError, match=r"not a \.fmu file"):
        catalog.resolve_fmu(config, "notes.txt")


def test_resolve_rejects_directories_named_like_fmus(config: Config, workspace: Path) -> None:
    """An extracted FMU directory is not accepted; the message asks for the archive."""
    (workspace / "dir.fmu").mkdir()
    with pytest.raises(UserError, match="directory"):
        catalog.resolve_fmu(config, "dir.fmu")


def test_resolve_rejects_absolute_path_outside_workspace(
    config: Config, fmu: FmuFactory, tmp_path: Path
) -> None:
    """A file outside the allowed roots is refused with the 'copy it in' advice."""
    outside = tmp_path / "elsewhere" / "Ball.fmu"
    outside.parent.mkdir()
    shutil.copy2(fmu("3.0", "BouncingBall"), outside)
    with pytest.raises(UserError, match=r"outside the workspace.*Copy the FMU into the workspace"):
        catalog.resolve_fmu(config, str(outside))


def test_resolve_rejects_parent_traversal_out_of_workspace(
    config: Config, fmu: FmuFactory, tmp_path: Path
) -> None:
    """``../`` segments are resolved before the containment check."""
    shutil.copy2(fmu("3.0", "BouncingBall"), tmp_path / "Ball.fmu")
    with pytest.raises(UserError, match="outside the workspace"):
        catalog.resolve_fmu(config, "../Ball.fmu")


def test_resolve_rejects_nonexistent_absolute_path_outside_workspace(config: Config) -> None:
    """A path outside the roots is refused before its existence is revealed."""
    with pytest.raises(UserError, match="outside the workspace"):
        catalog.resolve_fmu(config, "/definitely/not/here.fmu")


def test_resolve_rejects_symlink_that_points_outside_workspace(
    config: Config, fmu: FmuFactory, workspace: Path, tmp_path: Path
) -> None:
    """A symlink inside the workspace whose target is outside is refused."""
    outside = tmp_path / "elsewhere" / "Ball.fmu"
    outside.parent.mkdir()
    shutil.copy2(fmu("3.0", "BouncingBall"), outside)
    _symlink(workspace / "link.fmu", outside)
    with pytest.raises(UserError, match=r"outside the workspace.*symlink"):
        catalog.resolve_fmu(config, "link.fmu")


def test_resolve_rejects_dangling_symlink_that_points_outside_workspace(
    config: Config, workspace: Path, tmp_path: Path
) -> None:
    """A dangling symlink to an outside target gets the outside error, not 'not found'."""
    _symlink(workspace / "dangling.fmu", tmp_path / "nowhere.fmu")
    with pytest.raises(UserError, match="outside the workspace"):
        catalog.resolve_fmu(config, "dangling.fmu")


def test_resolve_follows_symlink_that_stays_inside_workspace(
    config: Config, synth_fmu: Callable[[str], Path], workspace: Path
) -> None:
    """A symlink to another workspace file resolves to that file."""
    target = synth_fmu("real.fmu")
    _symlink(workspace / "alias.fmu", target)
    assert catalog.resolve_fmu(config, "alias.fmu") == target.resolve()


# find_fmus -----------------------------------------------------------------------------------


def test_find_fmus_skips_ignored_directories_and_sorts_by_path(
    config: Config, synth_fmu: Callable[[str], Path]
) -> None:
    """Skipped directories are not scanned; the rest come back sorted by relative path."""
    for name in (
        "b.fmu",
        "a/z.fmu",
        "a/y.FMU",
        ".git/x.fmu",
        "node_modules/p/x.fmu",
        ".agentic-fmi/r.fmu",
    ):
        synth_fmu(name)
    found = [catalog.relative_path(config, p) for p in catalog.find_fmus(config)]
    assert found == ["a/y.FMU", "a/z.fmu", "b.fmu"]


def test_find_fmus_stops_at_the_maximum_depth(
    config: Config, synth_fmu: Callable[[str], Path]
) -> None:
    """A directory at ``MAX_SCAN_DEPTH`` is scanned; one level deeper is not."""
    at_limit = "/".join(["d"] * catalog.MAX_SCAN_DEPTH) + "/in.fmu"
    too_deep = "/".join(["d"] * (catalog.MAX_SCAN_DEPTH + 1)) + "/out.fmu"
    synth_fmu(at_limit)
    synth_fmu(too_deep)
    assert [catalog.relative_path(config, p) for p in catalog.find_fmus(config)] == [at_limit]


def test_find_fmus_ignores_symlinks_that_leave_the_workspace(
    config: Config, synth_fmu: Callable[[str], Path], workspace: Path, tmp_path: Path
) -> None:
    """Outside and dangling symlinks are skipped; inside ones are listed."""
    inside = synth_fmu("real.fmu")
    outside = tmp_path / "outside.fmu"
    shutil.copy2(inside, outside)
    _symlink(workspace / "out.fmu", outside)
    _symlink(workspace / "gone.fmu", tmp_path / "missing.fmu")
    _symlink(workspace / "in.fmu", inside)
    found = [catalog.relative_path(config, p) for p in catalog.find_fmus(config)]
    assert found == ["in.fmu", "real.fmu"]


# list_fmus -----------------------------------------------------------------------------------


def test_list_fmus_reports_model_facts_for_reference_fmus(config: Config, fmu: FmuFactory) -> None:
    """Entries carry model name, FMI version, interfaces and the quick runnable flag."""
    fmu("2.0", "BouncingBall")
    fmu("3.0", "Clocks")
    page = catalog.list_fmus(config)
    by_path = {entry.path: entry for entry in page.items}
    assert page.total == 2 and page.next_offset is None
    ball = by_path["2.0/BouncingBall.fmu"]
    assert (ball.model_name, ball.fmi_version, ball.interfaces) == (
        "BouncingBall",
        "2.0",
        ["CoSimulation", "ModelExchange"],
    )
    assert ball.runnable is True and ball.problem is None and ball.size_bytes > 0
    clocks = by_path["3.0/Clocks.fmu"]
    assert clocks.interfaces == ["ScheduledExecution"]
    assert clocks.runnable is False and clocks.problem == "ScheduledExecution only"


def test_list_fmus_marks_unreadable_and_fmi1_entries_with_a_problem(
    config: Config, write_fmu: WriteFmu, fmi1_xml: str, workspace: Path
) -> None:
    """Corrupt, XML-less and FMI 1.0 archives are listed with a problem and null facts."""
    (workspace / "corrupt.fmu").write_bytes(b"not a zip")
    write_fmu("empty.fmu", {"readme.txt": "x"})
    write_fmu("old.fmu", {"modelDescription.xml": fmi1_xml})
    by_path = {entry.path: entry for entry in catalog.list_fmus(config).items}
    assert "not a zip" in str(by_path["corrupt.fmu"].problem)
    assert "no modelDescription.xml" in str(by_path["empty.fmu"].problem)
    assert "FMI 1.0 is not supported" in str(by_path["old.fmu"].problem)
    for entry in by_path.values():
        assert entry.model_name is None and entry.runnable is None and entry.interfaces == []


def test_list_fmus_filters_by_substring_and_glob_case_insensitively(
    config: Config, synth_fmu: Callable[[str], Path]
) -> None:
    """A plain query is a substring of the path; a query with ``*`` is a glob."""
    for name in ("models/Pump.fmu", "models/valve.fmu", "other/pump_v2.fmu"):
        synth_fmu(name)
    assert [e.path for e in catalog.list_fmus(config, pattern="PUMP").items] == [
        "models/Pump.fmu",
        "other/pump_v2.fmu",
    ]
    assert [e.path for e in catalog.list_fmus(config, pattern="models/*").items] == [
        "models/Pump.fmu",
        "models/valve.fmu",
    ]
    assert [e.path for e in catalog.list_fmus(config, pattern="val*").items] == ["models/valve.fmu"]
    assert catalog.list_fmus(config, pattern="nothing").total == 0


def test_list_fmus_pages_with_next_offset_and_caps_the_limit(
    config: Config, synth_fmu: Callable[[str], Path]
) -> None:
    """``limit`` pages the result, ``next_offset`` continues it, and ``page_max`` caps ``limit``."""
    small = dataclasses.replace(config, limits=Limits(page_default=2, page_max=3))
    for i in range(5):
        synth_fmu(f"m{i}.fmu")
    first = catalog.list_fmus(small)
    assert (first.total, first.offset, first.limit, first.next_offset) == (5, 0, 2, 2)
    second = catalog.list_fmus(small, offset=first.next_offset or 0, limit=99)
    assert [e.path for e in second.items] == ["m2.fmu", "m3.fmu", "m4.fmu"]
    assert (second.limit, second.next_offset) == (3, None)


def test_list_fmus_default_page_is_50(config: Config, synth_fmu: Callable[[str], Path]) -> None:
    """With the default limits a page holds 50 entries and reports the total."""
    for i in range(52):
        synth_fmu(f"m{i:02d}.fmu")
    page = catalog.list_fmus(config)
    assert (page.total, len(page.items), page.limit, page.next_offset) == (52, 50, 50, 50)


def test_list_fmus_rejects_offset_past_the_end_and_empty_pages_are_fine(
    config: Config, synth_fmu: Callable[[str], Path]
) -> None:
    """An offset past the end says how many FMUs exist; an empty workspace lists nothing."""
    assert catalog.list_fmus(config).total == 0
    synth_fmu("a.fmu")
    with pytest.raises(UserError, match="there are 1 FMUs"):
        catalog.list_fmus(config, offset=1)
    with pytest.raises(UserError, match="limit must be at least 1"):
        catalog.list_fmus(config, limit=0)
