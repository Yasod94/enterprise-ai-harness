---
name: plan-format
description: Write plan.md for the change harness, with per-component changes and small tasks that each fit one Task Runner session. Use in the plan stage, after impact is approved.
---

# Plan format

Input: `requirement.md`, `clarifications.md`, `impact.json`. Output: `plan.md` from the template in the change-artifacts skill.

## Minimal-change rules

- The smallest change that meets the acceptance criteria. No speculative options, extension points or refactors the requirement does not need.
- Reuse the patterns already in the component. Name the file you copied the pattern from.
- Prefer the standard library and existing dependencies over new ones. A new dependency needs a line under Risks.
- Every changed behavior gets a test at the component's public interface.

## Tasks

- One task per coherent change that can be checked on its own, usually one component and a handful of files.
- `Files` lists every path the task may touch. The Task Runner stops if it needs anything else, and the hook asks before edits to files outside `impact.json`.
- `Check` is a command that passes or fails, scoped to the task where possible.
- `Cites` lists the clarification ids the task depends on, so the Task Runner reads only those.
- Order by dependency. Schema and contract changes come before their producers and consumers.
- `Tier` picks the model that implements the task. Use the tier table in the Planner's instructions; when unsure between two tiers, pick the higher one and say why in Steps. The person may change any Tier during review, and the approved plan fixes it.

## Coverage check before the review card

- Every component in `impact.json` has at least one task, or a line under Out of scope saying why not.
- Every acceptance criterion maps to at least one task's Check or to the Test plan.

Write the result of this check in one line on the card's Summary. The writing-plans skill has more guidance on task granularity if a task feels too large.
