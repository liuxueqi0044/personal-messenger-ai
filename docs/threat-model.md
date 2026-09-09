# M13 threat model

Status: code and offline acceptance baseline. This document is not evidence that real-account sending is safe.

## Assets and trust boundaries

Protected assets are QQ/WeChat account state, contacts and conversation text, model API keys, M9 signing keys, local evidence, backups, RulePack versions, authorization state, and the user's desktop input/focus.

Trust boundaries:

1. Official QQ/WeChat clients are external applications. Their login state never proves an Adapter capability.
2. Adapters may observe or execute only capabilities certified for the exact environment fingerprint.
3. Local Hub is the state and authorization authority. The model, WebUI, MCP and evidence viewer cannot mint send authorization.
4. M13 is a governance sidecar. It can downgrade, pause, quarantine and notify; it cannot create a business-path shortcut.
5. Cloud model input must already be minimized by M6–M8. M13 logging and metrics never become an alternate conversation store.

## Threats and enforced controls

| Threat | Control | Failure state |
|---|---|---|
| API/M9 key committed or logged | User-scoped Windows DPAPI, opaque filenames, recursive structured-log redaction | SecretStore fails closed |
| Prompt text or hostile nested object leaks through logs | Sensitive-key normalization, value-pattern redaction, depth/cycle limits, no object `repr`, no traceback | Redacted marker only |
| Screenshot persists indefinitely | Evidence content in SecretStore, metadata/content separation, bounded TTL, expiry deletion and access audit | Expired evidence cannot be read |
| PII/high-cardinality metric label | Exact metric, label-name and label-value allowlists | Metric rejected |
| Client update inherits old send ability | Exact client/Windows/DPI/theme/window/Adapter fingerprint matrix | L0 until read fixtures; send quarantined |
| Main-account login is mistaken for validation | Promotion requires a dedicated-test-account attestation and zero error/contension counts | Maximum L1 |
| Uncertain send is retried | Incident pause/quarantine plus M1/M9 state-machine rules | Manual reconciliation only |
| Restored database releases stale work | Restore pauses all, invalidates authorizations, cancels expired pacing and marks due pacing for review | No bulk dispatch |
| Dependency introduces protocol emulation, hooks, injection, memory access or platform DB decryption | Offline dependency/source gate and SBOM | Build audit fails |
| "Human-like" behavior becomes anti-detection | Architecture and review explicitly reject fingerprint spoofing, captcha bypass and anti-detection code | Feature not implemented |

## Assumptions

- The Windows account and host OS are not already fully compromised.
- Production wiring uses `WindowsDPAPISecretStore`, not a test double.
- Local database file access is restricted to the user's Windows account; full-disk encryption remains an operating-system deployment control.
- M1 integrates M13 pause/invalidation callbacks transactionally before any real sending is enabled.
- Evidence and backup manifest directories are local, not cloud-synchronized by default.

## Residual risks and real-world gates

- DPAPI does not protect data from malware already executing as the same Windows user.
- A screenshot may contain more information than OCR text; evidence mode therefore stays opt-in and short lived.
- Regex redaction is defense in depth, not permission to log arbitrary raw input. Callers must pass stable codes and structured fields.
- QQ/WeChat UI and accessibility trees can change without notice. Each exact environment requires fixture revalidation.
- Real QQ dedicated-test-account sending, seven-day QQ soak, and all zero-error desktop-contention criteria are not yet complete.
- WeChat background sending is still capability-dependent and must remain L1 when the exact D0 send path is unsupported.

