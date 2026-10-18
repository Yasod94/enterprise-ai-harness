"""A developer's own settings: the template the build ships, and what a given file changes.

One file, ~/.copilot/<plugin>.local.yaml, serves two purposes:

- Keys on the team's `personal_overrides` list are read by the hook on every call,
  so they work with the plugin installed from the marketplace, with no rebuild.
- Every other key is an overlay on harness.yaml. It only takes effect in a personal
  build, `harness install-user`, which builds and installs a copy for one developer.

A harness.local.yaml at the root of one service repo clone takes runtime keys only,
for that clone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from enterprise_ai_harness.config import PERSONAL_RUNTIME_KEYS, Config

TEMPLATE_FILE = "templates/personal-settings.local.yaml"

def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def template(cfg: Config) -> str:
    """The personal settings file, every runtime key commented out at the team's value."""
    allowed = set(cfg.personal_overrides)
    review = cfg.raw.get("review") or {}
    budgets = cfg.raw.get("budgets") or {}
    hooks = cfg.raw.get("hooks") or {}
    runs = cfg.raw.get("runs") or {}
    index = cfg.code_index
    name = cfg.plugin_name
    team = "(the team keeps this one the same for everyone)"
    deep = (cfg.tiers.get("deep") or {}).get("models") or ["Name (copilot)"]
    deep_model = repr_model(deep[0])

    def note(key: str) -> str:
        return "" if key in allowed else f"   {team}"

    out = [
        f"# Personal settings for {name} {cfg.plugin.get('version', '')}.",
        f"# Save as ~/.copilot/{name}.local.yaml (all your repos), or as harness.local.yaml",
        "# at the root of one service repo clone (that clone only; the hook keeps it out of git).",
        "# Uncomment a line to change it; a line left commented keeps the team value shown.",
        "# The hook reads this file on every call, so changes apply at once: no rebuild, no restart.",
        "# Write one `key: value` per line. [lists] and {maps} on one line are ignored here.",
        "",
        f"review:{note('review')}",
    ]
    out.append(f"#  implement: {review.get('implement', 'per-task')}".ljust(34)
               + "# per-task | per-batch-<n> | none; the other stages always end in a review")
    out += ["", f"budgets:{note('budgets')}                  # per agent scope; at the limit, type \"extend\" for 50% more"]
    for scope, spec in budgets.items():
        out.append(f"#  {scope}:")
        for k in ("tool_calls", "output_kb"):
            if k in (spec or {}):
                out.append(f"#    {k}: {spec[k]}")
    out += [
        "",
        "hooks:",
        f"#  audit: {_scalar(hooks.get('audit', False))}".ljust(34) + f"# log tool names to {cfg.runs_dir}/.audit.log"
        + note("hooks.audit"),
        f"#  out_of_plan_files: {hooks.get('out_of_plan_files', 'ask')}".ljust(34) + "# ask | allow | deny"
        + note("hooks.out_of_plan_files"),
        "",
        "runs:",
        f"#  commit: {_scalar(runs.get('commit', False))}".ljust(34) + "# true: the Reviewer commits the run record with the PR"
        + note("runs.commit"),
        "",
        "code_index:",
        f"#  seed_refresh: {index.get('seed_refresh', 'stale')}".ljust(34) + "# stale | every-pr | manual"
        + note("code_index.seed_refresh"),
        f"#  seed_max_age_days: {index.get('seed_max_age_days', 7)}".ljust(34) + "# with stale: refresh when older"
        + note("code_index.seed_max_age_days"),
        f"#  sync: {index.get('sync', '')}" + note("code_index.sync"),
        "",
        "# ---- These need a personal build ----",
        "# Anything else from harness.yaml can go below: models, servers, capabilities, agents.",
        "# The plugin from the marketplace ignores these keys. To apply them, turn the",
        "# marketplace plugin off and, in a clone of the harness repo, run",
        "#   uv run harness install-user",
        "# which builds a copy with this file applied and installs it for you only.",
        "# `~` removes a setting; `enabled: false` turns a server off.",
        "#",
        "# models:",
        "#   agents:",
        "#     planner: deep                 # run the Planner on the deep tier",
        "#   tiers:",
        "#     deep:",
        f"#       models: [{deep_model}]",
        "# servers:",
        "#   kb:",
        "#     url: https://kb.example.internal/mcp",
        "#     enabled: true",
        "#   ctx:",
        "#     enabled: false                # do without context-mode; its capability falls back",
        "",
    ]
    return "\n".join(out)


def repr_model(name: str) -> str:
    return '"' + name.replace('"', '\\"') + '"'


def describe(cfg: Config, data: dict, repo_level: bool = False) -> dict[str, list[str]]:
    """Sort the keys of a personal file: applied by the hook now, needing a personal build,
    or ignored (not allowed by the team, or not usable in a repo-level file)."""
    allowed = set(cfg.personal_overrides)
    result: dict[str, list[str]] = {"now": [], "build": [], "ignored": []}
    for top, value in (data or {}).items():
        children = value.items() if isinstance(value, dict) else [(None, value)]
        for child, _ in children:
            dotted = f"{top}.{child}" if child is not None else str(top)
            runtime = top if top in ("review", "budgets") else dotted
            if runtime in PERSONAL_RUNTIME_KEYS:
                (result["now"] if runtime in allowed else result["ignored"]).append(dotted)
            elif repo_level:
                result["ignored"].append(dotted)
            else:
                result["build"].append(dotted)
    return result


def read(path: Path) -> dict:
    data = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}
