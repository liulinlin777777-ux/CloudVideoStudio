# P3-01 — Research and creative brief

This change adds a human-facing, project-bound brief for audience, goal, topic, key message, tone, duration and constraints. It also stores manually entered research sources and factual claims with source references, then records an explicit local approval action.

## User flow

1. Open the local CloudVideoStudio editor and complete the brief fields.
2. Add source records with a title, citation and optional publisher, locator and notes. Source details are entered by the user; the app does not fetch URLs or claim to verify them.
3. Add factual claims and select the source records that support each claim.
4. Save a draft, or approve it after the audience, goal, topic and at least one cited claim are present.
5. Reopen the same project to reload the saved brief. Editing a saved approval returns it to draft and requires a new approval.

## Storage and boundaries

- The artifact is JSON schema version 1, stored beside the local preview project under a fixed project-derived filename. Writes use a temporary file and atomic replacement.
- The artifact is bound to the current local project ID; its file path is derived by the server and is never accepted from browser input.
- HTTP writes require the local editor origin. No external service, AI provider, web scraper, publishing integration or additional dependency is used.
- A local approval records that the user clicked the approval control. It does not identify a person, prove source accuracy, or replace later product review.
- This is P3-01 only. It does not generate scripts, perform media intelligence, suggest cuts, or satisfy the full P3 acceptance contract.

## Dependency and status

The implementation is based on the delivered P2-B candidate commit `52539b958ce9e4be92c2197136bd0090ee282c7e`. PR #12 remains unmerged, so any review PR must remain explicitly stacked on that dependency until integration is authorized. P2-C PR #14 is not modified by this work.

Passing local tests or browser checks for this feature does not mean P2-C, P2-D, P3-01 independent review, or whole-product acceptance has passed.
