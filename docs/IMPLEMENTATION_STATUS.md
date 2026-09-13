# Implementation status

This document separates implementation, offline verification, controlled-environment evidence and production readiness. A passing test does not silently promote a capability to a higher assurance level.

## Implemented

| Area | Status | Evidence boundary |
|---|---|---|
| Domain kernel and durable Hub | Implemented | Commands, events, inbox/outbox behavior and replay invariants are covered by automated tests. |
| Conversation observation | Implemented | Read-only adapters, monotonic cursors and normalized conversation events are available. |
| Multi-contact isolation | Implemented | Memory, plans, pacing state, operation IDs and ledgers are scoped by conversation. |
| Structured reply planning | Implemented | Providers must return schema-valid `ReplyPlan` objects; free-form text cannot directly authorize delivery. |
| Deterministic policy | Implemented | Eligibility, consent, pause state, evidence freshness and one-time authorization are checked outside the model. |
| Durable pacing | Implemented | Delays, segmentation, recovery and fairness are persisted and revalidated at execution time. |
| Delivery state machine | Implemented | `PREPARE → COMMIT → VERIFY` includes idempotency and an explicit `UNCERTAIN` quarantine state. |
| Local Web UI and MCP gateway | Implemented | Both are loopback/least-privilege control surfaces and expose no unrestricted send primitive. |
| Security and privacy controls | Implemented | DPAPI-backed secrets, redaction, evidence TTL, incident handling and metrics allow-lists are included. |

## Controlled environment

- A supervised direct-message path has been exercised in an isolated Windows test environment.
- Visual selection is bounded to a certified contact-row crop and is followed by fresh-process identity verification.
- The public snapshot omits contact identities, message text, account identifiers, screenshots and runtime evidence paths.
- Desktop-client compatibility is profile-specific; a logged-in client alone is never considered sufficient proof.

## Deliberately not production-ready

- Persistent unattended sending remains disabled pending longer soak testing and version-specific compatibility evidence.
- Group conversations are outside the automatic-send scope and must fail closed.
- Any ambiguous commit, stale observation, identity mismatch or incomplete verification stops execution and requires review.
- Real credentials, private chat data and deployment state are not distributed with the repository.

For the exact safety gates, see [acceptance-matrix.md](acceptance-matrix.md), [threat-model.md](threat-model.md), [privacy-model.md](privacy-model.md), and [VISUAL-CONVERSATION-SELECTION.md](VISUAL-CONVERSATION-SELECTION.md).
