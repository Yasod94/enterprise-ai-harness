from change_harness.build import build
from change_harness.config import load
from change_harness.doctor import check


def levels(findings, contains):
    return [f.level for f in findings if contains in f.message]


def test_clean_harness_has_no_errors(harness):
    findings = check(harness)
    assert not [f for f in findings if f.level == "error"], [str(f) for f in findings]


def test_placeholder_server_is_an_error(harness):
    text = (harness / "harness.yaml").read_text().replace("url: https://kb.example.internal/mcp", "url: <kb-mcp-url>")
    (harness / "harness.yaml").write_text(text)
    build(load(harness))
    assert "error" in levels(check(harness), "server kb has a placeholder")


def test_unbuilt_change_is_an_error(harness):
    (harness / "sources/agents/intake.agent.md").write_text(
        (harness / "sources/agents/intake.agent.md").read_text() + "\nnew rule\n")
    assert "error" in levels(check(harness), "plugin/ is out of date")


def test_secret_in_config_is_an_error(harness):
    text = (harness / "harness.yaml").read_text().replace(
        "  atlassian:\n", "  atlassian:\n    headers:\n      Authorization: Bearer abcdefghijklmnopqrstuvwxyz123\n", 1)
    (harness / "harness.yaml").write_text(text)
    assert "error" in levels(check(harness), "seems to contain a secret")


def test_tool_cap_is_enforced(harness):
    text = (harness / "harness.yaml").read_text().replace(
        "servers:\n", "servers:\n  huge:\n    type: http\n    url: https://huge.example/mcp\n"
                      "    stages: [impact]\n    est_tools: 200\n", 1)
    (harness / "harness.yaml").write_text(text)
    build(load(harness))
    findings = check(harness)
    assert "error" in levels(findings, "impact-analyst: about")
    assert "warn" in levels(findings, "server huge adds all its tools")


def test_missing_stage_skill_is_an_error(harness):
    text = (harness / "harness.yaml").read_text().replace("skills: []\n", "skills:\n  - name: not-there\n    stages: [grill]\n")
    (harness / "harness.yaml").write_text(text)
    build(load(harness))
    assert "error" in levels(check(harness), "loads skill not-there")


def test_server_tool_names_in_agent_text_are_flagged(harness):
    path = harness / "sources/agents/planner.agent.md"
    text = path.read_text()
    path.write_text(text + "\nLook in .codegraph/codegraph.db first.\n")
    assert not levels(check(harness), "names server tool")
    path.write_text(text + "\nCall codegraph/codegraph_search first.\n")
    assert "warn" in levels(check(harness), "names server tool codegraph/c")
