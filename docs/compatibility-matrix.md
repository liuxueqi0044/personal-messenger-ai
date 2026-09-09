# Compatibility and capability governance

The compatibility key is the hash of:

- platform and client version;
- client file signature;
- Windows version;
- DPI scale and theme;
- window mode and window fingerprint;
- Adapter version.

No field is inferred from a nearby version. Changing any field creates an unknown environment, selects it as active, limits it to L0, and quarantines sending.

## Promotion state machine

| State | Required evidence | Maximum level | Sending quarantine |
|---|---|---:|---|
| Unknown/changed | Exact fingerprint observed only | L0 | On |
| Read validated | Passing exact-fingerprint read-only suite, at least 500 samples | L1 draft only | On |
| Dedicated send validated | Prior read pass plus at least 100 dedicated-test-account sends; zero wrong recipient, duplicate, foreground contention and account warning | L2 human approved | Off |
| Soak validated | Prior send pass plus zero safety failures; QQ 7 days, WeChat 14 days | L3 whitelist auto | Off |

A failed safety attestation downgrades or refuses promotion. An incomplete zero-error soak preserves at most the already certified L2 state; it does not grant L3.

## Current project status

- Code and offline fixture governance: implemented and tested.
- Current QQ login: observed by the project, but login alone is not a fixture attestation.
- Current QQ exact environment: remains unverified/quarantined until read fixtures and dedicated test-account sends are recorded.
- QQ 9.9.26 profile-window identity acquisition: `NO-GO`; opening the profile window took foreground and UIA close could not prove restoration. Do not retry this path automatically.
- QQ L3: blocked until the dedicated test-account suite and seven-day zero-error soak pass.
- WeChat L2/L3: blocked unless the exact background send and verification path reaches D0 requirements; otherwise M4 observation/L1 remains valid.

The matrix file records historical exact fingerprints, but only the active fingerprint controls the current maximum level.
