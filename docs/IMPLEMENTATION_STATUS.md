# Implementation status

This document separates implementation, offline verification, controlled-environment evidence and production readiness. A passing test does not silently promote a capability to a higher assurance level.

The proposed refactoring baseline is [ARCHITECTURE-V1.md](ARCHITECTURE-V1.md), dated 2026-10-01. It describes the target architecture and migration gates; the design document itself does not upgrade any implementation or live-verification status below.

## Architecture implementation, 2026-10-01

The first W0 changes are implemented and offline-tested:

- Runtime configuration builds now hold the same ownership mutex as the runner. Frozen manifest/config rejection happens before registration changes. Publication uses a write-ahead rollback record, startup fences, atomic file replacement, and interrupted-build recovery; existing business databases are not rolled back or reset.
- Parent request events and child UI stages persist to bounded, content-free diagnostic files even without stdout. First/last failure and last successful request remain available across subsequent requests and worker replacement. These diagnostics do not replace the execution journal.
- Windows UI children spawn with the installed `pythonw.exe`; the multiprocessing executable setting is restored after the spawn. A bounded guest probe passed HEALTH with foreground unchanged. Its subsequent failed OBSERVE retained first/last failure and the source call chain with zero diagnostic-write errors.

The full suite passed after the startup-fence additions (1508 passed, 2 skipped). Live installation and new driver acceptance are tracked separately; no new real delivery is claimed by these results. W1–W5 and the remainder of operational-entry consolidation are not marked complete.

A subsequent live probe isolated the selection failure: the selected row's background and dominant ratio matched, but rounded-corner antialiasing changed its distinct-colour count from 12 to 14. The versioned `horizontal-strips-v2` profile samples two fixed background strips away from text and corners. It requires all 1744 pixels to agree and retains the exact runtime target, unique selected row, two control rows, repeated samples and independent header checks. Twenty visible rows in two guest samples and two unselected-row hover probes reproduced the three distinct uniform backgrounds. The QQ adapter tests passed (625 tests); end-to-end guest observation with this change is a separate gate.

The first W1 session-lifecycle step is implemented. An explicit `--refresh-current-session` operation can update QQ process/window/row locators in a stopped, paused generation after checking its expected ID and config digest. Every existing contact must have fresh evidence with unchanged stable identity. Immutable, hash-linked session snapshots preserve the original generation, adoption boundaries, persona, policies and business databases. Runner validation and supervisor startup select the current session snapshot; ordinary configuration builds cannot silently refresh it. The integration suite passed (1620 passed, 2 skipped) before the subsequent row-enumeration optimization. Guest publication and real delivery remain separate acceptance gates.

Read-only guest diagnostics then isolated repeated full-window UIA enumeration as the main observation cost. Each row enumeration now has an independent short-lived read phase, caching duplicate property reads only within that enumeration and returning only IDs and rectangles. Hover and pixel sampling retain no live controls, and each later stability sample obtains a fresh root. One complete enumeration may be rebuilt on `UIA_E_ELEMENTNOTAVAILABLE`; other failures, repeated invalidation, changed targets and expired deadlines remain failures. Stage logs distinguish selection before/after content reading. This optimization does not reduce identity checks or extend request deadlines; its live latency must be measured separately.

At commit `6ee2086`, the full Windows suite passed (1624 passed, 2 skipped), and frozen release `r20261001-12` was installed and independently verified. After one observation rejected a changing row list, four diagnostic enumerations showed identical row IDs, order and rectangles. A subsequent HEALTH and two independent OBSERVE requests passed: 26.8 s and 27.3 s, each reading 14 visible bubbles, with the canonical config unchanged and no messages sent. These limited samples establish a read-only baseline, not sustained reliability or delivery readiness. Guest wall-clock rate adjustment was also measured, so elapsed monotonic time and UTC duration are recorded separately.

The first live session refresh exposed an existing `one-shot-intents` subdirectory that the initial-generation flat-directory check rejected. The refresh-specific compatibility fix preserves canonical UUID intent files without reading their contents; unknown directories, reparse points, hard links and nesting remain rejected. The Windows builder/publication tests passed (73 tests). Frozen `r20261001-13` was installed and independently verified, then published session revision 2 in the original generation. Before/after hashes matched for all existing business SQLite/WAL files and old intent files, as well as the original config and manifest; adoption was unchanged and the global pause remained set. The normal supervisor subsequently started with the new revision and reachable local Web UI while paused. This passes one controlled restart/session-publication recovery path; real delivery and broader W1/W2 acceptance remain outstanding.

The next normal-runtime trial received a fresh inbound message and scheduled a model reply, but PREPARE exhausted its pre-write reserve before changing the composer: 65.8 s monotonic, 76.4 s UTC, with no commit intent or receipt. Stage evidence showed three full identity envelopes before the first write. The certified path now reads bubbles and the empty composer inside one complete before/header/after envelope, followed by a separate complete post-write identity/readback envelope. Legacy and weaker paths retain their original checks. ValuePattern writes now also check empty text, scope and focus at the write boundary and verify exact readback. This does not make UI access atomic: an external same-window conversation switch can still occur between calls, and a failed post-write check retains cleanup ownership. COMMIT checks and the 90 s request / 20 s pre-write reserve settings are unchanged. Full Windows regression at `43c005c`: 1676 passed, 2 skipped (47.38 s). Frozen `r20261001-14` was installed and independently verified; live prepare/abort and delivery results must be recorded separately.

The frozen r14 guest then passed a no-send HEALTH → PREPARE → ABORT probe using the unchanged 90/20 timing settings. PREPARE completed in 46.9 s monotonic / 49.4 s UTC; ABORT completed in 66.2 s / 65.1 s. A separate final read confirmed an empty composer, no cleanup obligation remained, and the canonical config was unchanged. No COMMIT was requested. The normal r14 supervisor subsequently started paused on the original generation and session revision 2. The prior failed inbound operation remains terminal and will not be replayed; normal real delivery still needs a new inbound message. These are single controlled samples, not sustained-operation acceptance.

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
