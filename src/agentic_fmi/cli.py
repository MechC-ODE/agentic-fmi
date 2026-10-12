"""Command-line entry point for Agentic FMI (the ``afmi`` script, also ``python -m agentic_fmi``).

Subcommands:
- ``afmi mcp --workspace DIR``: the MCP server over stdio (``mcp/server.py``).
- ``afmi worker RUN_DIR``: one run's worker process (``worker.py``), spawned by the server.

Imports are lazy per subcommand: the worker never imports the MCP SDK, and the server never
imports ``engines/``. Nothing may be printed to stdout before the MCP server starts serving.

Collaborators: ``agentic_fmi.__init__`` (version), ``config.load_config``,
``mcp.server.serve_stdio``, ``worker.main``.
"""

import argparse
from pathlib import Path

from agentic_fmi import __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the ``afmi`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="afmi",
        description="Explore, run, control, compose and generate FMUs from Claude via MCP.",
    )
    parser.add_argument("--version", action="version", version=f"afmi {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    mcp = commands.add_parser("mcp", help="run the MCP server over stdio")
    mcp.add_argument(
        "--workspace",
        required=True,
        type=Path,
        help="workspace root (the Claude Code project directory); the run store defaults to "
        "<workspace>/.agentic-fmi unless AFMI_STORE_DIR is set",
    )

    worker = commands.add_parser("worker", help="run one simulation (spawned by the server)")
    worker.add_argument("run_dir", type=Path, help="the run directory, .../runs/<id>")
    return parser


def _run_mcp(workspace: Path) -> int:
    from agentic_fmi.config import load_config
    from agentic_fmi.mcp.server import serve_stdio

    serve_stdio(load_config(workspace))
    return 0


def _run_worker(run_dir: Path) -> int:
    from agentic_fmi.worker import main as worker_main

    return worker_main(run_dir)


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return the process exit code."""
    args = build_parser().parse_args(argv)
    if args.command == "mcp":
        return _run_mcp(args.workspace)
    if args.command == "worker":
        return _run_worker(args.run_dir)
    build_parser().print_help()
    return 0
