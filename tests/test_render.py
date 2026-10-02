import json

import yaml

from change_harness.cli import main
from change_harness.render import sync


def frontmatter(path):
    text = path.read_text()
    return yaml.safe_load(text.split("---")[1])


def test_init_renders_everything(initialized):
    r = initialized
    agents = sorted(p.name for p in (r / ".github/agents").glob("*.agent.md"))
    assert "change-orchestrator.agent.md" in agents and len(agents) == 10
    for skill in ("grill-me", "grilling", "change-artifacts", "impact-analysis", "plan-format", "tdd"):
        assert (r / ".github/skills" / skill / "SKILL.md").exists()
    assert (r / ".github/skills/grill-me/SOURCE.md").read_text().count("mattpocock/skills") == 1
    assert (r / ".github/hooks/harness/harness-hook.cjs").exists()
    mcp = json.loads((r / ".vscode/mcp.json").read_text())
    assert mcp["servers"]["kb"]["url"] == "https://kb.example.internal/mcp"
    assert "orders-db-ro" not in mcp["servers"]          # disabled by default
    lock = json.loads((r / ".harness/lock.json").read_text())
    assert lock["source"] == "https://github.com/acme/change-harness"
    assert ".github/agents/griller.agent.md" in lock["files"]
    assert ".changes/**/.usage.json" in (r / ".gitignore").read_text()


def test_agent_frontmatter_is_valid_yaml_with_hooks_and_handoffs(initialized):
    fm = frontmatter(initialized / ".github/agents/griller.agent.md")
    assert fm["name"] == "Griller"
    assert "kb/*" in fm["tools"] and "codegraph/*" in fm["tools"] and "extras-grill" in fm["tools"]
    assert fm["agents"] == ["Component Scout"]
    cmd = fm["hooks"]["PreToolUse"][0]["command"]
    assert cmd == "node .github/hooks/harness/harness-hook.cjs pre-tool grill"
    labels = [h["label"] for h in fm["handoffs"]]
    assert labels[0].startswith("Approve") and "Revise" in labels and "Stop" in labels
    assert fm["handoffs"][0]["agent"] == "Impact Analyst"
    body = (initialized / ".github/agents/griller.agent.md").read_text()
    assert "(../skills/grilling/SKILL.md)" in body and "{{" not in body


def test_subagents_are_hidden_from_picker(initialized):
    assert frontmatter(initialized / ".github/agents/task-runner.agent.md")["user-invocable"] is False
    assert "user-invocable" not in frontmatter(initialized / ".github/agents/planner.agent.md")


def test_sync_is_idempotent(initialized):
    report = sync(initialized)
    assert report.written == [] and report.removed == [] and report.conflicts == []
    assert main(["sync", "--repo", str(initialized), "--check"]) == 0


def test_hand_edit_is_reported_not_overwritten(initialized):
    agent = initialized / ".github/agents/planner.agent.md"
    agent.write_text(agent.read_text() + "\nmy tweak\n")
    report = sync(initialized)
    assert ".github/agents/planner.agent.md" in report.conflicts
    assert "my tweak" in agent.read_text()
    sync(initialized, force=True)
    assert "my tweak" not in agent.read_text()


def test_repo_skill_overrides_packaged_one(repo):
    custom = repo / ".github/skills/tdd/SKILL.md"
    custom.parent.mkdir(parents=True)
    custom.write_text("---\nname: tdd\ndescription: our own tdd rules\n---\nours\n")
    main(["init", "--repo", str(repo)])
    assert custom.read_text().endswith("ours\n")
    lock = json.loads((repo / ".harness/lock.json").read_text())
    assert not any(p.startswith(".github/skills/tdd/") for p in lock["files"])


def test_foreign_mcp_servers_are_kept(repo):
    (repo / ".vscode").mkdir()
    (repo / ".vscode/mcp.json").write_text('{\n  // ours\n  "servers": {"mine": {"type": "stdio", "command": "x",}},\n}')
    main(["init", "--repo", str(repo)])
    servers = json.loads((repo / ".vscode/mcp.json").read_text())["servers"]
    assert "mine" in servers and "github-ro" in servers


def test_agents_md_keeps_existing_text_and_renders_commands(repo):
    (repo / "AGENTS.md").write_text("# Team rules\n\nUse Java 17.\n")
    ext = repo / ".harness/extensions.yaml"
    ext.parent.mkdir()
    ext.write_text("commands:\n  test: ./gradlew test\n")
    main(["init", "--repo", str(repo)])
    text = (repo / "AGENTS.md").read_text()
    assert text.startswith("# Team rules") and "Use Java 17." in text
    assert "- test: `./gradlew test`" in text
    main(["sync", "--repo", str(repo)])
    assert (repo / "AGENTS.md").read_text().count("harness:start") == 1


def test_placeholder_server_falls_back_to_alt(repo):
    main(["init", "--repo", str(repo)])  # kb still has its placeholder
    fm = frontmatter(repo / ".github/agents/griller.agent.md")
    assert "kb/*" not in fm["tools"] and "atlassian/searchConfluence" in fm["tools"]
    assert "atlassian/*" not in fm["tools"]
    body = (repo / ".github/agents/griller.agent.md").read_text()
    assert "`atlassian/searchConfluence`, `atlassian/getConfluenceContent` (standing in for kb)" in body
    assert "| arch.graph | not configured (kb)" in body
    assert "kb" not in json.loads((repo / ".vscode/mcp.json").read_text())["servers"]


def test_capability_override_and_narrowed_tools(initialized):
    ext = initialized / ".harness/extensions.yaml"
    ext.write_text(ext.read_text() + (
        "  inventory-db:\n    type: stdio\n    command: inv-mcp\n    est_tools: 3\n"
        "capabilities:\n  db.schema:\n    bind: inventory-db\n  tickets.write:\n    tools: [addCommentToJiraIssue]\n"))
    sync(initialized)
    scout = frontmatter(initialized / ".github/agents/component-scout.agent.md")
    assert "inventory-db/*" in scout["tools"]
    reviewer = frontmatter(initialized / ".github/agents/reviewer.agent.md")
    assert "atlassian/addCommentToJiraIssue" in reviewer["tools"] and "atlassian/*" not in reviewer["tools"]


def test_removed_skill_is_cleaned_up(initialized):
    src = initialized / "team-skills/kafka-review"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: kafka-review\ndescription: review kafka contracts\n---\nbody\n")
    ext = initialized / ".harness/extensions.yaml"
    original = ext.read_text()
    ext.write_text(original + "skills:\n  - name: kafka-review\n    source: team-skills/kafka-review\n    stages: [plan]\n")
    sync(initialized)
    assert (initialized / ".github/skills/kafka-review/SKILL.md").exists()
    assert "(../skills/kafka-review/SKILL.md)" in (initialized / ".github/agents/planner.agent.md").read_text()
    ext.write_text(original)
    report = sync(initialized)
    assert ".github/skills/kafka-review/SKILL.md" in report.removed
    assert not (initialized / ".github/skills/kafka-review").exists()


def test_agent_tool_added_when_subagents_listed(initialized):
    fm = frontmatter(initialized / ".github/agents/change-orchestrator.agent.md")
    assert "agent" in fm["tools"] and fm["agents"] == ["Harness Check"]
    names = {frontmatter(p)["name"] for p in (initialized / ".github/agents").glob("*.agent.md")}
    for p in (initialized / ".github/agents").glob("*.agent.md"):
        fm = frontmatter(p)
        assert all(h["agent"] in names for h in fm.get("handoffs", [])), p.name
        assert all(a in names for a in fm.get("agents", [])), p.name
