"""Response styles: the one-line status view of a run, build or live-control result.

In ninja mode Claude answers a run, build or live-control result with one short line instead of
a summary. :func:`ninja_line` makes that line from a :class:`RunView`: a deterministic sentence
that starts with the ninja emoji and quotes numbers with at most four significant digits.
:func:`ninja_view` is the view the MCP adapters return in ninja mode: the same fields, without
the summary, the build facts and the log tail (the error code and message stay, because Claude
needs them to act), and with the line plus the rule "reply with this line only" as the hint.
:func:`confirmation` is the line ``response_style`` returns when the style changes.

Pure functions over a ``RunView``: no I/O and no store access. The chosen style lives in
``preferences.json`` (``store/runstore.py``, ``store.protocol.Preferences``) and is applied by
the adapters in ``mcp/server.py``.

Collaborators: ``runs.RunView`` (the view it shortens; the field set stays unchanged, so no
output schema grows), ``store/protocol.py`` (``RunState``, ``ResponseStyle``).
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Final

from agentic_fmi.runs import RunView
from agentic_fmi.store.protocol import ResponseStyle, RunState

NINJA: Final = "🥷"
SIGNIFICANT_DIGITS: Final = 4
"""Digits a number in a ninja line keeps (a line is for a glance, not for numbers to quote)."""
NINJA_RULE: Final = "Ninja mode: reply with this line only; give details only if the user asks."
"""What follows the line in a ninja view's hint."""
CONFIRMATIONS: Final[dict[ResponseStyle, str]] = {
    "ninja": f"{NINJA} Ninja mode on: status lines only.",
    "talkative": "Talkative mode on: full summaries.",
}
_ENDED_EARLY: Final = re.compile(r"^Ended early at t=\S+ of t_stop=([0-9.eE+-]+):")
"""The start of the hint of a run whose FMU ended it before ``t_stop`` (``runs._hint`` puts
``status.reason`` first, and ``worker`` words it "ended early at t=... of t_stop=...")."""


def confirmation(style: ResponseStyle) -> str:
    """The line that confirms a switch to ``style``."""
    return CONFIRMATIONS[style]


def ninja_line(view: RunView) -> str:
    """One short sentence for ``view``, starting with the ninja emoji.

    A run: ``🥷 r12 in: BouncingBall running, t=1.2 of 3 s.`` (queued, running and paused runs;
    a live run adds ``(live)``), ``🥷 r12 done: BouncingBall, t=3 s.`` (an FMU that ended the
    run early adds ``Ended early (t_stop=3 s).``), ``🥷 r12 stopped at t=1.2 s.`` and
    ``🥷 r12 failed: simulation_error.`` A system run names the system. A build job:
    ``🥷 r9 building pi2.``, ``🥷 pi2 built.``, ``🥷 pi2 build failed: build_failed.``
    """
    return _build_line(view) if view.kind == "build" else _run_line(view)


def ninja_view(view: RunView) -> RunView:
    """``view`` as ninja mode returns it: no summary, no build facts, no log tail, and the
    :func:`ninja_line` plus the rule as the hint. ``values`` stay (they are only there when the
    caller asked for them)."""
    error = view.error
    if error is not None and error.log_tail is not None:
        error = error.model_copy(update={"log_tail": None})
    hint = f"{ninja_line(view)} {NINJA_RULE}"
    return view.model_copy(update={"summary": None, "build": None, "error": error, "hint": hint})


def _num(value: float) -> str:
    """``value`` with at most :data:`SIGNIFICANT_DIGITS` significant digits, as ``3`` or ``1.2``."""
    rounded = float(f"{value:.{SIGNIFICANT_DIGITS}g}")
    return f"{rounded + 0.0:g}"  # + 0.0: no "-0"


def _name(view: RunView) -> str:
    """The model's name: the FMU's file stem; a system run's file stem is the system's name."""
    return PurePosixPath(view.fmu).stem


def _at(view: RunView, unit: str = "") -> str:
    """`` at t=1.2`` (with ``unit``), or nothing if the run has no time yet."""
    return "" if view.t is None else f" at t={_num(view.t)}{unit}"


def _code(view: RunView) -> str:
    """``: simulation_error``, or nothing if the view carries no error."""
    return "" if view.error is None else f": {view.error.code.value}"


def _run_line(view: RunView) -> str:
    head = f"{NINJA} {view.run_id}"
    name = _name(view)
    live = " (live)" if view.speed is not None else ""
    match view.state:
        case RunState.QUEUED:
            return f"{head} in: {name} queued{live}."
        case RunState.RUNNING:
            return f"{head} in: {name} running{live}{_progress(view)}."
        case RunState.PAUSED:
            return f"{head} in: {name} paused{_at(view)} (live)."
        case RunState.FINISHED:
            return f"{head} done: {name}{_reached(view)}.{_ended_early(view)}"
        case RunState.STOPPED:
            return f"{head} stopped{_at(view, ' s')}."
        case _:
            return f"{head} failed{_code(view)}."


def _progress(view: RunView) -> str:
    """``, t=1.2 of 3 s`` while the time and the horizon are known."""
    if view.t is None or view.t_stop is None:
        return ""
    return f", t={_num(view.t)} of {_num(view.t_stop)} s"


def _reached(view: RunView) -> str:
    """``, t=3 s``: where a finished run ended."""
    return "" if view.t is None else f", t={_num(view.t)} s"


def _ended_early(view: RunView) -> str:
    """`` Ended early (t_stop=3 s).`` when the hint says the FMU ended the run before t_stop."""
    found = _ENDED_EARLY.match(view.hint or "")
    return "" if found is None else f" Ended early (t_stop={_num(float(found.group(1)))} s)."


def _build_line(view: RunView) -> str:
    name = _name(view)
    match view.state:
        case RunState.QUEUED | RunState.RUNNING | RunState.PAUSED:
            return f"{NINJA} {view.run_id} building {name}."
        case RunState.FINISHED:
            return f"{NINJA} {name} built."
        case RunState.STOPPED:
            return f"{NINJA} {view.run_id} build of {name} stopped."
        case _:
            return f"{NINJA} {name} build failed{_code(view)}."
