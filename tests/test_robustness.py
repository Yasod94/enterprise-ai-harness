"""Regression tests for gaps found by the independent review: edit-tool shapes, terminal
commands, missing or broken runs, personal settings, budgets, sessions, prompt parsing,
task review cadence, pause, and parallel hook calls."""

import concurrent.futures
import json
import sys

import pytest

from conftest import NODE, run_hook, start_run

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required for hook tests")

PLAN = "# Plan\n\n## Tasks\n\n### T1. Config\n\n- Tier: standard\n- Files: src/OrderPublisher.java\n\n" \
       "### T2. Retry\n\n- Tier: standard\n- Files: src/OrderPublisher.java\n"


def decision(out):
    return out["hookSpecificOutput"]["permissionDecision"] if out else None


def reason(out):
    return out["hookSpecificOutput"].get("permissionDecisionReason", "") if out else ""


def pre(h, repo, scope, tool, tool_input, **extra):
    return run_hook(h, repo, "pre-tool", scope, {"tool_name": tool, "tool_input": tool_input, **extra})[1]


def prompt(h, repo, scope, text, **extra):
    return run_hook(h, repo, "prompt", scope, {"prompt": text, **extra})


def state(run):
    return json.loads((run / "state.json").read_text())


def usage(run):
    return json.loads((run / ".usage.json").read_text())


def approve_until(h, repo, run, last, plan=PLAN):
    files = {"intake": {"requirement.md": "r\n"}, "grill": {"clarifications.md": "c\n"},
             "impact": {"impact.md": "i\n", "impact.json": json.dumps({"components": [
                 {"name": "orders", "files": ["src/OrderPublisher.java"]}]})},
             "plan": {"plan.md": plan}, "implement": {"progress.md": "p\n"}}
    for stage in ("intake", "grill", "impact", "plan", "implement"):
        for name, text in files[stage].items():
            (run / name).write_text(text)
        if stage == last:
            return
        extra = " skip cross-check" if stage == "plan" else ""
        code, _, err = prompt(h, repo, stage, f"[harness:approve {stage}]{extra}")
        assert code == 0, err


# ---------- edit tools VS Code really uses ----------

def test_nested_and_patch_edits_are_guarded(harness, service):
    run = start_run(service)
    approve_until(harness, service, run, "plan")
    abs_file = str(service / "src/OrderPublisher.java")
    multi = {"explanation": "x", "replacements": [{"filePath": abs_file, "oldString": "a", "newString": "b"}]}
    patch = {"input": f"*** Begin Patch\n*** Update File: {abs_file}\n@@\n-a\n+b\n*** End Patch", "explanation": "x"}
    assert decision(pre(harness, service, "implement-task", "multi_replace_string_in_file", multi)) == "deny"
    assert decision(pre(harness, service, "implement-task", "apply_patch", patch)) == "deny"
    assert decision(pre(harness, service, "implement-task", "apply_patch", {"input": "no headers"})) == "deny"
    assert decision(pre(harness, service, "grill", "multi_replace_string_in_file", multi)) == "deny"
    prompt(harness, service, "implement", "[harness:approve plan] skip cross-check")
    assert pre(harness, service, "implement-task", "apply_patch", patch) is None
    other = {"input": "*** Begin Patch\n*** Add File: src/Other.java\n+x\n*** End Patch"}
    assert decision(pre(harness, service, "implement-task", "apply_patch", other)) == "ask"


def test_file_uri_with_encoded_characters(harness, service):
    start_run(service)
    uri = "file://" + str(service / ".ai-harness/ABC-123/my%20notes.md")
    assert pre(harness, service, "grill", "create_file", {"uri": uri}) is None
    uri = "file://" + str(service / "src/My%20Main.java")
    assert decision(pre(harness, service, "grill", "create_file", {"uri": uri})) == "deny"


@pytest.mark.skipif(sys.platform == "linux", reason="case-insensitive file systems only")
def test_case_variants_of_owned_files(harness, service):
    start_run(service)
    assert decision(pre(harness, service, "intake", "create_file", {"filePath": ".ai-harness/ABC-123/State.json"})) == "deny"
    assert decision(pre(harness, service, "plan", "create_file", {"filePath": ".ai-harness/ABC-123/Plan-Check.md"})) == "deny"


# ---------- terminal and sandbox commands ----------

def test_commands_cannot_write_owned_files_in_any_stage(harness, service):
    start_run(service)
    for scope in ("verify", "implement", "orchestrator"):
        out = pre(harness, service, scope, "run_in_terminal", {"command": "echo '{}' > .ai-harness/ABC-123/state.json"})
        assert decision(out) == "deny", scope
    out = pre(harness, service, "plan", "run_in_terminal", {"command": "sed -i s/a/b/ .ai-harness/ABC-123/plan-check.md"})
    assert decision(out) == "deny"
    assert pre(harness, service, "verify", "run_in_terminal", {"command": "cat .ai-harness/ABC-123/state.json"}) is None


def test_code_changing_commands_ask_before_plan_stages(harness, service):
    start_run(service)
    assert decision(pre(harness, service, "orchestrator", "run_in_terminal", {"command": "echo hi > src/A.java"})) == "ask"
    assert decision(pre(harness, service, "verify", "run_in_terminal", {"command": "git apply fix.patch"})) == "ask"
    for tool in ("mcp_ctx_ctx_execute", "ctx/ctx_execute"):
        out = pre(harness, service, "impact", tool, {"language": "shell", "code": "echo x > src/A.java"})
        assert decision(out) == "ask", tool
    assert pre(harness, service, "verify", "run_in_terminal", {"command": "./gradlew test"}) is None
    assert pre(harness, service, "orchestrator", "run_in_terminal", {"command": "git checkout -b ABC-123"}) is None
    assert pre(harness, service, "intake", "run_in_terminal",
               {"command": "echo note >> .ai-harness/ABC-123/requirement.md"}) is None
    out = pre(harness, service, "implement", "mcp_ctx_ctx_execute", {"language": "shell", "code": "make"})
    assert decision(out) == "deny" and "not approved" in reason(out)


READ_ONLY_COMMANDS = [
    'grep -n "a > b" src/OrderPublisher.java',
    "ls src 2>/dev/null",
    "npm test 2>&1 | tail -20",
    "git log --format='%h > %s' -5",
    'rg "publish" src > /dev/null && echo found',
    "gh pr view 12 --json body --jq '.body | length > 0'",
    "Get-ChildItem src > $null",
    "git diff --stat",
]

WRITING_COMMANDS = [
    'bash -c "echo x > src/OrderPublisher.java"',
    "python3 -c \"open('src/OrderPublisher.java','w').write('x')\"",
    "find src -name '*.orig' -delete",
    "rm -rf build",
    "echo x > .ai-harness/ABC-123/notes.md && echo y > src/A.java",
]


@pytest.mark.parametrize("command", READ_ONLY_COMMANDS)
def test_read_only_commands_run_without_asking(harness, service, command):
    start_run(service)
    for scope in ("grill", "impact", "verify"):
        assert pre(harness, service, scope, "run_in_terminal", {"command": command}) is None, (scope, command)


@pytest.mark.parametrize("command", WRITING_COMMANDS)
def test_writing_commands_still_ask(harness, service, command):
    start_run(service)
    assert decision(pre(harness, service, "impact", "run_in_terminal", {"command": command})) == "ask", command


def test_sandbox_code_is_not_unquoted(harness, service):
    start_run(service)
    out = pre(harness, service, "impact", "mcp_ctx_ctx_execute",
              {"language": "python", "code": 'open("src/OrderPublisher.java", "w").write("x")'})
    assert decision(out) == "ask"
    out = pre(harness, service, "impact", "mcp_ctx_ctx_execute",
              {"language": "shell", "code": 'grep -c "a > b" src/OrderPublisher.java'})
    assert out is None
    out = pre(harness, service, "verify", "run_in_terminal",
              {"command": "jq '.stage = \"done\"' .ai-harness/ABC-123/state.json > t && mv t .ai-harness/ABC-123/state.json"})
    assert decision(out) == "deny"


# ---------- missing, broken and switched runs ----------

def test_broken_state_blocks_instead_of_switching_off(harness, service):
    run = start_run(service)
    (run / "state.json").write_text('{"run": "ABC-123",}')
    out = pre(harness, service, "implement", "replace_string_in_file", {"filePath": "src/OrderPublisher.java"})
    assert decision(out) == "deny" and "cannot be used" in reason(out)
    code, _, err = prompt(harness, service, "plan", "[harness:approve plan]")
    assert code == 2 and "cannot be read" in err


def test_chat_stays_with_its_run(harness, service):
    start_run(service, "ABC-123")
    prompt(harness, service, "intake", "hello", session_id="s1")
    other = start_run(service, "XYZ-9")  # another chat switched the active run
    (other / "requirement.md").write_text("r\n")
    code, _, err = prompt(harness, service, "intake", "[harness:approve intake]", session_id="s1")
    assert code == 2 and "working on run ABC-123" in err
    assert "intake" not in state(other)["approvals"]
    out = pre(harness, service, "grill", "create_file", {"filePath": ".ai-harness/XYZ-9/clarifications.md"}, session_id="s1")
    assert decision(out) == "deny"
    # The orchestrator switching runs in this chat moves the chat with it.
    run_hook(harness, service, "post-tool", "orchestrator",
             {"tool_name": "create_file", "tool_input": {"filePath": ".ai-harness/current"}, "session_id": "s1"})
    code, _, err = prompt(harness, service, "intake", "[harness:approve intake]", session_id="s1")
    assert code == 0, err


def test_other_runs_state_is_protected(harness, service):
    start_run(service, "OLD-1")
    start_run(service, "ABC-123")
    out = pre(harness, service, "orchestrator", "create_file", {"filePath": ".ai-harness/OLD-1/state.json"})
    assert decision(out) == "deny"
    assert pre(harness, service, "orchestrator", "create_file", {"filePath": ".ai-harness/NEW-2/state.json"}) is None
    assert decision(pre(harness, service, "grill", "create_file", {"filePath": "harness.local.yaml"})) == "deny"


# ---------- personal settings ----------

def test_partial_personal_settings_keep_team_budgets(harness, service):
    run = start_run(service)
    (service / "harness.local.yaml").write_text("﻿review:\n  implement: per-batch-3\nbudgets: {intake: {tool_calls: 1}}\n")
    _, out, _ = prompt(harness, service, "intake", "status?")
    assert "of 20 tool calls" in out["hookSpecificOutput"]["additionalContext"]
    (service / "harness.local.yaml").write_text("hooks:\n  edit_tools: \"^$\"\n  out_of_plan_files: allow\n")
    assert decision(pre(harness, service, "grill", "create_file", {"filePath": "src/Main.java"})) == "deny"
    assert run.exists()


# ---------- budgets ----------

def test_extend_raises_output_budget(harness, service):
    run = start_run(service)
    approve_until(harness, service, run, "implement")
    (run / ".usage.json").write_text(json.dumps({"implement": {"tool_calls": 1, "output_bytes": 600 * 1024}}))
    out = pre(harness, service, "implement", "read_file", {"filePath": "src/OrderPublisher.java"})
    assert "Output budget for implement reached" in reason(out)
    prompt(harness, service, "implement", "extend")
    assert pre(harness, service, "implement", "read_file", {"filePath": "src/OrderPublisher.java"}) is None


def test_budgets_reset_per_request_dispatch_and_revise(harness, service):
    run = start_run(service)
    (run / ".usage.json").write_text(json.dumps({"orchestrator": {"tool_calls": 30, "output_bytes": 0},
                                                 "scout": {"tool_calls": 25, "output_bytes": 0},
                                                 "intake": {"tool_calls": 20, "output_bytes": 0}}))
    assert decision(pre(harness, service, "orchestrator", "read_file", {"filePath": "x"})) == "deny"
    prompt(harness, service, "orchestrator", "status")
    assert pre(harness, service, "orchestrator", "read_file", {"filePath": "x"}) is None
    assert decision(pre(harness, service, "scout", "read_file", {"filePath": "x"})) == "deny"
    pre(harness, service, "grill", "runSubagent", {"agentName": "Component Scout", "prompt": "who calls X?", "description": "callers"})
    assert pre(harness, service, "scout", "read_file", {"filePath": "x"}) is None
    assert decision(pre(harness, service, "intake", "read_file", {"filePath": "x"})) == "deny"
    prompt(harness, service, "intake", "revise: shorter summary")
    assert pre(harness, service, "intake", "read_file", {"filePath": "x"}) is None


def test_parallel_calls_do_not_lose_counts(harness, service):
    run = start_run(service)
    def call(i):
        return pre(harness, service, "grill", "read_file", {"filePath": f"src/F{i}.java"})
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(call, range(20)))
    assert usage(run)["grill"]["tool_calls"] == 20


# ---------- prompt parsing ----------

def test_markers_and_commands_only_count_at_the_start(harness, service):
    run = start_run(service)
    (run / "requirement.md").write_text("r\n")
    code, _, _ = prompt(harness, service, "intake", 'What does "[harness:approve intake]" do?')
    assert code == 0 and "intake" not in state(run)["approvals"]
    code, _, _ = prompt(harness, service, "intake", "extend the retry window to 30 seconds")
    assert code == 0 and "_extra" not in json.loads((run / ".usage.json").read_text() or "{}")
    code, _, _ = prompt(harness, service, "orchestrator", "back to the drawing board")
    assert code == 0
    approve_until(harness, service, run, "plan")
    code, _, _ = prompt(harness, service, "plan", "approve, but do not skip the cross-check")
    assert code == 0 and "plan" not in state(run)["approvals"]
    code, _, _ = prompt(harness, service, "orchestrator", "go back to grill")
    assert code == 0 and state(run)["stage"] == "grill"


# ---------- review cadence, pause ----------

def test_implement_approval_needs_every_task_run_and_reviewed(harness, service):
    run = start_run(service)
    approve_until(harness, service, run, "implement")
    def dispatch(task):
        return pre(harness, service, "implement", "runSubagent",
                   {"agentName": "Task Runner (standard)", "prompt": f"{task}: do it", "description": task})
    assert dispatch("T1") is None
    assert "has not reviewed" in reason(dispatch("T2"))
    code, _, err = prompt(harness, service, "reviewer", "x")  # unrelated scope, no effect
    code, _, err = prompt(harness, service, "verify", "[harness:approve implement]")
    assert code == 2 and "T2 has not run yet" in err
    prompt(harness, service, "implement", "[harness:approve task] next")
    assert dispatch("T2") is None
    assert "not a task" in reason(dispatch("T9"))
    assert "Start the Task Runner prompt" in reason(pre(harness, service, "implement", "runSubagent",
                                                        {"agentName": "Task Runner (standard)", "prompt": "do it", "description": "x"}))
    code, _, err = prompt(harness, service, "verify", "[harness:approve implement] All tasks approved.")
    assert code == 0, err


def test_stop_pauses_tools_until_resume(harness, service):
    run = start_run(service)
    prompt(harness, service, "grill", "stop")
    assert state(run)["stage_status"] == "paused"
    assert "paused" in reason(pre(harness, service, "grill", "read_file", {"filePath": "x"}))
    prompt(harness, service, "orchestrator", "resume ABC-123")
    assert pre(harness, service, "grill", "read_file", {"filePath": "x"}) is None


def test_pre_compact_keeps_approved_artifacts_unchanged(harness, service):
    run = start_run(service)
    approve_until(harness, service, run, "implement", plan="# Plan\n")
    prompt(harness, service, "verify", "[harness:approve implement]")
    run_hook(harness, service, "pre-compact", "verify", {"trigger": "auto"})
    (run / "verification.md").write_text("v\n")
    code, _, err = prompt(harness, service, "verify", "[harness:approve verify]")
    assert code == 0, err
