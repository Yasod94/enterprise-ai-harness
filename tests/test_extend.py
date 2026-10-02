import pytest
import yaml

from change_harness.cli import main
from change_harness.extend import ExtendError, add_server, insert_entry, parse_github


def test_insert_entry_keeps_comments():
    text = "# top\nskills: []\n#  - name: example\nservers: {}\n"
    out = insert_entry(text, "skills", "  - name: a\n")
    assert out.startswith("# top\nskills:\n  - name: a\n#  - name: example")
    assert yaml.safe_load(out)["skills"] == [{"name": "a"}]
    out2 = insert_entry(out, "skills", "  - name: b\n")
    assert [s["name"] for s in yaml.safe_load(out2)["skills"]] == ["b", "a"]


def test_insert_entry_adds_missing_key():
    out = insert_entry("review: {}\n", "servers", "  x:\n    type: http\n")
    assert yaml.safe_load(out)["servers"] == {"x": {"type": "http"}}


def test_insert_entry_refuses_inline_values():
    with pytest.raises(ExtendError):
        insert_entry("skills: [a]\n", "skills", "  - name: b\n")


def test_parse_github_forms():
    assert parse_github("mattpocock/skills") == ("mattpocock", "skills", None)
    assert parse_github("github:mattpocock/skills@abc123") == ("mattpocock", "skills", "abc123")
    assert parse_github("https://github.com/obra/superpowers.git") == ("obra", "superpowers", None)


def test_add_server_cli_renders_tools_into_stage_agents(initialized):
    ext_file = initialized / ".harness/extensions.yaml"
    ext_file.write_text("# Extra MCP servers live here\n" + ext_file.read_text())
    code = main(["add", "server", "splunk", "--repo", str(initialized), "--url", "https://splunk.example/mcp",
                 "--stages", "impact", "--tools", "search_logs"])
    assert code == 0
    ext = (initialized / ".harness/extensions.yaml").read_text()
    assert "# Extra MCP servers" in ext  # comments survive
    assert yaml.safe_load(ext)["servers"]["splunk"]["tools"] == ["search_logs"]
    for agent in ("impact-analyst", "component-scout"):
        text = (initialized / f".github/agents/{agent}.agent.md").read_text()
        assert "splunk/search_logs" in text
    assert "splunk" not in (initialized / ".github/agents/planner.agent.md").read_text()


def test_add_server_rejects_duplicates_and_missing_target(initialized):
    add_server(initialized, "x", url="https://x.example/mcp", stages=["plan"])
    with pytest.raises(ExtendError):
        add_server(initialized, "x", url="https://x.example/mcp")
    with pytest.raises(ExtendError):
        add_server(initialized, "y")


def test_add_local_skill_with_stage(initialized):
    src = initialized / "skills-src/event-contracts"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: event-contracts\ndescription: event contract rules\n---\nrules\n")
    code = main(["add", "skill", "skills-src/event-contracts", "--repo", str(initialized), "--stages", "plan,verify"])
    assert code == 0
    assert (initialized / ".github/skills/event-contracts/SKILL.md").exists()
    for agent in ("planner", "reviewer"):
        assert "(../skills/event-contracts/SKILL.md)" in (initialized / f".github/agents/{agent}.agent.md").read_text()
    assert main(["add", "skill", "skills-src/event-contracts", "--repo", str(initialized)]) == 2
