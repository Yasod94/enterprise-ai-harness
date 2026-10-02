"""Add skills and MCP servers to a service repo through .harness/extensions.yaml."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path

import yaml

from change_harness.config import DATA, EXTENSIONS_PATH, Config

SKILLS_DIR = Path(".github/skills")
GITHUB_RE = re.compile(r"^(?:github:|https://github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?(?:@([\w./-]+))?$")


class ExtendError(Exception):
    pass


# ---------- extensions.yaml text editing (keeps comments) ----------

def ensure_extensions_file(repo: Path) -> Path:
    path = repo / EXTENSIONS_PATH
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((DATA / "repo" / "extensions.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    return path


def insert_entry(text: str, key: str, entry_yaml: str) -> str:
    """Insert an already-indented YAML block under a top-level key."""
    lines = text.splitlines()
    block = entry_yaml.rstrip("\n").splitlines()
    for i, line in enumerate(lines):
        m = re.match(rf"^{re.escape(key)}:\s*(.*?)\s*(#.*)?$", line)
        if not m:
            continue
        rest = m.group(1)
        if rest not in ("", "[]", "{}"):
            raise ExtendError(f"{EXTENSIONS_PATH}: write `{key}` as a block (not inline) to add entries automatically")
        lines[i] = f"{key}:"
        lines[i + 1:i + 1] = block
        return "\n".join(lines) + "\n"
    return text.rstrip("\n") + f"\n{key}:\n" + "\n".join(block) + "\n"


def indent(text: str, spaces: int = 2) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line else line for line in text.splitlines()) + "\n"


def _write_extensions(path: Path, new_text: str) -> None:
    data = yaml.safe_load(new_text)
    if not isinstance(data, dict):
        raise ExtendError("extensions.yaml would no longer be a mapping; not written")
    path.write_text(new_text, encoding="utf-8")


# ---------- GitHub skill vendoring ----------

def parse_github(source: str) -> tuple[str, str, str | None]:
    m = GITHUB_RE.match(source.strip())
    if not m:
        raise ExtendError(f"not a GitHub source: {source} (use owner/repo, github:owner/repo or a local folder)")
    return m.group(1), m.group(2), m.group(3)


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise ExtendError(f"git {' '.join(args)} failed: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


def vendor_github_skill(repo: Path, owner: str, name: str, skill: str | None, ref: str | None,
                        dest_name: str) -> str:
    """Copy one skill folder from GitHub into .github/skills/<dest_name>/. Returns the commit."""
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
        dest = repo / SKILLS_DIR / dest_name
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
            f"- Vendored: {date.today().isoformat()} by change-harness. Review it like code before merging.\n",
            encoding="utf-8",
        )
        return commit


def vendor_missing_github_skills(cfg: Config) -> list[str]:
    notes = []
    for entry in cfg.skills_ext:
        source = entry.get("source", "")
        if not source.startswith("github:"):
            continue
        dest = cfg.repo / SKILLS_DIR / entry["name"]
        if (dest / "SKILL.md").exists():
            continue
        try:
            owner, name, ref = parse_github(source)
            commit = vendor_github_skill(cfg.repo, owner, name, entry.get("skill"), ref, entry["name"])
            notes.append(f"vendored skill {entry['name']} from {owner}/{name}@{commit[:12]}")
        except (ExtendError, OSError, subprocess.SubprocessError) as exc:
            notes.append(f"could not vendor skill {entry['name']}: {exc}")
    return notes


# ---------- public commands ----------

def add_skill(repo: Path, source: str, skill: str | None = None, stages: list[str] | None = None,
              name: str | None = None) -> list[str]:
    path = ensure_extensions_file(repo)
    current = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    local = (repo / source).resolve()
    if local.is_dir() and (local / "SKILL.md").exists():
        skill_name = name or skill or local.name
        entry = {"name": skill_name, "source": local.relative_to(repo.resolve()).as_posix()}
        messages = [f"added local skill {skill_name} from {entry['source']}"]
    else:
        owner, repo_name, ref = parse_github(source)
        skill_name = name or skill or repo_name
        commit = vendor_github_skill(repo, owner, repo_name, skill, ref, skill_name)
        entry = {"name": skill_name, "source": f"github:{owner}/{repo_name}@{commit}"}
        if skill:
            entry["skill"] = skill
        messages = [f"vendored skill {skill_name} from {owner}/{repo_name}@{commit[:12]} into {SKILLS_DIR / skill_name}"]
    if any(e.get("name") == skill_name for e in current.get("skills") or []):
        raise ExtendError(f"skill {skill_name} is already in {EXTENSIONS_PATH}")
    if stages:
        entry["stages"] = stages
    text = insert_entry(path.read_text(encoding="utf-8"), "skills", indent(yaml.safe_dump([entry], sort_keys=False)))
    _write_extensions(path, text)
    messages.append(f"added skill {skill_name} to {EXTENSIONS_PATH}")
    return messages


def add_server(repo: Path, name: str, url: str | None = None, command: str | None = None,
               args: list[str] | None = None, stages: list[str] | None = None, tools: list[str] | None = None,
               est_tools: int | None = None, capability: str | None = None) -> list[str]:
    if bool(url) == bool(command):
        raise ExtendError("give either --url (http server) or --command (stdio server)")
    path = ensure_extensions_file(repo)
    current = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if name in (current.get("servers") or {}):
        raise ExtendError(f"server {name} is already in {EXTENSIONS_PATH}")
    entry: dict = {"type": "http", "url": url} if url else {"type": "stdio", "command": command}
    if args:
        entry["args"] = args
    if stages:
        entry["stages"] = stages
    if tools:
        entry["tools"] = tools
    entry["est_tools"] = est_tools or (len(tools) if tools else 20)
    text = insert_entry(path.read_text(encoding="utf-8"), "servers", indent(yaml.safe_dump({name: entry}, sort_keys=False)))
    messages = [f"added server {name} to {EXTENSIONS_PATH}"]
    if capability:
        if capability in (current.get("capabilities") or {}):
            raise ExtendError(f"capability {capability} is already overridden in {EXTENSIONS_PATH}")
        text = insert_entry(text, "capabilities", indent(yaml.safe_dump({capability: {"bind": name}}, sort_keys=False)))
        messages.append(f"bound capability {capability} to {name} for this repo")
    _write_extensions(path, text)
    if not stages and not capability:
        messages.append("note: no --stages or --capability given, so no agent gets these tools yet")
    return messages
