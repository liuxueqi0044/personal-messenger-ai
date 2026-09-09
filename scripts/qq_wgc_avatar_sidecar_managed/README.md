# Managed QQ WGC avatar sidecar

This is the exact-HWND, read-only Windows Graphics Capture implementation used
when Smart App Control prevents installing or running an unsigned native build.
It targets the installed .NET 10 host, emits no apphost executable, and exposes
only `CreateForWindow` through its managed COM projection.

The sidecar accepts a base64 HMAC key and one bounded JSON request on stdin. It
captures three frames for the authenticated QQ HWND, computes a normalized ROI
HMAC in memory, clears key/material buffers, and emits only the established
redacted avatar report. It has no navigation, desktop source, global input,
clipboard, image encoding, image file, or send capability.

Build and run:

```powershell
dotnet build .\QQ.WgcAvatar.csproj --configuration Release
dotnet .\bin\Release\net10.0-windows10.0.26100.0\QQ.WgcAvatar.dll
```

The implementation uses the MIT-licensed `Vortice.Direct3D11` NuGet package
version 3.8.3. The package is pinned in the project and covered by the project
dependency/SBOM checks.
