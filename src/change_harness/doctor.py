"""Checks for the harness repo: configuration, tool counts, skills, secrets and build state."""

from __future__ import annotations

import re
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass

import yaml

from change_harness.build import build
from change_harness.config import CONFIG_FILE, Config, ConfigError, has_placeholder, load

TOOL_CAP = 128
TOKENS_PER_TOOL = 250  # rough size of one tool definition in the prompt
SECRET_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"Bearer\s+(?!\$\{)[A-Za-z0-9._-]{20,}"),
    re.compile(r"(?i)[\"']?(password|token|secret|api[_-]?key)[\"']?\s*:\s*[\"'](?!\$\{)[^\"']{8,}[\"']"),
]


@dataclass
class Finding:
    level: str  # ok | warn | error
    message: str

    def __str__(self) -> str:
        return {"ok": "✓", "warn": "!", "error": "✗"}[self.level] + " " + self.message


def _frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    try:
        return yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return {"__error__": True}


def check(root=None, live: bool = False) -> list[Finding]:
    out: list[Finding] = []
    try:
        cfg = load(root)
    except (ConfigError, yaml.YAMLError) as exc:
        return [Finding("error", f"configuration is invalid:\n{exc}")]
    out.append(Finding("ok", f"{CONFIG_FILE} loads: plugin {cfg.plugin_name} {cfg.plugin.get('version', '')}, "
                             f"{len(cfg.agents)} agents, {len(cfg.servers)} servers"))
    if has_placeholder(cfg.plugin.get("repository", "")):
        out.append(Finding("warn", "plugin.repository still has a placeholder"))

    for name, server in cfg.servers.items():
        if server.get("enabled", True) is False:
            out.append(Finding("ok", f"server {name} is disabled; capabilities bound to it use their fallback"))
        elif has_placeholder({k: v for k, v in server.items() if k not in ("stages", "tools")}):
            out.append(Finding("error", f"server {name} has a placeholder value; fill it in or set enabled: false"))

    for agent_id in cfg.agents:
        count = cfg.estimate_tool_count(agent_id)
        level = "error" if count > TOOL_CAP else "warn" if count > 100 else "ok"
        out.append(Finding(level, f"{agent_id}: about {count} tools, ~{count * TOKENS_PER_TOOL:,} tokens of tool definitions"))
        for server in cfg.stage_servers_for(agent_id):
            if not cfg.servers[server].get("tools"):
                out.append(Finding("warn", f"{agent_id}: server {server} adds all its tools; list `tools` to narrow it"))

    report = build(cfg, check=True)
    if report.changed:
        out.append(Finding("error", f"plugin/ is out of date ({len(report.written) + len(report.removed)} files); run `harness build`"))
    else:
        out.append(Finding("ok", "plugin/ matches the sources"))

    skills_root = cfg.sources / "skills"
    names = {p.parent.name: p for p in skills_root.glob("*/*/SKILL.md")}
    for agent_id in cfg.agents:
        for skill in cfg.skills_for(agent_id):
            if skill not in names:
                out.append(Finding("error", f"{agent_id} loads skill {skill}, but no sources/skills/*/{skill}/SKILL.md exists"))
    dupes = [p.parent.name for p in skills_root.glob("*/*/SKILL.md")]
    for name in sorted({n for n in dupes if dupes.count(n) > 1}):
        out.append(Finding("error", f"skill {name} exists in both vendor/ and team/"))
    for name, skill_md in sorted(names.items()):
        fm = _frontmatter(skill_md.read_text(encoding="utf-8"))
        if fm.get("__error__") or not fm.get("name") or not fm.get("description"):
            out.append(Finding("error", f"skill {name}: SKILL.md needs name and description frontmatter"))
        elif fm["name"] != name:
            out.append(Finding("warn", f"skill {name}: frontmatter name is {fm['name']}"))
        source = skill_md.parent / "SOURCE.md"
        if skill_md.parent.parent.name == "vendor" and not source.exists():
            out.append(Finding("warn", f"skill {name}: vendored without SOURCE.md"))
        elif source.exists() and "License: unknown" in source.read_text(encoding="utf-8"):
            out.append(Finding("warn", f"skill {name}: license unknown; check it before merging"))

    if cfg.servers:
        server_ref = re.compile(r"(?<![\w./-])(" + "|".join(map(re.escape, cfg.servers)) + r")/[\w*]")  # not paths like .codegraph/x
        team_texts = sorted((cfg.sources / "agents").glob("*.agent.md")) + sorted((skills_root / "team").glob("*/SKILL.md"))
        for path in team_texts:
            hit = server_ref.search(path.read_text(encoding="utf-8"))
            if hit:
                out.append(Finding("warn", f"{path.relative_to(cfg.root)} names server tool {hit.group(0)}...; name the capability instead"))

    for rel in (CONFIG_FILE, "plugin/mcp.json"):
        path = cfg.root / rel
        if path.exists() and any(p.search(path.read_text(encoding="utf-8")) for p in SECRET_PATTERNS):
            out.append(Finding("error", f"{rel} seems to contain a secret; use an OAuth server or an input prompt"))

    for tool, why in (("node", "the hook, code graph scripts and npx servers"), ("git", "skill vendoring")):
        out.append(Finding("ok", f"{tool} found") if shutil.which(tool) else Finding("warn", f"{tool} not found ({why})"))

    if live:
        out += live_checks(cfg)
    return out


def live_checks(cfg: Config) -> list[Finding]:
    out = []
    for name, server in cfg.servers.items():
        if not cfg.server_usable(name):
            continue
        if server["type"] == "http":
            req = urllib.request.Request(server["url"], method="GET", headers={"Accept": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=6)
                out.append(Finding("ok", f"{name}: reachable"))
            except urllib.error.HTTPError as exc:
                level = "ok" if exc.code in (400, 401, 403, 405, 406) else "warn"
                out.append(Finding(level, f"{name}: reachable (HTTP {exc.code}; sign-in happens in VS Code)"))
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                out.append(Finding("warn", f"{name}: not reachable from here ({exc})"))
        else:
            found = shutil.which(server["command"])
            out.append(Finding("ok" if found else "warn", f"{name}: command {server['command']} {'found' if found else 'not found'}"))
    return out


def summarize(findings: list[Finding]) -> tuple[str, int]:
    errors = sum(f.level == "error" for f in findings)
    warns = sum(f.level == "warn" for f in findings)
    return "\n".join(map(str, findings)) + f"\n\n{errors} error(s), {warns} warning(s)", (1 if errors else 0)
