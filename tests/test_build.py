import json

import yaml

from enterprise_ai_harness.build import AGENTS_DIR, build
from enterprise_ai_harness.config import load


def frontmatter(path):
    return yaml.safe_load(path.read_text().split("---")[1])


def test_plugin_layout(harness):
    p = harness / "plugin"
    manifest = json.loads((p / "plugin.json").read_text())
    assert manifest["$schema"].endswith("/1.0.0/plugin.schema.json") and manifest["name"] == "enterprise-ai-harness"
    # 9 single agents + 3 Task Runners (one per tier) + Plan and Impact Checkers
    assert len(list((harness / AGENTS_DIR).glob("*.agent.md"))) == 14
    for skill in ("grill-me", "change-artifacts", "impact-analysis", "plan-format", "tdd"):
        assert (p / "skills" / skill / "SKILL.md").exists()
    assert (p / "scripts/harness-hook.cjs").exists() and (p / "scripts/codegraph-index.cjs").exists()
    mcp = json.loads((p / "mcp.json").read_text())
    assert mcp["mcpServers"]["kb"]["url"] == "https://kb.example.internal/mcp"
    assert "orders-db-ro" not in mcp["mcpServers"]
    assert mcp["mcpServers"]["codegraph"]["env"]["DO_NOT_TRACK"] == "1"
    market = json.loads((harness / ".github/plugin/marketplace.json").read_text())
    assert market["plugins"][0] == {"name": "enterprise-ai-harness", "source": "./plugin",
                                    "description": manifest["description"], "version": manifest["version"]}
    # Agent Plugins 1.0 names remote servers "streamable-http"; VS Code's own mcp.json says "http".
    assert {s["type"] for s in mcp["mcpServers"].values()} <= {"stdio", "streamable-http"}
    assert mcp["mcpServers"]["atlassian"]["url"] == "https://mcp.atlassian.com/v2/mcp"


def test_manual_only_skills_and_hook_config(harness):
    sdd = (harness / "plugin/skills/subagent-driven-development/SKILL.md").read_text()
    assert "disable-model-invocation: true" in sdd.split("---")[1]
    assert "disable-model-invocation" not in (harness / "sources/skills/vendor/subagent-driven-development/SKILL.md").read_text()
    cfg = json.loads((harness / "plugin/scripts/config.json").read_text())
    assert cfg["runs"] == {"dir": ".ai-harness", "commit": False}
    assert cfg["agent_scopes"]["Task Runner (deep)"] == "implement-task"
    assert cfg["agent_scopes"]["Component Scout"] == "scout" and cfg["agent_scopes"]["Plan Checker"] == "cross-check"


def test_agent_frontmatter_hooks_use_plugin_root(harness):
    fm = frontmatter(harness / AGENTS_DIR / "griller.agent.md")
    assert fm["name"] == "Griller" and fm["agents"] == ["Component Scout"]
    assert "kb/*" in fm["tools"] and "codegraph/*" in fm["tools"] and "extras-grill" in fm["tools"]
    hook = fm["hooks"]["PreToolUse"][0]
    assert hook["command"] == 'node "${PLUGIN_ROOT}/scripts/harness-hook.cjs" pre-tool grill'
    assert fm["hooks"]["UserPromptSubmit"][0]["timeout"] == 120
    body = (harness / AGENTS_DIR / "griller.agent.md").read_text()
    assert "(../../skills/grill-me/SKILL.md)" in body and "{{" not in body


def test_all_handoff_and_subagent_targets_exist(harness):
    files = list((harness / AGENTS_DIR).glob("*.agent.md"))
    names = {frontmatter(p)["name"] for p in files}
    for p in files:
        fm = frontmatter(p)
        assert all(h["agent"] in names for h in fm.get("handoffs", [])), p.name
        assert all(a in names for a in fm.get("agents", [])), p.name
        assert "agent" in fm["tools"] or not fm.get("agents"), p.name
    assert frontmatter(harness / AGENTS_DIR / "task-runner-fast.agent.md")["user-invocable"] is False


def test_build_is_idempotent_and_check_detects_drift(harness):
    cfg = load(harness)
    assert not build(cfg).changed
    agent = harness / AGENTS_DIR / "planner.agent.md"
    agent.write_text(agent.read_text() + "\nlocal edit\n")
    stray = harness / "plugin/skills/old-skill/SKILL.md"
    stray.parent.mkdir(parents=True)
    stray.write_text("x")
    report = build(cfg, check=True)
    assert f"{AGENTS_DIR}/planner.agent.md" in report.written and "plugin/skills/old-skill/SKILL.md" in report.removed
    build(cfg)
    assert "local edit" not in agent.read_text() and not stray.parent.exists()


def test_placeholder_server_falls_back_to_alt(harness):
    text = (harness / "harness.yaml").read_text().replace("url: https://kb.example.internal/mcp", "url: <kb-mcp-url>")
    (harness / "harness.yaml").write_text(text)
    build(load(harness))
    fm = frontmatter(harness / AGENTS_DIR / "griller.agent.md")
    assert "kb/*" not in fm["tools"] and "atlassian/searchConfluence" in fm["tools"]
    body = (harness / AGENTS_DIR / "griller.agent.md").read_text()
    assert "(standing in for kb)" in body and "| arch.graph | not configured (kb)" in body


def test_server_prefix_is_applied(harness):
    text = (harness / "harness.yaml").read_text().replace('server_prefix: ""', 'server_prefix: "enterprise-ai-harness."')
    (harness / "harness.yaml").write_text(text)
    cfg = load(harness)
    build(cfg)
    fm = frontmatter(harness / AGENTS_DIR / "griller.agent.md")
    assert "enterprise-ai-harness.codegraph/*" in fm["tools"]
    assert cfg.estimate_tool_count("griller") == load(harness).estimate_tool_count("griller")


def test_stage_skill_and_stage_server_are_wired(harness):
    text = (harness / "harness.yaml").read_text()
    text = text.replace("skills: []\n", "skills:\n  - name: tdd\n    stages: [plan]\n")
    text = text.replace("servers:\n", "servers:\n  splunk:\n    type: http\n    url: https://splunk.example/mcp\n"
                                      "    stages: [impact]\n    tools: [search_logs]\n", 1)
    (harness / "harness.yaml").write_text(text)
    build(load(harness))
    assert "(../../skills/tdd/SKILL.md)" in (harness / AGENTS_DIR / "planner.agent.md").read_text()
    for agent in ("impact-analyst", "component-scout"):
        assert "splunk/search_logs" in frontmatter(harness / AGENTS_DIR / f"{agent}.agent.md")["tools"]
    assert "splunk" not in (harness / AGENTS_DIR / "planner.agent.md").read_text()
