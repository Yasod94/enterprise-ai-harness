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


# ---------- second review: roots, task history, commands, stop and resume, locks ----------

def test_monorepo_subfolder_is_the_root(harness, tmp_path):
    from conftest import git
    top = tmp_path / "mono"
    svc = top / "services/orders"
    (svc / "src").mkdir(parents=True)
    (svc / "src/A.java").write_text("class A {}\n")
    git(top, "init", "-q")
    code, out, err = prompt(harness, svc, "orchestrator", "ABC-1")
    assert code == 0, err
    state = svc / ".ai-harness/ABC-1/state.json"
    out = pre(harness, svc, "orchestrator", "create_file", {"filePath": str(state), "content": "{}"})
    assert out is None, reason(out)
    run = start_run(svc, "ABC-1")
    (run / "requirement.md").write_text("r\n")
    code, _, err = prompt(harness, svc, "intake", "[harness:approve intake]")
    assert code == 0, err
    assert not (top / ".ai-harness").exists()
    exclude = (top / ".git/info/exclude").read_text()
    assert "/services/orders/.ai-harness/" in exclude
    assert ".ai-harness" not in git(top, "status", "--porcelain", "--untracked-files=all")


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need admin rights on Windows")
def test_folder_opened_through_a_symlink(harness, service, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(service, target_is_directory=True)
    start_run(service)
    out = pre(harness, link, "intake", "create_file",
              {"filePath": str(service / ".ai-harness/ABC-123/requirement.md"), "content": "r"})
    assert out is None, reason(out)
    out = pre(harness, link, "intake", "create_file", {"filePath": str(service / "src/OrderPublisher.java"), "content": "x"})
    assert decision(out) == "deny"


def test_back_to_implement_keeps_task_history(harness, service):
    run = start_run(service)
    approve_until(harness, service, run, "implement")
    for task in ("T1", "T2"):
        out = pre(harness, service, "implement", "runSubagent",
                  {"agentName": "Task Runner (standard)", "prompt": f"{task}: go", "description": task})
        assert out is None, reason(out)
        if task == "T1":
            assert prompt(harness, service, "implement", "[harness:approve task]")[0] == 0
    assert prompt(harness, service, "implement", "[harness:approve implement]")[0] == 0
    (run / "verification.md").write_text("v\n")
    code, _, err = prompt(harness, service, "verify", "[harness:revise implement] fix the log line")
    assert code == 0, err
    assert state(run)["tasks_dispatched"] == ["T1", "T2"]
    out = pre(harness, service, "implement", "runSubagent",
              {"agentName": "Task Runner (standard)", "prompt": "T2: fix the log line", "description": "T2"})
    assert out is None, reason(out)
    code, _, err = prompt(harness, service, "implement", "[harness:approve implement]")
    assert code == 0, err
    # Revising the plan does start the tasks over.
    code, _, err = prompt(harness, service, "verify", "[harness:revise plan] split T2")
    assert code == 0, err
    assert "tasks_dispatched" not in state(run)


def test_task_id_comes_from_the_start_of_the_prompt(harness, service):
    run = start_run(service)
    approve_until(harness, service, run, "implement")
    out = pre(harness, service, "implement", "runSubagent",
              {"agentName": "Task Runner (standard)", "prompt": "Context: depends on T1. Now implement T2.", "description": "x"})
    assert decision(out) == "deny" and "Start the Task Runner prompt" in reason(out)
    code, _, err = prompt(harness, service, "implement", "[harness:approve task]")
    assert code == 2 and "No task is waiting" in err
    out = pre(harness, service, "implement", "runSubagent",
              {"agentName": "Task Runner (standard)", "prompt": "**T1**: config", "description": "T1"})
    assert out is None, reason(out)
    assert decision(pre(harness, service, "implement", "runSubagent",
                        {"agentName": "Task Runner (standard)", "prompt": "T2: retry", "description": "T2"})) == "deny"


@pytest.mark.parametrize("command", [
    "rm -rf src", "sed -i 's/a/b/' src/*", "mv src old", "echo x > Makefile", "echo x 1> src/A.java",
    "make &> src/out.txt", "cp -r /tmp/x src", "python3 -c \"import pathlib; pathlib.Path('src/A.java').write_text('x')\"",
    "node -e \"require('fs').appendFileSync('src/A.java', 'x')\"",
])
def test_more_writing_commands_ask(harness, service, command):
    start_run(service)
    for scope in ("verify", "grill", "orchestrator"):
        assert decision(pre(harness, service, scope, "run_in_terminal", {"command": command})) == "ask", (scope, command)


@pytest.mark.parametrize("command", [
    "java -cp target/classes Main", "docker run --rm img", "cat src/A.java | grep -c '=>'",
    "gh pr create --title 'ABC-1: x' --body 'T1 -> done, see OrderPublisher.java'",
    "mvn -q test > /tmp/test.log; tail -50 /tmp/test.log",
    "mkdir -p .ai-harness/ABC-2 && echo ABC-2 > .ai-harness/current",
])
def test_more_harmless_commands_run(harness, service, command):
    start_run(service)
    scope = "orchestrator" if "current" in command else "verify"
    assert pre(harness, service, scope, "run_in_terminal", {"command": command}) is None, command


def test_owned_files_and_index_are_refused_to_commands_everywhere(harness, service):
    run = start_run(service)
    approve_until(harness, service, run, "implement")
    for scope in ("implement-task", "verify", "orchestrator"):
        for command in ("echo {} 1> .ai-harness/ABC-123/state.json", "rm -rf .codegraph", "echo x &> harness.local.yaml",
                        "echo ABC-9 > .ai-harness/current"):
            out = pre(harness, service, scope, "run_in_terminal", {"command": command})
            if scope == "orchestrator" and command.endswith("current"):
                continue
            assert decision(out) == "deny", (scope, command)
    batch = {"commands": [{"label": "x", "command": "echo x > src/A.java"}]}
    assert decision(pre(harness, service, "impact", "mcp_ctx_ctx_batch_execute", batch)) == "ask"


def test_no_run_terminal_and_unknown_edits(harness, service):
    assert decision(pre(harness, service, "orchestrator", "run_in_terminal", {"command": "rm -rf src"})) == "ask"
    out = pre(harness, service, "implement-task", "edit_notebook_file", {"cellId": "1", "newCode": "x"})
    assert decision(out) == "deny"


def test_stop_and_resume_follow_the_named_run(harness, service):
    a = start_run(service, "A-1")
    prompt(harness, service, "intake", "hello", session_id="S1")  # S1 works on A-1
    b = start_run(service, "B-2")
    prompt(harness, service, "intake", "hello", session_id="S2")  # S2 works on B-2
    code, ctx, err = prompt(harness, service, "orchestrator", "[harness:stop] stop", session_id="S1")
    assert code == 0, err
    assert state(a)["stage_status"] == "paused" and state(b).get("stage_status") != "paused"
    code, out, err = prompt(harness, service, "orchestrator", "resume A-1", session_id="S3")
    assert code == 0, err
    assert state(a)["stage_status"] == "working"
    assert (service / ".ai-harness/current").read_text().strip() == "A-1"
    assert "Resumed run A-1" in out["hookSpecificOutput"]["additionalContext"]


def test_damaged_sessions_file_does_not_break_prompts(harness, service):
    start_run(service)
    for text in ('{"S1": null, "S2": {"run": 5}}', "[1, 2]", "not json"):
        (service / ".ai-harness/.sessions.json").write_text(text)
        code, out, err = prompt(harness, service, "intake", "hi", session_id="S1")
        assert code == 0 and "Harness status" in out["hookSpecificOutput"]["additionalContext"], (text, err)


def test_lock_left_by_a_dead_hook_is_taken_over_at_once(harness, service):
    import time
    start_run(service)
    lock = service / ".ai-harness/.lock"
    lock.write_text("999999\n" + __import__("socket").gethostname() + "\n")
    old = time.time() - 1
    __import__("os").utime(lock, (old, old))
    started = time.time()
    assert pre(harness, service, "intake", "read_file", {"filePath": "src/OrderPublisher.java"}) is None
    assert time.time() - started < 2.5
    assert not lock.exists()
    lock.mkdir()  # not a file: wait, then go on without the lock
    started = time.time()
    code, _, err = run_hook(harness, service, "pre-tool", "intake", {"tool_name": "read_file", "tool_input": {}})
    assert code == 0, err
    assert time.time() - started < 10
    lock.rmdir()


def test_no_runs_folder_outside_git(harness, tmp_path):
    folder = tmp_path / "plain"
    folder.mkdir()
    code, _, err = prompt(harness, folder, "orchestrator", "hello")
    assert code == 0, err
    pre(harness, folder, "intake", "read_file", {"filePath": "x"})
    assert not (folder / ".ai-harness").exists()


def test_harness_check_tool_calls_refresh_the_hook_marker(harness, service):
    prompt(harness, service, "orchestrator", "hello")
    marker = service / ".ai-harness/.hooks-ok"
    marker.write_text("2000-01-01T00:00:00.000Z\n")
    pre(harness, service, "check", "run_in_terminal", {"command": "node -e \"console.log(1)\""})
    assert marker.read_text().startswith("20") and not marker.read_text().startswith("2000")


# ---------- third pass: tasks, notebook cells, file-writing MCP tools ----------

def test_vscode_tasks_and_notebook_cells_are_guarded(harness, service):
    start_run(service)
    out = pre(harness, service, "verify", "run_task", {"workspaceFolder": str(service), "id": "shell: build"})
    assert decision(out) == "ask" and "cannot inspect" in reason(out)
    out = pre(harness, service, "verify", "create_and_run_task",
              {"workspaceFolder": str(service), "task": {"label": "x", "type": "shell", "command": "rm", "args": ["-rf", "src"]}})
    assert decision(out) == "ask"
    out = pre(harness, service, "verify", "create_and_run_task",
              {"workspaceFolder": str(service), "task": {"label": "t", "type": "shell", "command": "./gradlew", "args": ["test"]}})
    assert out is None, reason(out)
    assert decision(pre(harness, service, "grill", "run_notebook_cell", {"filePath": "a.ipynb", "cellId": "c1"})) == "ask"
    out = pre(harness, service, "implement-task", "run_task", {"workspaceFolder": str(service), "id": "build"})
    assert decision(out) == "deny" and "not approved" in reason(out)


def test_file_writing_mcp_tools_are_edits(harness, service):
    start_run(service)
    out = pre(harness, service, "grill", "mcp_filesystem_write_file", {"path": str(service / "src/OrderPublisher.java"), "content": "x"})
    assert decision(out) == "deny"
    out = pre(harness, service, "grill", "mcp_filesystem_write_file", {"path": str(service / ".ai-harness/ABC-123/notes.md"), "content": "x"})
    assert out is None, reason(out)
    out = pre(harness, service, "verify", "mcp_atlassian_addOrEditJiraIssueComment", {"issueIdOrKey": "ABC-123", "commentBody": "PR"})
    assert out is None, reason(out)


def test_extended_length_windows_paths_are_inside_the_workspace(harness, service):
    import os
    start_run(service)
    if os.name != "nt":
        pytest.skip("Windows path forms")
    p = "\\\\?\\" + str(service / "src/OrderPublisher.java")
    assert decision(pre(harness, service, "grill", "create_file", {"filePath": p, "content": "x"})) == "deny"


GUARD_READS = [
    "git diff main...HEAD -- src", "rg -n 'publish\\(' src", 'grep -rn "a<b" src', "ls -la | sort -k5 -n",
    "./gradlew test --tests 'OrderPublisherTest' 2>&1 | tail -n 40", "pytest -q 2>&1 | tail -5", "dotnet test --no-build",
    "jq '.components[] | select(.confidence == \"low\")' .ai-harness/ABC-123/impact.json", "Get-Content src/A.java | Select-String 'x'",
    "git show HEAD:src/A.java > /dev/null", "python3 -c 'import json;print(json.load(open(\"package.json\"))[\"name\"])'",
    "git push -u origin HEAD", "diff <(sort a) <(sort b)", "curl -s --write-out '%{http_code}' http://x", "git commit --fixup HEAD",
    "gofmt -l .", "prettier --check src", "git checkout main",
]
GUARD_WRITES = [
    "echo x>src/A.java", "git checkout -- src/A.java", "git reset --hard HEAD~1", "git clean -fd", "perl -pi -e 's/a/b/' src/A.java",
    "tee src/A.java < /dev/null", "Set-Content src/A.java x", "Remove-Item -Recurse src", "cat > src/A.java <<EOF\nx\nEOF",
    "npx prettier --write src", "eslint --fix src", "./gradlew spotlessApply", "gofmt -w .", "tar -xzf a.tgz -C src",
    "bash -c 'rm -rf src'", "find src -type f -delete", "2>err.log ./gradlew test", "echo x >| src/A.java",
    "ruby -e \"File.write('src/A.java','x')\"", "deno eval \"Deno.writeTextFileSync('src/A.java','x')\"",
]


def test_command_guard_on_common_commands(harness, service):
    run = start_run(service)
    for command in GUARD_READS + GUARD_WRITES:
        (run / ".usage.json").unlink(missing_ok=True)
        got = decision(pre(harness, service, "verify", "run_in_terminal", {"command": command}))
        assert got == (None if command in GUARD_READS else "ask"), command


def test_approved_artifacts_are_locked_until_revised(harness, service):
    run = start_run(service)
    approve_until(harness, service, run, "implement")
    for scope, name in (("plan", "plan.md"), ("implement", "plan.md"), ("implement-task", "requirement.md"), ("grill", "impact.json")):
        out = pre(harness, service, scope, "replace_string_in_file",
                  {"filePath": str(run / name), "oldString": "a", "newString": "b"})
        assert decision(out) == "deny" and "back to" in reason(out), (scope, name)
    assert pre(harness, service, "implement", "create_file", {"filePath": str(run / "progress.md"), "content": "p"}) is None
    code, _, err = prompt(harness, service, "implement", "back to plan")
    assert code == 0, err
    assert pre(harness, service, "plan", "replace_string_in_file",
               {"filePath": str(run / "plan.md"), "oldString": "a", "newString": "b"}) is None


def test_pressing_an_old_approve_button_again_changes_nothing(harness, service):
    run = start_run(service)
    approve_until(harness, service, run, "implement")
    out = pre(harness, service, "implement", "runSubagent",
              {"agentName": "Task Runner (standard)", "prompt": "T1: config", "description": "T1"})
    assert out is None, reason(out)
    before = state(run)
    code, out, err = prompt(harness, service, "implement", "[harness:approve plan] skip cross-check. Approved again.")
    assert code == 0, err
    assert "already approved and unchanged" in out["hookSpecificOutput"]["additionalContext"]
    after = state(run)
    assert after["tasks_dispatched"] == ["T1"] and after["approvals"] == before["approvals"] and after["stage"] == "implement"


def test_tool_input_sent_as_a_json_string(harness, service):
    start_run(service)
    payload = json.dumps({"filePath": str(service / "src/OrderPublisher.java"), "content": "x"})
    assert decision(pre(harness, service, "grill", "create_file", payload)) == "deny"
