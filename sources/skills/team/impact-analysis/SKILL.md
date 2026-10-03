---
name: impact-analysis
description: Find every component a change touches, with evidence and confidence, and write impact.md and impact.json. Use in the impact stage of the change harness, after clarifications are approved.
---

# Impact analysis

Input: `requirement.md` and `clarifications.md` only. Output: `impact.json` (schema in the change-artifacts skill templates) and `impact.md`.

## Steps

1. Seeds. List the entities and behaviors the change touches: domain objects, events, tables, endpoints, flags, named components. Take them from the two input files.
2. Architecture first. Query `arch.graph` for each seed: services, Kafka topics, MQ queues, tables, APIs and their producers and consumers. This gives the cross-repo candidate list.
3. Code. For each candidate in this repo, run one Component Scout subagent with this brief:
   - component name, seeds, the question ("what in this component must change for <behavior>?"),
   - return at most 30 lines in the scout's format.
   Run scouts for independent components in parallel. Do not read whole files yourself.
4. Classify each candidate:
   - high: a caller, consumer, schema or contract that the change provably reaches, with a code.graph or arch.graph result to cite.
   - medium: reached by name or convention but not traced end to end.
   - low: plausible but unproven. Each low item becomes a question on the review card.
5. Record what you checked and ruled out under "Checked, not impacted". This is what lets the reviewer trust the list.
6. Write `impact.json` first, then `impact.md` from it. Keep `impact.md` under 200 lines; detail belongs in the JSON.

## Things that are easy to miss in event-driven systems

- Consumers of a topic or queue whose payload changes, including ones in other repos.
- Schema registry subjects and compatibility mode.
- Replays and backfills that will see old and new payloads.
- Feature flags that gate the path, and their defaults per environment.
- Batch jobs that read the same table.

## Output budget

Large tool output goes through `output.sandbox`. If code.graph returns an index-missing message, say so on the card; the Harness Check can build the index.
