# Engineering journey

Personal Messenger AI was built incrementally. Its history is kept to show how the safety model, runtime and operational evidence evolved—not to imply that the project appeared fully formed in one publication step.

## Preserved milestones

| Milestone | Engineering outcome |
|---|---|
| Sanitized V5 source baseline | Established the fail-closed domain, adapter and privacy boundaries used by later work. |
| Multi-contact runtime foundation | Added conversation-scoped state, memory, cursors and orchestration so one contact cannot inherit another contact's context or delivery state. |
| Durable VM delivery and controls | Added persistent operation tracking, execution guards and recovery behavior around Windows/VM automation. |
| Offline acceptance record | Separated code existence, offline contracts, controlled-environment capability and production readiness into different assurance claims. |
| Completed V5 runtime | Integrated multi-contact planning, policy, pacing, control surfaces and failure settlement. |
| Continued working-tree development | Added bounded visual selection, process-generation handoff, direct-message identity checks, one-shot reply settlement and expanded adversarial tests. |
| Public portfolio snapshot | Removed private/runtime material, documented the architecture and retained known verification failures without changing their logic. |

## Verification baseline at publication

A diagnostic run using the existing host-helper layout reached:

```text
1138 passed, 2 skipped, 4 failed, 1 warning
```

The four existing failures were deliberately not repaired during publication:

1. a short-deadline selection-confirmation test observes more retry phases than its expected contract;
2. the current MCP adapter imports the 1.x `FastMCP` API while an unconstrained install can resolve to MCP 2.x;
3. one cursor re-anchor test does not receive the expected selection handoff;
4. one cursor re-anchor unavailable-path test starts an additional worker beyond its scripted fixture.

Some host-side acceptance tests also expect deployment helpers from the private sibling VM workspace. Those runtime artifacts and credentials are intentionally excluded from this public repository.

This baseline is included as engineering evidence, not as a claim of production readiness. The publication pass performed privacy cleanup and documentation only; it did not alter the failing runtime logic.

## What the history demonstrates

- requirements were translated into explicit invariants and failure states;
- model output was kept behind deterministic policy and authorization;
- desktop automation was treated as an untrusted subsystem requiring independent proof;
- concurrency, crash recovery, uncertain commits and privacy leakage were tested directly;
- incomplete evidence stayed visible instead of being promoted into a success claim.
