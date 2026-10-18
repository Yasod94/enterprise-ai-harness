import json

import pytest
import yaml

from enterprise_ai_harness.cli import main
from enterprise_ai_harness.config import load
from enterprise_ai_harness.install import _strip_jsonc, install_user


def test_strip_jsonc_keeps_strings():
    text = '{\n  // note\n  "a": "http://x//y", /* block */\n  "b": [1, 2,],\n}\n'
    assert json.loads(_strip_jsonc(text)) == {"a": "http://x//y", "b": [1, 2]}


def test_install_and_uninstall(harness, tmp_path):
    copilot, vscode = tmp_path / "copilot", tmp_path / "vscode"
    vscode.mkdir()
    (vscode / "mcp.json").write_text('{\n  // mine\n  "servers": {"github-ro": {"type": "http", "url": "https://mine"},'
                                     ' "other": {"command": "x"}},\n}\n')
    messages = install_user(load(harness), copilot, vscode)
    assert "kept your existing server github-ro" in " ".join(messages)
    assert "// mine" in (vscode / "mcp.json.before-harness").read_text()  # comments kept in a backup

    agent = (copilot / "agents/griller.agent.md").read_text()
    scripts = (copilot / "enterprise-ai-harness/scripts").as_posix()
    hook = yaml.safe_load(agent.split("---")[1])["hooks"]["PreToolUse"][0]["command"]
    assert hook == f'node "{scripts}/harness-hook.cjs" pre-tool grill'
    assert "${PLUGIN_ROOT}" not in agent and "(../skills/grill-me/SKILL.md)" in agent
    assert (copilot / "skills/grill-me/SKILL.md").exists()
    assert (copilot / "enterprise-ai-harness/scripts/config.json").exists()

    servers = json.loads((vscode / "mcp.json").read_text())["servers"]
    assert servers["github-ro"]["url"] == "https://mine" and "other" in servers
    assert servers["kb"]["url"] == "https://kb.example.internal/mcp" and "codegraph" in servers
    assert servers["kb"]["type"] == "http"  # VS Code's user mcp.json name for streamable HTTP

    # Reinstall is clean, uninstall removes only what was added.
    install_user(load(harness), copilot, vscode)
    install_user(load(harness), copilot, vscode, uninstall=True)
    servers = json.loads((vscode / "mcp.json").read_text())["servers"]
    assert set(servers) == {"github-ro", "other"}
    assert not (copilot / "agents").exists() and not (copilot / "skills").exists()
    assert not (copilot / "enterprise-ai-harness/installed.json").exists()


def test_install_requires_build(harness, tmp_path):
    import shutil
    shutil.rmtree(harness / "plugin")
    with pytest.raises(FileNotFoundError):
        install_user(load(harness), tmp_path / "c", tmp_path / "v")


def test_cli_install_user(harness, tmp_path):
    code = main(["--root", str(harness), "install-user", "--copilot-dir", str(tmp_path / "c"),
                 "--vscode-user-dir", str(tmp_path / "v")])
    assert code == 0 and (tmp_path / "c/agents/change-orchestrator.agent.md").exists()


def test_uninstall_without_mcp_json_creates_nothing(harness, tmp_path):
    copilot, vscode = tmp_path / "copilot", tmp_path / "vscode"
    install_user(load(harness), copilot, vscode)
    (vscode / "mcp.json").unlink()
    install_user(load(harness), copilot, vscode, uninstall=True)
    assert not (vscode / "mcp.json").exists()


@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="node is required")
def test_installed_hook_command_runs(harness, service, tmp_path):
    """The hook command written into a user-level agent runs as a shell command and guards edits."""
    import subprocess
    copilot = tmp_path / "copilot"
    install_user(load(harness), copilot, tmp_path / "vscode")
    agent = (copilot / "agents/planner.agent.md").read_text()
    command = yaml.safe_load(agent.split("---")[1])["hooks"]["PreToolUse"][0]["command"]
    event = {"cwd": str(service), "tool_name": "create_file",
             "tool_input": {"filePath": str(service / "src/OrderPublisher.java"), "content": "x"}}
    proc = subprocess.run(command, shell=True, input=json.dumps(event), capture_output=True, text=True, cwd=service)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
