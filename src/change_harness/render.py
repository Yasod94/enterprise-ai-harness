"""Render harness files into a service repo and keep them in step with the lock file."""

from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from change_harness import __version__
from change_harness.config import DATA, Config, load

AGENTS_DIR = ".github/agents"
SKILLS_DIR = ".github/skills"
HOOKS_DIR = ".github/hooks/harness"
WORKFLOW = ".github/workflows/change-harness.yml"
MCP_JSON = ".vscode/mcp.json"
LOCK = ".harness/lock.json"
DEFAULT_SOURCE = "https://github.com/<org>/change-harness"

HOOK_EVENTS = [
    ("UserPromptSubmit", "prompt"),
    ("PreToolUse", "pre-tool"),
    ("PostToolUse", "post-tool"),
    ("PreCompact", "pre-compact"),
]

GITIGNORE_LINES = [
    "# change-harness (personal and runtime files)",
    "harness.local.yaml",
    ".changes/current",
    ".changes/.hooks-ok",
    ".changes/.checked",
    ".changes/.audit.log",
    ".changes/**/.usage.json",
]

BLOCK_START = "<!-- harness:start"
BLOCK_END = "<!-- harness:end -->"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def q(text: str) -> str:
    """Quote a scalar for YAML frontmatter."""
    return json.dumps(text, ensure_ascii=False)


def yaml_list(items: list[str]) -> str:
    return "[" + ", ".join("'" + i.replace("'", "''") + "'" for i in items) + "]"


# ---------- agents ----------

def agent_name(cfg: Config, agent_id: str) -> str:
    return cfg.agents[agent_id].get("name", agent_id)


def render_frontmatter(cfg: Config, agent_id: str) -> str:
    agent = cfg.agents[agent_id]
    lines = [
        "# Rendered by change-harness. Change .harness/extensions.yaml, not this file.",
        f"name: {q(agent_name(cfg, agent_id))}",
        f"description: {q(agent.get('description', ''))}",
        f"tools: {yaml_list(cfg.tools_for(agent_id))}",
    ]
    subagents = agent.get("subagents") or []
    if subagents:
        lines.append(f"agents: {yaml_list([agent_name(cfg, s) for s in subagents])}")
    models = (cfg.raw.get("models") or {}).get(agent_id)
    if models:
        lines.append(f"model: {yaml_list(models if isinstance(models, list) else [models])}")
    if agent.get("user_invocable") is False:
        lines.append("user-invocable: false")
    lines.append("hooks:")
    scope = agent["scope"]
    for event, mode in HOOK_EVENTS:
        lines += [
            f"  {event}:",
            "    - type: command",
            f"      command: node {HOOKS_DIR}/harness-hook.cjs {mode} {scope}",
        ]
    return "\n".join(lines)


def render_capabilities(cfg: Config, agent_id: str) -> str:
    agent = cfg.agents[agent_id]
    caps = agent.get("capabilities") or []
    ext = cfg.extension_servers_for(agent_id)
    stage = cfg.stage_of_scope(agent.get("scope", ""))
    if not caps and not ext:
        return ""
    rows = ["## Capabilities", "", "| Capability | Tools | If unavailable |", "| --- | --- | --- |"]
    for cap in caps:
        spec = cfg.capabilities[cap]
        tools = cfg.capability_tools(cap)
        if tools:
            cell = ", ".join(f"`{t}`" for t in tools)
            server = cfg.effective_bind(cap)
            if server != spec["bind"]:
                cell += f" (standing in for {spec['bind']})"
        else:
            cell = f"not configured ({spec['bind']})"
        rows.append(f"| {cap} | {cell} | {spec.get('fallback', 'Say it was unavailable.')} |")
    for name in ext:
        if cfg.server_usable(name):
            tools = ", ".join(f"`{t}`" for t in cfg.server_tools(name))
            rows.append(f"| repo: {name} | {tools} | Say it was unavailable. |")
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
    lines += [f"- [{s}](../skills/{s}/SKILL.md)" for s in skills]
    return "\n".join(lines)


def render_agent(cfg: Config, agent_id: str) -> str:
    template = (DATA / "agents" / f"{agent_id}.agent.md").read_text(encoding="utf-8")
    text = template.replace("{{generated}}", render_frontmatter(cfg, agent_id))
    text = re.sub(r"\{\{agent:([\w-]+)\}\}", lambda m: q(agent_name(cfg, m.group(1))), text)
    text = text.replace("{{skills}}", render_skill_refs(cfg, agent_id))
    text = text.replace("{{capabilities}}", render_capabilities(cfg, agent_id))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.rstrip() + "\n"


# ---------- skills ----------

def _walk(traversable, prefix=""):
    for item in sorted(traversable.iterdir(), key=lambda t: t.name):
        rel = f"{prefix}{item.name}"
        if item.is_dir():
            yield from _walk(item, rel + "/")
        else:
            yield rel, item.read_bytes()


def packaged_skills() -> dict[str, dict[str, bytes]]:
    skills: dict[str, dict[str, bytes]] = {}
    for group in ("vendor", "team"):
        root = DATA / "skills" / group
        for skill_dir in root.iterdir():
            if skill_dir.is_dir():
                skills[skill_dir.name] = dict(_walk(skill_dir))
    return skills


def local_extension_skills(cfg: Config) -> dict[str, dict[str, bytes]]:
    skills = {}
    for entry in cfg.skills_ext:
        source = entry.get("source", "")
        if source and not source.startswith("github:"):
            src = (cfg.repo / source).resolve()
            if src.is_dir():
                skills[entry["name"]] = {
                    p.relative_to(src).as_posix(): p.read_bytes() for p in sorted(src.rglob("*")) if p.is_file()
                }
    return skills


# ---------- hooks, mcp, workflow ----------

def render_hook_config(cfg: Config) -> str:
    config = {
        "version": __version__,
        "stages": cfg.stage_ids,
        "artifacts": {s["id"]: s.get("artifacts", []) for s in cfg.stages},
        "budgets": cfg.raw.get("budgets", {}),
        "review": cfg.raw.get("review", {}),
        "hooks": cfg.raw.get("hooks", {}),
        "allowed_paths": {
            "default": [".changes/"],
            "orchestrator": [".changes/", ".harness/extensions.yaml"],
        },
    }
    return json.dumps(config, indent=2) + "\n"


def server_entry(server: dict) -> dict:
    skip = {"est_tools", "tools", "stages", "enabled", "optional"}
    entry = {k: v for k, v in server.items() if k not in skip}
    return entry


def strip_jsonc(text: str) -> str:
    out, i, in_str = [], 0, False
    while i < len(text):
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < len(text):
                out.append(text[i + 1]); i += 2; continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True; out.append(c)
        elif text.startswith("//", i):
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def render_mcp(cfg: Config, existing: str | None, owned: list[str]) -> tuple[str, list[str]]:
    current = {}
    if existing:
        try:
            current = json.loads(strip_jsonc(existing))
        except json.JSONDecodeError:
            current = {}
    ours = {name: server_entry(s) for name, s in cfg.servers.items() if cfg.server_usable(name)}
    servers = {k: v for k, v in (current.get("servers") or {}).items() if k not in owned and k not in ours}
    servers.update(ours)
    inputs = [i for i in (current.get("inputs") or []) if i.get("id") not in {x.get("id") for x in cfg.raw.get("inputs", [])}]
    inputs += cfg.raw.get("inputs", []) + (cfg.extensions.get("inputs") or [])
    doc = {"servers": servers}
    if inputs:
        doc["inputs"] = inputs
    for key, value in current.items():
        if key not in ("servers", "inputs"):
            doc[key] = value
    return json.dumps(doc, indent=2) + "\n", sorted(ours)


def render_agents_md(cfg: Config, existing: str | None) -> str:
    block = (DATA / "repo" / "AGENTS.block.md").read_text(encoding="utf-8").rstrip()
    commands = cfg.raw.get("commands") or {}
    if commands:
        cmd_lines = ["", "Repo commands:", ""] + [f"- {k}: `{v}`" for k, v in commands.items()]
        block = block.replace(BLOCK_END, "\n".join(cmd_lines) + "\n" + BLOCK_END)
    if not existing:
        return "# Agent instructions\n\n" + block + "\n"
    start = existing.find(BLOCK_START)
    end = existing.find(BLOCK_END)
    if start >= 0 and end > start:
        return existing[:start] + block + existing[end + len(BLOCK_END):]
    return existing.rstrip() + "\n\n" + block + "\n"


def render_gitignore(existing: str | None) -> str:
    text = existing or ""
    lines = text.splitlines()
    missing = [line for line in GITIGNORE_LINES if line not in lines]
    if not missing:
        return text
    return (text.rstrip() + "\n\n" if text.strip() else "") + "\n".join(missing) + "\n"


# ---------- sync ----------

@dataclass
class SyncReport:
    written: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    overrides: list[str] = field(default_factory=list)
    skipped_servers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.written or self.removed)


def read_lock(repo: Path) -> dict:
    path = repo / LOCK
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def desired_files(cfg: Config, repo: Path, lock: dict) -> tuple[dict[str, bytes], list[str]]:
    files: dict[str, bytes] = {}
    for agent_id in cfg.agents:
        files[f"{AGENTS_DIR}/{agent_id}.agent.md"] = render_agent(cfg, agent_id).encode()
    locked = set(lock.get("files", {}))
    overrides = []
    all_skills = {**packaged_skills(), **local_extension_skills(cfg)}
    for name, skill_files in all_skills.items():
        base = f"{SKILLS_DIR}/{name}/"
        dest = repo / SKILLS_DIR / name
        if dest.exists() and not any(p.startswith(base) for p in locked):
            overrides.append(name)
            continue
        for rel, data in skill_files.items():
            files[base + rel] = data
    files[f"{HOOKS_DIR}/harness-hook.cjs"] = (DATA / "hooks" / "harness-hook.cjs").read_bytes()
    files[f"{HOOKS_DIR}/config.json"] = render_hook_config(cfg).encode()
    files[WORKFLOW] = (DATA / "workflows" / "change-harness.yml").read_bytes()
    return files, overrides


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if "/scripts/" in path.as_posix() or path.suffix == ".cjs":
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_text_if_changed(repo: Path, rel: str, new: str, report: SyncReport, check: bool) -> None:
    path = repo / rel
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old != new:
        report.written.append(rel)
        if not check:
            _write(path, new.encode())


def sync(repo: Path, check: bool = False, force: bool = False, source: str | None = None) -> SyncReport:
    from change_harness.extend import vendor_missing_github_skills

    repo = repo.resolve()
    cfg = load(repo)
    lock = read_lock(repo)
    report = SyncReport()

    if not check:
        report.notes += vendor_missing_github_skills(cfg)

    files, report.overrides = desired_files(cfg, repo, lock)
    locked: dict[str, str] = lock.get("files", {})
    new_lock_files: dict[str, str] = {}

    for rel, data in sorted(files.items()):
        path = repo / rel
        new_hash = sha(data)
        if path.exists():
            current = sha(path.read_bytes())
            if current == new_hash:
                new_lock_files[rel] = new_hash
                continue
            edited = rel in locked and current != locked[rel]
            foreign = rel not in locked
            if (edited or foreign) and not force:
                report.conflicts.append(rel)
                if rel in locked:
                    new_lock_files[rel] = locked[rel]
                continue
        report.written.append(rel)
        new_lock_files[rel] = new_hash
        if not check:
            _write(path, data)

    for rel, old_hash in sorted(locked.items()):
        if rel in files:
            continue
        path = repo / rel
        if not path.exists():
            continue
        if sha(path.read_bytes()) == old_hash or force:
            report.removed.append(rel)
            if not check:
                path.unlink()
                parent = path.parent
                while parent != repo and parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
        else:
            report.conflicts.append(f"{rel} (no longer managed and edited by hand; left in place)")

    mcp_path = repo / MCP_JSON
    existing = mcp_path.read_text(encoding="utf-8") if mcp_path.exists() else None
    mcp_text, owned = render_mcp(cfg, existing, lock.get("servers", []))
    _write_text_if_changed(repo, MCP_JSON, mcp_text, report, check)
    report.skipped_servers = sorted(n for n in cfg.servers if not cfg.server_usable(n))

    agents_path = repo / "AGENTS.md"
    _write_text_if_changed(repo, "AGENTS.md",
                           render_agents_md(cfg, agents_path.read_text(encoding="utf-8") if agents_path.exists() else None),
                           report, check)
    if not (repo / ".github/copilot-instructions.md").exists():
        _write_text_if_changed(repo, ".github/copilot-instructions.md",
                               (DATA / "repo" / "copilot-instructions.md").read_text(encoding="utf-8"), report, check)
    gi = repo / ".gitignore"
    _write_text_if_changed(repo, ".gitignore", render_gitignore(gi.read_text(encoding="utf-8") if gi.exists() else None),
                           report, check)

    new_lock = {
        "harness_version": __version__,
        "source": source or lock.get("source") or DEFAULT_SOURCE,
        "servers": owned,
        "files": dict(sorted(new_lock_files.items())),
    }
    lock_text = json.dumps(new_lock, indent=2) + "\n"
    old_lock_text = (repo / LOCK).read_text(encoding="utf-8") if (repo / LOCK).exists() else None
    if lock_text != old_lock_text:
        report.written.append(LOCK)
        if not check:
            _write(repo / LOCK, lock_text.encode())
    return report
