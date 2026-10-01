# Implementation status

This document separates implementation, offline verification, controlled-environment evidence and production readiness. A passing test does not silently promote a capability to a higher assurance level.

The proposed refactoring baseline is [ARCHITECTURE-V1.md](ARCHITECTURE-V1.md), dated 2026-10-01. It describes the target architecture and migration gates; the design document itself does not upgrade any implementation or live-verification status below.

## Architecture implementation, 2026-10-01

The first W0 changes are implemented and offline-tested:

- Runtime configuration builds now hold the same ownership mutex as the runner. Frozen manifest/config rejection happens before registration changes. Publication uses a write-ahead rollback record, startup fences, atomic file replacement, and interrupted-build recovery; existing business databases are not rolled back or reset.
- Parent request events and child UI stages persist to bounded, content-free diagnostic files even without stdout. First/last failure and last successful request remain available across subsequent requests and worker replacement. These diagnostics do not replace the execution journal.
- Windows UI children spawn with the installed `pythonw.exe`; the multiprocessing executable setting is restored after the spawn. This removes the detached `python.exe` console creation path; actual foreground behavior still needs guest verification.

The full suite passed after the startup-fence additions (1508 passed, 2 skipped). Live installation and new driver acceptance are tracked separately; no new real delivery is claimed by these results. W1–W5 and the remainder of operational-entry consolidation are not marked complete.

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
