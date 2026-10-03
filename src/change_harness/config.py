"""Load and validate the team configuration (harness.yaml) of a harness repo."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CONFIG_FILE = "harness.yaml"
PLACEHOLDER = re.compile(r"<[a-z0-9][a-z0-9 _.-]*>", re.IGNORECASE)

BUILTIN_TOOL_ESTIMATE = {
    "read": 3, "search": 4, "edit": 3, "execute": 3, "agent": 1, "todos": 1, "web": 1, "browser": 6,
}


class ConfigError(Exception):
    pass


def find_root(start: Path | None = None) -> Path:
    """Walk up from `start` to the folder that holds harness.yaml and sources/."""
    here = (start or Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / CONFIG_FILE).exists() and (candidate / "sources").is_dir():
            return candidate
    raise ConfigError(f"no {CONFIG_FILE} with a sources/ folder found in {here} or its parents")


def read_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return data


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
    root: Path

    @property
    def sources(self) -> Path:
        return self.root / "sources"

    @property
    def plugin(self) -> dict:
        return self.raw.get("plugin", {}) or {}

    @property
    def plugin_name(self) -> str:
        return self.plugin.get("name", "change-harness")

    @property
    def servers(self) -> dict[str, dict]:
        return self.raw.get("servers", {}) or {}

    @property
    def capabilities(self) -> dict[str, dict]:
        return self.raw.get("capabilities", {}) or {}

    @property
    def agents(self) -> dict[str, dict]:
        return self.raw.get("agents", {}) or {}

    @property
    def stages(self) -> list[dict]:
        return self.raw.get("stages", []) or []

    @property
    def stage_ids(self) -> list[str]:
        return [s["id"] for s in self.stages]

    @property
    def skills_ext(self) -> list[dict]:
        return self.raw.get("skills", []) or []

    @property
    def code_index(self) -> dict:
        return copy.deepcopy(self.capabilities.get("code.graph", {}).get("index", {}) or {})

    def server_usable(self, name: str) -> bool:
        server = self.servers.get(name)
        if not server or server.get("enabled", True) is False:
            return False
        return not has_placeholder({k: v for k, v in server.items() if k not in ("stages", "tools")})

    def tool_ref(self, server: str, tool: str) -> str:
        """How agents name a server's tool. `plugin.server_prefix` covers hosts that
        namespace plugin-provided servers (set it if VS Code shows them prefixed)."""
        return f"{self.plugin.get('server_prefix', '')}{server}/{tool}"

    def server_tools(self, name: str) -> list[str]:
        tools = self.servers[name].get("tools")
        if tools:
            return [self.tool_ref(name, t) for t in tools]
        return [self.tool_ref(name, "*")]

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
            return [self.tool_ref(server, t) for t in narrowed]
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

    def stage_servers_for(self, agent_id: str) -> list[str]:
        """Servers added for specific stages (`stages:` on the server) that reach this agent."""
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
        for name in self.stage_servers_for(agent_id):
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
            server = server.removeprefix(self.plugin.get("server_prefix", ""))
            total += int(self.servers.get(server, {}).get("est_tools", 20)) if name == "*" else 1
        return total


def load(root: Path | None = None) -> Config:
    root = find_root(root) if root is None or not (Path(root) / CONFIG_FILE).exists() else Path(root).resolve()
    cfg = Config(raw=read_yaml(root / CONFIG_FILE), root=root)
    errors = validate(cfg)
    if errors:
        raise ConfigError("\n".join(errors))
    return cfg


def validate(cfg: Config) -> list[str]:
    errors: list[str] = []
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", cfg.plugin_name):
        errors.append("plugin.name must be lowercase letters, digits and dashes")
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
        if not (cfg.sources / "agents" / f"{agent_id}.agent.md").exists():
            errors.append(f"agent {agent_id}: sources/agents/{agent_id}.agent.md is missing")
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
            errors.append("skills: every entry needs a name")
        for stage in entry.get("stages", []) or []:
            if stage not in cfg.stage_ids:
                errors.append(f"skill {entry.get('name')}: unknown stage {stage}")
    refresh = cfg.code_index.get("seed_refresh", "stale")
    if refresh not in ("stale", "every-pr", "manual"):
        errors.append("code.graph index.seed_refresh must be stale, every-pr or manual")
    return errors
