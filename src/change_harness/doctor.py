"""Static and optional live checks for a service repo's harness setup."""

from __future__ import annotations

import re
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import yaml

from change_harness.config import EXTENSIONS_PATH, ConfigError, has_placeholder, load
from change_harness.render import AGENTS_DIR, MCP_JSON, SKILLS_DIR, read_lock, sync

TOOL_CAP = 128
TOKENS_PER_TOOL = 250  # rough size of one tool definition in the prompt
SECRET_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"Bearer\s+(?!\$\{)[A-Za-z0-9._-]{20,}"),
    re.compile(r"(?i)\"(password|token|secret|api[_-]?key)\"\s*:\s*\"(?!\$\{)[^\"]{8,}\""),
]


@dataclass
class Finding:
    level: str  # ok | warn | error
    message: str

    def __str__(self) -> str:
        mark = {"ok": "✓", "warn": "!", "error": "✗"}[self.level]
        return f"{mark} {self.message}"


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


def check(repo: Path, live: bool = False) -> list[Finding]:
    repo = repo.resolve()
    out: list[Finding] = []
    try:
        cfg = load(repo)
    except (ConfigError, yaml.YAMLError) as exc:
        return [Finding("error", f"configuration is invalid:\n{exc}")]
    out.append(Finding("ok", f"configuration loads ({len(cfg.agents)} agents, {len(cfg.servers)} servers)"))

    # Servers that are on but still hold placeholders.
    for name, server in cfg.servers.items():
        if server.get("enabled", True) is False:
            out.append(Finding("ok", f"server {name} is disabled; capabilities bound to it use their fallback"))
        elif has_placeholder({k: v for k, v in server.items() if k not in ("stages", "tools")}):
            out.append(Finding("error", f"server {name} has a placeholder value; fill it in (team harness.yaml or "
                                         f"{EXTENSIONS_PATH}) or set enabled: false"))

    # Tool counts and tool-definition tokens per agent.
    for agent_id in cfg.agents:
        count = cfg.estimate_tool_count(agent_id)
        tokens = count * TOKENS_PER_TOOL
        level = "error" if count > TOOL_CAP else "warn" if count > 100 else "ok"
        out.append(Finding(level, f"{agent_id}: about {count} tools, ~{tokens:,} tokens of tool definitions"))
        for server in cfg.extension_servers_for(agent_id):
            if not cfg.servers[server].get("tools"):
                out.append(Finding("warn", f"{agent_id}: repo server {server} adds all its tools; list `tools` to narrow it"))

    # Rendered files up to date.
    try:
        report = sync(repo, check=True)
        if report.changed:
            out.append(Finding("warn", f"{len(report.written) + len(report.removed)} rendered files are out of date; run `harness sync`"))
        else:
            out.append(Finding("ok", "rendered files are up to date"))
        for path in report.conflicts:
            out.append(Finding("error", f"{path} was edited by hand; move the change to {EXTENSIONS_PATH} or rerun sync with --force"))
        for name in report.overrides:
            out.append(Finding("warn", f"skill {name} in {SKILLS_DIR} replaces the harness version for this repo"))
    except ConfigError as exc:
        out.append(Finding("error", str(exc)))

    # Skills referenced by agents exist and have valid frontmatter.
    skills_root = repo / SKILLS_DIR
    for agent_id in cfg.agents:
        for skill in cfg.skills_for(agent_id):
            if not (skills_root / skill / "SKILL.md").exists():
                out.append(Finding("error", f"{agent_id} loads skill {skill}, but {SKILLS_DIR}/{skill}/SKILL.md is missing"))
    if skills_root.exists():
        for skill_md in sorted(skills_root.glob("*/SKILL.md")):
            fm = _frontmatter(skill_md.read_text(encoding="utf-8"))
            name = skill_md.parent.name
            if fm.get("__error__") or not fm.get("name") or not fm.get("description"):
                out.append(Finding("error", f"skill {name}: SKILL.md needs name and description frontmatter"))
            elif fm["name"] != name:
                out.append(Finding("warn", f"skill {name}: frontmatter name is {fm['name']}"))
            source = skill_md.parent / "SOURCE.md"
            if source.exists() and "License: unknown" in source.read_text(encoding="utf-8"):
                out.append(Finding("warn", f"skill {name}: license unknown; check it before merging"))

    # Agents and skills should name capabilities, not servers.
    server_ref = re.compile(r"\b(" + "|".join(map(re.escape, cfg.servers)) + r")/[\w*]")
    for path in sorted((repo / AGENTS_DIR).glob("*.agent.md")) + sorted(skills_root.glob("*/SKILL.md")):
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".md" and path.name.endswith(".agent.md"):
            text = text.split("\n---", 1)[-1]          # skip frontmatter tools list
            text = text.split("## Capabilities", 1)[0]  # skip the rendered capability table
        hit = server_ref.search(text)
        if hit:
            out.append(Finding("warn", f"{path.relative_to(repo)} names server tool {hit.group(0)}...; name the capability instead"))

    # Secrets in committed config.
    for rel in (MCP_JSON, EXTENSIONS_PATH.as_posix()):
        path = repo / rel
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if any(p.search(text) for p in SECRET_PATTERNS):
                out.append(Finding("error", f"{rel} seems to contain a secret; use an `inputs` prompt or ${{env:...}} instead"))

    # Local tools developers need.
    for tool, why in (("node", "hook script and npx servers"), ("git", "branches and skill vendoring")):
        if shutil.which(tool):
            out.append(Finding("ok", f"{tool} found"))
        else:
            out.append(Finding("warn", f"{tool} not found on this machine ({why})"))

    lock = read_lock(repo)
    if lock and "<org>" in lock.get("source", ""):
        out.append(Finding("warn", "lock.json source is a placeholder; rerun `harness init --source <git url>` so the PR workflow can install the harness"))

    if live:
        out += live_checks(cfg)
    return out


def live_checks(cfg) -> list[Finding]:
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
            cmd = server["command"]
            found = shutil.which(cmd)
            out.append(Finding("ok" if found else "warn", f"{name}: command {cmd} {'found' if found else 'not found'}"))
    return out


def summarize(findings: list[Finding]) -> tuple[str, int]:
    errors = sum(f.level == "error" for f in findings)
    warns = sum(f.level == "warn" for f in findings)
    text = "\n".join(str(f) for f in findings)
    text += f"\n\n{errors} error(s), {warns} warning(s)"
    return text, (1 if errors else 0)

