"""Domain errors that core functions raise for failures the caller can fix.

Collaborators: ``fmu/``, ``signals.py``, ``runs.py`` and ``store/`` raise :class:`UserError`;
``mcp/server.py`` turns it into ``ToolError`` with the same text. Core modules never import the
MCP SDK, so the worker and the CLI stay free of it. Both engines raise :class:`SetRefused` and
``live.py`` catches it; it lives here, not in ``engines/base.py``, so the server (which imports
``live.py`` for its set checks) never imports ``engines/``.
"""

from collections.abc import Mapping


class UserError(Exception):
    """A failure the caller can fix. The message goes to the model verbatim.

    Write it as: what failed, why, and what to do next, in at most a few sentences, for example
    ``"Unknown variable 'hh' in start_values. Close matches: h. Call fmu_variables to list
    names."``. Never include a traceback, a file dump or more than ``Limits.message_max_chars``.
    """


class ProtocolError(Exception):
    """A run-store file is missing, unreadable, or written by an unsupported protocol version.

    Readers raise it instead of guessing. ``mcp/server.py`` (``_call``) reports it to the model
    as a ``ToolError`` that names the file and points to ``run_list``.
    """


class SetRefused(RuntimeError):
    """A live ``set`` that failed partway (an FMU or OMSimulator refused a value).

    ``str(error)`` names the refused variable and the reason. ``applied`` holds the values that
    took effect before the refusal, so the caller can tell the user what changed (a multi-value
    set is not atomic). OMSimulator sets one variable at a time; FMPy sets one FMI type group at
    a time, so ``applied`` lists the groups before the failing one, not the failing group's own
    variables (an FMI call that fails may have applied some of them).
    """

    def __init__(self, message: str, applied: Mapping[str, bool | int | float]) -> None:
        super().__init__(message)
        self.applied: dict[str, bool | int | float] = dict(applied)
