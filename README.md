# enterprise-ai-harness

A VS Code agent plugin for GitHub Copilot. You give it a Jira key or a sentence describing a change, and Copilot works through six stages: intake, grill, impact, plan, implement and verify. Each stage ends with a short review card, and nothing moves on until you approve. Code edits stay locked until you approve the plan.

You install the plugin once. Service repos stay as they are: run files are kept out of git through `.git/info/exclude`, and the only thing the workflow commits besides your code change is an optional code graph seed (`.codegraph-seed/`) that saves the next clone an index build.

Every setting has a default that works without edits. You can change review, budget, guard and code graph settings for yourself in one small file, with no rebuild. Anything else (models, servers, agents) you can change for yourself with a personal build, and the team can change any of it for everyone.

Contents:

1. [What a run looks like](#what-a-run-looks-like)
2. [Developer setup](#developer-setup)
3. [Configuration](#configuration)
4. [Maintaining the harness](#maintaining-the-harness)
5. [Troubleshooting](#troubleshooting)
6. [How the guardrails work](#how-the-guardrails-work)
7. [Reference](#reference): token use, embedded skills, pilot checks, repository layout, development

## What a run looks like

| Stage | Agent | Writes | You review |
| --- | --- | --- | --- |
| 1 Intake | Intake | `requirement.md` | the summary of the ticket or request |
| 2 Grill | Griller (grill-me) | `clarifications.md` | the decisions, asked in rounds |
| 3 Impact | Impact Analyst + Component Scout | `impact.md`, `impact.json` | which components change, with evidence |
| 4 Plan | Planner, then Plan Checker on a stronger model | `plan.md`, `plan-check.md` | per-component changes, tasks and each task's model tier; approving unlocks code edits |
| 5 Implement | Implementer + a Task Runner per tier | code, `progress.md` | each task's diff and check result |
| 6 Verify | Reviewer | `verification.md`, the PR | plan coverage, full test run, review |

Runs live in `.ai-harness/<run-id>/` in your clone. The run id is the Jira key, or `local-<date>-<slug>` for a plain request.

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

You can also type a command, as the whole message: `approve`, `revise: <note>`, `stop`, `extend` (50% more budget for the current stage and its subagents), `back to <stage>` and `save code graph`; to the orchestrator, `resume <run-id>`. The orchestrator also answers `status`, `models`, `help`, `add skill ...` and `add server ...` in any wording. A sentence that only mentions a command does nothing.

You can edit any artifact before approving it. Approving records a hash of each artifact, and an approved artifact stays read-only until you go back to its stage.

## Developer setup

### What you need

| Need | Why |
| --- | --- |
| VS Code with GitHub Copilot Chat, signed in | the agents run in Copilot Chat |
| A Copilot plan with the models listed under [Models](#models) | each stage runs on a model tier |
| Git | runs, branches and the PR |
| Node.js 22.13 or later on your `PATH` | the hook, CodeGraph and context-mode run on Node; 22.13 adds `node:sqlite`, used to save the code graph seed |
| Python 3.10+ and [uv](https://docs.astral.sh/uv/) | only for a personal build or the fallback install |

Check Node with `node --version`.

### Step 1: turn on agent plugins and add the marketplace

Open your VS Code user settings as JSON (`Preferences: Open User Settings (JSON)`) and add:

```json
"chat.plugins.enabled": true,
"chat.plugins.marketplaces": ["Yasod94/enterprise-ai-harness"]
```

The marketplace entry also accepts a Git URL (`https://...` or `git@...`). If the repository is private, your normal Git credentials must be able to clone it.

### Step 2: install the plugin

Open the Extensions view, search `@agentPlugins`, select enterprise-ai-harness and choose Install. You can also find it in the Chat view under the cog, Agent Customizations, Plugins.

That installs the agents, skills, hook scripts and MCP server definitions. Nothing is copied into your repositories.

### Step 3: check the hook settings

The approval gate is a hook, and VS Code runs hooks only when:

- `chat.useHooks` is on (it is by default; your organization may turn it off), and
- the workspace is trusted. Trust the folder when VS Code asks, or run `Workspaces: Manage Workspace Trust`.

If your organization sets `allowManagedHooksOnly`, plugin hooks run only when an admin force-enables the plugin through managed settings. The Harness Check in step 6 tells you whether the hook ran.

### Step 4: open a service repo and start a run

1. Clone the service repo and open its folder in VS Code. Open one service folder per window; in a monorepo, open the service's own folder.
2. In the Chat view, pick Change Orchestrator from the agents dropdown.
3. Type a Jira key (`ABC-123`) or describe the change in a sentence.

The orchestrator creates the run, switches to a `change/<run-id>` branch (it asks first if you have uncommitted changes) and tells you which button to press.

### Step 5: sign in to the servers

The first time a server is used, VS Code asks you to sign in through the browser. No tokens go in files.

| Server | Sign-in | Used for |
| --- | --- | --- |
| `github-ro`, `github-rw` | your GitHub account | reading repos and PRs; opening the PR |
| `atlassian` | your Atlassian account (Rovo MCP v2; v1 sign-ins do not carry over) | Jira tickets and Confluence pages |
| `codegraph`, `ctx` | none, they run locally through `npx` | code graph and large command output |
| `kb` | your KB server's sign-in | off by default; on once your team sets its URL |

If you skip a sign-in, the stages that need that server use their fallback and say so on the review card.

### Step 6: first run in a repo

On the first run in each clone, the orchestrator runs the Harness Check. It reports which capabilities answer, whether the hook is running, and the state of the code graph:

- The repo has a committed seed and you have no local index: the hook restores the index from `.codegraph-seed/` on your first prompt. Nothing to do.
- No seed yet: the Harness Check builds the index with `codegraph init` (a few minutes on a large repo). Then type `save code graph` and commit `.codegraph-seed/`, as a small PR of its own, so the rest of the team skips the build.

The hook adds `.ai-harness/`, `.codegraph/` and `harness.local.yaml` to the clone's `.git/info/exclude`, so run files never show up in `git status`.

### Step 7 (optional): your own settings

The defaults suit most work. To change review cadence, budgets, the out-of-plan rule, run-file commits or the code graph refresh for yourself, see [Changing a setting for yourself](#changing-a-setting-for-yourself).

### Updates and removal

VS Code checks marketplace plugins for updates every 24 hours when `extensions.autoUpdate` is on. To update now, run `Extensions: Check for Extension Updates`. To remove the plugin, uninstall it from the Extensions view; your personal settings file and your clones' run folders stay until you delete them.

If you used a version that kept runs in `.changes/`, move or delete that folder and remove the `.changes/` line from `.git/info/exclude`; that line can hide changie's files.

### When agent plugins are blocked

If your organization disables agent plugins, install the same agents and servers at user level:

```bash
git clone https://github.com/Yasod94/enterprise-ai-harness
cd enterprise-ai-harness
uv run harness install-user              # run again after a git pull to update
uv run harness install-user --uninstall  # removes only what it installed
```

This writes agents to `~/.copilot/agents`, skills to `~/.copilot/skills` and the scripts to `~/.copilot/enterprise-ai-harness/scripts`, and merges the servers into your VS Code user `mcp.json`. Servers you already have with the same name are kept, and if your `mcp.json` has comments, the original is saved as `mcp.json.before-harness`. `install-user` builds from the repo each time and applies your personal settings file if you have one.

### Recommending the plugin from a service repo

A service repo can prompt new joiners to install the plugin with one small file, `.github/copilot/settings.json`:

```json
{
  "extraKnownMarketplaces": {
    "enterprise-ai-harness": { "source": { "source": "github", "repo": "Yasod94/enterprise-ai-harness" } }
  },
  "enabledPlugins": { "enterprise-ai-harness@enterprise-ai-harness": true }
}
```

VS Code shows the recommendation the first time a chat message is sent in that workspace, and under `@agentPlugins @recommended` in the Extensions view.

## Configuration

### How settings are layered

| Layer | File | Who edits it | Takes effect |
| --- | --- | --- | --- |
| 1. Defaults | `harness.yaml` in this repo, built into the plugin | nobody needs to | as shipped |
| 2. Team | the same `harness.yaml`, changed by maintainers | maintainers, by PR | when developers get the new plugin version |
| 3. You, every repo | `~/.copilot/enterprise-ai-harness.local.yaml` | you | runtime keys at once; other keys after a personal build |
| 4. You, one clone | `harness.local.yaml` at the clone's root | you | runtime keys at once (other keys are ignored) |

A later layer wins over an earlier one, key by key. Unset keys keep the value from the layer below.

Runtime keys are the settings the hook reads on every call, so a change applies on your next message or tool call, with the plugin from the marketplace and no restart. The team lists them in `personal_overrides`; by default they are:

```text
review  budgets  hooks.audit  hooks.out_of_plan_files  runs.commit
code_index.seed_refresh  code_index.seed_max_age_days  code_index.sync
```

Every other key is built into the agent files and the plugin's `mcp.json` (models, servers, tools, agents). The marketplace plugin cannot take those from your file; a [personal build](#a-personal-build-for-everything-else) can.

To see what applies to you, run `uv run harness config` in a clone of this repo. It prints the effective configuration and sorts the keys in your file into "apply now", "need `harness install-user`" and "ignored".

### Default settings

In the "You can change it" column, "at run time" means in your personal file with no rebuild, "personal build" means with `harness install-user`, and "team" means only in the team's `harness.yaml`.

Runs and reviews:

| Setting | Default | What it does | You can change it |
| --- | --- | --- | --- |
| `runs.dir` | `.ai-harness` | run folder in each clone; agent and skill text is built from it | team |
| `runs.commit` | `false` | `true`: the Reviewer commits the run record with the PR, and the git excludes switch to runtime files only | at run time |
| `review.implement` | `per-task` | review after each task, `per-batch-<n>` after every n tasks, or `none` | at run time |
| `review.<other stages>` | `required` | intake, grill, impact, plan and verify always end in a review; plan approval is always enforced | at run time (no effect beyond the status line) |

Budgets, in tool calls per agent scope (at the limit the agent stops and shows its card; type `extend` for 50% more):

| Scope | Default | Scope | Default |
| --- | --- | --- | --- |
| `orchestrator` | 30 per request | `plan` | 40 |
| `check` (Harness Check) | 20 per check | `implement` | 300, 600 KB of output |
| `intake` | 20 | `implement-task` (each Task Runner) | 30, 200 KB |
| `grill` | 40 | `verify` | 40 |
| `impact` | 80, 200 KB | `cross-check` (Checkers) | 40 |
| `scout` (each Component Scout) | 25 | | |

All budgets: at run time, as `budgets.<scope>.tool_calls` and `budgets.<scope>.output_kb`.

Guard:

| Setting | Default | What it does | You can change it |
| --- | --- | --- | --- |
| `hooks.out_of_plan_files` | `ask` | edits to files not in `impact.json` (tests excepted): `ask`, `allow` or `deny` | at run time |
| `hooks.audit` | `false` | log each tool name and its argument keys to `.ai-harness/.audit.log` | at run time |
| `hooks.edit_tools`, `terminal_tools`, `subagent_tools` | patterns matching VS Code's tool names | which tool calls count as edits, commands and subagents | personal build |
| `personal_overrides` | the runtime keys above | which settings developers may change at run time | team |

Models (details under [Models](#models)):

| Setting | Default | You can change it |
| --- | --- | --- |
| `models.tiers.fast` | Claude Haiku 4.5, then GPT-5 mini | personal build |
| `models.tiers.standard` | Claude Sonnet 5.5, then GPT-5.4 | personal build |
| `models.tiers.deep` | Claude Opus 5.5, then GPT-5.5 | personal build |
| `models.agents` | orchestrator, Harness Check, Intake, Component Scout: fast; Planner, Implementer, Reviewer: standard; Griller, Impact Analyst: deep; Task Runner: by task | personal build |
| `models.task_default` | `standard` | personal build |
| `models.cross_check.plan` | Plan Checker on deep, required when the Planner's tier is below deep | personal build |
| `models.cross_check.impact` | Impact Checker on deep, offered as a button | personal build |
| a task's tier | the Planner proposes one per task | during plan review, edit `- Tier:` in `plan.md` |

Servers and capabilities (details under [Servers and capabilities](#servers-and-capabilities)):

| Setting | Default | You can change it |
| --- | --- | --- |
| `servers.*` | GitHub (read-only and PR-writing), Atlassian Rovo v2, CodeGraph 1.6.1, context-mode 1.0.169 on; `kb` and `orders-db-ro` off | personal build |
| `capabilities.*` | each capability bound to one server, with a fallback | personal build |

Code graph (under `capabilities.code.graph.index` in `harness.yaml`, `code_index` in your file):

| Setting | Default | What it does | You can change it |
| --- | --- | --- | --- |
| `seed_refresh` | `stale` | when verify is approved, refresh the committed seed if older than `seed_max_age_days`; `every-pr`; or `manual` | at run time |
| `seed_max_age_days` | `7` | age that counts as stale | at run time |
| `sync` | `npx -y @colbymchenry/codegraph@1.6.1 sync --quiet` | command the hook runs after edits when the CodeGraph server is not running | at run time |
| `dir`, `file`, `seed_dir` | `.codegraph`, `codegraph.db`, `.codegraph-seed` | where the index and seed live | personal build |

Skills and agents:

| Setting | Default | You can change it |
| --- | --- | --- |
| `agents.*` | 11 agents, each with its tool sets, capabilities, subagents and start-up skills | personal build |
| `stages` | intake, grill, impact, plan, implement, verify with their artifacts | team (agents depend on them) |
| `skills` | none beyond the embedded ones | personal build, or your own skill folders (see [Adding skills and servers](#adding-skills-and-mcp-servers)) |
| `skills_manual_only` | writing-plans, subagent-driven-development, verification-before-completion, ponytail | personal build |

### Changing a setting for yourself

#### Runtime settings, no rebuild

1. Create the file `~/.copilot/enterprise-ai-harness.local.yaml` (on Windows, `%USERPROFILE%\.copilot\enterprise-ai-harness.local.yaml`). Start from [templates/personal-settings.local.yaml](templates/personal-settings.local.yaml), which lists every runtime key commented out at its default, or run `uv run harness config --init` in a clone of this repo to write it for you.
2. Uncomment the lines you want and change the values.
3. Save. The next message or tool call uses them.

For one clone only, put the same keys in `harness.local.yaml` at the clone's root. It wins over your home file, and the hook keeps it out of git.

Example: review every two tasks, allow edits outside `impact.json` without asking, give impact analysis more room, and never refresh the seed from your PRs:

```yaml
review:
  implement: per-batch-2
hooks:
  out_of_plan_files: allow
budgets:
  impact:
    tool_calls: 120
code_index:
  seed_refresh: manual
```

The hook reads this file with a small parser: write one `key: value` per line, nested by indentation. Lists and `{...}` maps on one line are ignored. A value the hook does not accept (a typo, or `out_of_plan_files: maybe`) is ignored and the team value stays, and so is any key the team took off `personal_overrides`.

#### A personal build for everything else

Models, servers, tools and agents are built into the agent files, so changing them for yourself takes a build of your own:

1. Clone this repo and install [uv](https://docs.astral.sh/uv/).
2. Add the keys to `~/.copilot/enterprise-ai-harness.local.yaml`, in the same shape as `harness.yaml`. Full YAML works here. `~` removes a setting, and `enabled: false` turns a server off (its capabilities fall back).

   ```yaml
   models:
     agents:
       planner: deep               # plan on the deep tier; the plan cross-check is then not required
     tiers:
       standard:
         models: ["GPT-5.4 (copilot)"]
   servers:
     kb:
       url: https://kb.example.internal/mcp
       enabled: true
     ctx:
       enabled: false
   ```

3. Check what will apply: `uv run harness config` (or `uv run harness config models` for one section).
4. Disable the enterprise-ai-harness plugin in the Extensions view, so each agent appears once.
5. Run `uv run harness install-user`, then `Developer: Reload Window` in VS Code.

After a `git pull` of this repo, run `install-user` again to pick up team changes. `--team-only` installs the team build without your file, and `--uninstall` removes the personal install; re-enable the plugin afterwards.

A personal build can change anything, including what the team keeps fixed for runtime overrides. The hook is a guardrail for your own work; branch protection and PR review stay the team's controls.

### Changing a setting for the team

Maintainers change `harness.yaml` (or `sources/`) in this repo:

1. Edit `harness.yaml`, or use `uv run harness add skill ...` / `uv run harness add server ...`.
2. Bump `plugin.version`.
3. Run `uv run harness build` and `uv run harness doctor`.
4. Open a PR. CI runs the tests and `harness build --check`, which fails when `plugin/` does not match the sources.

Developers get the change with the next plugin update. To stop developers changing a runtime setting, remove it from `personal_overrides`.

### Servers and capabilities

Agents never name a server. They name a capability, and `harness.yaml` binds each capability to a server, with a fallback when the server is off or does not answer:

| Capability | Default server | Fallback |
| --- | --- | --- |
| `tickets.read`, `tickets.write` | `atlassian` | ask you to paste the ticket; tell you which ticket update to make |
| `docs.search` | `kb`, else `atlassian` (Confluence) | ask you for page links |
| `arch.graph` | `kb` | docs and the code itself, said on the card |
| `code.graph` | `codegraph` | VS Code search and reading files |
| `db.schema` | `orders-db-ro` (off) | the repo's migration files |
| `repo.read` | `github-ro` | `git log`, `git show`, reading files |
| `repo.write` | `github-rw` | a PR title and body for you to paste |
| `output.sandbox` | `ctx` (context-mode) | `tail` or `grep` on long output |

To swap a server, add it under `servers` and change the capability's `bind` (`uv run harness add server <name> --url <url> --capability docs.search` does both), then rebuild. `tools` narrows a server to the tools a capability needs, which keeps each agent's tool list short.

The KB server ships off. To turn it on, set `servers.kb.url`, delete its `enabled: false` line and rebuild (team), or set both in your personal file and run a personal build (you).

Secrets never go in these files. Use OAuth servers, or `${input:<id>}` references that VS Code keeps in its secret storage. `harness doctor` flags anything that looks like a token.

### Models

Each stage agent runs on a model tier, and each plan task carries its own tier:

| Decision | Made by | Where |
| --- | --- | --- |
| Which models each tier means, and each stage agent's tier | the team (or you, with a personal build) | `models` in `harness.yaml` |
| Each task's tier | the Planner proposes, you change it during plan review | `- Tier:` on each task in `plan.md`; approval locks it |
| Running a higher-tier task | you | "Continue on the deep model" button in the Implementer |
| Skipping a required cross-check | you | "Approve and skip the cross-check", recorded in `approval.json` |

- The build writes each agent's tier into its `model:` frontmatter and builds one Task Runner per tier. The Implementer runs each task with the Task Runner for its `Tier`.
- VS Code does not start a subagent on a model that costs more than the calling session's model. Before a deep task, a standard Implementer stops and shows "Continue on the deep model", which switches the session's model; after that, every tier up to deep can run.
- The hook refuses a Task Runner whose tier differs from the task's `Tier` in the approved plan, or that sits above the session's tier.
- Cross-check: when the Planner's tier is below the checker's, the plan needs a review by the Plan Checker on the deep model before approval. The Checker writes `plan-check.md` (a verdict and up to 10 findings, each with evidence) and nothing else. "Apply the findings" sends them back to the Planner. The hook blocks plan approval until the check has run, unless you press "Approve and skip the cross-check". `when` can be `below`, `always`, `offer` or `never`.
- `harness doctor` reports an agent whose subagent has a higher tier, since VS Code would refuse to start it.

Model names must match what the VS Code model picker shows for your Copilot plan, qualified as `Name (vendor)`. The first model in each list is used when it is available.

### Code graph

The agents use [CodeGraph](https://github.com/colbymchenry/codegraph) to find symbols, callers and call paths without reading whole files. Its index is a SQLite file in `.codegraph/`, which CodeGraph keeps out of git. To avoid rebuilding it in every clone, the harness commits a compressed snapshot:

```text
.codegraph-seed/
  codegraph.db.gz   gzip of a consistent SQLite snapshot (VACUUM INTO)
  SEED.json         commit it was built at, date, sizes
```

- First prompt in a clone with a seed and no index: the hook restores the index. CodeGraph re-parses only files that changed since the seed. A seed that is a Git LFS pointer, not gzip, or not a SQLite database is reported once and left alone.
- While the agent edits code: if the CodeGraph server is running, its file watcher keeps the index current. If not, the hook runs one background `sync` loop after edits and commands in the implement stage; edits made during a sync are picked up before it exits.
- When verify is approved, the hook refreshes the seed according to `seed_refresh`, and the Reviewer commits it on its own: "Refresh code graph seed".

The seed is binary, so two branches that both refresh it conflict. Take either side; CodeGraph catches up on its next start. For a single writer, set `seed_refresh: manual` and copy [templates/codegraph-seed-refresh.yml](templates/codegraph-seed-refresh.yml) into the service repo's workflows (set `SERVICE_DIR` in it for a monorepo service folder). Seeds over 50 MB get a warning to move them to Git LFS.

The CodeGraph server runs with `CODEGRAPH_MCP_TOOLS=explore,node,search,callers,callees,impact`, because by default it exposes only `codegraph_explore`. Pin the same CodeGraph version in `setup` and `sync`; after an upgrade, refresh the seed in each repo.

### Adding skills and MCP servers

| Level | Skill | MCP server |
| --- | --- | --- |
| You | a folder in `~/.copilot/skills/<name>/` | your user `mcp.json`, with its tools in a tool set named `extras-<stage>` (for example `extras-impact`); every stage agent already lists one |
| One repo | a folder in the service repo's `.github/skills/<name>/` | the service repo's `.vscode/mcp.json` plus an `extras-<stage>` tool set |
| Team | `harness add skill ...` in this repo, then a PR | `harness add server ...` in this repo, then a PR |

Listing a skill under `stages` makes those stage agents load it at start. Without that, Copilot loads it on demand from its description.

## Maintaining the harness

Everything the plugin contains is built from `harness.yaml` and `sources/`. You need Python 3.10+, uv and Node.js 22.13+.

First release:

1. Set `plugin.repository`. Turn on the servers your team has (the KB URL) and set `enabled: false` on servers it does not use.
2. Open the VS Code model picker on your Copilot plan and check that each name in `models.tiers` appears there.
3. Run `uv run harness build` and `uv run harness doctor`. Doctor reports placeholders, tool counts per agent against the 128-tool cap, tool-definition tokens, unbuilt changes, skill licenses and anything that looks like a secret.
4. Commit `plugin/`, `.github/plugin/marketplace.json` and `templates/personal-settings.local.yaml` with the source change, and push to the default branch.

For later changes, follow [Changing a setting for the team](#changing-a-setting-for-the-team). Do not edit files under `plugin/` by hand; the next build overwrites them. When a model is retired, edit the tier list, bump `plugin.version` and rebuild; no agent text changes.

| Command | What it does |
| --- | --- |
| `harness build [--check]` | render `plugin/`, the marketplace manifest and the personal settings template; `--check` exits 1 if anything would change |
| `harness doctor [--live]` | configuration, tool counts, build drift, skills, licenses, secrets; `--live` also checks that servers answer |
| `harness config [section] [--personal FILE] [--team-only]` | print the effective configuration and how your personal file applies |
| `harness config --init` / `--template` | write or print the personal settings template |
| `harness add skill <owner/repo> --skill <name> --stages grill` | vendor a skill from GitHub at a pinned commit, with `SOURCE.md` |
| `harness add skill <folder> --stages plan` | add a team skill folder |
| `harness add server <name> --url <url> --stages impact --tools a,b` | add an MCP server for some stages |
| `harness add server <name> --command <cmd> --capability db.schema` | point a capability at a different server |
| `harness install-user [--personal FILE] [--team-only] [--uninstall]` | personal build or fallback install at user level |
| `harness status --repo <clone>` | list runs in a service repo clone |

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Approve buttons do nothing, code edits are never blocked | Workspace trust and `chat.useHooks`. With `allowManagedHooksOnly`, the plugin must be force-enabled by an admin. Ask the orchestrator to run the Harness Check, which reports whether the hook ran. |
| An agent says the "Harness status" line is missing | Same as above: the hook is not running for that agent. |
| `node` not found, or the hook fails on save | Install Node.js 22.13+ and make sure VS Code sees it on `PATH` (restart VS Code after installing). |
| A capability shows as missing | Sign in to that server in the MCP servers view, or ask the maintainer whether the narrowed tool names still match the server. |
| A personal setting has no effect | Run `uv run harness config` in a clone of this repo: the key may need a personal build, or the team may have taken it off `personal_overrides`. Check the file uses one `key: value` per line. |
| Agents appear twice | You have both the marketplace plugin and a personal build; disable one. |
| A command keeps asking for permission | Outside implement, commands that look like they write files ask first. Run it in implement, or allow it once. |
| "plan.md was approved ... type back to plan" | Approved artifacts are read-only. Type `back to plan`, edit, and approve again. |
| Code graph answers look stale | Start the CodeGraph server (it catches up on start), or type `save code graph`. |
| Merge conflict in `.codegraph-seed/` | Take either side. CodeGraph re-parses changed files on its next start. |

## How the guardrails work

`harness-hook.cjs` runs as agent-scoped hooks set in each harness agent's frontmatter (`node "${PLUGIN_ROOT}/scripts/harness-hook.cjs" ...`). Subagents (Component Scout, Task Runners, Harness Check) carry their own hooks, because VS Code applies a subagent's frontmatter hooks, not its caller's, to the subagent's tool calls.

- `UserPromptSubmit` records approvals. The buttons send prompts that start with `[harness:approve <stage>]`; the hook checks that earlier stages are approved and unchanged, hashes the stage's artifacts into `state.json`, and moves the run on. Plan approval also writes `approval.json`. Approving implement needs every task in `plan.md` to have run and been reviewed; going back to implement from verify keeps that history, so a fix reruns only the task it touches. Pressing an old Approve button again on an unchanged stage changes nothing. The hook also keeps the run folder, `.codegraph/` and `harness.local.yaml` in `.git/info/exclude` and restores the code graph seed.
- `PreToolUse` guards edits and commands:
  - Before plan approval, harness agents can only edit the run folder; after it, the Implementer and Task Runners can edit code, and the hook asks before edits to files not in `impact.json` (tests excepted). The Reviewer and the Checkers cannot change code. Approved artifacts are read-only until their stage is revised.
  - File paths are read from every place VS Code's edit tools put them, including `multi_replace_string_in_file` replacements and the file headers inside an `apply_patch` body. An implement-stage edit whose files cannot be identified still needs an approved plan. An MCP tool whose name says it writes and that names a file is guarded like an edit.
  - Terminal and sandbox commands (`run_in_terminal`, context-mode's execute tools, each command of a batch, VS Code tasks and notebook cells) need an approved plan in implement. In other stages, a command that looks like it writes files needs your OK unless every path it names is in the run folder: redirects (including `1>` and `&>`), `rm`, `mv`, `cp`, `sed -i`, `find -delete`, formatters with `--write` or `--fix`, PowerShell's `Set-Content` and similar, file writes in `python -c` or `node -e` code, and `git pull`, `merge`, `rebase`, `reset --hard`, `checkout -- <file>`. Quoted text in a plain command is data, and redirects to `/dev/null` or the temp folder are ignored, so `grep "a > b"` or `npm test 2>&1 | tail` run without asking. A task from `tasks.json` or a notebook cell, whose code the hook cannot see, always asks outside implement. A command that writes hook-owned files or `.codegraph/` is refused in every stage. These are pattern checks, and a command can still hide a write.
  - Without a usable run (no `current`, or a `state.json` that does not parse), harness agents can only create run files, and approvals are refused with a reason.
  - A chat stays with the run it started on. If another chat switches the active run, approvals and edits from the first chat are refused until you resume its run. `stop` pauses the run the chat works on until `resume <run-id>`, which also makes that run active.
  - The hook works from the opened folder, so a service opened as a subfolder of a monorepo, or through a symlink, keeps its own `.ai-harness/`, `.codegraph/` and seed, with git excludes written for that subfolder.
  - Task Runners start only for task ids in the approved plan, given at the start of the prompt (`T3: ...`), on the task's tier, and only after you reviewed the previous task (or batch). A task review is refused when no task is waiting for one.
  - Budgets start over when a stage starts or is revised; the orchestrator's budget lasts one request and a subagent's one dispatch. Counters are updated under a lock, so parallel tool calls are all counted.
- `PostToolUse` counts output size and starts the background code graph sync. `PreCompact` appends a status note to `status.md`, which is not an approved artifact.

Agents cannot edit `state.json`, `approval.json` or `.usage.json` of any run, `.codegraph/`, `.codegraph-seed/`, `harness.local.yaml`, or (except the orchestrator) the run folder's `current`. Only a Checker writes `<stage>-check.md`. Path checks ignore case on Windows and macOS.

This is a guardrail against mistakes, not a security boundary. Branch protection and PR review remain the real controls.

## Reference

### Token use

- Each agent sees only its own tools. Atlassian Rovo v2 (about 25 primary tools) and the GitHub read server are narrowed to the few each capability needs. `harness doctor` prints the tool count and an estimate of tool-definition tokens per agent; with the default servers that comes to 14 to 36 tools per agent, well under the 128-tool limit.
- Heavy reading happens in subagents (Component Scout, Task Runner) that return summaries of 20 to 30 lines, so file contents stay out of the main conversation.
- Stages hand over through files with size limits. A new chat resumes any run from those files with `resume <run-id>`.
- CodeGraph returns symbols and call paths instead of whole files; context-mode keeps large command output in a sandbox.
- Long vendored skills (writing-plans, subagent-driven-development) are manual-only, so Copilot does not load them from their descriptions.

### Embedded skills

| Skill | Source | Used by |
| --- | --- | --- |
| grill-me | [mattpocock/skills](https://github.com/mattpocock/skills) (MIT) | Griller, at start |
| to-questionnaire, handoff, tdd, code-review | mattpocock/skills (MIT) | on demand; code-review at Reviewer start |
| writing-plans, subagent-driven-development, verification-before-completion | [obra/superpowers](https://github.com/obra/superpowers) (MIT) | manual only (`/<name>`) |
| ponytail | [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail) (MIT) | Task Runners, at start; manual only elsewhere |
| change-artifacts, intake, impact-analysis, plan-format | this repo | stage agents, at start |

Upstream, grill-me's file only points to the grilling skill, so the harness ships them as one skill named grill-me: grilling's process under grill-me's name. Each vendored skill carries `SOURCE.md` (repo, path, commit, license) and the upstream `LICENSE`. Vendored skill bodies are not edited; harness rules live in the agent that loads the skill.

### Check during the pilot

The VS Code contract was checked against the current docs and the `microsoft/vscode` and `vscode-copilot-chat` sources (October 2026): hook events and stdin fields, exit code 2 for blocking, `${PLUGIN_ROOT}` in plugin agents' hooks, subagent hooks, the `com.github.copilot/agents/` layout, `mcp.json` with `mcpServers`, and the edit, terminal and subagent tool names. Still worth confirming on your machines:

- UserPromptSubmit `additionalContext` (the "Harness status" line) works in the current source, but the docs list only the common output fields for that event. If the line never appears, approvals still work; agents are told to stop and ask for the Harness Check.
- Organization hook policy (`allowManagedHooksOnly`), and that agent-scoped hooks run in the Local harness (the Copilot agent-host target uses the Copilot CLI hook format).
- Tool names in hook payloads for MCP tools: turn on `hooks.audit` for a day; `.ai-harness/.audit.log` shows the real names.
- GitHub Enterprise Server needs the local GitHub MCP server instead of the hosted one. An Atlassian Data Center site needs a self-hosted Atlassian MCP server.
- Licenses: context-mode is under the Elastic License 2.0, not an OSI license; check it with your OSS review, or set `servers.ctx.enabled: false`. CodeGraph and the vendored skills are MIT.
- Tool-set grouping: VS Code's source marks `<server>/*` and user tool sets as deprecated for the Local harness. Narrowed names (`server/tool`) keep working; the `extras-<stage>` sets and `codegraph/*`, `ctx/*`, `kb/*` may need explicit tool lists later.
- That the handoff `model` field switches the session's model, and that each tier's models are allowed as subagents on your Copilot plan.
- Seed size and conflicts on your largest repo.

### Repository layout

```text
harness.yaml                     team configuration: the defaults for every setting
sources/
  agents/*.agent.md              agent templates; task-runner is built once per tier, cross-checker once per checked stage
  skills/vendor/                 vendored skills, pinned, with SOURCE.md and LICENSE
  skills/team/                   team-written skills and artifact templates
  scripts/harness-hook.cjs       approvals, edit and command guard, budgets, sessions, personal settings, git excludes
  scripts/codegraph-index.cjs    seed restore, background sync, seed save
plugin/                          built output: what VS Code installs (do not edit)
.github/plugin/marketplace.json  makes this repo a plugin marketplace
src/enterprise_ai_harness/       the `harness` command
templates/
  personal-settings.local.yaml   built: every runtime setting at its default, to copy
  codegraph-seed-refresh.yml     optional weekly seed refresh for a service repo
tests/
```

`plugin/` is generated and committed, since VS Code installs straight from the repo. CI fails if it is out of date with `sources/` and `harness.yaml`.

### Development

```bash
uv run --extra test pytest -q     # Node 22.13+ is needed for the hook and code graph tests
CODEGRAPH_E2E=1 uv run --extra test pytest -q tests/test_codegraph_e2e.py   # against the real CodeGraph CLI
uv run harness build --check
```
