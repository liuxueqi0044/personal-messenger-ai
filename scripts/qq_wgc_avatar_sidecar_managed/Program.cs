using System.Diagnostics;
using System.Diagnostics.CodeAnalysis;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text.Json;
using Vortice.Direct3D;
using Vortice.Direct3D11;
using Vortice.DXGI;
using Windows.Graphics;
using Windows.Graphics.Capture;
using Windows.Graphics.DirectX;
using Windows.Graphics.DirectX.Direct3D11;
using WinRT;
using WinRtDirect3DDevice = Windows.Graphics.DirectX.Direct3D11.IDirect3DDevice;

return Sidecar.Run(args);

internal sealed class SidecarFailure(string code) : Exception(code)
{
    internal string Code { get; } = code;
}

internal sealed record Candidate(double X, double Y, double Width, double Height);

internal sealed record CaptureRequest(
    int ProcessId,
    nint WindowHandle,
    string HeaderDigest,
    string StructureDigest,
    IReadOnlyList<Candidate> Candidates);

[StructLayout(LayoutKind.Sequential)]
internal readonly record struct NativeRect(int Left, int Top, int Right, int Bottom);

[ComImport]
[Guid("3628E81B-3CAC-4C60-B7F4-23CE0E0C3356")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IGraphicsCaptureItemInterop
{
    nint CreateForWindow(nint window, [In] ref Guid iid);

    // This interface has another native method, but it is deliberately not
    // projected here. The sidecar has no non-window capture entry point.
}

[ComImport]
[Guid("A9B3D012-3DF2-4EE3-B8D1-8695F457D3C1")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IDirect3DDxgiInterfaceAccess
{
    nint GetInterface([In] ref Guid iid);
}

internal static class NativeMethods
{
    internal const uint GaRoot = 2;

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool IsWindow(nint window);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool IsWindowVisible(nint window);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool IsIconic(nint window);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool IsZoomed(nint window);

    [DllImport("user32.dll")]
    internal static extern nint GetForegroundWindow();

    [DllImport("user32.dll")]
    internal static extern nint GetAncestor(nint window, uint flags);

    [DllImport("user32.dll")]
    internal static extern uint GetWindowThreadProcessId(nint window, out uint processId);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool GetWindowRect(nint window, out NativeRect rectangle);

    [DllImport("combase.dll")]
    internal static extern int RoInitialize(uint initializationType);

    [DllImport("combase.dll")]
    internal static extern void RoUninitialize();

    [DllImport("d3d11.dll")]
    internal static extern int CreateDirect3D11DeviceFromDXGIDevice(
        nint dxgiDevice,
        out nint graphicsDevice);
}

internal static class Sidecar
{
    private const int KeyLineLimit = 256;
    private const int JsonLineLimit = 65_536;
    private const int TailLimit = 1_024;
    private const int FrameCount = 3;
    private static string InternalStage = "STARTUP";
    private static readonly Guid GraphicsCaptureItemIid =
        new("79C3F95B-31F7-4EC2-A464-632EF5D30760");
    private static readonly Guid Texture2DIid =
        new("6F15AAF2-D208-4E89-9AB4-489535D34F9C");
    private static readonly byte[] HmacDomain =
        "personal-messenger-ai/avatar-hmac-v1"u8.ToArray();
    private static readonly HashSet<string> RequestFields =
    [
        "protocol",
        "operation",
        "process_id",
        "window_handle",
        "expected_header_digest",
        "structure_digest",
        "candidates",
        "capture_api",
        "desktop_capture",
    ];
    private static readonly HashSet<string> CandidateFields =
    [
        "normalized_x",
        "normalized_y",
        "normalized_width",
        "normalized_height",
    ];

    internal static int Run(string[] args)
    {
        Console.InputEncoding = System.Text.Encoding.UTF8;
        Console.OutputEncoding = new System.Text.UTF8Encoding(false);
        byte[] key = [];
        var runtimeInitialized = false;
        try
        {
            if (args.Length != 0)
            {
                Fail("ARGUMENTS_FORBIDDEN");
            }
            var encodedKey = ReadLimitedLine(Console.In, KeyLineLimit, "KEY_STDIN_INVALID");
            var requestJson = ReadLimitedLine(Console.In, JsonLineLimit, "REQUEST_STDIN_INVALID");
            RequireWhitespaceTail(Console.In);
            key = DecodeKey(encodedKey);
            var request = ParseRequest(requestJson);
            InternalStage = "INITIALIZE_WINRT";
            var initializeResult = NativeMethods.RoInitialize(1);
            if (initializeResult < 0)
            {
                Fail("WINRT_INITIALIZATION_FAILED");
            }
            runtimeInitialized = true;
            var result = Capture(request, key);
            EmitSuccess(request, result.AvatarHmac, result.StableMatchCount);
            return 0;
        }
        catch (SidecarFailure failure)
        {
            EmitFailure(failure.Code);
            return 2;
        }
        catch (Exception exception)
        {
            EmitFailure(
                $"WGC_INTERNAL_ERROR_{InternalStage}_{exception.HResult:X8}");
            return 2;
        }
        finally
        {
            CryptographicOperations.ZeroMemory(key);
            if (runtimeInitialized)
            {
                NativeMethods.RoUninitialize();
            }
        }
    }

    private static string ReadLimitedLine(TextReader reader, int limit, string error)
    {
        var buffer = new char[limit + 1];
        var count = 0;
        while (true)
        {
            var value = reader.Read();
            if (value < 0 || value == '\n')
            {
                break;
            }
            if (value == '\r')
            {
                if (reader.Peek() == '\n')
                {
                    reader.Read();
                }
                break;
            }
            if (count >= limit)
            {
                Fail(error);
            }
            buffer[count++] = (char)value;
        }
        if (count == 0)
        {
            Fail(error);
        }
        return new string(buffer, 0, count);
    }

    private static void RequireWhitespaceTail(TextReader reader)
    {
        var count = 0;
        while (true)
        {
            var value = reader.Read();
            if (value < 0)
            {
                return;
            }
            if (++count > TailLimit || !char.IsWhiteSpace((char)value))
            {
                Fail("REQUEST_STDIN_INVALID");
            }
        }
    }

    private static byte[] DecodeKey(string encoded)
    {
        try
        {
            var key = Convert.FromBase64String(encoded);
            if (key.Length is < 32 or > 64)
            {
                CryptographicOperations.ZeroMemory(key);
                Fail("KEY_STDIN_INVALID");
            }
            return key;
        }
        catch (FormatException)
        {
            Fail("KEY_STDIN_INVALID");
            return [];
        }
    }

    private static CaptureRequest ParseRequest(string json)
    {
        try
        {
            using var document = JsonDocument.Parse(
                json,
                new JsonDocumentOptions
                {
                    AllowTrailingCommas = false,
                    CommentHandling = JsonCommentHandling.Disallow,
                    MaxDepth = 6,
                });
            var root = document.RootElement;
            RequireExactFields(root, RequestFields, "REQUEST_SCHEMA_INVALID");
            RequireString(root, "protocol", "qq-wgc-avatar-v1");
            RequireString(root, "operation", "capture_current_avatar");
            RequireString(root, "capture_api", "WindowsGraphicsCapture");
            if (root.GetProperty("desktop_capture").ValueKind != JsonValueKind.False)
            {
                Fail("DESKTOP_CAPTURE_FORBIDDEN");
            }
            var processId = ReadPositiveInt(root, "process_id", int.MaxValue);
            var handleValue = ReadPositiveLong(root, "window_handle", 9_007_199_254_740_991L);
            var headerDigest = ReadDigest(root, "expected_header_digest");
            var structureDigest = ReadDigest(root, "structure_digest");
            var candidatesElement = root.GetProperty("candidates");
            if (candidatesElement.ValueKind != JsonValueKind.Array)
            {
                Fail("CANDIDATE_SCHEMA_INVALID");
            }
            var candidates = candidatesElement.EnumerateArray()
                .Select(ParseCandidate)
                .ToArray();
            if (candidates.Length is < 2 or > 256)
            {
                Fail("CANDIDATE_COUNT_INVALID");
            }
            return new CaptureRequest(
                processId,
                checked((nint)handleValue),
                headerDigest,
                structureDigest,
                candidates);
        }
        catch (SidecarFailure)
        {
            throw;
        }
        catch (Exception exception) when (
            exception is JsonException or InvalidOperationException or OverflowException)
        {
            Fail("REQUEST_SCHEMA_INVALID");
            throw;
        }
    }

    private static Candidate ParseCandidate(JsonElement element)
    {
        RequireExactFields(element, CandidateFields, "CANDIDATE_SCHEMA_INVALID");
        var x = ReadCoordinate(element, "normalized_x");
        var y = ReadCoordinate(element, "normalized_y");
        var width = ReadCoordinate(element, "normalized_width");
        var height = ReadCoordinate(element, "normalized_height");
        if (width <= 0 || height <= 0 || x + width > 1 || y + height > 1)
        {
            Fail("CANDIDATE_GEOMETRY_INVALID");
        }
        return new Candidate(x, y, width, height);
    }

    private static void RequireExactFields(
        JsonElement element,
        HashSet<string> expected,
        string error)
    {
        if (element.ValueKind != JsonValueKind.Object)
        {
            Fail(error);
        }
        var names = element.EnumerateObject().Select(property => property.Name).ToArray();
        if (names.Length != expected.Count || names.Any(name => !expected.Contains(name)))
        {
            Fail(error);
        }
    }

    private static void RequireString(JsonElement root, string name, string expected)
    {
        if (root.GetProperty(name).ValueKind != JsonValueKind.String ||
            !string.Equals(root.GetProperty(name).GetString(), expected, StringComparison.Ordinal))
        {
            Fail("REQUEST_SCHEMA_INVALID");
        }
    }

    private static int ReadPositiveInt(JsonElement root, string name, int maximum)
    {
        if (!root.GetProperty(name).TryGetInt32(out var value) || value <= 0 || value > maximum)
        {
            Fail("REQUEST_SCHEMA_INVALID");
        }
        return value;
    }

    private static long ReadPositiveLong(JsonElement root, string name, long maximum)
    {
        if (!root.GetProperty(name).TryGetInt64(out var value) || value <= 0 || value > maximum)
        {
            Fail("REQUEST_SCHEMA_INVALID");
        }
        return value;
    }

    private static double ReadCoordinate(JsonElement root, string name)
    {
        var property = root.GetProperty(name);
        if (property.ValueKind != JsonValueKind.Number)
        {
            Fail("CANDIDATE_GEOMETRY_INVALID");
        }
        var value = property.GetDouble();
        if (!double.IsFinite(value) || value < 0 || value > 1)
        {
            Fail("CANDIDATE_GEOMETRY_INVALID");
        }
        return value;
    }

    private static string ReadDigest(JsonElement root, string name)
    {
        if (root.GetProperty(name).ValueKind != JsonValueKind.String)
        {
            Fail("REQUEST_SCHEMA_INVALID");
        }
        var value = root.GetProperty(name).GetString() ?? string.Empty;
        if (value.Length != 64 || value.Any(character =>
                character is not (>= '0' and <= '9') and not (>= 'a' and <= 'f')))
        {
            Fail("REQUEST_SCHEMA_INVALID");
        }
        return value;
    }

    private static (string AvatarHmac, int StableMatchCount) Capture(
        CaptureRequest request,
        byte[] key)
    {
        InternalStage = "VALIDATE_WINDOW";
        var originalForeground = ValidateWindow(request);
        var originalRectangle = GetRectangle(request.WindowHandle);
        if (!GraphicsCaptureSession.IsSupported())
        {
            Fail("WGC_UNSUPPORTED");
        }
        InternalStage = "CREATE_D3D11_DEVICE";
        using var d3dDevice = CreateDevice(out var context);
        using (context)
        using (var dxgiDevice = d3dDevice.QueryInterface<IDXGIDevice>())
        {
            InternalStage = "CREATE_PROJECTED_DEVICE";
            var projectedDevice = CreateProjectedDevice(dxgiDevice);
            InternalStage = "CREATE_CAPTURE_ITEM";
            var item = CreateCaptureItem(request.WindowHandle);
            var size = item.Size;
            if (size.Width < 300 || size.Height < 200)
            {
                Fail("CAPTURE_SIZE_INVALID");
            }
            InternalStage = "VALIDATE_CANDIDATES";
            ValidateCandidatePixelGeometry(request.Candidates, size);
            var hashes = Enumerable.Range(0, request.Candidates.Count)
                .Select(_ => new List<string>(FrameCount))
                .ToArray();
            InternalStage = "CAPTURE_FRAMES";
            CaptureFrames(
                request,
                key,
                d3dDevice,
                context,
                projectedDevice,
                item,
                size,
                originalForeground,
                hashes);
            InternalStage = "VALIDATE_RESULT";
            EnsureWindowUnchanged(request, originalRectangle, originalForeground);
            var stable = hashes
                .Where(values => values.Count == FrameCount &&
                    values.Distinct(StringComparer.Ordinal).Count() == 1)
                .Select(values => values[0])
                .ToArray();
            if (stable.Length < 2)
            {
                Fail("PENDING_AVATAR_UNSTABLE");
            }
            var unique = stable.Distinct(StringComparer.Ordinal).ToArray();
            if (unique.Length != 1)
            {
                Fail("PENDING_AVATAR_NOT_UNIQUE");
            }
            return (unique[0], stable.Length);
        }
    }

    private static ID3D11Device CreateDevice(out ID3D11DeviceContext context)
    {
        var levels = new[]
        {
            FeatureLevel.Level_11_1,
            FeatureLevel.Level_11_0,
            FeatureLevel.Level_10_1,
            FeatureLevel.Level_10_0,
        };
        var result = D3D11.D3D11CreateDevice(
            nint.Zero,
            DriverType.Hardware,
            DeviceCreationFlags.BgraSupport,
            levels,
            out var device,
            out context);
        if (result.Failure || device is null || context is null)
        {
            device?.Dispose();
            context?.Dispose();
            Fail("D3D11_DEVICE_FAILED");
        }
        using (var multithread = device.QueryInterface<ID3D11Multithread>())
        {
            multithread.SetMultithreadProtected(true);
        }
        return device;
    }

    private static WinRtDirect3DDevice CreateProjectedDevice(IDXGIDevice dxgiDevice)
    {
        var result = NativeMethods.CreateDirect3D11DeviceFromDXGIDevice(
            dxgiDevice.NativePointer,
            out var pointer);
        if (result < 0 || pointer == nint.Zero)
        {
            if (pointer != nint.Zero)
            {
                Marshal.Release(pointer);
            }
            Fail("PROJECTED_DEVICE_FAILED");
        }
        try
        {
            return MarshalInterface<WinRtDirect3DDevice>.FromAbi(pointer);
        }
        finally
        {
            Marshal.Release(pointer);
        }
    }

    private static GraphicsCaptureItem CreateCaptureItem(nint window)
    {
        var interop = GraphicsCaptureItem.As<IGraphicsCaptureItemInterop>();
        var iid = GraphicsCaptureItemIid;
        var pointer = interop.CreateForWindow(window, ref iid);
        if (pointer == nint.Zero)
        {
            Fail("WGC_ITEM_FAILED");
        }
        try
        {
            return MarshalInterface<GraphicsCaptureItem>.FromAbi(pointer);
        }
        finally
        {
            Marshal.Release(pointer);
        }
    }

    private static void CaptureFrames(
        CaptureRequest request,
        byte[] key,
        ID3D11Device device,
        ID3D11DeviceContext context,
        WinRtDirect3DDevice projectedDevice,
        GraphicsCaptureItem item,
        SizeInt32 size,
        nint originalForeground,
        IReadOnlyList<List<string>> hashes)
    {
        using var frameReady = new AutoResetEvent(false);
        InternalStage = "CREATE_FRAME_POOL";
        using var pool = Direct3D11CaptureFramePool.CreateFreeThreaded(
            projectedDevice,
            DirectXPixelFormat.B8G8R8A8UIntNormalized,
            2,
            size);
        InternalStage = "CREATE_CAPTURE_SESSION";
        using var session = pool.CreateCaptureSession(item);
        session.IsCursorCaptureEnabled = false;
        void OnFrameArrived(object? sender, object args) => frameReady.Set();

        pool.FrameArrived += OnFrameArrived;
        try
        {
            InternalStage = "START_CAPTURE";
            session.StartCapture();
            var captureDeadline = Stopwatch.StartNew();
            var capturedFrameCount = 0;
            while (capturedFrameCount < FrameCount)
            {
                InternalStage = "WAIT_FRAME";
                if (NativeMethods.GetForegroundWindow() != originalForeground)
                {
                    Fail("FOREGROUND_CHANGED");
                }
                var remaining = TimeSpan.FromSeconds(8) - captureDeadline.Elapsed;
                if (remaining <= TimeSpan.Zero || !frameReady.WaitOne(remaining))
                {
                    Fail("WGC_FRAME_TIMEOUT");
                }
                using var frame = pool.TryGetNextFrame();
                if (frame is null)
                {
                    continue;
                }
                if (frame.ContentSize.Width != size.Width ||
                    frame.ContentSize.Height != size.Height)
                {
                    Fail("CAPTURE_SIZE_CHANGED");
                }
                InternalStage = "GET_TEXTURE";
                using var texture = GetTexture(frame.Surface);
                InternalStage = "HASH_FRAME";
                HashFrame(request.Candidates, key, device, context, texture, size, hashes);
                capturedFrameCount++;
            }
        }
        finally
        {
            pool.FrameArrived -= OnFrameArrived;
        }
    }

    private static ID3D11Texture2D GetTexture(IDirect3DSurface surface)
    {
        var access = surface.As<IDirect3DDxgiInterfaceAccess>();
        var iid = Texture2DIid;
        var pointer = access.GetInterface(ref iid);
        if (pointer == nint.Zero)
        {
            Fail("SURFACE_ACCESS_FAILED");
        }
        return new ID3D11Texture2D(pointer);
    }

    private static unsafe void HashFrame(
        IReadOnlyList<Candidate> candidates,
        byte[] key,
        ID3D11Device device,
        ID3D11DeviceContext context,
        ID3D11Texture2D source,
        SizeInt32 contentSize,
        IReadOnlyList<List<string>> hashes)
    {
        InternalStage = "CHECK_SURFACE";
        var description = source.Description;
        if (description.Format != Format.B8G8R8A8_UNorm ||
            description.SampleDescription.Count != 1 ||
            description.Width < contentSize.Width ||
            description.Height < contentSize.Height)
        {
            Fail("SURFACE_FORMAT_INVALID");
        }
        var stagingDescription = new Texture2DDescription(
            description.Format,
            description.Width,
            description.Height,
            1,
            1,
            BindFlags.None,
            ResourceUsage.Staging,
            CpuAccessFlags.Read,
            1,
            0,
            ResourceOptionFlags.None);
        InternalStage = "CREATE_STAGING_TEXTURE";
        using var staging = device.CreateTexture2D(stagingDescription);
        InternalStage = "COPY_TEXTURE";
        context.CopyResource(staging, source);
        InternalStage = "MAP_TEXTURE";
        var mapped = context.Map(
            staging,
            0,
            MapMode.Read,
            Vortice.Direct3D11.MapFlags.None);
        try
        {
            if (mapped.DataPointer == nint.Zero || mapped.RowPitch < contentSize.Width * 4)
            {
                Fail("SURFACE_MAP_FAILED");
            }
            InternalStage = "HASH_CANDIDATES";
            for (var index = 0; index < candidates.Count; index++)
            {
                var hash = HashCandidate(
                    candidates[index],
                    key,
                    (byte*)mapped.DataPointer,
                    checked((int)mapped.RowPitch),
                    contentSize.Width,
                    contentSize.Height);
                if (hash is not null)
                {
                    hashes[index].Add(hash);
                }
            }
        }
        finally
        {
            context.Unmap(staging, 0);
        }
    }

    private static unsafe string? HashCandidate(
        Candidate candidate,
        byte[] key,
        byte* pixels,
        int rowPitch,
        int frameWidth,
        int frameHeight)
    {
        var left = (int)Math.Floor(candidate.X * frameWidth);
        var top = (int)Math.Floor(candidate.Y * frameHeight);
        var width = (int)Math.Ceiling(candidate.Width * frameWidth);
        var height = (int)Math.Ceiling(candidate.Height * frameHeight);
        if (width < 8 || height < 8)
        {
            Fail("CANDIDATE_PIXEL_SIZE_TOO_SMALL_DURING_CAPTURE");
        }
        if (width > 128 || height > 128)
        {
            Fail("CANDIDATE_PIXEL_SIZE_TOO_LARGE_DURING_CAPTURE");
        }
        if (left < 0 || top < 0 || left + width > frameWidth || top + height > frameHeight)
        {
            Fail("CANDIDATE_PIXEL_BOUNDS_INVALID_DURING_CAPTURE");
        }
        var minimumLuminance = 255;
        var maximumLuminance = 0;
        var colors = new HashSet<int>();
        for (var y = top; y < top + height; y++)
        {
            var row = pixels + checked(y * rowPitch);
            for (var x = left; x < left + width; x++)
            {
                var pixel = row + checked(x * 4);
                var luminance = (pixel[0] * 11 + pixel[1] * 59 + pixel[2] * 30) / 100;
                minimumLuminance = Math.Min(minimumLuminance, luminance);
                maximumLuminance = Math.Max(maximumLuminance, luminance);
                if (colors.Count < 16)
                {
                    colors.Add((pixel[0] << 16) | (pixel[1] << 8) | pixel[2]);
                }
            }
        }
        if (maximumLuminance - minimumLuminance < 12 || colors.Count < 8)
        {
            return null;
        }
        const int normalizedSize = 16;
        var material = new byte[HmacDomain.Length + normalizedSize * normalizedSize * 3];
        try
        {
            HmacDomain.CopyTo(material, 0);
            var cursor = HmacDomain.Length;
            for (var outputY = 0; outputY < normalizedSize; outputY++)
            {
                var sourceY = top + Math.Min(
                    height - 1,
                    (outputY * height + normalizedSize / 2) / normalizedSize);
                var row = pixels + checked(sourceY * rowPitch);
                for (var outputX = 0; outputX < normalizedSize; outputX++)
                {
                    var sourceX = left + Math.Min(
                        width - 1,
                        (outputX * width + normalizedSize / 2) / normalizedSize);
                    var pixel = row + checked(sourceX * 4);
                    material[cursor++] = pixel[0];
                    material[cursor++] = pixel[1];
                    material[cursor++] = pixel[2];
                }
            }
            return Convert.ToHexString(HMACSHA256.HashData(key, material))
                .ToLowerInvariant();
        }
        finally
        {
            CryptographicOperations.ZeroMemory(material);
        }
    }

    private static nint ValidateWindow(CaptureRequest request)
    {
        var window = request.WindowHandle;
        if (!NativeMethods.IsWindow(window) || !NativeMethods.IsWindowVisible(window) ||
            NativeMethods.IsIconic(window) || !NativeMethods.IsZoomed(window) ||
            NativeMethods.GetAncestor(window, NativeMethods.GaRoot) != window ||
            NativeMethods.GetWindowThreadProcessId(window, out var processId) == 0 ||
            processId != (uint)request.ProcessId)
        {
            Fail("WINDOW_SCOPE_INVALID");
        }
        try
        {
            using var process = Process.GetProcessById(request.ProcessId);
            if (!string.Equals(process.ProcessName, "QQ", StringComparison.OrdinalIgnoreCase) ||
                process.MainWindowHandle != window)
            {
                Fail("WINDOW_SCOPE_INVALID");
            }
        }
        catch (ArgumentException)
        {
            Fail("WINDOW_SCOPE_INVALID");
        }
        var foreground = NativeMethods.GetForegroundWindow();
        if (foreground == window)
        {
            Fail("QQ_FOREGROUND");
        }
        return foreground;
    }

    private static NativeRect GetRectangle(nint window)
    {
        if (!NativeMethods.GetWindowRect(window, out var rectangle) ||
            rectangle.Right - rectangle.Left < 300 || rectangle.Bottom - rectangle.Top < 200)
        {
            Fail("WINDOW_SCOPE_INVALID");
        }
        return rectangle;
    }

    private static void EnsureWindowUnchanged(
        CaptureRequest request,
        NativeRect originalRectangle,
        nint originalForeground)
    {
        if (ValidateWindow(request) != originalForeground ||
            GetRectangle(request.WindowHandle) != originalRectangle)
        {
            Fail("WINDOW_CHANGED");
        }
    }

    private static void ValidateCandidatePixelGeometry(
        IReadOnlyList<Candidate> candidates,
        SizeInt32 size)
    {
        foreach (var candidate in candidates)
        {
            var left = (int)Math.Floor(candidate.X * size.Width);
            var top = (int)Math.Floor(candidate.Y * size.Height);
            var width = (int)Math.Ceiling(candidate.Width * size.Width);
            var height = (int)Math.Ceiling(candidate.Height * size.Height);
            if (width < 8 || height < 8)
            {
                Fail("CANDIDATE_PIXEL_SIZE_TOO_SMALL_BEFORE_CAPTURE");
            }
            if (width > 128 || height > 128)
            {
                Fail("CANDIDATE_PIXEL_SIZE_TOO_LARGE_BEFORE_CAPTURE");
            }
            if (left < 0 || top < 0 || left + width > size.Width || top + height > size.Height)
            {
                Fail("CANDIDATE_PIXEL_BOUNDS_INVALID_BEFORE_CAPTURE");
            }
        }
    }

    private static void EmitSuccess(
        CaptureRequest request,
        string avatarHmac,
        int stableMatchCount)
    {
        var report = new
        {
            probe_version = "qq-uia-current-avatar-v1",
            mode = "current_chat_avatar_capture",
            succeeded = true,
            status = "CURRENT_AVATAR_CAPTURED",
            process_id = request.ProcessId,
            window_handle = request.WindowHandle.ToInt64(),
            is_maximized = true,
            is_foreground_before = false,
            is_foreground_after = false,
            header_candidate_count = 1,
            active_header_digest = request.HeaderDigest,
            candidate_row_count = request.Candidates.Count,
            stable_match_count = stableMatchCount,
            avatar_hmac = avatarHmac,
            structure_digest = request.StructureDigest,
            capture_api = "WindowsGraphicsCapture",
            privacy = new
            {
                exact_hwnd = true,
                desktop_capture = false,
                image_bytes_emitted = false,
                emitted_chat_text = false,
                emitted_control_names = false,
                emitted_runtime_ids = false,
                absolute_screen_coordinates_emitted = false,
                navigation_performed = false,
                foreground_changed = false,
                write_actions_supported = false,
                mouse_input_used = false,
                keyboard_input_used = false,
                clipboard_used = false,
                foreground_requested = false,
                composer_or_send_accessed = false,
            },
        };
        Console.WriteLine(JsonSerializer.Serialize(report));
    }

    private static void EmitFailure(string status)
    {
        Console.WriteLine(JsonSerializer.Serialize(new
        {
            protocol = "qq-wgc-avatar-v1",
            succeeded = false,
            status,
        }));
    }

    [DoesNotReturn]
    private static void Fail(string code) => throw new SidecarFailure(code);
}
