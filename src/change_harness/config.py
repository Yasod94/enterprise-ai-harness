"""Load and merge harness configuration.

Team defaults ship in the package (data/harness.yaml). A service repo extends
them with .harness/extensions.yaml. Personal settings in harness.local.yaml are
read by the hook at runtime only, so they never change committed files.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

DATA = resources.files("change_harness") / "data"
EXTENSIONS_PATH = Path(".harness/extensions.yaml")
PLACEHOLDER = re.compile(r"<[a-z0-9][a-z0-9 _.-]*>", re.IGNORECASE)

BUILTIN_TOOL_ESTIMATE = {
    "read": 3, "search": 4, "edit": 3, "execute": 3, "agent": 1, "todos": 1, "web": 1, "browser": 6,
}


class ConfigError(Exception):
    pass


def read_yaml(path: Path | Any) -> dict:
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return data


def deep_merge(base: dict, extra: dict | None) -> dict:
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def has_placeholder(value: Any) -> bool:
    if isinstance(value, str):
        return bool(PLACEHOLDER.search(value)) and "${" not in value
    if isinstance(value, dict):
        return any(has_placeholder(v) for v in value.values())
    if isinstance(value, list):
        return any(has_placeholder(v) for v in value)
    return False


@dataclass
class Config:
    raw: dict
    repo: Path
    extensions: dict = field(default_factory=dict)

    # ---- accessors ----
    @property
    def servers(self) -> dict[str, dict]:
        return self.raw.get("servers", {})

    @property
    def capabilities(self) -> dict[str, dict]:
        return self.raw.get("capabilities", {})

    @property
    def agents(self) -> dict[str, dict]:
        return self.raw.get("agents", {})

    @property
    def stages(self) -> list[dict]:
        return self.raw.get("stages", [])

    @property
    def stage_ids(self) -> list[str]:
        return [s["id"] for s in self.stages]

    @property
    def skills_ext(self) -> list[dict]:
        return self.raw.get("skills", []) or []

    def server_usable(self, name: str) -> bool:
        server = self.servers.get(name)
        if not server or server.get("enabled", True) is False:
            return False
        return not has_placeholder({k: v for k, v in server.items() if k not in ("stages", "tools")})

    def server_tools(self, name: str) -> list[str]:
        server = self.servers[name]
        tools = server.get("tools")
        if tools:
            return [f"{name}/{t}" for t in tools]
        return [f"{name}/*"]

    def effective_bind(self, cap: str) -> str | None:
        """The server that answers a capability: `bind`, else `alt` when bind is not usable."""
        spec = self.capabilities.get(cap, {})
        for name in [spec.get("bind"), *(spec.get("alt") or [])]:
            if name and self.server_usable(name):
                return name
        return None

    def capability_tools(self, cap: str) -> list[str]:
        server = self.effective_bind(cap)
        if not server:
            return []
        narrowed = self.capabilities[cap].get("tools")
        if isinstance(narrowed, dict):
            narrowed = narrowed.get(server)
        elif server != self.capabilities[cap].get("bind"):
            narrowed = None
        if narrowed:
            return [f"{server}/{t}" for t in narrowed]
        return self.server_tools(server)

    def stage_of_scope(self, scope: str) -> str | None:
        if scope in self.stage_ids:
            return scope
        return {"implement-task": "implement", "scout": "impact"}.get(scope)

    def agents_for_stage(self, stage: str) -> list[str]:
        """Agents whose scope is the stage, plus their subagents."""
        names: list[str] = []
        for agent_id, agent in self.agents.items():
            if agent.get("scope") == stage:
                names.append(agent_id)
                names.extend(agent.get("subagents", []) or [])
        return list(dict.fromkeys(names))

    def extension_servers_for(self, agent_id: str) -> list[str]:
        result = []
        for name, server in self.servers.items():
            for stage in server.get("stages", []) or []:
                if agent_id in self.agents_for_stage(stage):
                    result.append(name)
        return list(dict.fromkeys(result))

    def skills_for(self, agent_id: str) -> list[str]:
        skills = list(self.agents[agent_id].get("skills", []) or [])
        for entry in self.skills_ext:
            for stage in entry.get("stages", []) or []:
                if agent_id in self.agents_for_stage(stage):
                    skills.append(entry["name"])
        return list(dict.fromkeys(skills))

    def tools_for(self, agent_id: str) -> list[str]:
        agent = self.agents[agent_id]
        tools = list(agent.get("builtin", []) or [])
        if agent.get("subagents") and "agent" not in tools:
            tools.append("agent")  # VS Code needs the agent tool for the `agents` list to work
        for cap in agent.get("capabilities", []) or []:
            tools.extend(self.capability_tools(cap))
        for name in self.extension_servers_for(agent_id):
            if self.server_usable(name):
                tools.extend(self.server_tools(name))
        stage = self.stage_of_scope(agent.get("scope", ""))
        if stage:
            tools.append(f"extras-{stage}")
        return list(dict.fromkeys(tools))

    def estimate_tool_count(self, agent_id: str) -> int:
        total = 0
        for tool in self.tools_for(agent_id):
            if tool.startswith("extras-"):
                continue
            if "/" not in tool:
                total += BUILTIN_TOOL_ESTIMATE.get(tool, 1)
                continue
            server, name = tool.split("/", 1)
            if name == "*":
                total += int(self.servers.get(server, {}).get("est_tools", 20))
            else:
                total += 1
        return total


def load(repo: Path) -> Config:
    base = read_yaml(DATA / "harness.yaml")
    ext_file = repo / EXTENSIONS_PATH
    ext = read_yaml(ext_file) if ext_file.exists() else {}
    merged = merge_extensions(base, ext)
    cfg = Config(raw=merged, repo=repo, extensions=ext)
    errors = validate(cfg)
    if errors:
        raise ConfigError("\n".join(errors))
    return cfg


def merge_extensions(base: dict, ext: dict) -> dict:
    merged = copy.deepcopy(base)
    for name, server in (ext.get("servers") or {}).items():
        merged.setdefault("servers", {})[name] = deep_merge(merged.get("servers", {}).get(name, {}), server)
    for name, cap in (ext.get("capabilities") or {}).items():
        merged.setdefault("capabilities", {})[name] = deep_merge(merged.get("capabilities", {}).get(name, {}), cap)
    for key in ("review", "budgets", "hooks", "models"):
        if ext.get(key):
            merged[key] = deep_merge(merged.get(key, {}) or {}, ext[key])
    merged["skills"] = list(ext.get("skills") or [])
    merged["commands"] = dict(ext.get("commands") or {})
    return merged


def validate(cfg: Config) -> list[str]:
    errors: list[str] = []
    for name, server in cfg.servers.items():
        kind = server.get("type")
        if kind not in ("http", "stdio"):
            errors.append(f"server {name}: type must be http or stdio")
        elif kind == "http" and not server.get("url"):
            errors.append(f"server {name}: http servers need a url")
        elif kind == "stdio" and not server.get("command"):
            errors.append(f"server {name}: stdio servers need a command")
        for stage in server.get("stages", []) or []:
            if stage not in cfg.stage_ids:
                errors.append(f"server {name}: unknown stage {stage}")
    for cap, spec in cfg.capabilities.items():
        if spec.get("bind") not in cfg.servers:
            errors.append(f"capability {cap}: bound to unknown server {spec.get('bind')}")
        for alt in spec.get("alt") or []:
            if alt not in cfg.servers:
                errors.append(f"capability {cap}: alt server {alt} is unknown")
    for agent_id, agent in cfg.agents.items():
        for cap in agent.get("capabilities", []) or []:
            if cap not in cfg.capabilities:
                errors.append(f"agent {agent_id}: unknown capability {cap}")
        for sub in agent.get("subagents", []) or []:
            if sub not in cfg.agents:
                errors.append(f"agent {agent_id}: unknown subagent {sub}")
        if not agent.get("scope"):
            errors.append(f"agent {agent_id}: missing scope")
    for stage in cfg.stages:
        if stage.get("agent") not in cfg.agents:
            errors.append(f"stage {stage.get('id')}: unknown agent {stage.get('agent')}")
    for entry in cfg.skills_ext:
        if not entry.get("name"):
            errors.append("extensions skills: every entry needs a name")
        for stage in entry.get("stages", []) or []:
            if stage not in cfg.stage_ids:
                errors.append(f"skill {entry.get('name')}: unknown stage {stage}")
    return errors
