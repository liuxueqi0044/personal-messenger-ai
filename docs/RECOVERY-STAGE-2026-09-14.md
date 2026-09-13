# QQ Runtime Recovery Stage — 2026-09-14

This checkpoint records the current engineering state without including account identifiers, contact labels, message contents, screenshots, databases, credentials, or runtime artifacts.

## Completed in this stage

- Hardened the host/guest worker protocol with authenticated selection handoffs, command/result correlation, deadlines, worker-epoch checks, and replay-resistant attestations.
- Added fail-safe cleanup and manual-cleanup escalation for ambiguous prepare/commit/verify outcomes.
- Tightened visual-selection evidence validation, process identity checks, runtime identity binding, palette/row proofs, and neutral-hover requirements.
- Fixed the maximized-window neutral-hover path used by the QQ UI worker.
- Expanded persistence, transport, visual-selection, worker, and adversarial regression coverage.
- Added a guarded message-cursor reanchor workflow that validates configuration offline before updating a binding cursor.
- Built and installed guest release `r20260914-02`; the current full regression suite completed with `1303 passed` and two known non-blocking warnings.
- Reanchored two anonymous direct-message bindings and cleared their local binding pauses after verification.

## Safety state at checkpoint

- The host runtime is gracefully stopped.
- The worker process is not running.
- Global sending remains paused.
- No uncertain send or manual-cleanup operation is active.
- Live one-shot validation remains intentionally pending until a fresh inbound event is available.

## Publication boundary

Generated outputs, runtime databases, credentials, diagnostics, screenshots, compiled binaries, local machine paths, account identifiers, contact labels, and chat text are excluded from this checkpoint.
