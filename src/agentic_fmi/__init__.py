"""Agentic FMI: explore, run, control, compose and generate FMUs from Claude via MCP.

Collaborators: ``agentic_fmi.cli`` is the command-line entry point (``afmi``).
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("agentic-fmi")
except PackageNotFoundError:  # running from a source tree that is not installed
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
