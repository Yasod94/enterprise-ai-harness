# change-harness

A change workflow for GitHub Copilot in VS Code. A developer gives it a Jira key or a sentence, and Copilot works through six stages: intake, grill, impact, plan, implement and verify. Each stage ends with a short review card, and nothing moves on until the developer approves. Code edits stay locked until the plan is approved.

Every external tool sits behind a capability name (`tickets.read`, `code.graph`, ...). One config file decides which MCP server answers each capability, so swapping a server is a one-line change.

## For developers

1. Open the service repo in VS Code with Copilot signed in.
2. Pick **Change Orchestrator** in the agents dropdown and type a Jira key or describe the change.
3. Click through the browser sign-ins VS Code shows the first time each server is used.

There is nothing to install beyond Node (already needed for the npx-based servers). The agents, skills, hooks and `.vscode/mcp.json` are committed to the repo.

### How a run works

| Stage | Agent | Writes | You review |
| --- | --- | --- | --- |
| 1 Intake | Intake | `requirement.md` | the summary of the ticket or request |
| 2 Grill | Griller (grill-me) | `clarifications.md` | the decisions, asked in rounds |
| 3 Impact | Impact Analyst + Component Scout | `impact.md`, `impact.json` | which components change, with evidence |
| 4 Plan | Planner | `plan.md` | per-component changes and tasks; approving unlocks code edits |
| 5 Implement | Implementer + Task Runner | code, `progress.md` | each task's diff and check result |
| 6 Verify | Reviewer | `verification.md`, the PR | plan coverage, full test run, review |

Runs live in `.changes/<run-id>/`. The run id is the Jira key, or `local-<date>-<slug>` for a plain request.

Each stage ends with a card like this, and buttons under it:

```text
Stage 3 of 6 · Impact analysis
Summary    4 components, 3 high confidence, 1 low
Changed    impact.md, impact.json
Question   Does the pricing consumer read the new field?
Skipped    none
Cost       18 of 80 tool calls
[Approve and continue]  [Revise]  [Stop]
```

You can also type `approve`, `revise: <note>`, `stop`, `extend` (adds 50% to the stage budget), or, to the orchestrator, `status`, `resume <run-id>`, `back to <stage>`, `help`, `add skill ...`, `add server ...`.

You can edit any artifact before approving it. Approving records a hash of each artifact; if one changes afterwards, the next stage refuses to start until it is approved again.

## For the harness maintainer

Set up a service repo once:

```bash
uvx --from git+https://github.com/<org>/change-harness change-harness init \
  --source https://github.com/<org>/change-harness
```

Then fill in the KB MCP URL (in this repo's `harness.yaml` for every repo, or in the service repo's `.harness/extensions.yaml`), run `harness doctor`, and commit. `init` writes:

```text
.github/agents/*.agent.md        one agent per stage, plus orchestrator and subagents
.github/skills/*/SKILL.md        embedded skills (vendored and team-written)
.github/hooks/harness/           hook script and its rendered config
.github/workflows/change-harness.yml   renders PR changes and opens upgrade PRs
.vscode/mcp.json                 servers (foreign servers already there are kept)
.harness/extensions.yaml         repo-owned additions; sync never overwrites it
.harness/memory/                 conventions.md, decisions.md
.harness/lock.json               harness version, source, hashes of rendered files
AGENTS.md                        a marked block is added; the rest is left alone
```

Commands:

| Command | What it does |
| --- | --- |
| `harness init` | first-time setup of a service repo |
| `harness sync [--check] [--force]` | re-render from config; `--check` exits 1 if anything would change |
| `harness doctor [--live]` | config, placeholders, tool counts and tool-definition tokens per agent, hand edits, skills, secrets |
| `harness add skill <owner/repo> --skill <name> --stages grill` | vendor a skill from GitHub with a pinned commit and `SOURCE.md` |
| `harness add skill <folder> --stages plan` | register a skill folder in the repo |
| `harness add server <name> --url <url> --stages impact --tools a,b` | add an MCP server for some stages |
| `harness add server <name> --command <cmd> --capability db.schema` | point a capability at a different server for this repo |
| `harness status` | list runs and their stage |

`sync` refuses to overwrite a rendered file that someone edited by hand and lists it instead. Put the change in `.harness/extensions.yaml`, or rerun with `--force`.

Upgrades: tag a release here. `release-dispatch.yml` sends a dispatch to each repo in `repos.txt`, and each repo's workflow opens an upgrade PR with the doctor output as its description. If this repo is private, service repos need a `CHANGE_HARNESS_TOKEN` secret with read access.

## Adding skills and MCP servers

| Level | Skill | MCP server |
| --- | --- | --- |
| Personal | drop a folder in `~/.copilot/skills/<name>/` | add it to your user `mcp.json` and put its tools in a tool set named `extras-<stage>` (e.g. `extras-impact`); every stage agent already lists one |
| Repo | `harness add skill ...`, or a folder in `.github/skills/<name>/` | `harness add server ...`, or edit `.harness/extensions.yaml` |
| Team | PR here under `src/change_harness/data/skills/` | PR here to `src/change_harness/data/harness.yaml` |

A repo skill with the same name as an embedded one replaces it for that repo; doctor reports it. Listing a skill under `stages` makes those stage agents load it at start. Without that, Copilot loads it on demand from its description.

Personal settings go in `harness.local.yaml` at the repo root (gitignored). The hook reads it directly, so only simple nested `key: value` lines are supported:

```yaml
review:
  implement: per-batch-3
budgets:
  impact:
    tool_calls: 120
hooks:
  audit: true
```

## Configuration

`src/change_harness/data/harness.yaml` holds the team defaults:

- `servers`: MCP servers. HTTP servers sign in with OAuth in VS Code; stdio servers start on demand. `tools` narrows a server to named tools; `est_tools` feeds the doctor's tool-count check. A server with `enabled: false` or a `<placeholder>` value is not rendered, and its capabilities use their fallback.
- `capabilities`: `bind` (server), `alt` (servers to use if `bind` is not configured), `tools` (narrowing per capability), `setup` (a one-time command the Harness Check runs), `fallback` (what an agent does when nothing answers).
- `stages`, `agents`: stage order, artifacts, and each agent's built-in tool sets, capabilities, subagents and start-up skills.
- `review`, `budgets`, `models`, `hooks`.

`.harness/extensions.yaml` in a service repo can add `skills`, `servers`, `inputs`, and override `capabilities`, `review`, `budgets`, `models` and `commands` (build and test commands rendered into AGENTS.md).

Secrets never go in these files. Use OAuth servers, or an `inputs` entry with `password: true` and `${input:<id>}` in the server's headers or env; VS Code keeps the value in its secret storage.

## How approvals and budgets are enforced

`harness-hook.cjs` runs as agent-scoped hooks, set in each harness agent's frontmatter, so it only runs while a harness agent is active:

- `UserPromptSubmit` records approvals. The approve buttons send `[harness:approve <stage>]`; the hook checks earlier stages are approved and unchanged, hashes the stage's artifacts into `state.json`, and moves the run to the next stage. Plan approval also writes `approval.json`.
- `PreToolUse` limits edits: before plan approval, harness agents can only edit `.changes/`; after it, the Implementer and Task Runner can edit code, and the hook asks before edits to files not listed in `impact.json` (test files excepted). The Reviewer can only edit `.changes/`. It also enforces tool-call and output budgets per stage.
- `PostToolUse` counts output size. `PreCompact` appends a status snapshot to `progress.md`.

Agents cannot edit `state.json`, `approval.json`, `.usage.json` or (except the orchestrator) `.changes/current`.

This is a guardrail against mistakes, not a security boundary. Branch protection and normal PR review remain the controls that matter.

## Token use

- Each agent sees only its own tools. Atlassian (about 120 tools) and the GitHub read server are narrowed to the handful each capability needs. `harness doctor` prints the tool count and an estimate of tool-definition tokens per agent; with every server configured, the defaults come to 13 to 33 tools per agent (KB counted at 8).
- Heavy reading happens in subagents (Component Scout, Task Runner) that return summaries of 20 to 30 lines, so file contents stay out of the main conversation.
- Stages hand over through files, with size limits per artifact. A new chat can resume any run from those files with `resume <run-id>`.
- codegraph returns symbols and call paths instead of whole files; context-mode keeps large command output in a sandbox.
- Start-up skills are kept short. Long ones (writing-plans, subagent-driven-development) are available but load only on demand.

## Embedded skills

| Skill | Source | Used by |
| --- | --- | --- |
| grill-me, grilling | [mattpocock/skills](https://github.com/mattpocock/skills) (MIT) | Griller, at start |
| to-questionnaire, handoff, tdd, code-review | mattpocock/skills (MIT) | on demand; code-review at Reviewer start |
| writing-plans, subagent-driven-development, verification-before-completion | [obra/superpowers](https://github.com/obra/superpowers) (MIT) | on demand |
| change-artifacts, intake, impact-analysis, plan-format | this repo | stage agents, at start |

grill-me's own file is a pointer to grilling, so both are vendored. Each vendored skill carries `SOURCE.md` (repo, path, commit, license) and the upstream `LICENSE`. Vendored files are not edited; harness rules live in the agent that loads the skill.

## Check during the pilot

- VS Code session target. The hook targets the **Local** session target (agent-scoped hooks are a Local feature). The Copilot (Agent Host) target uses the Copilot SDK hook format; test there before relying on it.
- Tool names in hook payloads. Turn on `hooks.audit` for a day; `.changes/.audit.log` shows the real tool names, so `edit_tools` and `terminal_tools` patterns can be tightened.
- Narrowed tool names. The Atlassian names come from Atlassian's supported-tools page; the GitHub read names are the common ones from github-mcp-server. If a name is wrong, VS Code ignores it and the agent uses the fallback, so the Harness Check will show it.
- GitHub Enterprise Server needs the local GitHub MCP server instead of the hosted one. An Atlassian Data Center site needs a self-hosted Atlassian MCP server.
- The context-mode package is launched with npx and not vendored; check its license with your OSS review.

## Development

```bash
uv run --extra test pytest -q     # needs node for the hook tests
```

Layout:

```text
src/change_harness/
  cli.py, config.py, render.py, doctor.py, extend.py
  data/harness.yaml              team defaults
  data/agents/*.agent.md         agent templates ({{generated}}, {{skills}}, {{capabilities}}, {{agent:id}})
  data/skills/vendor/            vendored skills, pinned
  data/skills/team/              team-written skills and artifact templates
  data/hooks/harness-hook.cjs    the hook
  data/repo/                     files seeded into service repos
  data/workflows/                the service-repo workflow
tests/
```
