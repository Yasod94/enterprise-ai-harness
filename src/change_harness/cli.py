"""Command line for harness maintainers and the PR workflow.

Developers never need this: they open the repo in VS Code and pick the Change
Orchestrator agent. The commands here render and check the committed files.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from change_harness import __version__
from change_harness.config import DATA, EXTENSIONS_PATH, ConfigError
from change_harness.doctor import check, summarize
from change_harness.extend import ExtendError, add_server, add_skill, ensure_extensions_file
from change_harness.render import SyncReport, sync


def _csv(value: str | None) -> list[str] | None:
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


def print_report(report: SyncReport, check_mode: bool) -> None:
    verb = "would write" if check_mode else "wrote"
    for path in report.written:
        print(f"  {verb} {path}")
    for path in report.removed:
        print(f"  {'would remove' if check_mode else 'removed'} {path}")
    for path in report.conflicts:
        print(f"  ! skipped {path}: changed by hand (use --force to overwrite)")
    for name in report.overrides:
        print(f"  · skill {name} is provided by this repo; harness version not rendered")
    for name in report.skipped_servers:
        print(f"  · server {name} not rendered (disabled or placeholder); its capabilities use their fallback")
    for note in report.notes:
        print(f"  · {note}")
    if not (report.written or report.removed or report.conflicts):
        print("  up to date")


def cmd_init(args) -> int:
    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists() and not args.no_git_check:
        print(f"{repo} is not a git repository (use --no-git-check to continue anyway)", file=sys.stderr)
        return 2
    ensure_extensions_file(repo)
    memory = repo / ".harness/memory"
    for name in ("conventions.md", "decisions.md"):
        target = memory / name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text((DATA / "repo" / "memory" / name).read_text(encoding="utf-8"), encoding="utf-8")
    example = repo / ".harness/harness.local.example.yaml"
    if not example.exists():
        example.write_text((DATA / "repo" / "harness.local.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    print(f"change-harness {__version__}: initialising {repo}")
    report = sync(repo, force=args.force, source=args.source)
    print_report(report, False)
    text, code = summarize(check(repo))
    print("\nDoctor:\n" + text)
    print("\nNext: commit the rendered files. Developers open the repo in VS Code and pick Change Orchestrator.")
    return code


def cmd_sync(args) -> int:
    repo = Path(args.repo).resolve()
    report = sync(repo, check=args.check, force=args.force, source=args.source)
    print_report(report, args.check)
    if args.check and report.changed:
        return 1
    return 1 if report.conflicts and not args.force else 0


def cmd_doctor(args) -> int:
    text, code = summarize(check(Path(args.repo), live=args.live))
    print(text)
    return code


def cmd_add(args) -> int:
    repo = Path(args.repo).resolve()
    if args.kind == "skill":
        messages = add_skill(repo, args.source, skill=args.skill, stages=_csv(args.stages), name=args.name)
    else:
        messages = add_server(repo, args.name, url=args.url, command=args.command, args=args.arg,
                              stages=_csv(args.stages), tools=_csv(args.tools), est_tools=args.est_tools,
                              capability=args.capability)
    for m in messages:
        print(f"  {m}")
    if args.no_sync:
        print(f"  run `harness sync` (or open a PR) to render the change")
        return 0
    report = sync(repo)
    print_report(report, False)
    print(f"\nCommit {EXTENSIONS_PATH} and the rendered files, then open a PR.")
    return 0


def cmd_status(args) -> int:
    repo = Path(args.repo).resolve()
    changes = repo / ".changes"
    current = (changes / "current").read_text().strip() if (changes / "current").exists() else None
    runs = sorted(changes.glob("*/state.json")) if changes.exists() else []
    if not runs:
        print("no runs yet")
        return 0
    for state_file in runs:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        run = state_file.parent.name
        approved = [k for k in (state.get("approvals") or {}) if k != "tasks"]
        marker = "*" if run == current else " "
        print(f"{marker} {run:40} stage {state.get('stage', '?'):10} {state.get('stage_status', ''):8} "
              f"approved: {', '.join(approved) or 'none'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="harness", description="Copilot change workflow harness")
    p.add_argument("--version", action="version", version=f"change-harness {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def repo_arg(sp):
        sp.add_argument("--repo", default=".", help="service repo root (default: current directory)")

    sp = sub.add_parser("init", help="set up a service repo (run once, then commit)")
    repo_arg(sp)
    sp.add_argument("--source", help="git URL of the change-harness repo, recorded for the PR workflow")
    sp.add_argument("--force", action="store_true", help="overwrite existing files with the same paths")
    sp.add_argument("--no-git-check", action="store_true")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("sync", help="render agents, skills, hooks and mcp.json from config")
    repo_arg(sp)
    sp.add_argument("--check", action="store_true", help="exit 1 if anything would change (CI)")
    sp.add_argument("--force", action="store_true", help="overwrite files edited by hand")
    sp.add_argument("--source", help="update the recorded harness source URL")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("doctor", help="check configuration, tool counts, skills and secrets")
    repo_arg(sp)
    sp.add_argument("--live", action="store_true", help="also check that servers are reachable from here")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("add", help="add a skill or MCP server for this repo")
    add_sub = sp.add_subparsers(dest="kind", required=True)
    sk = add_sub.add_parser("skill", help="vendor a skill from GitHub or register a local folder")
    repo_arg(sk)
    sk.add_argument("source", help="owner/repo[@ref], github:owner/repo, or a local folder with SKILL.md")
    sk.add_argument("--skill", help="skill folder name inside the repo (e.g. grill-with-docs)")
    sk.add_argument("--name", help="name to install it under (default: the skill name)")
    sk.add_argument("--stages", help="comma-separated stages that load it at start")
    sk.add_argument("--no-sync", action="store_true")
    sv = add_sub.add_parser("server", help="add an MCP server")
    repo_arg(sv)
    sv.add_argument("name")
    sv.add_argument("--url", help="http server URL")
    sv.add_argument("--command", help="stdio server command")
    sv.add_argument("--arg", action="append", help="stdio argument (repeat)")
    sv.add_argument("--stages", help="comma-separated stages whose agents get its tools")
    sv.add_argument("--tools", help="comma-separated tool names to allow (recommended)")
    sv.add_argument("--est-tools", type=int, help="number of tools the server exposes, for doctor")
    sv.add_argument("--capability", help="bind an existing capability to this server for this repo")
    sv.add_argument("--no-sync", action="store_true")
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("status", help="list change runs in this repo")
    repo_arg(sp)
    sp.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, ExtendError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
