import json

import pytest

from conftest import NODE, run_hook, start_run

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required for hook tests")


def approve(repo, stage, scope=None):
    return run_hook(repo, "prompt", scope or stage, {"prompt": f"[harness:approve {stage}] go"})


def edit(repo, scope, path, tool="editFiles"):
    return run_hook(repo, "pre-tool", scope, {"tool_name": tool, "tool_input": {"filePath": path}})


def state(run):
    return json.loads((run / "state.json").read_text())


def write(run, name, text="content\n"):
    (run / name).write_text(text)


def approve_through_plan(repo, run, impact_files=("src/OrderPublisher.java",)):
    write(run, "requirement.md")
    assert approve(repo, "intake")[0] == 0
    write(run, "clarifications.md")
    assert approve(repo, "grill")[0] == 0
    write(run, "impact.md")
    write(run, "impact.json", json.dumps({"run": "ABC-123", "components": [
        {"name": "orders", "change_type": "code", "confidence": "high", "files": list(impact_files), "evidence": []}]}))
    assert approve(repo, "impact")[0] == 0
    write(run, "plan.md")
    assert approve(repo, "plan")[0] == 0


def test_no_active_run_means_no_interference(initialized):
    code, out, _ = edit(initialized, "grill", "src/Main.java")
    assert code == 0 and out is None


def test_pre_plan_stage_can_only_edit_change_folder(initialized):
    start_run(initialized)
    code, out, _ = edit(initialized, "grill", "src/Main.java")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    code, out, _ = edit(initialized, "grill", ".changes/ABC-123/clarifications.md")
    assert out is None
    usage = json.loads((initialized / ".changes/ABC-123/.usage.json").read_text())
    assert usage["grill"]["tool_calls"] == 1


def test_state_file_and_current_pointer_are_protected(initialized):
    start_run(initialized)
    _, out, _ = edit(initialized, "intake", ".changes/ABC-123/state.json")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    _, out, _ = edit(initialized, "intake", ".changes/current")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    _, out, _ = edit(initialized, "orchestrator", ".changes/current")
    assert out is None


def test_absolute_paths_are_normalised(initialized):
    start_run(initialized)
    _, out, _ = edit(initialized, "grill", str(initialized / ".changes/ABC-123/clarifications.md"))
    assert out is None
    _, out, _ = edit(initialized, "grill", "file://" + str(initialized / "src/Main.java"))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_approve_requires_artifact_and_records_hash(initialized):
    run = start_run(initialized)
    code, _, err = approve(initialized, "intake")
    assert code == 2 and "requirement.md is missing" in err
    write(run, "requirement.md")
    code, out, _ = approve(initialized, "intake", scope="grill")
    assert code == 0
    assert "Approved intake" in out["hookSpecificOutput"]["additionalContext"]
    st = state(run)
    assert st["stage"] == "grill" and len(st["approvals"]["intake"]["files"]["requirement.md"]) == 64


def test_cannot_skip_stages(initialized):
    run = start_run(initialized)
    write(run, "plan.md")
    code, _, err = approve(initialized, "plan")
    assert code == 2 and "Approve the intake stage before plan" in err


def test_plain_approve_word_uses_agent_scope(initialized):
    run = start_run(initialized)
    write(run, "requirement.md")
    code, _, _ = run_hook(initialized, "prompt", "intake", {"prompt": "approve"})
    assert code == 0 and state(run)["stage"] == "grill"


def test_code_edits_unlock_only_after_plan_approval(initialized):
    run = start_run(initialized)
    _, out, _ = edit(initialized, "implement-task", "src/OrderPublisher.java")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    approve_through_plan(initialized, run)
    assert json.loads((run / "approval.json").read_text())["plan_sha256"]
    _, out, _ = edit(initialized, "implement-task", "src/OrderPublisher.java")
    assert out is None
    _, out, _ = edit(initialized, "implement-task", "src/test/OrderPublisherTest.java")
    assert out is None
    _, out, _ = edit(initialized, "implement-task", "src/Unplanned.java")
    assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
    _, out, _ = edit(initialized, "verify", "src/OrderPublisher.java")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_plan_change_after_approval_locks_code_again(initialized):
    run = start_run(initialized)
    approve_through_plan(initialized, run)
    write(run, "plan.md", "sneaky change\n")
    _, out, _ = edit(initialized, "implement-task", "src/OrderPublisher.java")
    decision = out["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny" and "plan.md changed" in decision["permissionDecisionReason"]
    _, out, _ = run_hook(initialized, "pre-tool", "implement", {"tool_name": "runInTerminal", "tool_input": {"command": "sed -i x"}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_revise_clears_later_approvals(initialized):
    run = start_run(initialized)
    approve_through_plan(initialized, run)
    code, out, _ = run_hook(initialized, "prompt", "orchestrator", {"prompt": "back to grill"})
    assert code == 0
    st = state(run)
    assert st["stage"] == "grill" and set(st["approvals"]) == {"intake"}
    _, out, _ = edit(initialized, "implement-task", "src/OrderPublisher.java")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_budget_denies_then_extend_allows(initialized):
    run = start_run(initialized)
    (run / ".usage.json").write_text(json.dumps({"intake": {"tool_calls": 20, "output_bytes": 0}}))
    _, out, _ = run_hook(initialized, "pre-tool", "intake", {"tool_name": "readFile", "tool_input": {}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Budget for intake reached" in out["hookSpecificOutput"]["permissionDecisionReason"]
    code, out, _ = run_hook(initialized, "prompt", "intake", {"prompt": "extend"})
    assert "extended by 10" in out["hookSpecificOutput"]["additionalContext"]
    _, out, _ = run_hook(initialized, "pre-tool", "intake", {"tool_name": "readFile", "tool_input": {}})
    assert out is None


def test_local_yaml_overrides_budget(initialized):
    run = start_run(initialized)
    (initialized / "harness.local.yaml").write_text("budgets:\n  intake:\n    tool_calls: 2  # small\n")
    (run / ".usage.json").write_text(json.dumps({"intake": {"tool_calls": 2, "output_bytes": 0}}))
    _, out, _ = run_hook(initialized, "pre-tool", "intake", {"tool_name": "readFile", "tool_input": {}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_output_budget_counts_tool_responses(initialized):
    run = start_run(initialized)
    run_hook(initialized, "post-tool", "impact", {"tool_name": "x", "tool_response": "a" * 300 * 1024})
    _, out, _ = run_hook(initialized, "pre-tool", "impact", {"tool_name": "readFile", "tool_input": {}})
    assert "Output budget for impact reached" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_task_dispatch_and_task_approval_reset_task_budget(initialized):
    run = start_run(initialized)
    approve_through_plan(initialized, run)
    (run / ".usage.json").write_text(json.dumps({"implement-task": {"tool_calls": 30, "output_bytes": 0}}))
    _, out, _ = edit(initialized, "implement-task", "src/OrderPublisher.java")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    run_hook(initialized, "pre-tool", "implement", {"tool_name": "runSubagent", "tool_input": {}})
    _, out, _ = edit(initialized, "implement-task", "src/OrderPublisher.java")
    assert out is None
    code, out, _ = run_hook(initialized, "prompt", "implement", {"prompt": "[harness:approve task] next"})
    assert "Task approval 1 recorded" in out["hookSpecificOutput"]["additionalContext"]


def test_status_line_and_hooks_ok_marker(initialized):
    start_run(initialized)
    _, out, _ = run_hook(initialized, "prompt", "grill", {"prompt": "what next?"})
    assert "Harness status: run ABC-123" in out["hookSpecificOutput"]["additionalContext"]
    assert (initialized / ".changes/.hooks-ok").exists()


def test_pre_compact_writes_status(initialized):
    run = start_run(initialized)
    run_hook(initialized, "pre-compact", "grill", {"trigger": "auto"})
    assert "Status at compaction" in (run / "progress.md").read_text()


def test_broken_hook_input_never_blocks(initialized):
    start_run(initialized)
    script = initialized / ".github/hooks/harness/harness-hook.cjs"
    import subprocess
    proc = subprocess.run(["node", str(script), "pre-tool", "grill"], input="not json", capture_output=True,
                          text=True, cwd=initialized)
    assert proc.returncode in (0, 1)
