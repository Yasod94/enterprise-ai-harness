"""Defaults and personal settings: what works out of the box, what a developer can change
at run time (the hook reads their file), and what needs a personal build."""

import json

import pytest
import yaml

from conftest import NODE, REPO, git, run_hook, start_run
from enterprise_ai_harness.build import AGENTS_DIR, build
from enterprise_ai_harness.cli import main
from enterprise_ai_harness.config import ConfigError, load
from enterprise_ai_harness.doctor import check
from enterprise_ai_harness.personal import TEMPLATE_FILE, describe, template

needs_node = pytest.mark.skipif(NODE is None, reason="node is required for hook tests")

PLAN = "# Plan\n\n## Tasks\n\n### T1. Config\n\n- Tier: standard\n- Files: src/OrderPublisher.java\n"


def write_personal(repo, text, where="home"):
    path = (repo.parent / "home/.copilot/enterprise-ai-harness.local.yaml") if where == "home" else repo / "harness.local.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def approve_plan(harness, repo, run):
    for stage, files in (("intake", {"requirement.md": "r\n"}), ("grill", {"clarifications.md": "c\n"}),
                         ("impact", {"impact.md": "i\n", "impact.json": json.dumps(
                             {"components": [{"name": "orders", "files": ["src/OrderPublisher.java"]}]})}),
                         ("plan", {"plan.md": PLAN})):
        for name, text in files.items():
            (run / name).write_text(text)
        extra = " skip cross-check" if stage == "plan" else ""
        code, _, err = run_hook(harness, repo, "prompt", stage, {"prompt": f"[harness:approve {stage}]{extra}"})
        assert code == 0, err


def edit(harness, repo, scope, rel):
    _, out, _ = run_hook(harness, repo, "pre-tool", scope,
                         {"tool_name": "create_file", "tool_input": {"filePath": str(repo / rel), "content": "x"}})
    return out["hookSpecificOutput"]["permissionDecision"] if out else None


# ---------- defaults ----------

def test_shipped_configuration_works_without_edits():
    cfg = load(REPO)
    assert not [f for f in check(REPO) if f.level == "error"], "the shipped harness.yaml should pass doctor"
    assert cfg.servers["kb"].get("enabled") is False  # off until a team sets its URL
    assert "atlassian" in cfg.capabilities["docs.search"]["alt"]
    assert cfg.personal_overrides == ["review", "budgets", "hooks.audit", "hooks.out_of_plan_files", "runs.commit",
                                      "code_index.seed_refresh", "code_index.seed_max_age_days", "code_index.sync"]


def test_template_is_built_and_current(harness):
    text = (harness / TEMPLATE_FILE).read_text()
    assert text == template(load(harness))
    assert "#  implement: per-task" in text and "#  out_of_plan_files: ask" in text
    assert (REPO / TEMPLATE_FILE).read_text() == template(load(REPO))


# ---------- run-time overrides (no rebuild) ----------

@needs_node
def test_personal_file_changes_review_budget_and_out_of_plan_rule(harness, service):
    run = start_run(service)
    approve_plan(harness, service, run)
    assert edit(harness, service, "implement-task", "src/Other.java") == "ask"  # team default
    write_personal(service, "hooks:\n  out_of_plan_files: allow\nbudgets:\n  intake:\n    tool_calls: 3\n")
    assert edit(harness, service, "implement-task", "src/Other.java") is None
    write_personal(service, "hooks:\n  out_of_plan_files: deny\n", where="repo")  # the clone's file wins
    assert edit(harness, service, "implement-task", "src/Other.java") == "deny"


@needs_node
def test_invalid_or_disallowed_personal_values_are_ignored(harness, service):
    run = start_run(service)
    approve_plan(harness, service, run)
    write_personal(service, "hooks:\n  out_of_plan_files: whatever\n  edit_tools: nothing\n")
    assert edit(harness, service, "implement-task", "src/Other.java") == "ask"
    # The team can take a setting off the list.
    text = (harness / "harness.yaml").read_text().replace("  - hooks.out_of_plan_files    # ask | allow | deny for edits outside impact.json\n", "")
    (harness / "harness.yaml").write_text(text)
    build(load(harness))
    write_personal(service, "hooks:\n  out_of_plan_files: allow\n")
    assert edit(harness, service, "implement-task", "src/Other.java") == "ask"


@needs_node
def test_switching_runs_commit_updates_the_git_excludes(harness, service):
    start_run(service)
    run_hook(harness, service, "prompt", "intake", {"prompt": "hi"})
    exclude = service / ".git/info/exclude"
    assert ".ai-harness/" in exclude.read_text().splitlines()
    write_personal(service, "runs:\n  commit: true\n", where="repo")
    run_hook(harness, service, "prompt", "intake", {"prompt": "hi"})
    lines = exclude.read_text().splitlines()
    assert ".ai-harness/" not in lines and ".ai-harness/*/.usage.json" in lines
    assert ".ai-harness/ABC-123/state.json" in git(service, "status", "--porcelain", "--untracked-files=all")
    assert lines.count("# enterprise-ai-harness") == 1
    (service / "harness.local.yaml").unlink()
    run_hook(harness, service, "prompt", "intake", {"prompt": "hi"})
    lines = exclude.read_text().splitlines()
    assert ".ai-harness/" in lines and ".ai-harness/*/.usage.json" not in lines


@needs_node
def test_personal_sync_command_reaches_the_code_graph(harness, service):
    import time
    from conftest import make_index
    run = start_run(service)
    approve_plan(harness, service, run)
    make_index(service)
    write_personal(service, "code_index:\n  sync: touch .codegraph/personal-sync\n")
    event = {"tool_name": "create_file", "tool_input": {"filePath": str(service / "src/OrderPublisher.java")},
             "tool_response": "ok"}
    run_hook(harness, service, "post-tool", "implement-task", event)
    deadline = time.time() + 10
    while not (service / ".codegraph/personal-sync").exists() and time.time() < deadline:
        time.sleep(0.1)
    assert (service / ".codegraph/personal-sync").exists()


# ---------- personal build (everything else) ----------

def test_describe_sorts_a_personal_file(harness):
    cfg = load(harness)
    data = {"review": {"implement": "per-batch-2"}, "hooks": {"audit": True, "edit_tools": "x"},
            "models": {"agents": {"planner": "deep"}}, "code_index": {"seed_refresh": "manual"}}
    got = describe(cfg, data)
    assert got["now"] == ["review.implement", "hooks.audit", "code_index.seed_refresh"]
    assert got["build"] == ["hooks.edit_tools", "models.agents"]
    assert describe(cfg, data, repo_level=True)["ignored"] == ["hooks.edit_tools", "models.agents"]


def test_overlay_merges_removes_and_validates(harness, tmp_path):
    f = tmp_path / "me.yaml"
    f.write_text("models:\n  agents:\n    planner: deep\nservers:\n  ctx:\n    enabled: false\n"
                 "  github-ro:\n    headers: ~\ncode_index:\n  seed_refresh: manual\n")
    cfg = load(harness, overlay=f)
    assert cfg.models["agents"]["planner"] == "deep" and cfg.models["agents"]["griller"] == "deep"
    assert cfg.servers["ctx"]["enabled"] is False and "headers" not in cfg.servers["github-ro"]
    assert cfg.code_index["seed_refresh"] == "manual"
    f.write_text("models:\n  agents:\n    planner: huge\n")
    with pytest.raises(ConfigError, match="unknown tier huge"):
        load(harness, overlay=f)


def test_install_user_applies_personal_settings_without_touching_plugin(harness, tmp_path):
    copilot, vscode = tmp_path / "copilot", tmp_path / "vscode"
    before = (harness / AGENTS_DIR / "planner.agent.md").read_text()
    (copilot).mkdir()
    (copilot / "enterprise-ai-harness.local.yaml").write_text(
        "models:\n  agents:\n    planner: deep\n    griller: standard\nservers:\n  ctx:\n    enabled: false\n")
    code = main(["--root", str(harness), "install-user", "--copilot-dir", str(copilot), "--vscode-user-dir", str(vscode)])
    assert code == 0
    planner = yaml.safe_load((copilot / "agents/planner.agent.md").read_text().split("---")[1])
    assert planner["model"][0] == "Claude Opus 5.5 (copilot)"
    assert "ctx" not in json.loads((vscode / "mcp.json").read_text())["servers"]
    assert (harness / AGENTS_DIR / "planner.agent.md").read_text() == before  # the committed build is untouched
    # --team-only installs the team build.
    main(["--root", str(harness), "install-user", "--copilot-dir", str(copilot), "--vscode-user-dir", str(vscode), "--team-only"])
    planner = yaml.safe_load((copilot / "agents/planner.agent.md").read_text().split("---")[1])
    assert planner["model"][0] == "Claude Sonnet 5.5 (copilot)"


def test_config_command_shows_and_initialises(harness, tmp_path, capsys):
    copilot = tmp_path / "copilot"
    assert main(["--root", str(harness), "config", "--init", "--copilot-dir", str(copilot)]) == 0
    written = (copilot / "enterprise-ai-harness.local.yaml").read_text()
    assert written == template(load(harness))
    assert main(["--root", str(harness), "config", "--init", "--copilot-dir", str(copilot)]) == 1  # never overwrites
    (copilot / "enterprise-ai-harness.local.yaml").write_text("review:\n  implement: per-batch-2\nmodels:\n  task_default: deep\n")
    capsys.readouterr()
    assert main(["--root", str(harness), "config", "review", "--copilot-dir", str(copilot)]) == 0
    out = capsys.readouterr().out
    assert "apply now (the hook reads them):   review.implement" in out
    assert "need `harness install-user`:       models.task_default" in out
    assert "implement: per-batch-2" in out


def test_runs_dir_reaches_agent_and_skill_text(harness):
    text = (harness / "harness.yaml").read_text().replace("  dir: .ai-harness ", "  dir: .change-runs ")
    (harness / "harness.yaml").write_text(text)
    build(load(harness))
    orchestrator = (harness / AGENTS_DIR / "change-orchestrator.agent.md").read_text()
    skill = (harness / "plugin/skills/change-artifacts/SKILL.md").read_text()
    assert ".change-runs/current" in orchestrator and ".ai-harness/" not in orchestrator
    assert ".change-runs/<run-id>/" in skill and ".ai-harness/" not in skill
    assert json.loads((harness / "plugin/scripts/config.json").read_text())["runs"]["dir"] == ".change-runs"


def test_readme_defaults_match_harness_yaml():
    """The README's default tables are written by hand; this keeps them honest."""
    readme = (REPO / "README.md").read_text()
    cfg = load(REPO)
    for scope, spec in cfg.raw["budgets"].items():
        row = next((line for line in readme.splitlines() if f"`{scope}`" in line and "|" in line), "")
        assert str(spec["tool_calls"]) in row, f"README budget row for {scope}"
    for tier, spec in cfg.tiers.items():
        first = spec["models"][0].removesuffix(" (copilot)")
        assert f"`models.tiers.{tier}` | {first}" in readme, tier
    for key in cfg.personal_overrides:
        assert key in readme, key
    index = cfg.code_index
    assert f"| `seed_refresh` | `{index['seed_refresh']}` |" in readme
    assert f"`{index['sync']}`" in readme
    assert "SETUP.md" not in readme
