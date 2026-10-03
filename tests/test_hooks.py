import json

import pytest

from conftest import NODE, run_hook as _run_hook, start_run

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required for hook tests")


@pytest.fixture
def ctx(harness, service):
    return harness, service


def run_hook(c, mode, scope, event=None, env=None):
    return _run_hook(c[0], c[1], mode, scope, event, env)


def approve(c, stage, scope=None, extra=""):
    return run_hook(c, "prompt", scope or stage, {"prompt": f"[harness:approve {stage}] {extra or 'go'}"})


def edit(c, scope, path, tool="editFiles"):
    return run_hook(c, "pre-tool", scope, {"tool_name": tool, "tool_input": {"filePath": path}})


def state(run):
    return json.loads((run / "state.json").read_text())


def write(run, name, text="content\n"):
    (run / name).write_text(text)


def approve_through_plan(c, run, impact_files=("src/OrderPublisher.java",)):
    write(run, "requirement.md")
    assert approve(c, "intake")[0] == 0
    write(run, "clarifications.md")
    assert approve(c, "grill")[0] == 0
    write(run, "impact.md")
    write(run, "impact.json", json.dumps({"run": "ABC-123", "components": [
        {"name": "orders", "change_type": "code", "confidence": "high", "files": list(impact_files), "evidence": []}]}))
    assert approve(c, "impact")[0] == 0
    write(run, "plan.md")
    assert approve(c, "plan", extra="skip cross-check")[0] == 0


def test_no_active_run_means_no_interference(ctx):
    code, out, _ = edit(ctx, "grill", "src/Main.java")
    assert code == 0 and out is None


def test_pre_plan_stage_can_only_edit_change_folder(ctx):
    start_run(ctx[1])
    code, out, _ = edit(ctx, "grill", "src/Main.java")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    code, out, _ = edit(ctx, "grill", ".changes/ABC-123/clarifications.md")
    assert out is None
    usage = json.loads((ctx[1] / ".changes/ABC-123/.usage.json").read_text())
    assert usage["grill"]["tool_calls"] == 1


def test_state_file_and_current_pointer_are_protected(ctx):
    start_run(ctx[1])
    _, out, _ = edit(ctx, "intake", ".changes/ABC-123/state.json")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    _, out, _ = edit(ctx, "intake", ".changes/current")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    _, out, _ = edit(ctx, "orchestrator", ".changes/current")
    assert out is None


def test_absolute_paths_are_normalised(ctx):
    start_run(ctx[1])
    _, out, _ = edit(ctx, "grill", str(ctx[1] / ".changes/ABC-123/clarifications.md"))
    assert out is None
    _, out, _ = edit(ctx, "grill", "file://" + str(ctx[1] / "src/Main.java"))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_approve_requires_artifact_and_records_hash(ctx):
    run = start_run(ctx[1])
    code, _, err = approve(ctx, "intake")
    assert code == 2 and "requirement.md is missing" in err
    write(run, "requirement.md")
    code, out, _ = approve(ctx, "intake", scope="grill")
    assert code == 0
    assert "Approved intake" in out["hookSpecificOutput"]["additionalContext"]
    st = state(run)
    assert st["stage"] == "grill" and len(st["approvals"]["intake"]["files"]["requirement.md"]) == 64


def test_cannot_skip_stages(ctx):
    run = start_run(ctx[1])
    write(run, "plan.md")
    code, _, err = approve(ctx, "plan")
    assert code == 2 and "Approve the intake stage before plan" in err


def test_plain_approve_word_uses_agent_scope(ctx):
    run = start_run(ctx[1])
    write(run, "requirement.md")
    code, _, _ = run_hook(ctx, "prompt", "intake", {"prompt": "approve"})
    assert code == 0 and state(run)["stage"] == "grill"


def test_code_edits_unlock_only_after_plan_approval(ctx):
    run = start_run(ctx[1])
    _, out, _ = edit(ctx, "implement-task", "src/OrderPublisher.java")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    approve_through_plan(ctx, run)
    assert json.loads((run / "approval.json").read_text())["plan_sha256"]
    _, out, _ = edit(ctx, "implement-task", "src/OrderPublisher.java")
    assert out is None
    _, out, _ = edit(ctx, "implement-task", "src/test/OrderPublisherTest.java")
    assert out is None
    _, out, _ = edit(ctx, "implement-task", "src/Unplanned.java")
    assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
    _, out, _ = edit(ctx, "verify", "src/OrderPublisher.java")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_plan_change_after_approval_locks_code_again(ctx):
    run = start_run(ctx[1])
    approve_through_plan(ctx, run)
    write(run, "plan.md", "sneaky change\n")
    _, out, _ = edit(ctx, "implement-task", "src/OrderPublisher.java")
    decision = out["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny" and "plan.md changed" in decision["permissionDecisionReason"]
    _, out, _ = run_hook(ctx, "pre-tool", "implement", {"tool_name": "runInTerminal", "tool_input": {"command": "sed -i x"}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_revise_clears_later_approvals(ctx):
    run = start_run(ctx[1])
    approve_through_plan(ctx, run)
    code, out, _ = run_hook(ctx, "prompt", "orchestrator", {"prompt": "back to grill"})
    assert code == 0
    st = state(run)
    assert st["stage"] == "grill" and set(st["approvals"]) == {"intake"}
    _, out, _ = edit(ctx, "implement-task", "src/OrderPublisher.java")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_budget_denies_then_extend_allows(ctx):
    run = start_run(ctx[1])
    (run / ".usage.json").write_text(json.dumps({"intake": {"tool_calls": 20, "output_bytes": 0}}))
    _, out, _ = run_hook(ctx, "pre-tool", "intake", {"tool_name": "readFile", "tool_input": {}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Budget for intake reached" in out["hookSpecificOutput"]["permissionDecisionReason"]
    code, out, _ = run_hook(ctx, "prompt", "intake", {"prompt": "extend"})
    assert "extended by 10" in out["hookSpecificOutput"]["additionalContext"]
    _, out, _ = run_hook(ctx, "pre-tool", "intake", {"tool_name": "readFile", "tool_input": {}})
    assert out is None


def test_local_yaml_overrides_budget(ctx):
    run = start_run(ctx[1])
    (ctx[1] / "harness.local.yaml").write_text("budgets:\n  intake:\n    tool_calls: 2  # small\n")
    (run / ".usage.json").write_text(json.dumps({"intake": {"tool_calls": 2, "output_bytes": 0}}))
    _, out, _ = run_hook(ctx, "pre-tool", "intake", {"tool_name": "readFile", "tool_input": {}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_output_budget_counts_tool_responses(ctx):
    run = start_run(ctx[1])
    run_hook(ctx, "post-tool", "impact", {"tool_name": "x", "tool_response": "a" * 300 * 1024})
    _, out, _ = run_hook(ctx, "pre-tool", "impact", {"tool_name": "readFile", "tool_input": {}})
    assert "Output budget for impact reached" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_task_dispatch_and_task_approval_reset_task_budget(ctx):
    run = start_run(ctx[1])
    approve_through_plan(ctx, run)
    (run / ".usage.json").write_text(json.dumps({"implement-task": {"tool_calls": 30, "output_bytes": 0}}))
    _, out, _ = edit(ctx, "implement-task", "src/OrderPublisher.java")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    run_hook(ctx, "pre-tool", "implement", {"tool_name": "runSubagent", "tool_input": {}})
    _, out, _ = edit(ctx, "implement-task", "src/OrderPublisher.java")
    assert out is None
    code, out, _ = run_hook(ctx, "prompt", "implement", {"prompt": "[harness:approve task] next"})
    assert "Task approval 1 recorded" in out["hookSpecificOutput"]["additionalContext"]


def test_status_line_and_hooks_ok_marker(ctx):
    start_run(ctx[1])
    _, out, _ = run_hook(ctx, "prompt", "grill", {"prompt": "what next?"})
    assert "Harness status: run ABC-123" in out["hookSpecificOutput"]["additionalContext"]
    assert (ctx[1] / ".changes/.hooks-ok").exists()


def test_pre_compact_writes_status(ctx):
    run = start_run(ctx[1])
    run_hook(ctx, "pre-compact", "grill", {"trigger": "auto"})
    assert "Status at compaction" in (run / "progress.md").read_text()


def test_broken_hook_input_never_blocks(ctx):
    start_run(ctx[1])
    script = ctx[0] / "plugin/scripts/harness-hook.cjs"
    import subprocess
    proc = subprocess.run(["node", str(script), "pre-tool", "grill"], input="not json", capture_output=True,
                          text=True, cwd=ctx[1])
    assert proc.returncode in (0, 1)


def test_run_files_are_excluded_locally(ctx):
    harness, repo = ctx
    start_run(repo)
    run_hook(ctx, "prompt", "intake", {"prompt": "hello"})
    exclude = (repo / ".git/info/exclude").read_text()
    assert ".changes/" in exclude and "harness.local.yaml" in exclude and ".codegraph/" in exclude
    run_hook(ctx, "prompt", "intake", {"prompt": "again"})
    assert (repo / ".git/info/exclude").read_text().count(".changes/") == 1
    import subprocess
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
    assert ".changes" not in status


def test_personal_settings_from_home_dir(ctx, tmp_path):
    harness, repo = ctx
    run = start_run(repo)
    home = tmp_path / "home"
    (home / ".copilot").mkdir(parents=True)
    (home / ".copilot/change-harness.local.yaml").write_text("budgets:\n  intake:\n    tool_calls: 1\n")
    (run / ".usage.json").write_text(json.dumps({"intake": {"tool_calls": 1, "output_bytes": 0}}))
    _, out, _ = run_hook(ctx, "pre-tool", "intake", {"tool_name": "readFile", "tool_input": {}},
                         env={"HOME": str(home), "USERPROFILE": str(home)})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_code_graph_files_are_protected(ctx):
    start_run(ctx[1])
    for path in (".codegraph/codegraph.db", ".codegraph-seed/codegraph.db.gz"):
        _, out, _ = edit(ctx, "orchestrator", path)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
