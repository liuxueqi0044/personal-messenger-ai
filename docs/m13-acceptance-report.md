# M13 acceptance report

Date: 2026-09-08  
Scope: security, privacy, observability, compatibility, incident, backup/recovery and supply-chain primitives.

## Completed in code and offline acceptance

- Windows DPAPI SecretStore and persistent M9 HMAC-key accessor.
- Recursive structured-log sanitizer and logger with no traceback/raw exception serialization.
- Protected EvidenceVault with short TTL, metadata/content separation, access audit and scoped/global clearing.
- Strict metric-name, label-name and label-value allowlists for every architecture-minimum metric.
- Exact-fingerprint VersionController with fail-closed downgrade and staged L0–L3 attestations.
- IncidentManager for uncertain send, account warning, captcha and foreground contention.
- Authenticated protected backup manifest, consistent SQLite snapshot and guarded restore reconciliation.
- Offline DependencyGate and direct-dependency SBOM with installed version/license where available.
- No anti-detection, device-fingerprint spoofing, captcha bypass, protocol emulation, hook, injection, process-memory reading or platform-database decryption implementation.

## Acceptance commands

Run from the project root:

```powershell
$env:PYTHONPATH='src'
pytest -q tests/observability
pytest -q
ruff check src/messenger_ai/observability tests/observability scripts/security_audit.py scripts/generate_sbom.py
ruff format --check src/messenger_ai/observability tests/observability scripts/security_audit.py scripts/generate_sbom.py
python scripts/security_audit.py --output docs/security-audit.json
python scripts/generate_sbom.py --output docs/sbom.json
```

Final offline results for this implementation pass:

- focused M13 suite: 38 passed;
- full project suite: 259 passed, 1 skipped;
- Ruff check and format check: passed;
- Python compilation: passed;
- dependency/source audit: 92 Python files scanned, zero prohibited findings;
- offline SBOM: 12 direct dependency/scope records.

The two test warnings are environmental/deprecation notices: the active pytest environment does not provide the configured asyncio plugin option, and Starlette reports a future `httpx` test-client migration. Neither changes the M13 security assertions.

## Not yet proven for real operation

- Current QQ login is not a capability certification.
- Exact-fingerprint QQ read-only 500-sample fixture is not complete.
- Dedicated QQ test-account 100-send suite with zero wrong recipients, duplicates and desktop contention is not complete.
- QQ seven-day and WeChat fourteen-day L3 soak gates are not complete.
- WeChat D0 background send/verify remains unsupported unless its separate M5 POC proves otherwise.
- M1 application wiring for atomic contact erasure and M13 incident/restore callbacks still needs an end-to-end integration test.
- Host deployment review must confirm Windows account ACLs, disk encryption, backup retention and API-key rotation.

Therefore M13 is complete as a code/offline-acceptance module, but real QQ/WeChat L2/L3 activation remains blocked by the listed gates.
