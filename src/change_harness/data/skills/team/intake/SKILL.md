---
name: intake
description: Turn a Jira ticket or a plain change request into requirement.md for the change harness. Use at the start of a change run.
---

# Intake

Goal: a short `requirement.md` the person can confirm in a minute. Use the template in the change-artifacts skill.

## From a Jira key

1. Read the ticket with `tickets.read`: summary, description, acceptance criteria, links, components.
2. For each linked Confluence page (functional spec, one-pager), use `docs.search` to get its title and the few lines that matter. Link the page; do not paste it.
3. Summarize in your own words. Copy acceptance criteria verbatim.

## From a plain request

1. The request text is in `state.json` under `request`.
2. Use `docs.search` with 2-3 key terms from the request to find related pages and tickets. List what you used under Links.
3. Write the acceptance criteria the request states. Anything a reviewer would need to accept the change but that the request does not say goes under Open questions. Those become the first grill questions.

## Both

- Under "Components named", list only components the source names. Impact analysis finds the rest.
- Stay under 60 lines.
- End with the review card.
