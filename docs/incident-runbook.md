# Incident runbook

## Automatic response

| Signal | Immediate action |
|---|---|
| `SEND_UNCERTAIN` | Pause the platform, quarantine Adapter, notify; never retry |
| Account warning | Pause all platforms, quarantine the affected Adapter, notify |
| Captcha or verification challenge | Pause all platforms, quarantine, notify; never solve or bypass automatically |
| Foreground contention | Pause the platform, quarantine, notify |
| Repeated target-resolution failure | Pause the platform, quarantine, notify |

Incident inputs are stable codes, not screenshots, conversation text or exception paths. The incident database stores the resolution approver and reason only as hashes.

## Human reconciliation

1. Keep the platform paused. Do not click resend.
2. Open the official client manually and confirm the exact conversation.
3. For `SEND_UNCERTAIN`, determine whether the candidate already appeared as an outgoing message. Record the evidence reference, never the screenshot path, in the operator workflow.
4. Inspect account warnings/captcha manually. Do not automate a challenge or retain its image beyond evidence TTL.
5. Re-run the exact environment's read-only fixture after client/UI changes.
6. If any send capability changed, run the dedicated test-account send fixture; never test promotion with the main account.
7. Create a `HumanIncidentResolution` with approver, substantive reason, timestamp and explicit `resume_authorized=true` only when safe.
8. The manager releases quarantine/resumes only after no open incident for the platform remains. Global warning/captcha recovery waits until no global incident remains.

## Restore incident

Restore is always an audited maintenance event. `BackupManager.restore` authenticates the manifest, verifies snapshot integrity, pauses the system, performs an atomic replacement, invalidates all pending authorizations, cancels expired pacing and marks already-due pacing for manual review. Bulk dispatch is always false. Resume only after queue inspection and a new live-state reconciliation.

## Escalation

Preserve short-lived evidence references and stable error codes. Do not copy raw chats, API keys, local file paths or screenshots into issue trackers. If wrong-recipient sending, duplicate sending, foreground input interference or an account warning occurs, the corresponding real-world promotion gate resets to unverified.

