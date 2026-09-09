# QQ Windows Graphics Capture Avatar Sidecar

This directory is the B1 build skeleton for a future, isolated native sidecar.
It is **not a capture implementation**: the current executable returns
`WGC_CORE_NOT_IMPLEMENTED`, takes no capture action, and must never be treated
as a usable avatar-identity provider.

The eventual sidecar is intended to use `IGraphicsCaptureItemInterop::CreateForWindow`
with a previously authenticated exact QQ HWND, rather than monitor or desktop
capture. It must preserve the existing Q3 contract: no foreground request,
navigation, mouse/keyboard/clipboard use, raw pixels, image files, chat text,
or identity values in stdout.

## Toolchain contract

The project is deliberately pinned to:

- Visual Studio 2022 / MSVC `v143`;
- Windows 11 SDK `10.0.26100.0` (x64);
- Release x64 only.

Run the read-only prerequisite check before any build:

```powershell
pwsh -File .\check_prerequisites.ps1
```

It prints `READY` only when the complete compiler, MSBuild, SDK headers, and
SDK libraries are present. Otherwise it prints `MISSING_TOOLCHAIN` and exits
non-zero; this is an environment prerequisite, not a claim that the Python
project failed.

When the prerequisites pass, build with:

```powershell
pwsh -File .\build.ps1
```

## Planned integration boundary

The production WGC implementation must be independently accepted before it is
connected to `scripts/qq_q3_avatar_assess.py`. Its only successful output may
be the already-defined `qq-uia-current-avatar-v1` JSON with
`capture_api=WindowsGraphicsCapture`; it must calculate the keyed ROI digest
locally and emit neither image data nor UIA names/text.

## Reference and licensing

The planned capture lifecycle follows the design direction of Microsoft's
Windows Graphics Capture APIs and the MIT-licensed
[robmikh/Win32CaptureSample](https://github.com/robmikh/Win32CaptureSample).
No source code from that sample is included in this B1 skeleton. Any later
copied or adapted material must retain its required attribution; see
`THIRD_PARTY_NOTICES.md`.
