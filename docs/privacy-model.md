# M13 privacy model

Status: local privacy primitives and offline tests are implemented; application-wide retention wiring remains an integration gate.

## Data minimization

The normal path stores stable IDs, hashes, reason codes, versions and aggregate metrics. It does not use logs or metrics as a copy of conversation history.

| Data | Normal handling | Retention/clearing |
|---|---|---|
| Message text | M1/M6 business storage only; excluded from M13 logs and metric labels | Controlled by contact deletion policy |
| Nickname/display name | UI projection only; redacted from M13 logs | Not retained by M13 |
| API and authorization keys | Windows user-scoped DPAPI | Explicit secret deletion/rotation |
| Evidence pixels/bytes | Local SecretStore entry; SQLite contains reference, hash, size and TTL only | Default 15 minutes, configurable up to 24 hours; conversation/all clear |
| Evidence access | Reference, action, outcome and timestamp | Audit contains no evidence body or raw conversation ID |
| Backup snapshot | Consistent SQLite snapshot stored through SecretStore | Explicit backup lifecycle; manifest is authenticated |
| Metrics | Numeric aggregate with fixed low-cardinality labels | No body, name, contact ID or path label |
| Incident | Stable type/signal code; approver and reason stored as hashes | No raw screenshot or exception text |

## Logging rules

`RedactingLogger` accepts only a stable event name and structured fields. The sanitizer recursively handles maps, sequences, Pydantic objects, bytes, cycles, confusable key names, bearer/API tokens, cookies, email, phone and local paths. Unknown object `repr`, exception traceback, chained exception state and arbitrary exception messages are never serialized.

Logging raw message bodies and then relying only on pattern replacement is prohibited. Body/text/content/name/path/token-like keys are replaced wholesale.

## Evidence lifecycle

1. `EvidenceVault.put` creates an opaque `ev_*` reference.
2. Evidence bytes are stored through `SecretStore`; metadata stores only an opaque secret name, conversation hash, content digest, size, media type and TTL.
3. Every create/read/delete/denied/expired access is audited.
4. At expiry, content is removed before the read fails with `EvidenceExpired`.
5. The user can clear one conversation's evidence or all evidence.

Production must construct the vault with `WindowsDPAPISecretStore`. The in-memory SecretStore used by tests is not a production option.

## Contact deletion integration gate

The M13 vault supports conversation-scoped clearing, but M6/M1 must call it in the same deletion workflow that removes messages, summaries, facts, contact overrides, drafts and pacing plans. Until that end-to-end deletion transaction is tested, application-wide erasure is not claimed complete.

