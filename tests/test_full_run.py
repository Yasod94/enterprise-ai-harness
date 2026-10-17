"""A whole run, start to PR, driven through the built plugin's hook with the tool names
and payload shapes VS Code sends (snake_case stdin; create_file, replace_string_in_file,
run_in_terminal, runSubagent with agentName/prompt/description)."""

import json
import subprocess
import time

import pytest
import yaml

from enterprise_ai_harness.build import AGENTS_DIR, build
from enterprise_ai_harness.config import load
from conftest import NODE, git, make_index, run_hook

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required for hook tests")

SESSION = "chat-1"
PLAN = """# Plan: ABC-123

## Tasks

### T1. Add the retry setting

- Component: orders
- Tier: fast
- Files: src/OrderPublisher.java
- Check: ./gradlew test

### T2. Retry with backoff

- Component: orders
- Tier: standard
- Files: src/OrderPublisher.java, src/test/OrderPublisherTest.java
- Check: ./gradlew test
"""


class Session:
    def __init__(self, harness, repo):
        self.harness, self.repo = harness, repo

    def say(self, scope, text):
        code, out, err = run_hook(self.harness, self.repo, "prompt", scope, {"prompt": text, "session_id": SESSION})
        context = out["hookSpecificOutput"]["additionalContext"] if out else ""
        return code, context, err

    def tool(self, scope, name, tool_input, response="ok"):
        _, out, _ = run_hook(self.harness, self.repo, "pre-tool", scope,
                             {"tool_name": name, "tool_input": tool_input, "session_id": SESSION, "tool_use_id": "t"})
        allowed = out is None or out["hookSpecificOutput"]["permissionDecision"] == "allow"
        if allowed:
            run_hook(self.harness, self.repo, "post-tool", scope,
                     {"tool_name": name, "tool_input": tool_input, "tool_response": response, "session_id": SESSION})
        return out["hookSpecificOutput"]["permissionDecision"] if out else "allow"

    def write(self, scope, rel, text):
        verdict = self.tool(scope, "create_file", {"filePath": str(self.repo / rel), "content": text})
        if verdict == "allow":
            path = self.repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return verdict


def test_a_change_from_jira_key_to_pr(harness, service):
    text = (harness / "harness.yaml").read_text().replace(
        "sync: npx -y @colbymchenry/codegraph@1.6.1 sync --quiet", "sync: touch .codegraph/synced")
    (harness / "harness.yaml").write_text(text)
    build(load(harness))
    make_index(service)
    s = Session(harness, service)
    run = ".ai-harness/ABC-123"

    # Orchestrator: no run yet, so only run files can be written.
    code, ctx, _ = s.say("orchestrator", "ABC-123")
    assert code == 0 and "No active run" in ctx
    assert s.write("orchestrator", "src/OrderPublisher.java", "x") == "deny"
    assert s.write("orchestrator", f"{run}/state.json", json.dumps(
        {"run": "ABC-123", "source": "jira", "stage": "intake", "approvals": {}})) == "allow"
    assert s.write("orchestrator", ".ai-harness/current", "ABC-123\n") == "allow"
    assert s.tool("orchestrator", "run_in_terminal", {"command": "git checkout -b change/ABC-123"}) == "allow"

    # Intake (opened by the orchestrator's handoff button).
    code, ctx, _ = s.say("intake", "Continue the active run at the intake stage.")
    assert "Harness status: run ABC-123, active stage intake" in ctx
    assert s.write("intake", f"{run}/requirement.md", "Retry MQ publish with backoff.\n") == "allow"
    assert s.write("intake", "src/OrderPublisher.java", "x") == "deny"

    # Grill: the Approve button sends the marker to the next agent.
    code, ctx, err = s.say("grill", "[harness:approve intake] Requirement approved. Start grilling.")
    assert code == 0, err
    assert s.tool("grill", "runSubagent", {"agentName": "Component Scout", "prompt": "Who calls publish?",
                                           "description": "find callers"}) == "allow"
    assert s.tool("scout", "mcp_codegraph_codegraph_callers", {"symbol": "publish"}) == "allow"
    assert s.write("grill", f"{run}/clarifications.md", "D1: 3 retries, exponential backoff.\n") == "allow"

    # Impact.
    code, _, err = s.say("impact", "[harness:approve grill] Clarifications approved. Run impact analysis.")
    assert code == 0, err
    assert s.write("impact", f"{run}/impact.md", "orders: publish path\n") == "allow"
    assert s.write("impact", f"{run}/impact.json", json.dumps({"run": "ABC-123", "components": [
        {"name": "orders", "change_type": "code", "confidence": "high",
         "files": ["src/OrderPublisher.java"], "evidence": []}]})) == "allow"

    # Plan, then the required cross-check on the deep tier.
    code, _, err = s.say("plan", "[harness:approve impact] Impact approved. Write the plan.")
    assert code == 0, err
    assert s.write("plan", f"{run}/plan.md", PLAN) == "allow"
    code, _, err = s.say("implement", "[harness:approve plan] Plan approved. Start with the first task.")
    assert code == 2 and "cross-check" in err
    code, ctx, err = s.say("cross-check", "[harness:cross-check plan] Check plan.md before the person approves it.")
    assert code == 0, err
    assert s.write("cross-check", f"{run}/plan.md", "rewritten") == "deny"
    assert s.write("cross-check", f"{run}/plan-check.md", "Verdict: pass\n\nChecked coverage and order.\n") == "allow"
    code, ctx, err = s.say("implement", "[harness:approve plan] Approved after the cross-check. Continue with the implement stage.")
    assert code == 0 and "Cross-check verdict: pass" in ctx, err

    # Implement: T1 on the fast Task Runner, review, T2 on the standard one.
    assert s.tool("implement", "runSubagent", {"agentName": "Task Runner (standard)", "prompt": "T1: add the setting",
                                               "description": "T1"}) == "deny"  # T1 is planned as fast
    assert s.tool("implement", "runSubagent", {"agentName": "Task Runner (fast)", "prompt": "T1: add the setting",
                                               "description": "T1"}) == "allow"
    s.say("implement-task", "T1: add the setting")
    assert s.tool("implement-task", "replace_string_in_file", {"filePath": str(service / "src/OrderPublisher.java"),
                                                               "oldString": "{}", "newString": "{ int retries = 3; }"}) == "allow"
    (service / "src/OrderPublisher.java").write_text("class OrderPublisher { int retries = 3; }\n")
    assert s.tool("implement-task", "run_in_terminal", {"command": "./gradlew test"}) == "allow"
    assert s.tool("implement", "runSubagent", {"agentName": "Task Runner (standard)", "prompt": "T2: backoff",
                                               "description": "T2"}) == "deny"  # T1 not reviewed yet
    code, ctx, _ = s.say("implement", "[harness:approve task] Continue with the next task.")
    assert "Task review 1 recorded" in ctx
    assert s.tool("implement", "runSubagent", {"agentName": "Task Runner (standard)", "prompt": "T2: backoff",
                                               "description": "T2"}) == "allow"
    assert s.tool("implement-task", "multi_replace_string_in_file", {"explanation": "backoff", "replacements": [
        {"filePath": str(service / "src/OrderPublisher.java"), "oldString": "3;", "newString": "3; long backoffMs = 200;"},
        {"filePath": str(service / "src/test/OrderPublisherTest.java"), "oldString": "", "newString": "class T {}"}]}) == "allow"
    deadline = time.time() + 10
    while not (service / ".codegraph/synced").exists() and time.time() < deadline:
        time.sleep(0.1)
    assert (service / ".codegraph/synced").exists()  # the index followed the edits
    assert s.write("implement", f"{run}/progress.md", "T1 done\nT2 done\n") == "allow"

    # Verify: approving implement needs every task run; then the seed refresh on approval.
    code, _, err = s.say("verify", "[harness:approve implement] All tasks approved. Verify the change.")
    assert code == 0, err
    assert s.write("verify", "src/OrderPublisher.java", "x") == "deny"
    assert s.tool("verify", "run_in_terminal", {"command": "./gradlew test"}) == "allow"
    assert s.write("verify", f"{run}/verification.md", "All tests pass.\n") == "allow"
    code, ctx, err = s.say("verify", "[harness:approve verify] Open the PR.")
    assert code == 0, err
    assert "Code graph seed saved" in ctx
    assert s.tool("verify", "run_in_terminal", {"command": "git add .codegraph-seed && git commit -m 'Refresh code graph seed'"}) == "allow"

    state = json.loads((service / run / "state.json").read_text())
    assert set(state["approvals"]) >= {"intake", "grill", "impact", "plan", "implement", "verify", "tasks"}
    assert state["stage"] == "done" and state["tasks_dispatched"] == ["T1", "T2"]
    approval = json.loads((service / run / "approval.json").read_text())
    assert approval["cross_check"]["verdict"] == "pass" and approval["cross_check"]["changed_after_check"] is False
    status = git(service, "status", "--porcelain", "--untracked-files=all")
    assert ".ai-harness" not in status and ".codegraph/" not in status
    assert "src/OrderPublisher.java" in status and ".codegraph-seed/codegraph.db.gz" in status


def test_built_agents_use_only_supported_frontmatter(harness):
    supported = {"name", "description", "argument-hint", "tools", "agents", "model", "user-invocable",
                 "disable-model-invocation", "target", "handoffs", "hooks"}
    for f in sorted((harness / AGENTS_DIR).glob("*.agent.md")):
        fm = yaml.safe_load(f.read_text().split("---")[1])
        assert set(fm) <= supported, (f.name, set(fm) - supported)
        assert "todos" not in fm["tools"], f.name
        for h in fm.get("handoffs", []):
            assert {"label", "agent", "prompt"} <= set(h) and set(h) <= {"label", "agent", "prompt", "send", "model"}, f.name
        for event, hooks in fm["hooks"].items():
            assert event in {"UserPromptSubmit", "PreToolUse", "PostToolUse", "PreCompact"}
            for hk in hooks:
                assert hk["type"] == "command" and hk["command"].startswith('node "${PLUGIN_ROOT}/scripts/')
                assert 0 < hk["timeout"] <= 120  # seconds


def test_plugin_mcp_json_matches_agent_plugins_schema(harness):
    mcp = json.loads((harness / "plugin/mcp.json").read_text())
    assert mcp["$schema"] == "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
    allowed = {"stdio": {"type", "command", "args", "env", "cwd"},
               "streamable-http": {"type", "url", "headers"}, "sse": {"type", "url", "headers"}}
    for name, server in mcp["mcpServers"].items():
        assert server["type"] in allowed, name
        assert set(server) <= allowed[server["type"]], (name, set(server) - allowed[server["type"]])
        assert all(isinstance(v, str) for v in server.get("env", {}).values()), name
        assert all(isinstance(v, str) for v in server.get("headers", {}).values()), name
