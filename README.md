# enterprise-ai-harness

A VS Code agent plugin for GitHub Copilot. A developer gives it a Jira key or a sentence, and Copilot works through six stages: intake, grill, impact, plan, implement and verify. Each stage ends with a short review card, and nothing moves on until the developer approves. Code edits stay locked until the plan is approved.

The plugin is installed once per developer. Service repos stay as they are: run files are kept out of git through `.git/info/exclude`, and the only thing the workflow commits besides the code change is an optional code graph seed (`.codegraph-seed/`) so later clones skip the index build.

Every external tool sits behind a capability name (`tickets.read`, `code.graph`, ...). `harness.yaml` decides which MCP server answers each capability, so swapping a server is a one-line change and a rebuild.

Setup steps for developers and maintainers are in [SETUP.md](SETUP.md).

## How a run works

| Stage | Agent | Writes | You review |
| --- | --- | --- | --- |
| 1 Intake | Intake | `requirement.md` | the summary of the ticket or request |
| 2 Grill | Griller (grill-me) | `clarifications.md` | the decisions, asked in rounds |
| 3 Impact | Impact Analyst + Component Scout | `impact.md`, `impact.json` | which components change, with evidence |
| 4 Plan | Planner, then Plan Checker on a stronger model | `plan.md`, `plan-check.md` | per-component changes, tasks and each task's model tier; approving unlocks code edits |
| 5 Implement | Implementer + a Task Runner per tier | code, `progress.md` | each task's diff and check result |
| 6 Verify | Reviewer | `verification.md`, the PR | plan coverage, full test run, review |

Runs live in `.ai-harness/<run-id>/` in the clone. The run id is the Jira key, or `local-<date>-<slug>` for a plain request. The folder name is set by `runs.dir`; it is not `.changes/` (changie's folder) or `.harness/` (Harness CI's), so excluding it from git hides nothing else.

Each stage ends with a card like this, with buttons under it:

```text
Stage 3 of 6 · Impact analysis
Summary    4 components, 3 high confidence, 1 low
Changed    impact.md, impact.json
Question   Does the pricing consumer read the new field?
Skipped    none
Cost       18 of 80 tool calls
[Approve and continue]  [Revise]  [Stop]
```

Typed commands work when the whole message is the command: `approve`, `revise: <note>`, `stop`, `extend` (adds 50% to the tool-call and output budgets of the current stage and its subagents), `back to <stage>`, `save code graph`, and, to the orchestrator, `resume <run-id>`. The orchestrator also answers `status`, `models`, `help`, `add skill ...` and `add server ...` in any wording. A sentence that only mentions a command, or a quoted `[harness:...]` marker, does nothing.

You can edit any artifact before approving it. Approving records a hash of each artifact. If an approved artifact changes afterwards, later approvals are refused, and so are code edits once the plan is involved, until the changed stage is approved again.

## Models

Each stage agent runs on a model tier, and each plan task carries its own tier. The tiers live in `harness.yaml`:

```yaml
models:
  tiers:                      # cheapest first; the first available model in a list is used
    fast:     { models: ["Claude Haiku 4.5 (copilot)", "GPT-5 mini (copilot)"], use_for: "config, renames, wiring" }
    standard: { models: ["Claude Sonnet 5.5 (copilot)", "GPT-5.4 (copilot)"], use_for: "feature logic in one component" }
    deep:     { models: ["Claude Opus 5.5 (copilot)", "GPT-5.5 (copilot)"], use_for: "concurrency, migrations, contracts" }
  agents:                     # leave an agent out to use the model picked in the chat
    griller: deep
    impact-analyst: deep
    planner: standard
    implementer: standard
    task-runner: by-task      # one Task Runner per tier
  task_default: standard
  cross_check:
    plan:   { checker: deep, when: below }   # required: the planner's tier is below deep
    impact: { checker: deep, when: offer }   # a button, not required
```

Who decides what:

| Decision | Made by | Where |
| --- | --- | --- |
| Which models each tier means, and each stage agent's tier | the team | `harness.yaml`, shipped in the plugin |
| Each task's tier | the Planner proposes, the person changes it during plan review | `- Tier:` on each task in `plan.md`; approval locks it |
| Running a higher-tier task | the person | "Continue on the deep model" button in the Implementer |
| Skipping a required cross-check | the person | "Approve and skip the cross-check", recorded in `approval.json` |

How it works:

- The build writes each agent's tier into its `model:` frontmatter and builds one Task Runner per tier ("Task Runner (fast)", ...). The Implementer runs each task with the Task Runner for its `Tier`.
- VS Code does not start a subagent on a model that costs more than the calling session's model. A standard Implementer therefore cannot start the deep Task Runner directly. It stops before a deep task and shows "Continue on the deep model", a handoff that switches the session's model; after that, every tier up to deep can run. A new chat starts on the Implementer's own tier again.
- The hook refuses a Task Runner whose tier differs from the task's `Tier` in the approved plan, or that sits above the session's tier, and its status line tells the agent which tier the session is on.
- Cross-check: when the Planner's tier is below the checker's, the plan needs a review by the Plan Checker on the deep model before approval. The Planner's card has "Cross-check with the deep model". The Checker writes `plan-check.md`, a verdict and up to 10 findings, each with evidence. "Apply the findings" sends them back to the Planner, which fixes what it agrees with and lists what it did with each finding in `plan.md`. The hook blocks plan approval until the check has run, unless the person presses "Approve and skip the cross-check". The Checker can write only its `plan-check.md`. `approval.json` records the verdict and whether the plan changed after the check, or the skip.
- `when: always` requires the check whatever the tiers; `offer` adds the button only; `never` removes the Checker.
- `harness doctor` reports an error when an agent's subagent has a higher tier than the agent, since VS Code would refuse to start it.

Model names must match what the VS Code model picker shows for your Copilot plan, qualified as `Name (vendor)`. Check them when your organization changes its enabled models.

## Code graph

The agents use [CodeGraph](https://github.com/colbymchenry/codegraph) to find symbols, callers and call paths without reading whole files. Its index is a SQLite file in `.codegraph/`, which CodeGraph keeps out of git.

To avoid rebuilding that index in every clone, the harness commits a compressed snapshot of it:

```text
.codegraph-seed/
  codegraph.db.gz   gzip of a consistent SQLite snapshot (VACUUM INTO)
  SEED.json         commit it was built at, date, sizes
```

- First prompt in a clone: if `.codegraph/` is missing and a seed exists, the hook restores the index from the seed. CodeGraph compares file sizes, times and hashes when it starts and re-parses only files that changed since the seed.
- The restore checks the seed first: a Git LFS pointer, a file that is not gzip, or one that does not hold a SQLite database is reported once and left alone, and CodeGraph builds the index on its own instead.
- No seed yet: the Harness Check runs `codegraph init --yes` once and asks you to type `save code graph`, then commit `.codegraph-seed/`.
- While the agent edits code: if the CodeGraph server is running, its file watcher keeps the index current. If not, the hook starts one background `codegraph sync` loop after an edit or terminal command in the implement stage. Edits made while it runs are queued, and the loop syncs again before it exits, so no edit is left out and syncs never overlap.
- When verify is approved: the hook refreshes the seed according to `code.graph.index.seed_refresh` (`stale`, the default, refreshes when the seed is older than 7 days; `every-pr`; `manual`). The Reviewer commits it as a separate commit, "Refresh code graph seed".

The seed is binary, so two branches that both refresh it will conflict. Resolve by taking either side; CodeGraph catches up on the next start. Teams that prefer a single writer can set `seed_refresh: manual` and use the scheduled workflow in [templates/codegraph-seed-refresh.yml](templates/codegraph-seed-refresh.yml). Seeds over 50 MB get a warning to move them to Git LFS.

The hook refuses agent edits to `.codegraph/` and `.codegraph-seed/`, including terminal commands that write there. Seed saves write to a temporary file in `.codegraph/` and rename it into place, so an interrupted save never leaves a partial file to commit. Saving needs Node.js 22.13 or later (for `node:sqlite`) or the `sqlite3` command; restoring works on any Node.

The CodeGraph server runs with `CODEGRAPH_MCP_TOOLS=explore,node,search,callers,callees,impact`, because by default it exposes only `codegraph_explore`, and impact analysis needs callers and impact.

## This repository

```text
harness.yaml                   team configuration: servers, capabilities, stages, agents, budgets
sources/
  agents/*.agent.md            agent templates ({{generated}}, {{skills}}, {{capabilities}}, {{agent:id}}, {{plugin}});
                               task-runner is built once per tier, cross-checker once per checked stage
  skills/vendor/               vendored skills, pinned, with SOURCE.md and LICENSE
  skills/team/                 team-written skills and artifact templates
  scripts/harness-hook.cjs     approvals, edit and command guard, budgets, sessions, run-file excludes
  scripts/codegraph-index.cjs  seed restore, background sync, seed save
plugin/                        built output: what VS Code installs (do not edit)
.github/plugin/marketplace.json  makes this repo a plugin marketplace
src/enterprise_ai_harness/            the `harness` build tool
templates/                     optional files for service repos
tests/
```

`plugin/` is generated and committed, since VS Code installs straight from the repo. CI fails if it is out of date with `sources/` and `harness.yaml`.

## Maintainer commands

| Command | What it does |
| --- | --- |
| `harness build [--check]` | render `plugin/` and the marketplace manifest; `--check` exits 1 if anything would change |
| `harness doctor [--live]` | placeholders, tool counts and tool-definition tokens per agent, build drift, skills, licenses, secrets |
| `harness add skill <owner/repo> --skill <name> --stages grill` | vendor a skill from GitHub at a pinned commit, with `SOURCE.md` |
| `harness add skill <folder> --stages plan` | add a team skill folder |
| `harness add server <name> --url <url> --stages impact --tools a,b` | add an MCP server for some stages |
| `harness add server <name> --command <cmd> --capability db.schema` | point a capability at a different server |
| `harness install-user [--uninstall]` | fallback install without the plugin system (see SETUP.md) |
| `harness status --repo <clone>` | list runs in a service repo clone |

## Adding skills and MCP servers

| Level | Skill | MCP server |
| --- | --- | --- |
| Personal | a folder in `~/.copilot/skills/<name>/` | your user `mcp.json`, with its tools in a tool set named `extras-<stage>` (for example `extras-impact`); every stage agent already lists one |
| One repo | a folder in the service repo's `.github/skills/<name>/` | the service repo's `.vscode/mcp.json` plus an `extras-<stage>` tool set |
| Team | `harness add skill ...` here, then a PR | `harness add server ...` here, then a PR |

Listing a skill under `stages` makes those stage agents load it at start. Without that, Copilot loads it on demand from its description.

Personal settings go in `~/.copilot/enterprise-ai-harness.local.yaml` (all repos) or `harness.local.yaml` at a clone's root (that clone only; excluded from git by the hook). The hook reads them directly, so only nested `key: value` lines work:

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

`harness.yaml` holds the team settings:

- `plugin`: name, version, description, repository, and `server_prefix` in case VS Code namespaces plugin MCP tools.
- `runs.dir` and `runs.commit`: the run folder (default `.ai-harness`); `commit: false` keeps it local, `true` lets the Reviewer commit the run record with the PR (usage counters, sessions and locks stay excluded).
- `skills_manual_only`: vendored skills that load only when someone types `/<name>` or an agent links them, so they do not take over the stages or developers' normal chats.
- `servers`: MCP servers. HTTP servers sign in with OAuth in VS Code; stdio servers start on demand. `tools` narrows a server to named tools; `est_tools` feeds the tool-count check. A server with `enabled: false` or a `<placeholder>` value is left out, and its capabilities use their `alt` server or fallback.
- `capabilities`: `bind`, `alt`, `tools` (narrowing per capability), `setup` (a one-time command the Harness Check runs), `index` (code graph seed settings), `fallback`.
- `stages`, `agents`: stage order, artifacts, and each agent's built-in tool sets, capabilities, subagents and start-up skills.
- `models`: tiers, each agent's tier, the default task tier, and cross-checks (see Models).
- `review`, `budgets`, `hooks`, `skills`.

Secrets never go in these files. Use OAuth servers, or `${input:<id>}` references that VS Code keeps in its secret storage. `harness doctor` flags anything that looks like a token.

## How approvals and budgets are enforced

`harness-hook.cjs` runs as agent-scoped hooks set in each harness agent's frontmatter (`node "${PLUGIN_ROOT}/scripts/harness-hook.cjs" ...`). Subagents (Component Scout, Task Runners, Harness Check) carry their own hooks, because VS Code applies a subagent's frontmatter hooks, not its caller's, to the subagent's tool calls.

- `UserPromptSubmit` records approvals. The buttons send prompts that start with `[harness:approve <stage>]`; the hook checks that earlier stages are approved and unchanged, hashes the stage's artifacts into `state.json`, and moves the run on. Plan approval also writes `approval.json`. Approving implement needs every task in `plan.md` to have run and been reviewed; going back to implement from verify keeps that history, so a fix reruns only the task it touches. The hook also keeps the run folder, `.codegraph/` and `harness.local.yaml` in `.git/info/exclude` and restores the code graph seed.
- `PreToolUse` guards edits and commands:
  - Before plan approval, harness agents can only edit the run folder; after it, the Implementer and Task Runners can edit code, and the hook asks before edits to files not in `impact.json` (tests excepted). The Reviewer and the Checkers cannot change code.
  - File paths are read from every place VS Code's edit tools put them, including `multi_replace_string_in_file` replacements and the file headers inside an `apply_patch` body. An implement-stage edit whose files cannot be identified still needs an approved plan.
  - Terminal and sandbox commands (`run_in_terminal`, context-mode's execute tools, each command of a batch) need an approved plan in implement. In other stages, a command that looks like it writes files (a redirect, including `1>` and `&>`, or `rm`, `mv`, `cp`, `sed -i`, `find -delete`, PowerShell's `Set-Content` and similar, or file writes in `python -c` and `node -e` code) needs the person's OK unless every path it names is in the run folder; so do `git pull`, `merge`, `rebase`, `reset --hard` and `checkout -- <file>`. Quoted text in a plain command is treated as data, and redirects to `/dev/null` or the temp folder are ignored, so `grep "a > b"` or `npm test 2>&1 | tail` run without asking. A command that writes hook-owned files or `.codegraph/` is refused in every stage. These are pattern checks: a command can still hide a write, which is one reason the hook is a guardrail rather than a sandbox.
  - Without a usable run (no `current`, or a `state.json` that does not parse), harness agents can only create run files, and approvals are refused with a reason instead of being ignored.
  - A chat stays with the run it started on. If another chat switches the active run, approvals and edits from the first chat are refused until the person resumes its run.
  - `stop` pauses the run the chat works on: harness agents' tool calls on it are refused until `resume <run-id>`, which also makes that run the active one.
  - The hook works from the opened folder, so a service opened as a subfolder of a monorepo, or through a symlink, keeps its own `.ai-harness/`, `.codegraph/` and seed; the git excludes are written for that subfolder.
  - Task Runners start only for task ids in the approved plan, given at the start of the prompt (`T3: ...`), on the task's tier, and only after the person reviewed the previous task (or batch, with `review.implement: per-batch-N`). A task review is refused when no task is waiting for one.
  - Budgets: each stage has tool-call and output limits that start over when the stage starts or is revised; the orchestrator's budget lasts one request and a subagent's one dispatch. Counters are updated under a lock, so parallel tool calls are all counted; a lock left by a hook that was killed is taken over at once.
- `PostToolUse` counts output size and starts the background code graph sync. `PreCompact` appends a status note to `status.md`, which is not an approved artifact.

Agents cannot edit `state.json`, `approval.json` or `.usage.json` of any run, `.codegraph/`, `.codegraph-seed/`, `harness.local.yaml`, or (except the orchestrator) the run folder's `current`. Only a Checker writes `<stage>-check.md`. Path checks ignore case on Windows and macOS.

Personal settings can change review modes, budgets and audit logging only; the edit-tool patterns and the out-of-plan rule stay with the team configuration.

This is a guardrail against mistakes, not a security boundary. Branch protection and PR review remain the real controls.

## Token use

- Each agent sees only its own tools. Atlassian Rovo v2 (about 25 primary tools, about 250 with `?tools=all`) and the GitHub read server are narrowed to the few each capability needs. `harness doctor` prints the tool count and an estimate of tool-definition tokens per agent; with every default server configured that comes to 14 to 42 tools per agent, well under the 128-tool limit.
- Heavy reading happens in subagents (Component Scout, Task Runner) that return summaries of 20 to 30 lines, so file contents stay out of the main conversation.
- Stages hand over through files with size limits. A new chat resumes any run from those files with `resume <run-id>`.
- CodeGraph returns symbols and call paths instead of whole files; context-mode keeps large command output in a sandbox.
- Start-up skills are short. Long ones (writing-plans, subagent-driven-development) are manual-only, so Copilot does not load them from their descriptions.

## Embedded skills

| Skill | Source | Used by |
| --- | --- | --- |
| grill-me | [mattpocock/skills](https://github.com/mattpocock/skills) (MIT) | Griller, at start |
| to-questionnaire, handoff, tdd, code-review | mattpocock/skills (MIT) | on demand; code-review at Reviewer start |
| writing-plans, subagent-driven-development, verification-before-completion | [obra/superpowers](https://github.com/obra/superpowers) (MIT) | manual only (`/<name>`) |
| ponytail | [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail) (MIT) | Task Runners, at start; manual only elsewhere |
| change-artifacts, intake, impact-analysis, plan-format | this repo | stage agents, at start |

Upstream, grill-me's file only points to the grilling skill, so the harness ships them as one skill named grill-me: grilling's process under grill-me's name. Each vendored skill carries `SOURCE.md` (repo, path, commit, license) and the upstream `LICENSE`. Vendored skill bodies are not edited; harness rules live in the agent that loads the skill.

## Check during the pilot

Most of the VS Code contract was checked against the current docs and the `microsoft/vscode` and `vscode-copilot-chat` sources (October 2026): hook events and stdin fields, exit code 2 for blocking, `${PLUGIN_ROOT}` expansion in plugin agents' hooks, subagent hooks, the `com.github.copilot/agents/` layout, `mcp.json` with `mcpServers`, tool names such as `create_file`, `replace_string_in_file`, `multi_replace_string_in_file`, `apply_patch`, `run_in_terminal` and `runSubagent`. Still worth confirming on your machines:

- UserPromptSubmit `additionalContext` (the "Harness status" line) works in the current source, but the docs list only the common output fields for that event. If the line never appears, approvals still work; agents are told to stop and ask for the Harness Check.
- Organization policy. With `allowManagedHooksOnly`, plugin hooks run only when the plugin is force-enabled through managed settings. Agent-scoped hooks also need a trusted workspace and `chat.useHooks` (on by default).
- Session target. Agent-scoped hooks run in the Local harness only; the Copilot (agent host) target uses the Copilot CLI hook format.
- Tool names in hook payloads for MCP tools. Turn on `hooks.audit` for a day; `.ai-harness/.audit.log` shows the real names.
- Atlassian Rovo v2 needs its own sign-in (v1 credentials do not carry over), and two tools (remote links, transitions) are reached through `discover` and `executeRead`.
- GitHub Enterprise Server needs the local GitHub MCP server instead of the hosted one. An Atlassian Data Center site needs a self-hosted Atlassian MCP server.
- Licenses: context-mode is under the Elastic License 2.0, not an OSI license; check it with your OSS review or set the `ctx` server to `enabled: false`. CodeGraph and the vendored skills are MIT.
- Tool-set grouping. VS Code's source marks per-server tool sets (`<server>/*`) and user tool sets as deprecated for the Local harness. Narrowed names (`server/tool`) keep working; the `extras-<stage>` sets and `codegraph/*`, `ctx/*`, `kb/*` may need explicit tool lists later.
- Models. That the handoff `model` field switches the session's model, and that each tier's models are allowed as subagents on your Copilot plan.
- Seed size and conflicts on your largest repo.

## Development

```bash
uv run --extra test pytest -q     # node 22.13+ is needed for the hook and code graph tests
CODEGRAPH_E2E=1 uv run --extra test pytest -q tests/test_codegraph_e2e.py   # against the real CodeGraph CLI
uv run harness build --check
```
