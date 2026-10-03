# Plan: <run-id>

## Approach

<3-6 lines: the smallest change that meets the acceptance criteria, and the existing patterns it reuses.>

## Changes by component

### <component>

- Change: <what and why>
- Files and symbols: <path>: <symbol>
- Schema or migration: <none | description>
- Event or contract change: <none | topic/schema, versioning>
- Config and flags: <none | flag name, default>

## Tasks

### T1. <title>

- Component: <name>
- Tier: <a tier name from the Planner's tier table, e.g. fast | standard | deep>
- Files: <paths this task may touch>
- Steps: <2-5 short steps>
- Check: <the command or test that proves it, e.g. `./gradlew test --tests OrderPublisherTest`>
- Depends on: <none | T-ids>
- Cites: <clarification ids, e.g. D2, D5>

## Test plan

- Unit: <...>
- Integration: <...>

## Rollout and rollback

- Rollout: <flag, order of deploys, backfill>
- Rollback: <how to undo>

## Risks

- <risk and mitigation>

## Out of scope

- <what this plan deliberately does not do>
