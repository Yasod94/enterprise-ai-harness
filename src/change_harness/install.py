"""Fallback install without the plugin system: copy the built plugin into a
developer's user-level Copilot folders. Nothing is written to service repos.

    ~/.copilot/agents/<agent>.agent.md     agents (plugin paths rewritten)
    ~/.copilot/skills/<skill>/             skills
    ~/.copilot/<plugin>/scripts/           hook and code graph scripts
    <VS Code user dir>/mcp.json            servers merged in (others kept)
"""

from __future__ import annotations

import json
import os
import platform
import shutil
from pathlib import Path

from change_harness.build import AGENTS_DIR, PLUGIN_DIR, SKILL_LINK_PREFIX, SKILLS_DIR, SCRIPTS_DIR
from change_harness.config import Config


def default_vscode_user_dir() -> Path:
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library/Application Support/Code/User"
    if system == "Windows":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "Code/User"
    return Path.home() / ".config/Code/User"


def _strip_jsonc(text: str) -> str:
    import re
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


def install_user(cfg: Config, copilot_dir: Path | None = None, vscode_user_dir: Path | None = None,
                 uninstall: bool = False) -> list[str]:
    copilot = (copilot_dir or Path.home() / ".copilot").expanduser()
    vscode = (vscode_user_dir or default_vscode_user_dir()).expanduser()
    home = copilot / cfg.plugin_name
    manifest_path = home / "installed.json"
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"files": [], "servers": []}
    messages: list[str] = []

    for rel in previous["files"]:
        p = Path(rel)
        if p.exists():
            p.unlink()
    dirs = {parent for f in previous["files"] for parent in Path(f).parents if copilot in parent.parents}
    for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        if d.is_dir() and not any(d.iterdir()):  # folders still holding the developer's own files stay
            d.rmdir()
    mcp_path = vscode / "mcp.json"
    mcp = json.loads(_strip_jsonc(mcp_path.read_text(encoding="utf-8"))) if mcp_path.exists() else {}
    servers = mcp.setdefault("servers", {})
    for name in previous["servers"]:
        servers.pop(name, None)
    if uninstall:
        mcp_path.parent.mkdir(parents=True, exist_ok=True)
        mcp_path.write_text(json.dumps(mcp, indent=2) + "\n", encoding="utf-8")
        if manifest_path.exists():
            manifest_path.unlink()
        return [f"removed {len(previous['files'])} files and {len(previous['servers'])} servers"]

    plugin = cfg.root / PLUGIN_DIR
    if not (plugin / "plugin.json").exists():
        raise FileNotFoundError("plugin/ is not built; run `harness build` first")
    scripts_abs = (home / "scripts").as_posix()
    written: list[str] = []

    def put(dest: Path, data: bytes) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        written.append(str(dest))

    for src in sorted((cfg.root / SCRIPTS_DIR).glob("*")):
        put(home / "scripts" / src.name, src.read_bytes())
    for src in sorted((cfg.root / SKILLS_DIR).rglob("*")):
        if src.is_file():
            put(copilot / "skills" / src.relative_to(cfg.root / SKILLS_DIR), src.read_bytes())
    for src in sorted((cfg.root / AGENTS_DIR).glob("*.agent.md")):
        text = src.read_text(encoding="utf-8")
        text = text.replace("${PLUGIN_ROOT}/scripts", scripts_abs).replace(f"]({SKILL_LINK_PREFIX}/", "](../skills/")
        put(copilot / "agents" / src.name, text.encode())

    plugin_mcp = json.loads((plugin / "mcp.json").read_text(encoding="utf-8"))
    added = []
    for name, entry in plugin_mcp.get("mcpServers", {}).items():
        if name in servers:
            messages.append(f"kept your existing server {name} in mcp.json")
            continue
        servers[name] = entry
        added.append(name)
    mcp_path.parent.mkdir(parents=True, exist_ok=True)
    mcp_path.write_text(json.dumps(mcp, indent=2) + "\n", encoding="utf-8")
    home.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({"files": written, "servers": added}, indent=2) + "\n")
    if shutil.which("node") is None:
        messages.append("node was not found; the hook and the npx servers need it")
    messages.insert(0, f"installed {len(written)} files under {copilot} and {len(added)} servers into {mcp_path}")
    return messages
