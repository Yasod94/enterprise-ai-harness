"""Add team skills and MCP servers to the plugin's harness.yaml (comments are kept)."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path

import yaml

from enterprise_ai_harness.config import CONFIG_FILE, Config

GITHUB_RE = re.compile(r"^(?:github:|https://github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?(?:@([\w./-]+))?$")


class ExtendError(Exception):
    pass


# ---------- harness.yaml text editing ----------

def insert_entry(text: str, key: str, entry_yaml: str) -> str:
    """Insert an already-indented YAML block right under a top-level key."""
    lines = text.splitlines()
    block = entry_yaml.rstrip("\n").splitlines()
    for i, line in enumerate(lines):
        m = re.match(rf"^{re.escape(key)}:\s*(.*?)\s*(#.*)?$", line)
        if not m:
            continue
        if m.group(1) not in ("", "[]", "{}"):
            raise ExtendError(f"write `{key}` as a block (not inline) to add entries automatically")
        lines[i] = f"{key}:"
        lines[i + 1:i + 1] = block
        return "\n".join(lines) + "\n"
    return text.rstrip("\n") + f"\n\n{key}:\n" + "\n".join(block) + "\n"


def rebind_capability(text: str, cap: str, server: str) -> str:
    """Change `bind:` of one capability under the top-level `capabilities:` block."""
    lines = text.splitlines()
    in_caps = in_cap = False
    for i, line in enumerate(lines):
        if re.match(r"^capabilities:\s*(#.*)?$", line):
            in_caps = True
            continue
        if in_caps and re.match(r"^\S", line) and line.strip() and not line.startswith("#"):
            break
        if in_caps and re.match(rf"^  {re.escape(cap)}:\s*(#.*)?$", line):
            in_cap = True
            continue
        if in_cap:
            if re.match(r"^  \S", line):
                break
            m = re.match(r"^(\s+bind:\s*)([^\s#]+)(.*)$", line)
            if m:
                lines[i] = f"{m.group(1)}{server}{m.group(3)}"
                return "\n".join(lines) + "\n"
    raise ExtendError(f"capability {cap} with a `bind:` line not found in {CONFIG_FILE}")


def indent(text: str, spaces: int = 2) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line else line for line in text.splitlines()) + "\n"


def _write_config(path: Path, new_text: str) -> None:
    if not isinstance(yaml.safe_load(new_text), dict):
        raise ExtendError(f"{CONFIG_FILE} would no longer be a mapping; not written")
    path.write_text(new_text, encoding="utf-8")


# ---------- GitHub vendoring ----------

def parse_github(source: str) -> tuple[str, str, str | None]:
    m = GITHUB_RE.match(source.strip())
    if not m:
        raise ExtendError(f"not a GitHub source: {source} (use owner/repo[@ref] or a local folder)")
    return m.group(1), m.group(2), m.group(3)


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise ExtendError(f"git {' '.join(args)} failed: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


def vendor_github_skill(dest: Path, owner: str, name: str, skill: str | None, ref: str | None) -> str:
    """Copy one skill folder from GitHub into `dest`, with SOURCE.md and LICENSE. Returns the commit."""
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        _git(["init", "-q"], work)
        _git(["remote", "add", "origin", f"https://github.com/{owner}/{name}.git"], work)
        _git(["fetch", "-q", "--depth", "1", "origin", ref or "HEAD"], work)
        _git(["checkout", "-q", "FETCH_HEAD"], work)
        commit = _git(["rev-parse", "HEAD"], work)
        if skill:
            matches = sorted(p.parent for p in work.rglob("SKILL.md") if p.parent.name == skill and ".git" not in p.parts)
        else:
            matches = [work] if (work / "SKILL.md").exists() else []
        if not matches:
            found = sorted({p.parent.name for p in work.rglob("SKILL.md")})
            raise ExtendError(f"skill {skill or '(root)'} not found in {owner}/{name}. Found: {', '.join(found[:20])}")
        src = matches[0]
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git", "agents"))
        license_file = next((work / n for n in ("LICENSE", "LICENSE.md", "LICENSE.txt") if (work / n).exists()), None)
        license_line = "unknown; check before merging"
        if license_file:
            shutil.copy(license_file, dest / "LICENSE")
            license_line = license_file.read_text(encoding="utf-8", errors="replace").splitlines()[0].strip()
        rel = src.relative_to(work).as_posix() or "."
        (dest / "SOURCE.md").write_text(
            "# Source\n\n"
            f"- Repository: https://github.com/{owner}/{name}\n"
            f"- Path: {rel}\n"
            f"- Commit: {commit}\n"
            f"- License: {license_line}. Full text in LICENSE next to this file.\n"
            f"- Vendored: {date.today().isoformat()} by enterprise-ai-harness. Do not edit; put harness rules in the agent that loads it.\n",
            encoding="utf-8",
        )
        return commit


# ---------- commands ----------

def add_skill(cfg: Config, source: str, skill: str | None = None, stages: list[str] | None = None,
              name: str | None = None) -> list[str]:
    config_path = cfg.root / CONFIG_FILE
    local = Path(source).expanduser()
    local = local if local.is_absolute() else (Path.cwd() / local)
    if local.is_dir() and (local / "SKILL.md").exists():
        skill_name = name or skill or local.name
        dest = cfg.sources / "skills" / "team" / skill_name
        if dest.exists():
            raise ExtendError(f"sources/skills/team/{skill_name} already exists")
        shutil.copytree(local, dest, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        messages = [f"copied skill {skill_name} into sources/skills/team/{skill_name}"]
    else:
        owner, repo_name, ref = parse_github(source)
        skill_name = name or skill or repo_name
        dest = cfg.sources / "skills" / "vendor" / skill_name
        if dest.exists():
            raise ExtendError(f"sources/skills/vendor/{skill_name} already exists")
        commit = vendor_github_skill(dest, owner, repo_name, skill, ref)
        messages = [f"vendored skill {skill_name} from {owner}/{repo_name}@{commit[:12]}"]
    if stages:
        bad = [s for s in stages if s not in cfg.stage_ids]
        if bad:
            raise ExtendError(f"unknown stage(s): {', '.join(bad)}")
        entry = {"name": skill_name, "stages": stages}
        text = insert_entry(config_path.read_text(encoding="utf-8"), "skills",
                            indent(yaml.safe_dump([entry], sort_keys=False, default_flow_style=None)))
        _write_config(config_path, text)
        messages.append(f"stages {', '.join(stages)} load {skill_name} at start")
    else:
        messages.append(f"{skill_name} loads on demand from its description")
    return messages


def add_server(cfg: Config, name: str, url: str | None = None, command: str | None = None,
               args: list[str] | None = None, stages: list[str] | None = None, tools: list[str] | None = None,
               est_tools: int | None = None, capability: str | None = None) -> list[str]:
    if bool(url) == bool(command):
        raise ExtendError("give either --url (http server) or --command (stdio server)")
    if name in cfg.servers:
        raise ExtendError(f"server {name} is already in {CONFIG_FILE}")
    if capability and capability not in cfg.capabilities:
        raise ExtendError(f"unknown capability {capability}")
    config_path = cfg.root / CONFIG_FILE
    entry: dict = {"type": "http", "url": url} if url else {"type": "stdio", "command": command}
    if args:
        entry["args"] = args
    if stages:
        entry["stages"] = stages
    if tools:
        entry["tools"] = tools
    entry["est_tools"] = est_tools or (len(tools) if tools else 20)
    text = insert_entry(config_path.read_text(encoding="utf-8"), "servers",
                        indent(yaml.safe_dump({name: entry}, sort_keys=False, default_flow_style=None)))
    messages = [f"added server {name}"]
    if capability:
        text = rebind_capability(text, capability, name)
        messages.append(f"capability {capability} now uses {name}")
    _write_config(config_path, text)
    if not stages and not capability:
        messages.append("note: no --stages or --capability given, so no agent gets these tools yet")
    return messages
