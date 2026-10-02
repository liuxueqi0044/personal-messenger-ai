# Guest-local release contract

A release is a directory copied once from host staging to `C:\PMAI\app\releases\<release-id>`. `manifest.json` hashes every executable and data file in that release. The manifest is verified before copying, after copying, and before every launch.

The release contains one verified wheel plus the explicit guest entry scripts: session bootstrap, selector pack, focus helper, runtime-config builder, runtime supervisor, `run_vm_runtime.py` and `run_vm_runtime_v2.py`. Both runners are always included. The installed wheel is imported from the guest venv; the runner scripts execute only from the local release directory, never from mounted media.

Build from a clean checkout by passing the existing, locally verified selector pack explicitly:

```powershell
uv build --wheel --out-dir work/dist
./scripts/deployment/Build-GuestLocalRelease.ps1 -WheelPath ./work/dist/personal_messenger_ai-0.1.0-py3-none-any.whl -ReleaseId candidate-v2 -StagingRoot ./work/releases -SelectorPackPath C:/PMAI-staging/selector-pack-session-1.json -HybridProbeHelperPath C:/PMAI-staging/QQ.UiaProbe.exe
```

`-SelectorPackPath` accepts a file outside the checkout, so private selector data does not need to be committed. Omitting it preserves the legacy sibling `qq-vm/deploy-media/qq-runtime-session-final-cc8a7201` location. The selected file is copied into the release and included in its integrity manifest; an absent selector pack fails the build.

`-HybridProbeHelperPath` is optional for a V1 release. When supplied it must name the self-contained single-file `QQ.UiaProbe.exe`. The EXE and all five fixed sibling native runtime dependencies (`D3DCompiler_47_cor3.dll`, `PenImc_cor3.dll`, `PresentationNative_cor3.dll`, `vcruntime140_cor3.dll`, `wpfgfx_cor3.dll`) must exist and are copied into the immutable manifest. V2's externally prepared settings must reference the verified local helper, not a mutable staging copy. Use a new release ID for each changed wheel, runner or helper.

Configuration publication requires a stopped runtime for both ordinary and isolated builds. The builder stages identity changes, validates immutable generation inputs before writes, and records the previous metadata bytes in `.runtime-session-1.json.publication.json` before changing any published file. A rejected candidate restores those bytes. An interrupted process leaves a startup fence; rerun the builder while stopped to roll back before trying again. Do not delete that record to force startup. The record and temporary data-directory fences are private recovery metadata, not exportable diagnostic logs. Existing runtime, memory, pacing, and message databases are never restored by this metadata transaction. This is process-interruption recovery, not a substitute for a consistent backup or a guarantee against disk failure.

After QQ restarts, refresh only its session locators in the existing business generation. Stop and pause the runtime, obtain fresh operator-observed evidence for every registered contact, and invoke the verified release's builder explicitly:

```powershell
C:\PMAI\app\.venv\Scripts\python.exe C:\PMAI\app\releases\<release-id>\build_session_observed_runtime_guest.py --refresh-current-session --expected-current-generation <generation-uuid> --expected-current-config-sha256 <sha256-of-canonical-config>
```

This operation rejects missing databases, an unpaused generation, stale expected values, an incomplete contact set, or changed stable identities. It cannot be combined with new-generation, contact-adoption or migration flags. Only PID, HWND, process creation time and row runtime-ID hash (including matching binding projections) may change. The original `runtime-config.json` and manifest remain immutable; revisions use `runtime-config.session-2.json`, then consecutive numbers with hashes of the root and previous snapshot. Publication validates the complete chain and publishes the canonical config last using the same rollback journal. `--check` reads active rules through an in-memory SQLite backup and does not initialize or migrate the live rules database. The supervisor verifies canonical bytes against the current snapshot and passes its revision and digest to the runner. Session revision is independent of business binding revision; no cursor, memory, adoption, message history or uncertain-send state is reset. Successful refresh leaves the runtime stopped and paused; starting or resuming is a separate operation.

An existing generation may contain `one-shot-intents`, created by the earlier single-attempt tool. Session refresh accepts this exact directory with canonical UUID `.json` regular files only; it rejects reparse points, hard links, nested directories and unknown directories. It never parses, rewrites or removes those intent files, including empty or incomplete files, because their existence itself prevents replay. New-generation creation retains its original flat-directory rule.

Driver diagnostics are under `C:\PMAI\data\logs`: `qq-worker-parent.jsonl` and `qq-worker-child.jsonl` each rotate at approximately 2 MB with three backups. `qq-worker-first-failure.json` freezes the first failure within a run, including ordinary safe failures; `qq-worker-last-failure.json` tracks the most recent failure. Requests are correlated by run/request/operation identifiers, not chat content. Child stage starts are flushed before UI calls so a hung child leaves its last entered stage. The canonical worker status also carries first/last failure, last successful request, and parent diagnostic-write failure count. The files intentionally omit message text, screenshots, visual labels, API keys, exception messages, and handoff tokens. UI subprocesses use the installed `pythonw.exe`, independently of the supervisor's console mode.

`Install-GuestLocalRelease.ps1` validates the guest and the installed package first. It copies and verifies the release, and only when installed `messenger_ai/*.py` differs from the release wheel, updates it with that local wheel using `--no-index --no-deps` and verifies again. It never starts QQ, calls a model, or sends a message. A later wheel update creates a new release id and manifest; it must not overwrite an existing release.

`Start-GuestLocalRuntime.ps1` retains its V1 build/start behavior; it does not opt into V2. `Validate` checks the local release and its installed package. `Bootstrap -BindingId session-contact-1|session-contact-2` writes the corresponding numbered report and defaults to contact 1. `BuildAndStart` ignores an existing contact-2 report unless explicitly given `-IncludeContact2`. It uses the durable local account contract: `C:\PMAI\data\runtime\qq-default-account` and local registry identifiers, never process-derived directories or IDs. A changed QQ session blocks for an explicit rebind and preserves existing durable data. The wrapper's run-scoped `Pause`, `Resume` and `GracefulStop` controls also work with the V2 supervisor.

## Opt-in V2 deployment

V2 uses the existing business generation and current immutable session snapshot. It does not rebuild registrations, enroll profile identity, reset adoption/cursors/persona, or replay historical failed operations. Its separate absolute JSON settings snapshot contains approved active bindings, externally supplied HMAC identity anchors and the current certified UI configuration. Prepare and freeze that snapshot privately while stopped; identity anchors and window/process metadata are not published here. If the QQ session or helper path changes, explicitly prepare and validate new matching settings before startup.

Pause and gracefully stop any current runtime using its verified release's wrapper before installing a changed wheel. After copying the frozen host release directory to guest staging, install and verify it locally:

```powershell
$sourceRelease = 'C:\PMAI-staging\candidate-v2'
& (Join-Path $sourceRelease 'Install-GuestLocalRelease.ps1') -SourceReleaseRoot $sourceRelease
if ($LASTEXITCODE -ne 0) { throw 'Release install failed' }
$release = 'C:\PMAI\app\releases\candidate-v2'
& (Join-Path $release 'Test-GuestLocalRelease.ps1') -ReleaseRoot $release -VerifyInstalled
if ($LASTEXITCODE -ne 0) { throw 'Release verification failed' }
```

The following paths and binding are placeholders for already approved private inputs. Read the current canonical revision and compute fixed digests once; neither startup nor `--check` republishes the original config or settings:

```powershell
$python = 'C:\PMAI\app\.venv\Scripts\python.exe'
$pythonw = 'C:\PMAI\app\.venv\Scripts\pythonw.exe'
$canonical = 'C:\PMAI\data\runtime-session-1.json'
$snapshot = '<absolute-current-immutable-config-snapshot>'
$settings = '<absolute-immutable-hybrid-settings.json>'
$binding = '<approved-binding-id>'
$config = Get-Content -LiteralPath $canonical -Raw | ConvertFrom-Json
$revision = if ($null -eq $config.session_binding) { 1 } else { [int]$config.session_binding.revision }
$configDigest = (Get-FileHash -LiteralPath $canonical -Algorithm SHA256).Hash.ToLowerInvariant()
$settingsDigest = (Get-FileHash -LiteralPath $settings -Algorithm SHA256).Hash.ToLowerInvariant()
& $python (Join-Path $release 'run_vm_runtime_v2.py') --config $snapshot --publication-config $canonical --hybrid-settings $settings --active-binding $binding --run-id ([guid]::NewGuid().ToString()) --expected-config-sha256 $configDigest --expected-session-binding-revision $revision --expected-hybrid-settings-sha256 $settingsDigest --check
if ($LASTEXITCODE -ne 0) { throw 'V2 configuration check failed' }
```

`--check` validates without acquiring a desktop worker, calling an API or starting the runtime. As in V1, publication fences are checked on actual startup rather than in check mode. Expected session revision is checked before reading vault secrets; the existing guest DPAPI provider key is used for configuration validation, with no new signing key created by check mode. `--config` remains the frozen snapshot and `--publication-config` is only the canonical publication fence.

Launch the existing guest supervisor directly through the installed `pythonw.exe` to select V2. It generates the actual run UUID, resolves the frozen business snapshot, and passes both publication/session fences and the fixed settings digest to the V2 runner:

```powershell
$supervisorArguments = @('--hybrid-settings', $settings, '--active-binding', $binding,
    '--expected-config-sha256', $configDigest,
    '--expected-session-binding-revision', [string]$revision,
    '--expected-hybrid-settings-sha256', $settingsDigest)
if ($null -ne $config.runtime_generation) {
    $supervisorArguments += @('--expected-generation-id', [string]$config.runtime_generation.generation_id)
}
& $pythonw (Join-Path $release 'qq_session_runtime_supervisor_guest.py') @supervisorArguments
```

For an explicitly approved set of several bindings, repeat `--active-binding <approved-binding-id>` for each, in both check and supervisor commands; the settings must cover exactly that set. Omitting `--hybrid-settings` keeps the supervisor's V1 runner. A changed settings file cannot silently replace the frozen launch bytes because the runner checks the captured digest again. This opt-in selects an implementation; it does not constitute multi-contact or delivery acceptance.

Each V2 desktop round has an original 45 s UTC/monotonic deadline including cleanup and a 20 s preparation write reserve. Cold preparation precedes the existing 10 s authorization; exact ticket adoption follows authorization without extending that TTL. Commit intent is persisted before a synchronous current-guard refresh and COMMIT IPC. VERIFY closes and reaps the old worker, then uses a fresh process and epoch. Failed cleanup or ambiguous commit retains ownership/operation holds; sending is never retried automatically.

With normal runtime stopped, read-only current-chat OBSERVE passed in 17.962 s with 16 bubbles. The production assembly then passed HEALTH → N1 current Mom `candidate_opened` in 28.712 s (zero model calls, zero navigation actions) → fresh N4 OBSERVE in 19.055 s (16 bubbles, separate worker epochs) → clean close. Global pause was restored, canonical SHA-256 was unchanged and all three historical failed operations were retained. These successes cover an already-current conversation. A later attempt from QQ Game Center returned `needs_attention` / `identity_chat_correlation_unproven` in 14.12 s with zero model calls and zero navigation actions; navigation from that starting page has not passed. Sending, reordered-list navigation, multi-contact use and soak reliability remain unverified. The latest full-suite baseline was 2991 passed, 2 skipped; later targeted performance, scope and CLI checks passed, but the final full suite for the combined current tree has not yet been recorded.

Use the wrapper controls against the supervisor's current run UUID. Resume is a separate operational action that enables ordinary processing; issue it only for the intended acceptance or running phase:

```powershell
& (Join-Path $release 'Start-GuestLocalRuntime.ps1') -Phase Pause
```

After confirming the intended resume phase:

```powershell
& (Join-Path $release 'Start-GuestLocalRuntime.ps1') -Phase Resume
```

To drain and stop the current run:

```powershell
& (Join-Path $release 'Start-GuestLocalRuntime.ps1') -Phase GracefulStop
```

Each invocation generates a new request UUID, reads the current status `run_id`, and waits for that exact run/request/action acknowledgement. Inspect the acknowledged phase before issuing the next control; do not treat an old status file or an accepted but still draining request as a completed pause/stop.

For a fresh isolated generation, `BuildIsolated -IsolatedRecoveryGeneration <UUID> -ContactIndex 3` selects only contact 3; `-ContactIndex 3,4` selects exactly contacts 3 and 4. The bottom-level builder accepts repeatable `--contact-index` only alongside `--isolated-recovery-generation`. This explicit set never implicitly adds contacts 1/2 or prior registrations. It cannot be mixed with `-IncludeContact2` / `--include-contact-2` or `-AdditionalContactIndex` / `--additional-contact-index`. Indices must be 1–9999. Adoption must belong to the selected set, any session refresh must cover the entire set, header migration must belong to refresh, and visual labels, when supplied, must cover the set one-to-one. Argument errors are rejected before creating the isolated generation directory or changing runtime configuration/databases. Reusing an existing generation UUID with a different explicit contact set is also rejected before writes; same-set retries retain the existing validation and retry behavior.

The host runner and guest wrapper forward their existing repeated `--contact-index` arguments as this exact set in isolated mode. Without isolated mode, those host/wrapper arguments retain their existing session-refresh semantics. Omitting the new bottom-level builder or launcher selection preserves compatibility selection and discovery. Isolated builds retain global pause, the active RulePack/persona snapshot, and the existing content-policy setting, and preserve the previous durable account data. Selecting contacts does not authorize starting, resuming, or sending; `BuildIsolated` only builds the paused generation. `BuildIsolatedAndStart` remains the separately selected start phase.

`build_visual_selection_acceptance_config_guest.py` first copies the already-valid canonical runtime configuration into an attempt-local file and adds only a fully covered, revalidated `visual_selection` section. It does not rebuild registrations, migrate evidence, change cursors, or overwrite the canonical runtime configuration. `qq_visual_selection_acceptance_guest.py` then acquires the same per-user runtime mutex, records a non-replayable attempt before any model or UI action, permits only one first-generation `SELECT_ONLY` request, retires that worker, and uses a provider-free second generation with `VERIFY_SELECTION_ONLY`. It cannot observe bubbles, access the composer, invoke Send, or start the full runtime. Its report contains only whitelisted structural identity fields and visual frame/model/latency metadata; row images, labels, message text, and secrets are excluded. The host runner removes the attempt-local configuration after the gate while preserving the intent record.

`Pause`, `Resume`, and `GracefulStop` use the run-scoped file control channel; they do not drive the guest desktop or terminate a process. The launcher reads the current runtime status, writes an atomically replaced request to `C:\PMAI\data\qq-session-runtime-control-request.json`, and waits up to 120 seconds for a matching acknowledgement in `C:\PMAI\data\qq-session-runtime-control-result.json`. Requests and results use their respective `pmai-qq-runtime-control-*-v1` schemas and match a generated request id plus the current status `run_id`. Pause-bearing actions first publish `accepted=true` / `pausing` after the in-memory fence is installed, then publish `paused` or `stopping` only after the current UI-bearing tick has drained. If that drain exceeds 120 seconds the launcher records `pausing` / `DRAIN_PENDING` (exit 4), which is a known accepted request and is never retried automatically; the later final acknowledgement remains inspectable. A timeout before any acceptance is recorded as `pending` / `TIMEOUT_UNKNOWN` (exit 3). A mismatched target is rejected as `TARGET_RUN_MISMATCH`. A successful graceful stop has result state `stopping`; once its child exits normally the supervisor reports `graceful_stopped`, with `ready=false`, rather than an installation or startup failure.

The supervisor assigns each runtime start a unique loopback WebUI port. A live recorded PID and a same-run `/inbox` response establish `web_reachable`, not QQ readiness by themselves. `ready` additionally requires fresh, same-run QQ evidence. For V2, the bounded status witness must match the actual run UUID and approved active set, and agree with current SQLite global revision/pause state. Every active direct conversation must retain its registered identity/revision, be unpaused and have `last_observed_at` at or after supervisor start, not in the future, and no older than 90 s. A finite V2 child may be idle after a successful observation; `cleanup_required` or a closed session blocks readiness. `runtime_started` records child launch only. Aggregate planning, send, receipt and pause counts remain diagnostics and do not prove a reply, model call or delivery.

Operational status and content-free process diagnostics are under `C:\PMAI\data` (logs are in `C:\PMAI\data\logs`). Runtime configuration, HMAC anchors and deployment settings are private inputs; do not publish them with those diagnostics. Release scripts do not print, copy or export guest vault secrets. The application retrieves its provider key from the guest vault for configuration validation and runtime startup.
