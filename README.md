# change-harness

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

Runs live in `.changes/<run-id>/` in the clone. The run id is the Jira key, or `local-<date>-<slug>` for a plain request.

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

You can also type `approve`, `revise: <note>`, `stop`, `extend` (adds 50% to the stage budget), or, to the orchestrator, `status`, `resume <run-id>`, `back to <stage>`, `save code graph`, `models`, `help`, `add skill ...`, `add server ...`.

You can edit any artifact before approving it. Approving records a hash of each artifact; if one changes afterwards, the next stage refuses to start until it is approved again.

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
| Skipping a required cross-check | the person | "Approve without cross-check", recorded in `approval.json` |

How it works:

- The build writes each agent's tier into its `model:` frontmatter and builds one Task Runner per tier ("Task Runner (fast)", ...). The Implementer runs each task with the Task Runner for its `Tier`.
- VS Code does not start a subagent on a model that costs more than the calling session's model. A standard Implementer therefore cannot start the deep Task Runner directly. It stops before a deep task and shows "Continue on the deep model", a handoff that switches the session's model; after that, every tier up to deep can run. A new chat starts on the Implementer's own tier again.
- The hook refuses a Task Runner whose tier differs from the task's `Tier` in the approved plan, or that sits above the session's tier, and its status line tells the agent which tier the session is on.
- Cross-check: when the Planner's tier is below the checker's, the plan needs a review by the Plan Checker on the deep model before approval. The Planner's card has "Cross-check with the deep model". The Checker writes `plan-check.md`, a verdict and up to 10 findings, each with evidence. "Apply the findings" sends them back to the Planner, which fixes what it agrees with and lists what it did with each finding in `plan.md`. The hook blocks plan approval until the check has run, unless the person presses "Approve without cross-check". `approval.json` records the verdict and whether the plan changed after the check, or the skip.
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
- No seed yet: the Harness Check runs `codegraph init` once and asks you to type `save code graph`, then commit `.codegraph-seed/`.
- While the agent edits code: if the CodeGraph server is running, its file watcher keeps the index current. If not, the hook starts `codegraph sync` in the background after each edit or terminal command in the implement stage (at most once every 15 seconds).
- When verify is approved: the hook refreshes the seed according to `code.graph.index.seed_refresh` (`stale`, the default, refreshes when the seed is older than 7 days; `every-pr`; `manual`). The Reviewer commits it as a separate commit, "Refresh code graph seed".

The seed is binary, so two branches that both refresh it will conflict. Resolve by taking either side; CodeGraph catches up on the next start. Teams that prefer a single writer can set `seed_refresh: manual` and use the scheduled workflow in [templates/codegraph-seed-refresh.yml](templates/codegraph-seed-refresh.yml). Seeds over 50 MB get a warning to move them to Git LFS.

The hook and the code graph server deny agent edits to `.codegraph/` and `.codegraph-seed/`; only the hook writes there.

## This repository

```text
harness.yaml                   team configuration: servers, capabilities, stages, agents, budgets
sources/
  agents/*.agent.md            agent templates ({{generated}}, {{skills}}, {{capabilities}}, {{agent:id}}, {{plugin}});
                               task-runner is built once per tier, cross-checker once per checked stage
  skills/vendor/               vendored skills, pinned, with SOURCE.md and LICENSE
  skills/team/                 team-written skills and artifact templates
  scripts/harness-hook.cjs     approvals, edit guard, budgets, run-file excludes
  scripts/codegraph-index.cjs  seed restore, background sync, seed save
plugin/                        built output: what VS Code installs (do not edit)
.github/plugin/marketplace.json  makes this repo a plugin marketplace
src/change_harness/            the `harness` build tool
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

Personal settings go in `~/.copilot/change-harness.local.yaml` (all repos) or `harness.local.yaml` at a clone's root (that clone only; excluded from git by the hook). The hook reads them directly, so only nested `key: value` lines work:

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
- `runs.commit`: `false` keeps `.changes/` local; `true` commits run files with the PR (runtime files stay excluded).
- `servers`: MCP servers. HTTP servers sign in with OAuth in VS Code; stdio servers start on demand. `tools` narrows a server to named tools; `est_tools` feeds the tool-count check. A server with `enabled: false` or a `<placeholder>` value is left out, and its capabilities use their `alt` server or fallback.
- `capabilities`: `bind`, `alt`, `tools` (narrowing per capability), `setup` (a one-time command the Harness Check runs), `index` (code graph seed settings), `fallback`.
- `stages`, `agents`: stage order, artifacts, and each agent's built-in tool sets, capabilities, subagents and start-up skills.
- `models`: tiers, each agent's tier, the default task tier, and cross-checks (see Models).
- `review`, `budgets`, `models`, `hooks`, `skills`.

Secrets never go in these files. Use OAuth servers, or `${input:<id>}` references that VS Code keeps in its secret storage. `harness doctor` flags anything that looks like a token.

## How approvals and budgets are enforced

`harness-hook.cjs` runs as agent-scoped hooks set in each harness agent's frontmatter (`node "${PLUGIN_ROOT}/scripts/harness-hook.cjs" ...`), so it only runs while a harness agent is active:

- `UserPromptSubmit` records approvals. The approve buttons send `[harness:approve <stage>]`; the hook checks that earlier stages are approved and unchanged, hashes the stage's artifacts into `state.json`, and moves the run on. Plan approval also writes `approval.json`. The same hook adds `.changes/`, `.codegraph/` and `harness.local.yaml` to `.git/info/exclude` and restores the code graph seed.
- `PreToolUse` limits edits: before plan approval, harness agents can only edit `.changes/`; after it, the Implementer and Task Runner can edit code, and the hook asks before edits to files not in `impact.json` (tests excepted). The Reviewer can only edit `.changes/`. It also enforces tool-call and output budgets per stage.
- `PostToolUse` counts output size and starts the background code graph sync. `PreCompact` appends a status snapshot to `progress.md`.

Agents cannot edit `state.json`, `approval.json`, `.usage.json`, `.codegraph/`, `.codegraph-seed/` or (except the orchestrator) `.changes/current`. Only a Checker writes `<stage>-check.md`.

This is a guardrail against mistakes, not a security boundary. Branch protection and PR review remain the real controls.

## Token use

- Each agent sees only its own tools. Atlassian (about 120 tools) and the GitHub read server are narrowed to the few each capability needs. `harness doctor` prints the tool count and an estimate of tool-definition tokens per agent; with every default server configured that comes to 14 to 33 tools per agent.
- Heavy reading happens in subagents (Component Scout, Task Runner) that return summaries of 20 to 30 lines, so file contents stay out of the main conversation.
- Stages hand over through files with size limits. A new chat resumes any run from those files with `resume <run-id>`.
- CodeGraph returns symbols and call paths instead of whole files; context-mode keeps large command output in a sandbox.
- Start-up skills are short. Long ones (writing-plans, subagent-driven-development) load only on demand.

## Embedded skills

| Skill | Source | Used by |
| --- | --- | --- |
| grill-me, grilling | [mattpocock/skills](https://github.com/mattpocock/skills) (MIT) | Griller, at start |
| to-questionnaire, handoff, tdd, code-review | mattpocock/skills (MIT) | on demand; code-review at Reviewer start |
| writing-plans, subagent-driven-development, verification-before-completion | [obra/superpowers](https://github.com/obra/superpowers) (MIT) | on demand |
| change-artifacts, intake, impact-analysis, plan-format | this repo | stage agents, at start |

grill-me's own file points to grilling, so both are vendored. Each vendored skill carries `SOURCE.md` (repo, path, commit, license) and the upstream `LICENSE`. Vendored files are not edited; harness rules live in the agent that loads the skill.

## Check during the pilot

- Plugin hooks. Confirm that agent-scoped hooks in plugin agents run and that `${PLUGIN_ROOT}` expands in them. If an organization sets `allowManagedHooksOnly`, plugin hooks only run when the plugin is force-enabled through managed settings.
- Plugin MCP server names. If tool references from plugin servers carry a prefix, set `plugin.server_prefix` and rebuild.
- Session target. Hooks target the Local session target. Test the Copilot (agent host) target before relying on it.
- Tool names in hook payloads. Turn on `hooks.audit` for a day; `.changes/.audit.log` shows the real tool names, so `edit_tools` and `terminal_tools` can be tightened.
- Narrowed tool names. The Atlassian names come from Atlassian's supported-tools page; the GitHub read names are the common ones from github-mcp-server. A wrong name is ignored by VS Code, and the Harness Check shows the capability as missing.
- GitHub Enterprise Server needs the local GitHub MCP server instead of the hosted one. An Atlassian Data Center site needs a self-hosted Atlassian MCP server.
- CodeGraph and context-mode run through npx and are not vendored; check their licenses and telemetry settings with your OSS review (`DO_NOT_TRACK=1` is set for CodeGraph).
- Seed size and conflicts on your largest repo.
- Models. That the handoff `model` field switches the session's model, that a plugin agent's `model` list applies when it runs as a subagent, and that each tier's models are allowed as subagents on your Copilot plan.

## Development

```bash
uv run --extra test pytest -q     # node 22.5+ is needed for the hook and code graph tests
uv run harness build --check
```
