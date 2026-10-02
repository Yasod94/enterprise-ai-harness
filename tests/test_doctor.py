from change_harness.doctor import check


def levels(findings, contains):
    return [f.level for f in findings if contains in f.message]


def test_clean_repo_has_no_errors(initialized):
    findings = check(initialized)
    assert not [f for f in findings if f.level == "error"], [str(f) for f in findings]


def test_placeholder_server_is_an_error(repo):
    from change_harness.cli import main
    main(["init", "--repo", str(repo)])
    assert "error" in levels(check(repo), "server kb has a placeholder")


def test_secret_in_mcp_json_is_an_error(initialized):
    mcp = initialized / ".vscode/mcp.json"
    mcp.write_text(mcp.read_text().replace('"servers": {', '"servers": {"leak": {"type": "http", "url": "https://x", '
                                                         '"headers": {"Authorization": "Bearer abcdefghijklmnopqrstuvwxyz123"}},', 1))
    assert "error" in levels(check(initialized), "seems to contain a secret")


def test_tool_cap_is_enforced(initialized):
    ext = initialized / ".harness/extensions.yaml"
    ext.write_text(ext.read_text() + "  huge:\n    type: http\n    url: https://huge.example/mcp\n"
                                     "    stages: [impact]\n    est_tools: 200\n")
    from change_harness.render import sync
    sync(initialized)
    findings = check(initialized)
    assert "error" in levels(findings, "impact-analyst: about")
    assert "warn" in levels(findings, "repo server huge adds all its tools")


def test_hand_edit_shows_up(initialized):
    agent = initialized / ".github/agents/intake.agent.md"
    agent.write_text(agent.read_text() + "\nlocal change\n")
    assert "error" in levels(check(initialized), "intake.agent.md was edited by hand")


def test_missing_stage_skill_is_an_error(initialized):
    ext = initialized / ".harness/extensions.yaml"
    ext.write_text(ext.read_text() + "skills:\n  - name: not-there\n    stages: [grill]\n")
    assert "error" in levels(check(initialized), "loads skill not-there")
