"""The core Claude Code plugin files: manifests, the MCP launch, the launcher script, the skills.

Collaborators: ``.claude-plugin/*.json`` (with ``mcpServers``), ``scripts/afmi-mcp`` and
``skills/fmi-expert/`` and ``skills/system-builder/`` at the repository root (the code under
test), ``mcp.server.TOOLS`` (the tool names the skills must match), ``agentic_fmi.system``
(the example system file must be a valid one) and ``agentic_fmi.ssp`` (the SSV snippet must
parse). The launcher runs with a fake ``uv``; no server starts and nothing is installed.
``claude plugin validate .`` is the end-to-end check and runs in the container, not here.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agentic_fmi.mcp.server import TOOLS
from agentic_fmi.ssp import parse_ssv
from agentic_fmi.system import SystemFile

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "afmi-mcp"
SKILL_DIR = ROOT / "skills" / "fmi-expert"
SYSTEM_SKILL_DIR = ROOT / "skills" / "system-builder"
SYSTEM_SKILL_MAX_LINES = 120
TOOL_NAMES = {fn.__name__ for fn, _hints in TOOLS}
TOOL_PATTERN = re.compile(r"`((?:workspace|fmu|run|system|response)_[a-z_]+)`")
NOT_TOOLS = {"run_id", "run_ids", "fmu_changed"}
"""A parameter or error-code name that matches :data:`TOOL_PATTERN` without being a tool."""


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _fake_uv(directory: Path) -> Path:
    """A ``uv`` that reports its environment, its cache and its arguments on stderr and exits."""
    uv = directory / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'echo "env=${UV_PROJECT_ENVIRONMENT:-unset} cache=${UV_CACHE_DIR:-unset} args=$*" >&2\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    return uv


def _launch(path: str, workspace: str, **env: str) -> subprocess.CompletedProcess[str]:
    """Run the launcher with exactly the given environment."""
    return subprocess.run(
        ["/bin/sh", str(LAUNCHER), workspace],
        env={"PATH": path, **env},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_plugin_manifest_names_the_plugin() -> None:
    manifest = _json(ROOT / ".claude-plugin" / "plugin.json")
    assert manifest["name"] == "agentic-fmi"
    assert not manifest["name"].startswith("claude-")
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
    assert manifest["license"] == "PolyForm-Noncommercial-1.0.0"


def test_marketplace_lists_the_plugin_from_the_repository_root() -> None:
    marketplace = _json(ROOT / ".claude-plugin" / "marketplace.json")
    entries = {entry["name"]: entry for entry in marketplace["plugins"]}
    assert set(entries) == {"agentic-fmi", "agentic-fmi-pane"}
    assert entries["agentic-fmi"]["source"] == "./"
    assert (
        entries["agentic-fmi"]["version"]
        == _json(ROOT / ".claude-plugin" / "plugin.json")["version"]
    )


def test_marketplace_lists_the_pane_which_depends_on_the_core_plugin() -> None:
    """ADR 0005 and ADR 0010: the pane is a separate plugin in ``pane/`` that needs the core."""
    marketplace = _json(ROOT / ".claude-plugin" / "marketplace.json")
    entry = next(e for e in marketplace["plugins"] if e["name"] == "agentic-fmi-pane")
    manifest = _json(ROOT / "pane" / ".claude-plugin" / "plugin.json")
    assert entry["source"] == "./pane"
    assert manifest["name"] == "agentic-fmi-pane"
    assert entry["version"] == manifest["version"]
    assert manifest["dependencies"] == ["agentic-fmi"]
    assert _json(ROOT / "pane" / "hooks" / "hooks.json") == {"modules": ["./register.tsx"]}


def test_manifest_starts_the_launcher_with_the_project_directory() -> None:
    server = _json(ROOT / ".claude-plugin" / "plugin.json")["mcpServers"]["fmi"]
    assert server["command"] == "sh"
    assert server["args"] == ["${CLAUDE_PLUGIN_ROOT}/scripts/afmi-mcp", "${CLAUDE_PROJECT_DIR}"]
    assert LAUNCHER.is_file()


def test_launcher_is_posix_sh_with_lf_endings() -> None:
    text = LAUNCHER.read_bytes()
    assert text.startswith(b"#!/bin/sh\n")
    assert b"\r" not in text
    assert subprocess.run(["/bin/sh", "-n", str(LAUNCHER)], check=False).returncode == 0


def test_launcher_reuses_the_environment_of_the_dev_container(tmp_path: Path) -> None:
    _fake_uv(tmp_path)
    done = _launch(
        str(tmp_path), "/lab", CLAUDE_PLUGIN_ROOT="/plugin", UV_PROJECT_ENVIRONMENT="/dev/venv"
    )
    assert done.returncode == 0
    assert done.stdout == ""
    assert done.stderr.strip() == (
        "env=/dev/venv cache=unset args=run --quiet --locked --no-dev --project /plugin "
        "afmi mcp --workspace /lab"
    )


def test_launcher_keeps_its_venv_and_uv_cache_in_the_plugin_data_directory(
    tmp_path: Path,
) -> None:
    """Cache and venv on one filesystem, so uv hardlinks instead of copying the environment."""
    _fake_uv(tmp_path)
    done = _launch(
        str(tmp_path), "/my dir", CLAUDE_PLUGIN_ROOT="/plugin", CLAUDE_PLUGIN_DATA="/data"
    )
    assert done.stdout == ""
    assert done.stderr.startswith("env=/data/venv cache=/data/uv-cache args=run ")
    assert done.stderr.strip().endswith("--workspace /my dir")


def test_launcher_keeps_a_uv_cache_the_user_chose(tmp_path: Path) -> None:
    _fake_uv(tmp_path)
    done = _launch(
        str(tmp_path),
        "/lab",
        CLAUDE_PLUGIN_ROOT="/plugin",
        CLAUDE_PLUGIN_DATA="/data",
        UV_CACHE_DIR="/fast/uv",
    )
    assert done.stderr.startswith("env=/data/venv cache=/fast/uv args=run ")


def test_launcher_without_uv_says_so_on_stderr(tmp_path: Path) -> None:
    done = _launch(str(tmp_path), "/lab", CLAUDE_PLUGIN_ROOT="/plugin")
    assert done.returncode == 127
    assert done.stdout == ""
    assert "'uv' is not on PATH" in done.stderr


def test_launcher_requires_exactly_the_workspace_argument(tmp_path: Path) -> None:
    done = subprocess.run(
        ["/bin/sh", str(LAUNCHER)], capture_output=True, text=True, check=False, timeout=30
    )
    assert done.returncode == 2
    assert done.stdout == ""
    assert "usage" in done.stderr


def test_skill_frontmatter_names_the_skill() -> None:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = text.split("---")[1]
    assert re.search(r"^name: fmi-expert$", frontmatter, re.MULTILINE)
    description = re.search(r"^description: (.+)$", frontmatter, re.MULTILINE)
    assert description is not None
    assert "FMU" in description.group(1)
    assert len(description.group(1)) <= 1024


@pytest.mark.parametrize("name", ["SKILL.md", "reference.md"])
def test_skill_files_name_only_real_tools(name: str) -> None:
    mentioned = set(TOOL_PATTERN.findall((SKILL_DIR / name).read_text(encoding="utf-8")))
    mentioned -= NOT_TOOLS
    assert mentioned <= TOOL_NAMES, f"unknown tools: {sorted(mentioned - TOOL_NAMES)}"


def test_skill_mentions_every_tool() -> None:
    text = "".join(
        (SKILL_DIR / name).read_text(encoding="utf-8") for name in ("SKILL.md", "reference.md")
    )
    missing = TOOL_NAMES - set(TOOL_PATTERN.findall(text))
    assert not missing, f"tools the skill never mentions: {sorted(missing)}"


def test_no_project_scoped_mcp_json_at_the_repo_root() -> None:
    # A root .mcp.json would also be read as project MCP config by anyone who opens the repo in
    # Claude Code, where ${CLAUDE_PLUGIN_ROOT} is unset and the server fails to start.
    assert not (ROOT / ".mcp.json").exists()


def _skill_text() -> str:
    return (SYSTEM_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def _code_block(text: str, language: str) -> str:
    found = re.search(rf"```{language}\n(.*?)```", text, re.DOTALL)
    assert found is not None, f"no {language} block"
    return found.group(1)


def test_system_builder_frontmatter_names_the_skill_and_its_triggers() -> None:
    frontmatter = _skill_text().split("---")[1]
    assert re.search(r"^name: system-builder$", frontmatter, re.MULTILINE)
    description = re.search(r"^description: (.+)$", frontmatter, re.MULTILINE)
    assert description is not None and len(description.group(1)) <= 1024
    for trigger in ("FMU", "connect", "closed loop", "SSP", "SSV"):
        assert trigger.lower() in description.group(1).lower(), trigger


def test_system_builder_stays_short() -> None:
    assert len(_skill_text().splitlines()) <= SYSTEM_SKILL_MAX_LINES


def test_system_builder_names_only_real_tools_and_covers_the_system_tools() -> None:
    mentioned = set(TOOL_PATTERN.findall(_skill_text())) - NOT_TOOLS
    assert mentioned <= TOOL_NAMES, f"unknown tools: {sorted(mentioned - TOOL_NAMES)}"
    needed = {"system_check", "system_import", "system_export", "run_start", "run_compare"}
    assert needed <= mentioned, f"tools the skill never mentions: {sorted(needed - mentioned)}"


def test_the_example_system_file_is_valid_and_is_the_one_the_skill_shows() -> None:
    example = (SYSTEM_SKILL_DIR / "examples" / "loop.json").read_text(encoding="utf-8")
    loop = SystemFile.model_validate_json(example)
    assert set(loop.components) == {"PI", "plant"} and len(loop.connections) == 2
    assert loop.experiment.step_size == 0.01 and loop.experiment.stop_time == 10
    assert json.loads(_code_block(_skill_text(), "json")) == json.loads(example)


def test_the_ssv_snippet_in_the_skill_parses() -> None:
    values, _units = parse_ssv(_code_block(_skill_text(), "xml").encode(), "SKILL.md")
    assert values == {"PI.Kp": 8.0}


def test_fmi_expert_points_to_the_system_builder_skill() -> None:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "(../system-builder/SKILL.md)" in text


SKILL_FILES = sorted((ROOT / "skills").glob("*/*.md"))
EXAMPLE_SOURCES = ("bouncing_ball.py", "pi_controller.py", "MSD.mo", "MSD.py")


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda path: f"{path.parent.name}/{path.name}")
def test_every_relative_link_in_a_skill_resolves(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text):
        if not target.startswith(("http://", "https://")):
            assert (path.parent / target).exists(), f"{path.name} links to missing {target}"


def test_fmi_expert_forbids_downloads_and_points_to_the_ready_made_examples() -> None:
    """A user who asks for an example gets a bundled one, built offline, never a download."""
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "Never download or install anything on your own" in text
    for source in ("bouncing_ball.py", "pi_controller.py", "MSD.py"):
        assert f"(../fmu-authoring/examples/{source})" in text
    assert "example or demo" in text.split("---")[1]


def test_the_authoring_skill_lists_every_bundled_example() -> None:
    examples = ROOT / "skills" / "fmu-authoring" / "examples"
    text = (examples.parent / "SKILL.md").read_text(encoding="utf-8")
    assert sorted(p.name for p in examples.iterdir() if p.suffix in (".py", ".mo")) == sorted(
        EXAMPLE_SOURCES
    )
    for source in EXAMPLE_SOURCES:
        assert f"(examples/{source})" in text
