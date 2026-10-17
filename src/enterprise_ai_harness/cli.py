"""Command line for the harness maintainer, run inside the harness repo.

Developers do not need it: they install the plugin in VS Code. `install-user`
is the fallback for machines where agent plugins are not allowed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from enterprise_ai_harness import __version__
from enterprise_ai_harness.build import BuildReport, build
from enterprise_ai_harness.config import ConfigError, load
from enterprise_ai_harness.doctor import check, summarize
from enterprise_ai_harness.extend import ExtendError, add_server, add_skill
from enterprise_ai_harness.install import install_user


def _csv(value: str | None) -> list[str] | None:
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


def print_build(report: BuildReport, check_mode: bool) -> None:
    for path in report.written:
        print(f"  {'would write' if check_mode else 'wrote'} {path}")
    for path in report.removed:
        print(f"  {'would remove' if check_mode else 'removed'} {path}")
    for name in report.skipped_servers:
        print(f"  · server {name} left out (disabled or placeholder); its capabilities use their fallback")
    if not report.changed:
        print("  plugin is up to date")


def cmd_build(args) -> int:
    cfg = load(args.root)
    report = build(cfg, check=args.check)
    print_build(report, args.check)
    return 1 if args.check and report.changed else 0


def cmd_doctor(args) -> int:
    text, code = summarize(check(args.root, live=args.live))
    print(text)
    return code


def cmd_add(args) -> int:
    cfg = load(args.root)
    if args.kind == "skill":
        messages = add_skill(cfg, args.source, skill=args.skill, stages=_csv(args.stages), name=args.name)
    else:
        messages = add_server(cfg, args.name, url=args.url, command=args.command, args=args.arg,
                              stages=_csv(args.stages), tools=_csv(args.tools), est_tools=args.est_tools,
                              capability=args.capability)
    for m in messages:
        print(f"  {m}")
    print_build(build(load(cfg.root)), False)
    print("\nCommit the change, open a PR, and bump plugin.version when you release.")
    return 0


def cmd_install_user(args) -> int:
    cfg = load(args.root)
    for m in install_user(cfg, copilot_dir=args.copilot_dir, vscode_user_dir=args.vscode_user_dir,
                          uninstall=args.uninstall):
        print(f"  {m}")
    return 0


def cmd_status(args) -> int:
    repo = Path(args.repo).resolve()
    changes = repo / ".ai-harness"
    current = (changes / "current").read_text().strip() if (changes / "current").exists() else None
    runs = sorted(changes.glob("*/state.json")) if changes.exists() else []
    if not runs:
        print("no runs in this clone")
        return 0
    for state_file in runs:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        run = state_file.parent.name
        approved = [k for k in (state.get("approvals") or {}) if k != "tasks"]
        print(f"{'*' if run == current else ' '} {run:40} stage {state.get('stage', '?'):10} "
              f"{state.get('stage_status', ''):8} approved: {', '.join(approved) or 'none'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="harness", description="Build and check the enterprise-ai-harness plugin")
    p.add_argument("--version", action="version", version=f"enterprise-ai-harness {__version__}")
    p.add_argument("--root", type=Path, default=None, help="harness repo root (default: found from the current folder)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("build", help="render plugin/ and the marketplace manifest from sources/ and harness.yaml")
    sp.add_argument("--check", action="store_true", help="exit 1 if plugin/ is out of date (CI)")
    sp.set_defaults(func=cmd_build)

    sp = sub.add_parser("doctor", help="check configuration, tool counts, skills, secrets and build state")
    sp.add_argument("--live", action="store_true", help="also check that servers are reachable from here")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("add", help="add a team skill or MCP server, then rebuild")
    add_sub = sp.add_subparsers(dest="kind", required=True)
    sk = add_sub.add_parser("skill", help="vendor a skill from GitHub or copy a local skill folder")
    sk.add_argument("source", help="owner/repo[@ref] or a local folder with SKILL.md")
    sk.add_argument("--skill", help="skill folder name inside the repo (e.g. grill-with-docs)")
    sk.add_argument("--name", help="name to install it under (default: the skill name)")
    sk.add_argument("--stages", help="comma-separated stages that load it at start")
    sv = add_sub.add_parser("server", help="add an MCP server")
    sv.add_argument("name")
    sv.add_argument("--url", help="http server URL")
    sv.add_argument("--command", help="stdio server command")
    sv.add_argument("--arg", action="append", help="stdio argument (repeat)")
    sv.add_argument("--stages", help="comma-separated stages whose agents get its tools")
    sv.add_argument("--tools", help="comma-separated tool names to allow (recommended)")
    sv.add_argument("--est-tools", type=int, help="number of tools the server exposes, for doctor")
    sv.add_argument("--capability", help="point an existing capability at this server")
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("install-user", help="fallback: install into ~/.copilot and the VS Code user mcp.json")
    sp.add_argument("--copilot-dir", type=Path)
    sp.add_argument("--vscode-user-dir", type=Path)
    sp.add_argument("--uninstall", action="store_true")
    sp.set_defaults(func=cmd_install_user)

    sp = sub.add_parser("status", help="list change runs in a service repo clone")
    sp.add_argument("--repo", default=".")
    sp.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, ExtendError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
