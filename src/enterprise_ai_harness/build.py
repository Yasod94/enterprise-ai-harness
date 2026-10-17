"""Build the VS Code agent plugin (plugin/) and the marketplace manifest from sources/.

Layout follows the Agent Plugins 1.0 format that VS Code reads:

    plugin/plugin.json
    plugin/mcp.json
    plugin/skills/<name>/SKILL.md
    plugin/scripts/                      hook and code graph scripts, config.json
    plugin/com.github.copilot/agents/    custom agents
    .github/plugin/marketplace.json      lets VS Code find the plugin in this repo
"""

from __future__ import annotations

import json
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from enterprise_ai_harness.config import CHECKER_AGENT, Config

PLUGIN_DIR = "plugin"
AGENTS_DIR = f"{PLUGIN_DIR}/com.github.copilot/agents"
SKILLS_DIR = f"{PLUGIN_DIR}/skills"
SCRIPTS_DIR = f"{PLUGIN_DIR}/scripts"
MARKETPLACE = ".github/plugin/marketplace.json"
PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
HOOK_CMD = 'node "${PLUGIN_ROOT}/scripts/harness-hook.cjs"'
SKILL_LINK_PREFIX = "../../skills"  # from com.github.copilot/agents/ to skills/

HOOK_EVENTS = [
    ("UserPromptSubmit", "prompt", 120),   # may refresh the code graph seed
    ("PreToolUse", "pre-tool", 15),
    ("PostToolUse", "post-tool", 15),
    ("PreCompact", "pre-compact", 15),
]


def q(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def yaml_list(items: list[str]) -> str:
    return "[" + ", ".join("'" + i.replace("'", "''") + "'" for i in items) + "]"


# ---------- agents ----------

@dataclass
class Variant:
    """One rendered agent file. Most agents have one; a by-task Task Runner has one per
    tier, and the Checker has one per cross-checked stage."""
    agent_id: str
    file_id: str
    name: str
    tier: str | None = None
    stage: str | None = None


CHECKLISTS = {
    "plan": [
        "Every component in `impact.json` has a task, or a line under Out of scope saying why not.",
        "Every acceptance criterion in `requirement.md` maps to a task's Check or to the Test plan.",
        "Every decision in `clarifications.md` that affects code is reflected, and no task contradicts one.",
        "Each task's Files list covers its Steps, and its Check would fail without the change.",
        "Task order respects dependencies: schema and contract changes before their producers and consumers.",
        "Each task's Tier fits the tier table below; flag a task that looks harder than its Tier.",
        "Rollout and rollback work for this change; Risks name every new dependency, migration and contract change.",
        "Nothing in the plan goes beyond the requirement.",
    ],
    "impact": [
        "Every entity and behavior in `requirement.md` and `clarifications.md` was traced.",
        "Consumers of each changed event, topic, table and API are listed, or ruled out with evidence.",
        "Each confidence level matches its evidence; low-confidence items appear as questions.",
        "`impact.json` and `impact.md` agree.",
    ],
}


def agent_name(cfg: Config, agent_id: str) -> str:
    return cfg.agents[agent_id].get("name", agent_id)


def variants(cfg: Config, agent_id: str) -> list[Variant]:
    base = agent_name(cfg, agent_id)
    if agent_id == CHECKER_AGENT:
        return [Variant(agent_id, f"{stage}-checker", f"{stage.capitalize()} {base}", spec["checker"], stage)
                for stage, spec in cfg.cross_checks().items()]
    if cfg.is_by_task(agent_id):
        return [Variant(agent_id, f"{agent_id}-{tier}", f"{base} ({tier})", tier) for tier in cfg.tier_order]
    return [Variant(agent_id, agent_id, base, cfg.agent_tier(agent_id) if cfg.agent_tier(agent_id) in cfg.tiers else None)]


def all_variants(cfg: Config) -> list[Variant]:
    return [v for agent_id in cfg.agents for v in variants(cfg, agent_id)]


def subagent_names(cfg: Config, agent_id: str) -> list[str]:
    return [v.name for v in variants(cfg, agent_id)]


def stage_agent(cfg: Config, stage: str) -> str:
    return next(s["agent"] for s in cfg.stages if s["id"] == stage)


def next_stage(cfg: Config, stage: str) -> str | None:
    ids = cfg.stage_ids
    i = ids.index(stage)
    return ids[i + 1] if i + 1 < len(ids) else None


def render_frontmatter(cfg: Config, v: Variant) -> str:
    agent = cfg.agents[v.agent_id]
    lines = [
        f"# Built by enterprise-ai-harness from sources/agents/{v.agent_id}.agent.md. Do not edit here.",
        f"name: {q(v.name)}",
        f"description: {q(agent.get('description', ''))}",
        f"tools: {yaml_list(cfg.tools_for(v.agent_id))}",
    ]
    subagents = agent.get("subagents") or []
    if subagents:
        lines.append(f"agents: {yaml_list([n for s in subagents for n in subagent_names(cfg, s)])}")
    models = cfg.agent_models(v.agent_id, v.tier)
    if models:
        lines.append(f"model: {yaml_list(models)}")
    if agent.get("user_invocable") is False:
        lines.append("user-invocable: false")
    lines.append("hooks:")
    for event, mode, timeout in HOOK_EVENTS:
        lines += [
            f"  {event}:",
            "    - type: command",
            f"      command: {q(f'{HOOK_CMD} {mode} {agent['scope']}')}",
            f"      timeout: {timeout}",
        ]
    return "\n".join(lines)


def handoff(label: str, agent: str, prompt: str, send: bool, model: str | None = None) -> str:
    lines = [f"  - label: {q(label)}", f"    agent: {q(agent)}", f"    prompt: {q(prompt)}",
             f"    send: {'true' if send else 'false'}"]
    if model:
        lines.append(f"    model: {q(model)}")
    return "\n".join(lines)


def render_cross_check_handoffs(cfg: Config, agent_id: str) -> str:
    stage = cfg.stage_of_scope(cfg.agents[agent_id].get("scope", ""))
    spec = cfg.cross_checks().get(stage or "")
    if not spec:
        return ""
    checker = next(v for v in variants(cfg, CHECKER_AGENT) if v.stage == stage)
    files = ", ".join(next(s for s in cfg.stages if s["id"] == stage).get("artifacts", []))
    out = [handoff(f"Cross-check with the {spec['checker']} model", checker.name,
                   f"[harness:cross-check {stage}] Check {files} before the person approves it.",
                   True, cfg.first_model(spec["checker"]))]
    nxt = next_stage(cfg, stage)
    if spec["required"]:
        target = agent_name(cfg, stage_agent(cfg, nxt)) if nxt else agent_name(cfg, "change-orchestrator")
        out.append(handoff("Approve and skip the cross-check", target,
                           f"[harness:approve {stage}] skip cross-check. Approved; continue with the {nxt or 'next'} stage.",
                           True))
    return "\n".join(out)


def render_tier_handoffs(cfg: Config, agent_id: str) -> str:
    runners = [s for s in cfg.agents[agent_id].get("subagents") or [] if cfg.is_by_task(s)]
    if not runners:
        return ""
    return "\n".join(
        handoff(f"Continue on the {tier} model", agent_name(cfg, agent_id),
                f"[harness:tier {tier}] Continue with the next task.", True, cfg.first_model(tier))
        for tier in cfg.tier_order)


def render_task_tiers(cfg: Config, intro: bool = True) -> str:
    if not cfg.tiers:
        return ""
    rows = ["Give every task a `Tier`. The person can change any Tier before approving the plan, and the "
            "Implementer runs each task on its Tier's model.", ""] if intro else []
    rows += ["| Tier | Models | Use for |", "| --- | --- | --- |"]
    for tier, spec in cfg.tiers.items():
        rows.append(f"| {tier} | {', '.join(spec['models'])} | {spec['use_for']} |")
    rows += ["", f"When nothing in the table decides it, the tier is {cfg.task_default}."]
    return "\n".join(rows)


def task_runner_of(cfg: Config, agent_id: str) -> str | None:
    return next((s for s in cfg.agents[agent_id].get("subagents") or [] if cfg.is_by_task(s)), None)


def render_task_runner_dispatch(cfg: Config, agent_id: str) -> str:
    if not task_runner_of(cfg, agent_id):
        return "Run the Task Runner subagent"
    return "Run the Task Runner for the task's `Tier` (see Task tiers below), starting its prompt with the task id,"


def render_task_tier_rules(cfg: Config, agent_id: str) -> str:
    runner = task_runner_of(cfg, agent_id)
    if not runner:
        return ""
    lines = ["## Task tiers", "", "Each task in the approved `plan.md` has a `Tier`. Run the matching Task Runner:", ""]
    lines += [f"- {v.tier}: {q(v.name)}" for v in variants(cfg, runner)]
    lines += ["",
              "VS Code does not start a subagent on a model that costs more than this session's model. The hook's "
              "status line gives this session's tier. When the next task's Tier is above it, stop and show the card "
              "with the Question line \"T<n> needs the <tier> model\"; the person presses \"Continue on the <tier> "
              "model\". Lower tiers run from any session. The hook refuses a Task Runner whose tier differs from the "
              "task's Tier in the approved plan."]
    return "\n".join(lines)


def render_capabilities(cfg: Config, agent_id: str) -> str:
    agent = cfg.agents[agent_id]
    caps = agent.get("capabilities") or []
    extra = cfg.stage_servers_for(agent_id)
    stage = cfg.stage_of_scope(agent.get("scope", ""))
    if not caps and not extra:
        return ""
    rows = ["## Capabilities", "", "| Capability | Tools | If unavailable |", "| --- | --- | --- |"]
    for cap in caps:
        spec = cfg.capabilities[cap]
        tools = cfg.capability_tools(cap)
        if tools:
            cell = ", ".join(f"`{t}`" for t in tools)
            if cfg.effective_bind(cap) != spec["bind"]:
                cell += f" (standing in for {spec['bind']})"
        else:
            cell = f"not configured ({spec['bind']})"
        rows.append(f"| {cap} | {cell} | {spec.get('fallback', 'Say it was unavailable.')} |")
    for name in extra:
        if cfg.server_usable(name):
            rows.append(f"| team: {name} | {', '.join(f'`{t}`' for t in cfg.server_tools(name))} | Say it was unavailable. |")
    if agent.get("show_setup"):
        setups = [(c, cfg.capabilities[c]["setup"]) for c in caps if cfg.capabilities[c].get("setup")]
        if setups:
            rows += ["", "Setup commands (run in the terminal; it asks for approval):", ""]
            rows += [f"- {c}: `{cmd}`" for c, cmd in setups]
    if stage:
        rows += ["", f"Tools the person added for themselves are in the `extras-{stage}` tool set, if it exists."]
    return "\n".join(rows)


def render_skill_refs(cfg: Config, agent_id: str) -> str:
    skills = cfg.skills_for(agent_id)
    if not skills:
        return ""
    lines = ["Before you start, read and follow these skills:", ""]
    lines += [f"- [{s}]({SKILL_LINK_PREFIX}/{s}/SKILL.md)" for s in skills]
    return "\n".join(lines)


def render_check_placeholders(cfg: Config, v: Variant, text: str) -> str:
    stage = v.stage
    if not stage:
        return text
    idx = cfg.stage_ids.index(stage)
    arts = next(s for s in cfg.stages if s["id"] == stage).get("artifacts", [])
    inputs = [a for s in cfg.stages[:idx] for a in s.get("artifacts", [])]
    nxt = next_stage(cfg, stage)
    checklist = CHECKLISTS.get(stage, ["The artifact follows its stage's skill and template, and is consistent with the earlier artifacts."])
    extra = "\n\n   Tier table:\n\n" + "\n".join("   " + l if l else "" for l in render_task_tiers(cfg, intro=False).split("\n")) if stage == "plan" and cfg.tiers else ""
    subs = {
        "stage": stage,
        "Stage": stage.capitalize(),
        "artifacts": ", ".join(f"`{a}`" for a in arts),
        "inputs": ", ".join(f"`{a}`" for a in inputs) or "requirement",
        "stage_agent": q(agent_name(cfg, stage_agent(cfg, stage))),
        "next_agent": q(agent_name(cfg, stage_agent(cfg, nxt)) if nxt else agent_name(cfg, "change-orchestrator")),
        "next_stage": nxt or "next",
        "checklist": "\n".join(f"   - {c}" for c in checklist) + extra,
    }
    return re.sub(r"\{\{check:(\w+)\}\}", lambda m: subs.get(m.group(1), m.group(0)), text)


def render_agent(cfg: Config, v: Variant) -> str:
    agent_id = v.agent_id
    template = (cfg.sources / "agents" / f"{agent_id}.agent.md").read_text(encoding="utf-8")
    text = template.replace("{{generated}}", render_frontmatter(cfg, v))
    text = render_check_placeholders(cfg, v, text)
    text = text.replace("{{cross_check_handoffs}}", render_cross_check_handoffs(cfg, agent_id))
    text = text.replace("{{tier_handoffs}}", render_tier_handoffs(cfg, agent_id))
    text = text.replace("{{task_tiers}}", render_task_tiers(cfg))
    text = text.replace("{{task_runner_dispatch}}", render_task_runner_dispatch(cfg, agent_id))
    text = text.replace("{{task_tier_rules}}", render_task_tier_rules(cfg, agent_id))
    text = re.sub(r"\{\{agent:([\w-]+)\}\}", lambda m: q(agent_name(cfg, m.group(1))), text)
    text = text.replace("{{skills}}", render_skill_refs(cfg, agent_id))
    text = text.replace("{{capabilities}}", render_capabilities(cfg, agent_id))
    text = text.replace("{{plugin}}", cfg.plugin_name)
    head, sep, body = text.partition("\n---\n")
    if sep:  # drop blank lines that empty handoff placeholders leave in the frontmatter
        head = re.sub(r"\n[ \t]*(?=\n)", "", head)
        text = head + sep + body
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.rstrip() + "\n"


# ---------- other plugin files ----------

def manual_only(skill_md: bytes) -> bytes:
    """Add `disable-model-invocation: true` to a SKILL.md frontmatter, so Copilot loads the
    skill only when someone types /<name>."""
    text = skill_md.decode("utf-8")
    if not text.startswith("---"):
        return skill_md
    end = text.find("\n---", 3)
    if end < 0 or "disable-model-invocation:" in text[:end]:
        return skill_md
    return (text[:end] + "\ndisable-model-invocation: true" + text[end:]).encode("utf-8")


def skill_files(cfg: Config) -> dict[str, dict[str, bytes]]:
    skills: dict[str, dict[str, bytes]] = {}
    manual = set(cfg.skills_manual_only)
    for group in ("vendor", "team"):
        base = cfg.sources / "skills" / group
        if not base.exists():
            continue
        for skill_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            files = {
                f.relative_to(skill_dir).as_posix(): f.read_bytes()
                for f in sorted(skill_dir.rglob("*")) if f.is_file() and "__pycache__" not in f.parts
            }
            if skill_dir.name in manual and "SKILL.md" in files:
                files["SKILL.md"] = manual_only(files["SKILL.md"])
            skills[skill_dir.name] = files
    return skills


def hook_config(cfg: Config) -> dict:
    return {
        "plugin": cfg.plugin_name,
        "version": cfg.plugin.get("version", "0.0.0"),
        "stages": cfg.stage_ids,
        "artifacts": {s["id"]: s.get("artifacts", []) for s in cfg.stages},
        "budgets": cfg.raw.get("budgets", {}),
        "review": cfg.raw.get("review", {}),
        "hooks": cfg.raw.get("hooks", {}),
        "runs": {"dir": cfg.runs_dir, "commit": bool((cfg.raw.get("runs") or {}).get("commit", False))},
        "code_index": cfg.code_index,
        "agent_scopes": {v.name: cfg.agents[v.agent_id]["scope"] for v in all_variants(cfg)},
        "models": models_config(cfg),
    }


def models_config(cfg: Config) -> dict:
    scope_tiers = {}
    for agent_id, agent in cfg.agents.items():
        tier = cfg.agent_tier(agent_id)
        if tier in cfg.tiers:
            scope_tiers.setdefault(agent["scope"], tier)
    runners = {v.name: v.tier for v in all_variants(cfg) if v.tier and cfg.is_by_task(v.agent_id)}
    return {
        "order": cfg.tier_order,
        "first_model": {t: cfg.first_model(t) for t in cfg.tier_order},
        "scope_tiers": scope_tiers,
        "task_runners": runners,
        "task_default": cfg.task_default,
        "cross_check": {stage: {"checker": spec["checker"], "required": spec["required"]}
                        for stage, spec in cfg.cross_checks().items()},
    }


def server_entry(server: dict) -> dict:
    """An mcp.json entry in Agent Plugins 1.0 form: remote servers are "streamable-http"
    (or "sse"); VS Code's own mcp.json calls the same thing "http"."""
    skip = {"est_tools", "tools", "stages", "enabled", "optional"}
    entry = {k: v for k, v in server.items() if k not in skip}
    if entry.get("type") == "http":
        entry["type"] = "streamable-http"
    return entry


def mcp_config(cfg: Config) -> dict:
    servers = {name: server_entry(s) for name, s in cfg.servers.items() if cfg.server_usable(name)}
    return {"$schema": MCP_SCHEMA, "mcpServers": servers}


def plugin_manifest(cfg: Config) -> dict:
    p = cfg.plugin
    manifest = {
        "$schema": PLUGIN_SCHEMA,
        "name": cfg.plugin_name,
        "version": p.get("version", "0.0.0"),
        "description": p.get("description", ""),
    }
    for key in ("repository", "license", "homepage"):
        if p.get(key):
            manifest[key] = p[key]
    manifest["keywords"] = ["copilot", "workflow", "review", "mcp"]
    return manifest


def marketplace(cfg: Config) -> dict:
    p = cfg.plugin
    return {
        "name": cfg.plugin_name,
        "metadata": {"description": p.get("description", ""), "version": p.get("version", "0.0.0")},
        "owner": {"name": p.get("owner", cfg.plugin_name)},
        "plugins": [{
            "name": cfg.plugin_name,
            "source": f"./{PLUGIN_DIR}",
            "description": p.get("description", ""),
            "version": p.get("version", "0.0.0"),
        }],
    }


def as_json(data: dict) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode()


def desired(cfg: Config) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for v in all_variants(cfg):
        files[f"{AGENTS_DIR}/{v.file_id}.agent.md"] = render_agent(cfg, v).encode()
    for name, sfiles in skill_files(cfg).items():
        for rel, data in sfiles.items():
            files[f"{SKILLS_DIR}/{name}/{rel}"] = data
    for script in sorted((cfg.sources / "scripts").glob("*.cjs")):
        files[f"{SCRIPTS_DIR}/{script.name}"] = script.read_bytes()
    files[f"{SCRIPTS_DIR}/config.json"] = as_json(hook_config(cfg))
    files[f"{PLUGIN_DIR}/mcp.json"] = as_json(mcp_config(cfg))
    files[f"{PLUGIN_DIR}/plugin.json"] = as_json(plugin_manifest(cfg))
    files[MARKETPLACE] = as_json(marketplace(cfg))
    return files


@dataclass
class BuildReport:
    written: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    skipped_servers: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.written or self.removed)


def build(cfg: Config, check: bool = False) -> BuildReport:
    report = BuildReport()
    files = desired(cfg)
    root = cfg.root
    for rel, data in sorted(files.items()):
        path = root / rel
        if path.exists() and path.read_bytes() == data:
            continue
        report.written.append(rel)
        if not check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            if rel.endswith(".cjs") or "/scripts/" in rel.removeprefix(SCRIPTS_DIR):
                path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    plugin_root = root / PLUGIN_DIR
    if plugin_root.exists():
        for path in sorted(p for p in plugin_root.rglob("*") if p.is_file()):
            rel = path.relative_to(root).as_posix()
            if rel not in files:
                report.removed.append(rel)
                if not check:
                    path.unlink()
        if not check:
            for d in sorted((p for p in plugin_root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                if not any(d.iterdir()):
                    d.rmdir()
    report.skipped_servers = sorted(n for n in cfg.servers if not cfg.server_usable(n))
    return report
