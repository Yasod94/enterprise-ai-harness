import pytest
import yaml

from change_harness.cli import main
from change_harness.config import load
from change_harness.extend import ExtendError, add_server, add_skill, insert_entry, parse_github, rebind_capability


def test_insert_entry_keeps_comments():
    text = "# top\nskills: []\n#  - name: example\nservers: {}\n"
    out = insert_entry(text, "skills", "  - name: a\n")
    assert out.startswith("# top\nskills:\n  - name: a\n#  - name: example")
    assert [s["name"] for s in yaml.safe_load(insert_entry(out, "skills", "  - name: b\n"))["skills"]] == ["b", "a"]


def test_insert_entry_refuses_inline_values():
    with pytest.raises(ExtendError):
        insert_entry("skills: [a]\n", "skills", "  - name: b\n")


def test_rebind_capability_only_touches_that_capability():
    text = ("capabilities:\n  code.graph:\n    bind: codegraph   # note\n    fallback: x\n"
            "  db.schema:\n    bind: orders-db-ro\nstages: []\n")
    out = rebind_capability(text, "db.schema", "inv-db")
    data = yaml.safe_load(out)
    assert data["capabilities"]["db.schema"]["bind"] == "inv-db"
    assert data["capabilities"]["code.graph"]["bind"] == "codegraph" and "# note" in out
    with pytest.raises(ExtendError):
        rebind_capability(text, "missing.cap", "x")


def test_parse_github_forms():
    assert parse_github("mattpocock/skills") == ("mattpocock", "skills", None)
    assert parse_github("github:mattpocock/skills@abc123") == ("mattpocock", "skills", "abc123")
    assert parse_github("https://github.com/obra/superpowers.git") == ("obra", "superpowers", None)


def test_add_server_with_stages_and_capability(harness):
    code = main(["--root", str(harness), "add", "server", "inv-db", "--command", "inv-mcp", "--stages", "impact",
                 "--tools", "describe_table", "--capability", "db.schema"])
    assert code == 0
    raw = (harness / "harness.yaml").read_text()
    assert "# Team configuration for the change-harness" in raw  # comments survive
    cfg = load(harness)
    assert cfg.servers["inv-db"]["tools"] == ["describe_table"]
    assert cfg.capabilities["db.schema"]["bind"] == "inv-db"
    scout = (harness / "plugin/com.github.copilot/agents/component-scout.agent.md").read_text()
    assert "inv-db/describe_table" in scout
    with pytest.raises(ExtendError):
        add_server(cfg, "inv-db", url="https://x.example/mcp")
    with pytest.raises(ExtendError):
        add_server(cfg, "y")


def test_add_local_skill_with_stages(harness, tmp_path):
    src = tmp_path / "event-contracts"
    src.mkdir()
    (src / "SKILL.md").write_text("---\nname: event-contracts\ndescription: event contract rules\n---\nrules\n")
    code = main(["--root", str(harness), "add", "skill", str(src), "--stages", "plan,verify"])
    assert code == 0
    assert (harness / "sources/skills/team/event-contracts/SKILL.md").exists()
    assert (harness / "plugin/skills/event-contracts/SKILL.md").exists()
    for agent in ("planner", "reviewer"):
        text = (harness / f"plugin/com.github.copilot/agents/{agent}.agent.md").read_text()
        assert "(../../skills/event-contracts/SKILL.md)" in text
    with pytest.raises(ExtendError):
        add_skill(load(harness), str(src))
    with pytest.raises(ExtendError):
        add_skill(load(harness), str(tmp_path / "nope-not-github"), stages=["bogus"])
