# Setup

## Developers

You need VS Code with GitHub Copilot signed in, Git, and Node.js 22.5 or later (the hook, CodeGraph and context-mode run on Node).

### Once per machine

1. Add two lines to your VS Code user settings (`Preferences: Open User Settings (JSON)`):

   ```json
   "chat.plugins.enabled": true,
   "chat.plugins.marketplaces": ["Yasod94/enterprise-ai-harness"]
   ```

   The marketplace entry also accepts a Git URL (`https://...` or `git@...`). If the repository is private, your normal Git credentials must be able to clone it.

2. Open the Extensions view, search `@agentPlugins`, select **enterprise-ai-harness** and choose Install. You can also find it in the Chat view under the cog, Agent Customizations, Plugins.

That installs the agents, skills, hook scripts and MCP server definitions. Nothing is copied into your repositories.

### Each repository

1. Clone the service repo and open it in VS Code. Trust the workspace when asked; hooks only run in trusted workspaces.
2. In the Chat view, pick **Change Orchestrator** from the agents dropdown and type a Jira key or describe the change.
3. The first time each server is used, VS Code asks you to sign in (GitHub, Atlassian, the KB server). These are browser sign-ins; no tokens go in files.

On the first run in a clone, the orchestrator calls the Harness Check, which reports which capabilities answer and the state of the code graph:

- Seed committed, no local index: the hook restores the index from `.codegraph-seed/` on your first prompt. Nothing to do.
- No seed yet: the Harness Check builds the index with `codegraph init` (a few minutes on a large repo). Then type `save code graph` and commit `.codegraph-seed/` so the rest of the team skips the build. A small PR of its own is fine.

The hook adds `.changes/`, `.codegraph/` and `harness.local.yaml` to the clone's `.git/info/exclude`, so run files never show up in `git status`.

Models need no setup. Each stage agent takes its model from the team's tiers. During plan review you can change any task's `Tier`; during implementation the Implementer asks you to press "Continue on the deep model" before a deep task. Type `models` to the orchestrator for a short explanation.

### Updates

VS Code checks marketplace plugins for updates every 24 hours when `extensions.autoUpdate` is on. To update now, run `Extensions: Check for Extension Updates`.

### Optional: recommend the plugin from a service repo

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

### Fallback when plugins are blocked

If your organization disables agent plugins, install the same files at user level instead:

```bash
git clone https://github.com/Yasod94/enterprise-ai-harness
cd enterprise-ai-harness
uv run harness install-user          # after a git pull, run it again to update
uv run harness install-user --uninstall
```

This writes agents to `~/.copilot/agents`, skills to `~/.copilot/skills`, the scripts to `~/.copilot/enterprise-ai-harness/scripts`, and merges the servers into your VS Code user `mcp.json` (servers you already have with the same name are kept). It records what it wrote, so `--uninstall` removes only those files and servers.

### Troubleshooting

| Symptom | Check |
| --- | --- |
| Approve buttons do nothing, code edits are never blocked | Workspace trust, and `chat.useHooks` (on by default; your organization may restrict hooks). The Harness Check reports whether the hook ran. |
| A capability shows as missing | Sign in to that server in the MCP view, or ask the maintainer whether the narrowed tool names match the server. |
| Code graph answers look stale | Start the CodeGraph server (it catches up on start), or type `save code graph` to sync and refresh the seed. |
| Merge conflict in `.codegraph-seed/` | Take either side. CodeGraph re-parses changed files on its next start. |

## Maintainers

Everything the plugin contains is built from `harness.yaml` and `sources/`. Python 3.10+ and [uv](https://docs.astral.sh/uv/) are needed for the build tool.

### First release

1. Fill in `plugin.repository` and the KB server URL in `harness.yaml`. Set `enabled: false` on servers your team does not use.
2. Build and check:

   ```bash
   uv run harness build
   uv run harness doctor
   ```

   Doctor reports placeholders, tool counts per agent against the 128-tool cap, tool-definition tokens, unbuilt changes, skill licenses and anything that looks like a secret.
3. Commit `plugin/` and `.github/plugin/marketplace.json` together with the source change, and push to the default branch. Developers point `chat.plugins.marketplaces` at this repository.

### Changes

1. Edit `harness.yaml` or `sources/`, or use `harness add skill ...` / `harness add server ...`.
2. Bump `plugin.version` in `harness.yaml`.
3. `uv run harness build`, then open a PR. CI runs the tests and `harness build --check`, which fails if `plugin/` does not match the sources.

Do not edit files under `plugin/` by hand; the next build overwrites them.

### Model tiers

`models` in `harness.yaml` maps tiers to model lists and sets each agent's tier (see the README's Models section). Before the first release:

1. Open the model picker in VS Code with your Copilot plan and check that each model name in `models.tiers` appears there. Write them qualified, as `Name (copilot)`; the first one in each list is also used on handoff buttons.
2. Keep tiers cheapest first. `harness doctor` reports an agent whose subagent has a higher tier, because VS Code will not start it.
3. Choose the cross-check policy per stage: `below` (default for plan), `always`, `offer` or `never`.

When a model is retired, edit the tier list, bump `plugin.version` and rebuild; no agent text changes.

### Code graph settings

In `harness.yaml`, under `capabilities.code.graph`:

```yaml
setup: npx -y @colbymchenry/codegraph@1.6.1 init --yes
index:
  dir: .codegraph
  file: codegraph.db
  sync: npx -y @colbymchenry/codegraph@1.6.1 sync --quiet
  seed_dir: .codegraph-seed
  seed_refresh: stale        # stale | every-pr | manual
  seed_max_age_days: 7
```

`stale` refreshes the committed seed on verify approval when it is older than `seed_max_age_days`. `every-pr` refreshes it on every change PR, which keeps it closest to main but makes seed conflicts between open PRs more likely. `manual` leaves it to developers (`save code graph`) or to the scheduled workflow in `templates/codegraph-seed-refresh.yml`.

Pin the CodeGraph version in both commands. A newer CodeGraph may change the index format; after an upgrade, refresh the seed in each repo.
