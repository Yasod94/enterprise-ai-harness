"""Model tiers per agent and per task, and cross-checks on a stronger model."""

import json
import re

import pytest
import yaml

from enterprise_ai_harness.build import AGENTS_DIR, build
from enterprise_ai_harness.config import ConfigError, load
from enterprise_ai_harness.doctor import check
from conftest import NODE, run_hook, start_run

DEEP = "Claude Opus 5.5 (copilot)"
STANDARD = "Claude Sonnet 5.5 (copilot)"


def fm(harness, file_id):
    return yaml.safe_load((harness / AGENTS_DIR / f"{file_id}.agent.md").read_text().split("---")[1])


def edit_yaml(harness, old, new):
    path = harness / "harness.yaml"
    text = path.read_text()
    assert old in text, old
    path.write_text(text.replace(old, new, 1))
    build(load(harness))


# ---------- build ----------

def test_agents_get_their_tier_models(harness):
    assert fm(harness, "griller")["model"][0] == DEEP
    assert fm(harness, "planner")["model"][0] == STANDARD
    assert fm(harness, "intake")["model"] == ["Claude Haiku 4.5 (copilot)", "GPT-5 mini (copilot)"]


def test_agent_left_out_uses_the_picker(harness):
    edit_yaml(harness, "    intake: fast\n", "")
    assert "model" not in fm(harness, "intake")


def test_one_task_runner_per_tier(harness):
    names = [fm(harness, f"task-runner-{t}")["name"] for t in ("fast", "standard", "deep")]
    assert names == ["Task Runner (fast)", "Task Runner (standard)", "Task Runner (deep)"]
    assert fm(harness, "task-runner-deep")["model"][0] == DEEP
    impl = fm(harness, "implementer")
    assert impl["agents"] == names
    tier_buttons = [h for h in impl["handoffs"] if h["prompt"].startswith("[harness:tier ")]
    assert [(h["prompt"].split()[1].rstrip("]"), h["model"]) for h in tier_buttons][2] == ("deep", DEEP)
    body = (harness / AGENTS_DIR / "implementer.agent.md").read_text()
    assert "## Task tiers" in body and '"Task Runner (deep)"' in body


def test_fixed_task_runner_tier_builds_one_runner(harness):
    edit_yaml(harness, "    task-runner: by-task\n", "    task-runner: fast\n")
    assert fm(harness, "task-runner")["model"][0] == "Claude Haiku 4.5 (copilot)"
    assert not (harness / AGENTS_DIR / "task-runner-deep.agent.md").exists()
    impl = fm(harness, "implementer")
    assert impl["agents"] == ["Task Runner"]
    assert not [h for h in impl["handoffs"] if "[harness:tier" in h["prompt"]]


def test_required_plan_check_adds_check_and_skip_buttons(harness):
    planner = fm(harness, "planner")
    labels = {h["label"]: h for h in planner["handoffs"]}
    check_btn = labels["Cross-check with the deep model"]
    assert check_btn["agent"] == "Plan Checker" and check_btn["model"] == DEEP
    assert check_btn["prompt"].startswith("[harness:cross-check plan]")
    assert "skip cross-check" in labels["Approve without cross-check"]["prompt"]
    checker = fm(harness, "plan-checker")
    assert checker["name"] == "Plan Checker" and checker["model"][0] == DEEP
    targets = {h["label"]: h["agent"] for h in checker["handoffs"]}
    assert targets["Apply the findings"] == "Planner" and targets["Approve plan as is"] == "Implementer"
    body = (harness / AGENTS_DIR / "plan-checker.agent.md").read_text()
    assert "plan-check.md" in body and "| deep |" in body and "{{" not in body


def test_offered_check_has_no_skip_button(harness):
    labels = [h["label"] for h in fm(harness, "impact-analyst")["handoffs"]]
    assert "Cross-check with the deep model" in labels and "Approve without cross-check" not in labels
    assert fm(harness, "impact-checker")["handoffs"][0]["agent"] == "Impact Analyst"


def test_check_not_required_when_stage_already_on_the_checker_tier(harness):
    edit_yaml(harness, "    planner: standard\n", "    planner: deep\n")
    labels = [h["label"] for h in fm(harness, "planner")["handoffs"]]
    assert "Cross-check with the deep model" in labels and "Approve without cross-check" not in labels
    cfg = json.loads((harness / "plugin/scripts/config.json").read_text())
    assert cfg["models"]["cross_check"]["plan"]["required"] is False


def test_never_removes_the_checker(harness):
    edit_yaml(harness, "plan: { checker: deep, when: below }", "plan: { checker: deep, when: never }")
    assert not (harness / AGENTS_DIR / "plan-checker.agent.md").exists()
    assert "Cross-check" not in (harness / AGENTS_DIR / "planner.agent.md").read_text().split("---")[1]


def test_no_tiers_keeps_the_old_shape(harness):
    text = (harness / "harness.yaml").read_text()
    start = text.index("models:\n")
    end = text.index("\nhooks:")
    (harness / "harness.yaml").write_text(text[:start] + "models: {}\n" + text[end:])
    build(load(harness))
    assert (harness / AGENTS_DIR / "task-runner.agent.md").exists()
    assert not list((harness / AGENTS_DIR).glob("*-checker.agent.md"))
    assert "model" not in fm(harness, "planner")
    assert "Run the Task Runner subagent with only" in (harness / AGENTS_DIR / "implementer.agent.md").read_text()


@pytest.mark.parametrize("old,new,message", [
    ("    planner: standard\n", "    planner: huge\n", "unknown tier huge"),
    ("    reviewer: standard\n", "    reviewer: by-task\n", "by-task is only for the Task Runner"),
    ("when: offer", "when: sometimes", "when must be one of"),
    ("plan: { checker: deep", "plan: { checker: giant", "checker must be a tier name"),
])
def test_invalid_model_config(harness, old, new, message):
    path = harness / "harness.yaml"
    path.write_text(path.read_text().replace(old, new, 1))
    with pytest.raises(ConfigError, match=message):
        load(harness)


def test_doctor_flags_subagent_above_caller(harness):
    edit_yaml(harness, "    component-scout: fast\n", "    component-scout: deep\n")
    edit_yaml(harness, "    griller: deep\n", "    griller: standard\n")
    errors = [f.message for f in check(harness) if f.level == "error"]
    assert any("griller (standard) calls component-scout (deep)" in m for m in errors), errors


# ---------- hook ----------

pytestmark_hook = pytest.mark.skipif(NODE is None, reason="node is required for hook tests")

PLAN = """# Plan: ABC-123

## Tasks

### T1. Add retry config

- Component: orders
- Tier: fast
- Files: src/OrderPublisher.java

### T2. Retry with backoff

- Component: orders
- Tier: deep
- Files: src/OrderPublisher.java
"""


def prompt(harness, repo, scope, text, **extra):
    return run_hook(harness, repo, "prompt", scope, {"prompt": text, **extra})


def approve_until_plan(harness, repo, run):
    for stage, files in (("intake", ["requirement.md"]), ("grill", ["clarifications.md"]),
                         ("impact", ["impact.md", "impact.json"])):
        for name in files:
            (run / name).write_text("{}" if name.endswith(".json") else "content\n")
        code, _, err = prompt(harness, repo, stage, f"[harness:approve {stage}]")
        assert code == 0, err
    (run / "plan.md").write_text(PLAN)


def state(run):
    return json.loads((run / "state.json").read_text())


@pytestmark_hook
def test_plan_approval_needs_the_cross_check(harness, service):
    run = start_run(service)
    approve_until_plan(harness, service, run)
    _, out, _ = prompt(harness, service, "plan", "what next?")
    assert "A cross-check on the deep tier is required before plan" in out["hookSpecificOutput"]["additionalContext"]
    code, _, err = prompt(harness, service, "plan", "[harness:approve plan] go")
    assert code == 2 and 'Press "Cross-check with the deep model"' in err
    assert "plan" not in state(run)["approvals"]


@pytestmark_hook
def test_skipping_the_check_is_recorded(harness, service):
    run = start_run(service)
    approve_until_plan(harness, service, run)
    code, out, err = prompt(harness, service, "implement", "[harness:approve plan] skip cross-check. Approved.")
    assert code == 0, err
    assert "Cross-check of plan skipped by" in out["hookSpecificOutput"]["additionalContext"]
    approval = json.loads((run / "approval.json").read_text())
    assert approval["cross_check"]["skipped"] is True and approval["cross_check"]["required"] is True


@pytestmark_hook
def test_cross_check_flow_records_verdict_and_later_edits(harness, service):
    run = start_run(service)
    approve_until_plan(harness, service, run)
    code, out, _ = prompt(harness, service, "cross-check", "[harness:cross-check plan] Check plan.md.")
    assert code == 0 and "Cross-check of plan started on the deep tier" in out["hookSpecificOutput"]["additionalContext"]
    assert state(run)["cross_checks"]["plan"]["checker"] == "deep"

    # Only the Checker writes plan-check.md; it may not touch code or the plan.
    edit = lambda scope, path: run_hook(harness, service, "pre-tool", scope,
                                        {"tool_name": "editFiles", "tool_input": {"filePath": path}})[1]
    assert edit("cross-check", ".changes/ABC-123/plan-check.md") is None
    assert edit("cross-check", "src/OrderPublisher.java")["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert edit("plan", ".changes/ABC-123/plan-check.md")["hookSpecificOutput"]["permissionDecision"] == "deny"

    (run / "plan-check.md").write_text("Verdict: changes needed\n\n## Findings\n\n1. [high] T2 misses the DLQ.\n")
    (run / "plan.md").write_text(PLAN + "\n## Cross-check findings\n\n1. Added the DLQ step to T2.\n")
    code, out, err = prompt(harness, service, "implement", "[harness:approve plan] Approved after the cross-check.")
    assert code == 0, err
    context = out["hookSpecificOutput"]["additionalContext"]
    assert "Cross-check verdict: changes needed; plan changed after the check" in context
    cc = json.loads((run / "approval.json").read_text())["cross_check"]
    assert cc["verdict"] == "changes needed" and cc["changed_after_check"] is True and len(cc["check_sha256"]) == 64


@pytestmark_hook
def test_cross_check_needs_the_artifact(harness, service):
    start_run(service)
    code, _, err = prompt(harness, service, "cross-check", "[harness:cross-check plan]")
    assert code == 2 and "plan.md is missing" in err


@pytestmark_hook
def test_offered_check_is_optional_and_revising_earlier_stage_clears_checks(harness, service):
    run = start_run(service)
    approve_until_plan(harness, service, run)  # impact approved without its (offered) check
    assert "cross_check" not in state(run)["approvals"]["impact"]
    prompt(harness, service, "cross-check", "[harness:cross-check plan]")
    prompt(harness, service, "plan", "[harness:revise plan] tighten T2")
    assert "plan" in state(run)["cross_checks"]  # revising the plan itself keeps the check
    prompt(harness, service, "orchestrator", "back to grill")
    assert "plan" not in state(run).get("cross_checks", {})


def dispatch(harness, repo, agent, task, session="s1"):
    return run_hook(harness, repo, "pre-tool", "implement",
                    {"tool_name": "runSubagent", "session_id": session,
                     "tool_input": {"agentName": agent, "prompt": f"{task}: implement the task block", "description": task}})


@pytestmark_hook
def test_task_runner_must_match_the_planned_tier_and_session(harness, service):
    run = start_run(service)
    approve_until_plan(harness, service, run)
    prompt(harness, service, "implement", "[harness:approve plan] skip cross-check")

    _, out, _ = dispatch(harness, service, "Task Runner (standard)", "T1")
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny" and 'Run "Task Runner (fast)" instead' in reason
    assert dispatch(harness, service, "Task Runner (fast)", "T1")[1] is None

    _, out, _ = dispatch(harness, service, "Task Runner (deep)", "T2")
    assert "this session runs on standard" in out["hookSpecificOutput"]["permissionDecisionReason"]

    code, out, _ = prompt(harness, service, "implement", "[harness:tier deep] Continue with the next task.", session_id="s1")
    assert code == 0 and "now runs on the deep tier (Claude Opus 5.5 (copilot))" in out["hookSpecificOutput"]["additionalContext"]
    assert dispatch(harness, service, "Task Runner (deep)", "T2", session="s1")[1] is None
    # A new chat starts on the Implementer's own tier again.
    _, out, _ = dispatch(harness, service, "Task Runner (deep)", "T2", session="s2")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    _, out, _ = prompt(harness, service, "implement", "status?", session_id="s1")
    assert "Model tier: deep" in out["hookSpecificOutput"]["additionalContext"]
    code, _, err = prompt(harness, service, "implement", "[harness:tier huge]")
    assert code == 2 and "Unknown model tier" in err


@pytestmark_hook
def test_dispatch_without_tier_info_is_left_alone(harness, service):
    run = start_run(service)
    approve_until_plan(harness, service, run)
    (run / "plan.md").write_text(re.sub(r"- Tier: \w+\n", "", PLAN))
    prompt(harness, service, "implement", "[harness:approve plan] skip cross-check")
    assert dispatch(harness, service, "Task Runner (fast)", "T2")[1] is None
    assert dispatch(harness, service, "Some Other Agent", "T2")[1] is None
