<!-- harness:start (managed by change-harness; edit outside these markers) -->
## Change harness

This repo uses the change harness for Copilot in VS Code. Pick **Change Orchestrator** in the agents dropdown and type a Jira key or describe the change. Each stage ends with a review card; approve, revise or stop with the buttons.

- Runs live in `.changes/<run-id>/`. Team conventions and past decisions: `.harness/memory/`.
- Add a skill or MCP server for this repo in `.harness/extensions.yaml`, or ask the orchestrator to. Personal settings go in `harness.local.yaml` (not committed).
- Agents name capabilities (`code.graph`, `tickets.read`, ...), never servers. The binding lives in the harness config.
- Prefer code.graph over reading whole files, and send long command output through the output.sandbox tools.
<!-- harness:end -->
