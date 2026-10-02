using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Microsoft.Win32;
using System.Windows.Automation;

static int ReadMaxNodes(string[] arguments)
{
    for (var index = 0; index < arguments.Length - 1; index += 1)
    {
        if (arguments[index] == "--max-nodes" && int.TryParse(arguments[index + 1], out var parsed))
        {
            return Math.Clamp(parsed, 1, 20_000);
        }
    }
    return 5_000;
}

static bool HasFlag(string[] arguments, string flag) =>
    arguments.Any(argument => string.Equals(argument, flag, StringComparison.Ordinal));

static int ReadWarmupMilliseconds(string[] arguments)
{
    for (var index = 0; index < arguments.Length - 1; index += 1)
    {
        if (arguments[index] == "--warmup-ms" && int.TryParse(arguments[index + 1], out var parsed))
        {
            return Math.Clamp(parsed, 250, 10_000);
        }
    }
    return 2_000;
}

static int ReadForegroundWaitMilliseconds(string[] arguments)
{
    for (var index = 0; index < arguments.Length - 1; index += 1)
    {
        if (arguments[index] == "--wait-for-foreground-ms" &&
            int.TryParse(arguments[index + 1], out var parsed))
        {
            return Math.Clamp(parsed, 0, 120_000);
        }
    }
    return 0;
}

static (bool Supplied, long Value) ReadLongOption(string[] arguments, string name)
{
    for (var index = 0; index < arguments.Length - 1; index += 1)
    {
        if (arguments[index] == name)
        {
            return long.TryParse(arguments[index + 1], out var parsed)
                ? (true, parsed)
                : (true, -1);
        }
    }
    return (false, 0);
}

static bool Supports(AutomationElement element, AutomationPattern pattern)
{
    try
    {
        return element.TryGetCurrentPattern(pattern, out _);
    }
    catch (ElementNotAvailableException)
    {
        return false;
    }
    catch (InvalidOperationException)
    {
        return false;
    }
}

static void Increment(Dictionary<string, int> counts, string? key)
{
    if (string.IsNullOrWhiteSpace(key))
    {
        return;
    }
    counts[key] = counts.GetValueOrDefault(key) + 1;
}

static object[] Top(Dictionary<string, int> counts, int limit = 20) =>
    counts.OrderByDescending(pair => pair.Value)
        .ThenBy(pair => pair.Key, StringComparer.Ordinal)
        .Take(limit)
        .Select(pair => (object)new { value = pair.Key, count = pair.Value })
        .ToArray();

static double SafeCoordinate(double value) => double.IsFinite(value) ? Math.Round(value) : 0;

static double Normalize(double value, double size) =>
    double.IsFinite(value) && size > 0 ? Math.Round(Math.Clamp(value / size, 0, 1), 6) : 0;

static string Sha256(string value) =>
    Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(value))).ToLowerInvariant();

// QQ NT may activate its Chromium render child. Owner-window chains do not
// count: only an actual same-process parent chain can reach this exact root.
static IntPtr ForegroundRoot()
{
    var foreground = NativeMethods.GetForegroundWindow();
    if (foreground == IntPtr.Zero) return IntPtr.Zero;
    var root = NativeMethods.GetAncestor(foreground, 2); // GA_ROOT, never GA_ROOTOWNER.
    if (root == IntPtr.Zero) return IntPtr.Zero;
    NativeMethods.GetWindowThreadProcessId(foreground, out var foregroundPid);
    NativeMethods.GetWindowThreadProcessId(root, out var rootPid);
    return foregroundPid == rootPid && NativeMethods.IsWindowVisible(root) ? root : IntPtr.Zero;
}

static string? RuntimeKey(AutomationElement element)
{
    try
    {
        var runtimeId = element.GetRuntimeId();
        return runtimeId is null ? null : Sha256(string.Join(".", runtimeId));
    }
    catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException)
    {
        return null;
    }
}

static string? ParentRuntimeKey(AutomationElement element, AutomationElement root)
{
    try
    {
        var parent = TreeWalker.ControlViewWalker.GetParent(element);
        return parent is null || parent.Equals(root) ? "__probe_root__" : RuntimeKey(parent);
    }
    catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException)
    {
        return null;
    }
}

static string FileSha256(string path)
{
    try
    {
        using var stream = File.OpenRead(path);
        return Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
    }
    catch (Exception exception) when (exception is IOException or UnauthorizedAccessException)
    {
        return new string('0', 64);
    }
}

static string ReadTheme()
{
    try
    {
        var raw = Registry.GetValue(
            @"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            "AppsUseLightTheme",
            null);
        return raw is int value ? (value == 0 ? "dark" : "light") : "unknown";
    }
    catch (Exception exception) when (exception is IOException or UnauthorizedAccessException)
    {
        return "unknown";
    }
}

static int CountOtherVisibleProcessWindows(int processId, IntPtr mainWindow)
{
    var count = 0;
    NativeMethods.EnumWindows((window, _) =>
    {
        NativeMethods.GetWindowThreadProcessId(window, out var candidateProcessId);
        if (candidateProcessId == processId && window != mainWindow && NativeMethods.IsWindowVisible(window))
        {
            count += 1;
        }
        return true;
    }, IntPtr.Zero);
    return count;
}

static string[] AncestorControlTypes(AutomationElement element, AutomationElement root, int limit = 8)
{
    var result = new List<string>();
    try
    {
        var walker = TreeWalker.ControlViewWalker;
        var current = walker.GetParent(element);
        while (current is not null && !current.Equals(root) && result.Count < limit)
        {
            var controlType = current.Current.ControlType?.ProgrammaticName ?? string.Empty;
            if (!string.IsNullOrWhiteSpace(controlType))
            {
                result.Add(controlType);
            }
            current = walker.GetParent(current);
        }
    }
    catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException)
    {
        return Array.Empty<string>();
    }
    result.Reverse();
    return result.ToArray();
}

static void Emit(object value)
{
    Console.WriteLine(JsonSerializer.Serialize(value, new JsonSerializerOptions { WriteIndented = true }));
}

static void EmitSelectionResult(
    bool succeeded,
    string status,
    int matchCount,
    bool selectionAttempted = false,
    string? matchEvidenceDigest = null,
    object? rightRegionEvidence = null,
    int rootVisibleTextMatchCount = 0,
    int leftVisibleTextMatchCount = 0,
    int rightVisibleTextMatchCount = 0)
{
    Emit(new
    {
        probe_version = "qq-uia-selection-v1",
        mode = "conversation_selection",
        succeeded,
        status,
        match_count = matchCount,
        selection_attempted = selectionAttempted,
        match_evidence_digest = matchEvidenceDigest,
        right_region_evidence = rightRegionEvidence,
        root_visible_text_match_count = rootVisibleTextMatchCount,
        left_visible_text_match_count = leftVisibleTextMatchCount,
        right_visible_text_match_count = rightVisibleTextMatchCount,
        privacy = new
        {
            target_from_stdin_only = true,
            emitted_target = false,
            emitted_control_names = false,
            emitted_chat_text = false,
            emitted_content_hashes_are_aggregate = true,
        },
    });
}

static void EmitCurrentChatResult(
    bool succeeded,
    string status,
    int headerCandidateCount = 0,
    string? activeHeaderDigest = null,
    string? rightRegionStructureDigest = null,
    object[]? messages = null,
    int? processId = null,
    long? windowHandle = null,
    bool? isMinimized = null,
    string? capturedAt = null)
{
    Emit(new
    {
        probe_version = "qq-uia-current-chat-v1",
        mode = "current_chat_capture",
        succeeded,
        status,
        header_candidate_count = headerCandidateCount,
        active_header_digest = activeHeaderDigest,
        right_region_structure_digest = rightRegionStructureDigest,
        messages = messages ?? Array.Empty<object>(),
        process_id = processId,
        window_handle = windowHandle,
        is_minimized = isMinimized,
        captured_at = capturedAt,
        privacy = new
        {
            exact_hwnd = true,
            desktop_capture_supported = false,
            changed_window_state = false,
            write_actions_supported = false,
            emitted_chat_text = true,
        },
    });
}

static void EmitCurrentIdentityResult(
    bool succeeded,
    string status,
    int headerCandidateCount = 0,
    string? activeHeaderDigest = null,
    int identityCandidateCount = 0,
    string? profileIdHmac = null,
    string? identityEvidenceType = null,
    string? profileStructureDigest = null,
    bool recoveryAttempted = false,
    bool originalViewRestored = false,
    bool foregroundChanged = false,
    int? processId = null,
    long? windowHandle = null,
    bool? isMaximized = null,
    bool? isForegroundBefore = null,
    bool? isForegroundAfter = null,
    bool transientNavigationPerformed = false,
    string? rightRegionStructureDigest = null,
    bool guestForeground = false,
    bool guestEnvironmentCertified = false,
    object? restorationDiagnostic = null,
    object? acquisitionMetadata = null)
{
    Emit(new
    {
        probe_version = guestForeground ? "qq-uia-guest-foreground-identity-v1" : "qq-uia-current-identity-v1",
        mode = guestForeground ? "guest_foreground_current_chat_identity" : "current_chat_identity",
        succeeded,
        status,
        header_candidate_count = headerCandidateCount,
        active_header_digest = activeHeaderDigest,
        identity_candidate_count = identityCandidateCount,
        profile_id_hmac = profileIdHmac,
        identity_evidence_type = identityEvidenceType,
        profile_structure_digest = profileStructureDigest,
        process_id = processId,
        window_handle = windowHandle,
        is_maximized = isMaximized,
        is_foreground_before = isForegroundBefore,
        is_foreground_after = isForegroundAfter,
        right_region_structure_digest = rightRegionStructureDigest,
        restoration_diagnostic = restorationDiagnostic,
        acquisition = acquisitionMetadata,
        guest_environment = guestForeground ? new
        {
            certified = guestEnvironmentCertified,
            machine = "PMAI-QQVM",
            user = "qqbot",
            hypervisor = "virtualbox",
        } : null,
        recovery = new
        {
            attempted = recoveryAttempted,
            original_view_restored = originalViewRestored,
            foreground_changed = foregroundChanged,
        },
        privacy = new
        {
            exact_hwnd = true,
            emitted_control_names = false,
            emitted_chat_text = false,
            raw_profile_id_emitted = false,
            hmac_key_from_stdin_only = true,
            hmac_key_emitted = false,
            desktop_capture_supported = false,
            mouse_input_used = false,
            keyboard_input_used = false,
            clipboard_used = false,
            foreground_requested = guestForeground,
            composer_send_attempted = false,
            composer_or_send_accessed = false,
            write_actions_supported = false,
            transient_navigation_performed = transientNavigationPerformed,
        },
    });
}

static void EmitAvatarResult(
    bool succeeded,
    string status,
    int candidateRowCount = 0,
    int stableMatchCount = 0,
    string? avatarHmac = null,
    string? activeHeaderDigest = null,
    string? structureDigest = null,
    int headerCandidateCount = 0,
    int? processId = null,
    long? windowHandle = null,
    bool? isMaximized = null,
    bool? isForegroundBefore = null,
    bool? isForegroundAfter = null,
    bool globalForegroundChanged = false,
    string captureApi = "PrintWindow")
{
    Emit(new
    {
        probe_version = "qq-uia-current-avatar-v1",
        mode = "current_chat_avatar_capture",
        succeeded,
        status,
        process_id = processId,
        window_handle = windowHandle,
        is_maximized = isMaximized,
        is_foreground_before = isForegroundBefore,
        is_foreground_after = isForegroundAfter,
        header_candidate_count = headerCandidateCount,
        active_header_digest = activeHeaderDigest,
        candidate_row_count = candidateRowCount,
        stable_match_count = stableMatchCount,
        avatar_hmac = avatarHmac,
        structure_digest = structureDigest,
        capture_api = captureApi,
        privacy = new
        {
            exact_hwnd = true,
            desktop_capture = false,
            image_bytes_emitted = false,
            emitted_chat_text = false,
            emitted_control_names = false,
            navigation_performed = false,
            foreground_changed = globalForegroundChanged,
            write_actions_supported = false,
            mouse_input_used = false,
            keyboard_input_used = false,
            clipboard_used = false,
            foreground_requested = false,
            composer_or_send_accessed = false,
        },
    });
}

static void EmitAvatarDiscoveryResult(
    bool succeeded,
    string status,
    int candidateCount = 0,
    string? activeHeaderDigest = null,
    string? structureDigest = null,
    object[]? candidates = null,
    int? processId = null,
    long? windowHandle = null,
    bool? isMaximized = null,
    bool? isForeground = null)
{
    Emit(new
    {
        probe_version = "qq-uia-current-avatar-discovery-v1",
        mode = "current_chat_avatar_discovery",
        succeeded,
        status,
        process_id = processId,
        window_handle = windowHandle,
        is_maximized = isMaximized,
        is_foreground = isForeground,
        is_background = isForeground is false,
        active_header_digest = activeHeaderDigest,
        candidate_count = candidateCount,
        structure_digest = structureDigest,
        candidates = candidates ?? Array.Empty<object>(),
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
    });
}

static void EmitCurrentSessionResult(
    bool succeeded,
    string status,
    int headerCandidateCount = 0,
    string? activeHeaderDigest = null,
    string? structureDigest = null,
    int? processId = null,
    long? windowHandle = null,
    string? processStartedAt = null,
    bool? isMaximized = null,
    bool? isForeground = null)
{
    Emit(new
    {
        probe_version = "qq-uia-current-session-v1",
        mode = "current_session_inspection",
        succeeded,
        status,
        process_id = processId,
        window_handle = windowHandle,
        process_started_at = processStartedAt,
        is_maximized = isMaximized,
        is_foreground = isForeground,
        header_candidate_count = headerCandidateCount,
        active_header_digest = activeHeaderDigest,
        structure_digest = structureDigest,
        privacy = new
        {
            exact_hwnd = true,
            emitted_chat_text = false,
            emitted_control_names = false,
            emitted_contact_identifier = false,
            desktop_capture_supported = false,
            navigation_performed = false,
            foreground_changed = false,
            write_actions_supported = false,
            mouse_input_used = false,
            keyboard_input_used = false,
            clipboard_used = false,
            composer_or_send_accessed = false,
        },
    });
}

static bool TryReadIdentityInput(out string expectedHeaderDigest, out byte[] hmacKey)
{
    expectedHeaderDigest = Console.In.ReadLine()?.Trim().ToLowerInvariant() ?? string.Empty;
    var encodedKey = Console.In.ReadLine()?.Trim() ?? string.Empty;
    hmacKey = Array.Empty<byte>();
    if (!System.Text.RegularExpressions.Regex.IsMatch(expectedHeaderDigest, "^[0-9a-f]{64}$"))
    {
        return false;
    }
    try
    {
        var decodedKey = Convert.FromBase64String(encodedKey);
        if (decodedKey.Length < 32)
        {
            Array.Clear(decodedKey, 0, decodedKey.Length);
            return false;
        }
        hmacKey = decodedKey;
        return true;
    }
    catch (FormatException)
    {
        return false;
    }
}

static bool TryReadAvatarDiscoveryInput(out string expectedHeaderDigest)
{
    expectedHeaderDigest = Console.In.ReadLine()?.Trim().ToLowerInvariant() ?? string.Empty;
    return System.Text.RegularExpressions.Regex.IsMatch(expectedHeaderDigest, "^[0-9a-f]{64}$");
}

static string HmacSha256(string value, byte[] key)
{
    using var hmac = new HMACSHA256(key);
    return Convert.ToHexString(hmac.ComputeHash(Encoding.UTF8.GetBytes(value))).ToLowerInvariant();
}

static int CountExactSubstrings(string text, string target)
{
    var count = 0;
    var offset = 0;
    while (offset <= text.Length - target.Length)
    {
        var found = text.IndexOf(target, offset, StringComparison.Ordinal);
        if (found < 0) break;
        count += 1;
        offset = found + target.Length;
    }
    return count;
}

static string NormalizeLocalText(string value)
{
    var builder = new StringBuilder(value.Length);
    var previousWhitespace = false;
    foreach (var character in value.Trim())
    {
        if (char.IsWhiteSpace(character))
        {
            if (!previousWhitespace) builder.Append(' ');
            previousWhitespace = true;
        }
        else
        {
            builder.Append(character);
            previousWhitespace = false;
        }
    }
    return builder.ToString();
}

static (int Root, int Left, int Right) VisibleTextMatchCounts(
    AutomationElementCollection elements,
    string target,
    double windowLeft,
    double windowTop,
    double windowWidth,
    double windowHeight)
{
    var root = 0;
    var left = 0;
    var right = 0;
    for (var index = 0; index < elements.Count; index += 1)
    {
        AutomationElement.AutomationElementInformation current;
        try
        {
            current = elements[index].Current;
        }
        catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException)
        {
            continue;
        }
        var bounds = current.BoundingRectangle;
        var visible = !current.IsOffscreen && !bounds.IsEmpty &&
            bounds.Width > 0 && bounds.Height > 0;
        if (!visible) continue;
        var count = CountExactSubstrings(current.Name ?? string.Empty, target);
        if (count == 0) continue;
        root += count;
        if (bounds.X < windowLeft + windowWidth * 0.35 &&
            bounds.Y >= windowTop + windowHeight * 0.05 &&
            bounds.Y <= windowTop + windowHeight * 0.92)
        {
            left += count;
        }
        if (bounds.X >= windowLeft + windowWidth * 0.28 &&
            bounds.Y >= windowTop + windowHeight * 0.05 &&
            bounds.Y <= windowTop + windowHeight * 0.90)
        {
            right += count;
        }
    }
    return (root, left, right);
}

static bool HasVisibleLocalDescendantText(
    AutomationElement candidate,
    string target,
    out string structuralEvidenceDigest)
{
    structuralEvidenceDigest = string.Empty;
    try
    {
        var candidateBounds = candidate.Current.BoundingRectangle;
        var descendants = candidate.FindAll(TreeScope.Descendants, Condition.TrueCondition);
        var evidence = new List<string>();
        var matched = false;
        for (var index = 0; index < descendants.Count && index < 300; index += 1)
        {
            var descendant = descendants[index];
            AutomationElement.AutomationElementInformation current;
            try
            {
                current = descendant.Current;
            }
            catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException)
            {
                continue;
            }
            var bounds = current.BoundingRectangle;
            var visible = !current.IsOffscreen && !bounds.IsEmpty &&
                bounds.Width > 0 && bounds.Height > 0 &&
                bounds.Left >= candidateBounds.Left && bounds.Right <= candidateBounds.Right &&
                bounds.Top >= candidateBounds.Top && bounds.Bottom <= candidateBounds.Bottom;
            if (!visible) continue;
            var text = current.Name ?? string.Empty;
            var controlType = current.ControlType?.ProgrammaticName ?? string.Empty;
            evidence.Add($"{RuntimeKey(descendant) ?? "none"}|{controlType}|{current.ClassName}|{text.Length}");
            if (text.Contains(target, StringComparison.Ordinal)) matched = true;
        }
        structuralEvidenceDigest = Sha256(string.Join("\n", evidence.OrderBy(line => line, StringComparer.Ordinal)));
        return matched;
    }
    catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException)
    {
        return false;
    }
}

static object? CaptureRightRegionEvidence(
    AutomationElement root,
    double windowLeft,
    double windowTop,
    double windowWidth,
    double windowHeight,
    string? target = null)
{
    try
    {
        var elements = root.FindAll(TreeScope.Descendants, Condition.TrueCondition);
        var structural = new List<string>();
        var content = new List<string>();
        var headerCandidates = new List<string>();
        var targetMatchEvidence = new List<string>();
        for (var index = 0; index < elements.Count && index < 5_000; index += 1)
        {
            var element = elements[index];
            AutomationElement.AutomationElementInformation current;
            try
            {
                current = element.Current;
            }
            catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException)
            {
                continue;
            }
            var bounds = current.BoundingRectangle;
            var visibleRightRegion = !current.IsOffscreen && !bounds.IsEmpty &&
                bounds.Width > 0 && bounds.Height > 0 &&
                bounds.X >= windowLeft + windowWidth * 0.28 &&
                bounds.Y >= windowTop + windowHeight * 0.05 &&
                bounds.Y <= windowTop + windowHeight * 0.90;
            if (!visibleRightRegion) continue;
            var controlType = current.ControlType?.ProgrammaticName ?? string.Empty;
            var className = current.ClassName ?? string.Empty;
            var automationId = current.AutomationId ?? string.Empty;
            structural.Add($"{controlType}|{className}|{automationId}|{Math.Round((bounds.X - windowLeft) / windowWidth, 4)}|{Math.Round((bounds.Y - windowTop) / windowHeight, 4)}");
            var name = current.Name ?? string.Empty;
            if (!string.IsNullOrEmpty(name)) content.Add(Sha256(name));
            var normalizedX = Normalize(bounds.X - windowLeft, windowWidth);
            var normalizedY = Normalize(bounds.Y - windowTop, windowHeight);
            var headerRegion = bounds.Y >= windowTop + windowHeight * 0.05 &&
                bounds.Y <= windowTop + windowHeight * 0.14;
            var isHeaderControl = controlType is "ControlType.Text" or "ControlType.Document";
            var normalizedName = NormalizeLocalText(name);
            if (headerRegion && isHeaderControl && !string.IsNullOrEmpty(normalizedName))
            {
                headerCandidates.Add($"{controlType}|{className}|{automationId}|{normalizedX}|{normalizedY}|{normalizedName}");
            }
            if (!string.IsNullOrEmpty(target) && name.Contains(target, StringComparison.Ordinal))
            {
                targetMatchEvidence.Add($"{Sha256(name)}|{controlType}|{normalizedX}|{normalizedY}");
            }
        }
        var activeHeaderDigest = headerCandidates.Count == 1
            ? Sha256(string.Join("\n", headerCandidates.OrderBy(line => line, StringComparer.Ordinal)))
            : null;
        var targetMatchEvidenceDigest = targetMatchEvidence.Count > 0
            ? Sha256(string.Join("\n", targetMatchEvidence.OrderBy(line => line, StringComparer.Ordinal)))
            : null;
        return new
        {
            available = true,
            node_count = structural.Count,
            structure_digest = Sha256(string.Join("\n", structural.OrderBy(line => line, StringComparer.Ordinal))),
            content_digest = Sha256(string.Join("\n", content.OrderBy(line => line, StringComparer.Ordinal))),
            header_candidate_count = headerCandidates.Count,
            active_header_digest = activeHeaderDigest,
            target_match_evidence_digest = targetMatchEvidenceDigest,
        };
    }
    catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException)
    {
        return null;
    }
}

static CurrentChatEvidence CaptureCurrentChatEvidence(
    AutomationElement root,
    AutomationElementCollection elements,
    double windowLeft,
    double windowTop,
    double windowWidth,
    double windowHeight)
{
    var structural = new List<string>();
    var headerCandidates = new List<string>();
    var messageCandidates = new List<CurrentChatMessageCandidate>();
    for (var index = 0; index < elements.Count; index += 1)
    {
        var element = elements[index];
        AutomationElement.AutomationElementInformation current;
        try
        {
            current = element.Current;
        }
        catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException)
        {
            continue;
        }
        var bounds = current.BoundingRectangle;
        var visibleRightRegion = !current.IsOffscreen && !bounds.IsEmpty &&
            bounds.Width > 0 && bounds.Height > 0 &&
            bounds.X >= windowLeft + windowWidth * 0.28 &&
            bounds.Y >= windowTop + windowHeight * 0.05 &&
            bounds.Y <= windowTop + windowHeight * 0.90;
        if (!visibleRightRegion) continue;
        var controlType = current.ControlType?.ProgrammaticName ?? string.Empty;
        var className = current.ClassName ?? string.Empty;
        var automationId = current.AutomationId ?? string.Empty;
        var normalizedX = Normalize(bounds.X - windowLeft, windowWidth);
        var normalizedY = Normalize(bounds.Y - windowTop, windowHeight);
        var normalizedWidth = Normalize(bounds.Width, windowWidth);
        var normalizedHeight = Normalize(bounds.Height, windowHeight);
        structural.Add($"{controlType}|{className}|{automationId}|{normalizedX}|{normalizedY}|{normalizedWidth}|{normalizedHeight}");
        var name = current.Name ?? string.Empty;
        var headerRegion = bounds.Y >= windowTop + windowHeight * 0.05 &&
            bounds.Y <= windowTop + windowHeight * 0.14;
        var isHeaderControl = controlType is "ControlType.Text" or "ControlType.Document";
        var normalizedName = NormalizeLocalText(name);
        if (headerRegion && isHeaderControl && !string.IsNullOrEmpty(normalizedName))
        {
            headerCandidates.Add($"{controlType}|{className}|{automationId}|{normalizedX}|{normalizedY}|{normalizedName}");
        }
        var messageBand = bounds.Y > windowTop + windowHeight * 0.14 &&
            bounds.Y < windowTop + windowHeight * 0.75;
        var messageTextControl = controlType is "ControlType.Text" or "ControlType.Document";
        var normalizedCenter = normalizedX + normalizedWidth / 2d;
        var centralSystemOrTime = normalizedCenter >= 0.47 && normalizedCenter <= 0.53;
        if (!messageBand || !messageTextControl || centralSystemOrTime || string.IsNullOrEmpty(name)) continue;
        var direction = normalizedCenter < 0.42 ? "inbound" :
            normalizedCenter > 0.58 ? "outbound" : "unknown";
        var ancestors = AncestorControlTypes(element, root, 3);
        var rowStructure = string.Join("|", new[]
        {
            direction,
            controlType,
            className,
            string.Join(">", ancestors),
            Math.Round(normalizedWidth, 2).ToString("F2"),
            Math.Round(normalizedHeight, 2).ToString("F2"),
        });
        messageCandidates.Add(new CurrentChatMessageCandidate(
            name,
            direction,
            normalizedY,
            Sha256(rowStructure),
            Sha256($"{RuntimeKey(element) ?? "none"}|{rowStructure}|{Sha256(name)}")));
    }
    var repeatedCandidates = messageCandidates
        .GroupBy(candidate => candidate.RowStructureDigest, StringComparer.Ordinal)
        .Where(group => group.Count() >= 2)
        .SelectMany(group => group)
        .GroupBy(candidate => candidate.SourceEvidenceHash, StringComparer.Ordinal)
        .Select(group => group.First())
        .OrderBy(candidate => candidate.NormalizedY)
        .ThenBy(candidate => candidate.SourceEvidenceHash, StringComparer.Ordinal)
        .ToArray();
    var activeHeaderDigest = headerCandidates.Count == 1
        ? Sha256(string.Join("\n", headerCandidates.OrderBy(line => line, StringComparer.Ordinal)))
        : null;
    var messages = repeatedCandidates.Select(candidate => new CurrentChatMessage(
        candidate.Text,
        candidate.SourceEvidenceHash,
        Sha256($"{candidate.SourceEvidenceHash}|{candidate.Direction}|{candidate.NormalizedY}"),
        candidate.Direction,
        candidate.Direction == "unknown" ? 0.2 : 0.9,
        0.9,
        candidate.NormalizedY)).ToArray();
    return new CurrentChatEvidence(
        headerCandidates.Count,
        activeHeaderDigest,
        Sha256(string.Join("\n", structural.OrderBy(line => line, StringComparer.Ordinal))),
        messages,
        messageCandidates.Count,
        repeatedCandidates.Length);
}

// Content-free diagnostics for the exact restoration gate. These lines contain
// control metadata and geometry only; never Name, chat text or an identifier.
static string[] CaptureRightRegionStructureLines(AutomationElementCollection elements,
    double windowLeft, double windowTop, double windowWidth, double windowHeight)
{
    var lines = new List<string>();
    for (var index = 0; index < elements.Count; index += 1)
    {
        try
        {
            var current = elements[index].Current;
            var bounds = current.BoundingRectangle;
            if (current.IsOffscreen || bounds.IsEmpty || bounds.Width <= 0 || bounds.Height <= 0 ||
                bounds.X < windowLeft + windowWidth * 0.28 ||
                bounds.Y < windowTop + windowHeight * 0.05 ||
                bounds.Y > windowTop + windowHeight * 0.90) continue;
            lines.Add($"{current.ControlType?.ProgrammaticName ?? string.Empty}|{current.ClassName ?? string.Empty}|" +
                $"{current.AutomationId ?? string.Empty}|{Normalize(bounds.X - windowLeft, windowWidth)}|" +
                $"{Normalize(bounds.Y - windowTop, windowHeight)}|{Normalize(bounds.Width, windowWidth)}|" +
                $"{Normalize(bounds.Height, windowHeight)}");
        }
        catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException) { }
    }
    return lines.OrderBy(line => line, StringComparer.Ordinal).ToArray();
}

static long MonotonicNanoseconds() => checked((long)(Stopwatch.GetTimestamp() *
    (1_000_000_000d / Stopwatch.Frequency)));

static object CaptureSelectedRowFence(AutomationElement root, string? headerDigest,
    string structureDigest, double windowLeft, double windowTop, double windowWidth,
    double windowHeight)
{
    var selected = new List<(string RuntimeHash, string Source)>();
    var complete = true;
    try
    {
        var fresh = root.FindAll(TreeScope.Descendants, Condition.TrueCondition);
        if (fresh.Count > 5_000) complete = false;
        for (var index = 0; index < fresh.Count && index < 5_000; index += 1)
        {
            try
            {
                var element = fresh[index];
                var current = element.Current;
                var tokens = (current.ClassName ?? "").Split(' ', StringSplitOptions.RemoveEmptyEntries);
                if (!tokens.Contains("recent-contact-item", StringComparer.Ordinal)) continue;
                var bounds = current.BoundingRectangle;
                if (current.IsOffscreen || bounds.IsEmpty || bounds.Width <= 0 || bounds.Height <= 0 ||
                    bounds.X < windowLeft || bounds.Right > windowLeft + windowWidth * 0.35 ||
                    bounds.Y < windowTop + windowHeight * 0.05 || bounds.Bottom > windowTop + windowHeight)
                    continue;
                var source = tokens.Contains("recent-contact-item--selected", StringComparer.Ordinal)
                    ? "selected_class" : null;
                if (source is null && element.TryGetCurrentPattern(SelectionItemPattern.Pattern, out var raw) &&
                    raw is SelectionItemPattern pattern && pattern.Current.IsSelected)
                    source = "selection_pattern";
                if (source is null) continue;
                var runtime = RuntimeKey(element);
                if (runtime is null) { complete = false; continue; }
                selected.Add((runtime, source));
            }
            catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException)
            { complete = false; }
        }
    }
    catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException)
    { complete = false; }
    return new
    {
        captured_at = DateTimeOffset.UtcNow.ToString("O"),
        captured_monotonic_ns = MonotonicNanoseconds(),
        header_digest = headerDigest,
        selected_row_candidate_count = complete ? selected.Count : 0,
        selected_row_runtime_id_hash = complete && selected.Count == 1 ? selected[0].RuntimeHash : null,
        selected_row_selection_source = complete && selected.Count == 1 ? selected[0].Source : null,
        active_chat_structure_digest = structureDigest,
    };
}

static IntPtr[] VisibleProcessWindowHandles(int processId)
{
    var handles = new List<IntPtr>();
    NativeMethods.EnumWindows((window, _) =>
    {
        NativeMethods.GetWindowThreadProcessId(window, out var candidateProcessId);
        if (candidateProcessId == processId && NativeMethods.IsWindowVisible(window))
        {
            handles.Add(window);
        }
        return true;
    }, IntPtr.Zero);
    return handles.Distinct().OrderBy(handle => handle.ToInt64()).ToArray();
}

static IntPtr[] WaitForStableNewProcessWindows(int processId, IntPtr[] existingWindows, int attempts = 8)
{
    var previous = Array.Empty<IntPtr>();
    for (var attempt = 0; attempt < attempts; attempt += 1)
    {
        var current = VisibleProcessWindowHandles(processId).Except(existingWindows).ToArray();
        if (current.Length > 1)
        {
            return current;
        }
        if (current.Length == 1 && previous.SequenceEqual(current))
        {
            return current;
        }
        previous = current;
        if (attempt + 1 < attempts) Thread.Sleep(75);
    }
    return previous;
}

static bool WaitForWindowToClose(int processId, IntPtr windowHandle, int attempts = 8)
{
    for (var attempt = 0; attempt < attempts; attempt += 1)
    {
        if (!VisibleProcessWindowHandles(processId).Contains(windowHandle)) return true;
        if (attempt + 1 < attempts) Thread.Sleep(75);
    }
    return false;
}

static ActiveHeaderEvidence FindActiveHeader(
    AutomationElement root,
    double windowLeft,
    double windowTop,
    double windowWidth,
    double windowHeight)
{
    var candidates = new List<(AutomationElement Element, string Line, string StructureLine)>();
    var rootStructureLine = string.Empty;
    try
    {
        var rootCurrent = root.Current;
        rootStructureLine = string.Join("|", new[]
        {
            rootCurrent.ControlType?.ProgrammaticName ?? string.Empty,
            rootCurrent.ClassName ?? string.Empty,
            rootCurrent.AutomationId ?? string.Empty,
        });
        var elements = root.FindAll(TreeScope.Descendants, Condition.TrueCondition);
        for (var index = 0; index < elements.Count && index < 5_000; index += 1)
        {
            var element = elements[index];
            AutomationElement.AutomationElementInformation current;
            try
            {
                current = element.Current;
            }
            catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException)
            {
                continue;
            }
            var bounds = current.BoundingRectangle;
            var visible = !current.IsOffscreen && !bounds.IsEmpty && bounds.Width > 0 && bounds.Height > 0;
            var inRightHeader = visible &&
                bounds.X >= windowLeft + windowWidth * 0.28 &&
                bounds.Y >= windowTop + windowHeight * 0.05 &&
                bounds.Y <= windowTop + windowHeight * 0.14;
            var controlType = current.ControlType?.ProgrammaticName ?? string.Empty;
            var normalizedName = NormalizeLocalText(current.Name ?? string.Empty);
            if (!inRightHeader || controlType is not ("ControlType.Text" or "ControlType.Document") ||
                string.IsNullOrEmpty(normalizedName))
            {
                continue;
            }
            var structureLine = string.Join("|", new[]
            {
                controlType,
                current.ClassName ?? string.Empty,
                current.AutomationId ?? string.Empty,
                Normalize(bounds.X - windowLeft, windowWidth).ToString(System.Globalization.CultureInfo.InvariantCulture),
                Normalize(bounds.Y - windowTop, windowHeight).ToString(System.Globalization.CultureInfo.InvariantCulture),
                Normalize(bounds.Width, windowWidth).ToString(System.Globalization.CultureInfo.InvariantCulture),
                Normalize(bounds.Height, windowHeight).ToString(System.Globalization.CultureInfo.InvariantCulture),
            });
            candidates.Add((element, string.Join("|", new[]
            {
                controlType,
                current.ClassName ?? string.Empty,
                current.AutomationId ?? string.Empty,
                Normalize(bounds.X - windowLeft, windowWidth).ToString(System.Globalization.CultureInfo.InvariantCulture),
                Normalize(bounds.Y - windowTop, windowHeight).ToString(System.Globalization.CultureInfo.InvariantCulture),
                normalizedName,
            }), structureLine));
        }
    }
    catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException)
    {
        return new ActiveHeaderEvidence(0, null, null, null);
    }
    var digest = candidates.Count == 1
        ? Sha256(string.Join("\n", candidates.Select(candidate => candidate.Line).OrderBy(line => line, StringComparer.Ordinal)))
        : null;
    var structureDigest = candidates.Count == 1
        ? Sha256($"qq-current-session-v1|{rootStructureLine}|{candidates[0].StructureLine}")
        : null;
    return new ActiveHeaderEvidence(
        candidates.Count,
        digest,
        candidates.Count == 1 ? candidates[0].Element : null,
        structureDigest);
}

static ProfileIdentityEvidence CaptureExplicitProfileIdentityEvidence(AutomationElement profileRoot)
{
    var candidates = new List<ProfileIdentityCandidate>();
    var structure = new List<string>();
    const string inlineLabelPattern = "^(?:QQ号|账号|QQ ID|QQ)\\s*[:：]\\s*([0-9]{5,12})$";
    try
    {
        var all = profileRoot.FindAll(TreeScope.Descendants, Condition.TrueCondition);
        for (var index = 0; index < all.Count && index < 2_000; index += 1)
        {
            var label = all[index];
            AutomationElement.AutomationElementInformation labelCurrent;
            try
            {
                labelCurrent = label.Current;
            }
            catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException)
            {
                continue;
            }
            var labelControlType = labelCurrent.ControlType?.ProgrammaticName ?? string.Empty;
            if (labelControlType is not ("ControlType.Text" or "ControlType.Document"))
            {
                continue;
            }
            var localName = NormalizeLocalText(labelCurrent.Name ?? string.Empty);
            var inlineMatch = System.Text.RegularExpressions.Regex.Match(localName, inlineLabelPattern);
            var labelBounds = labelCurrent.BoundingRectangle;
            if (!labelCurrent.IsOffscreen && !labelBounds.IsEmpty && labelBounds.Width > 0 &&
                labelBounds.Height > 0 && inlineMatch.Success)
            {
                structure.Add(string.Join("|", new[]
                {
                    "explicit_inline_qq_id",
                    labelControlType,
                    Math.Round(labelBounds.X, 2).ToString(System.Globalization.CultureInfo.InvariantCulture),
                    Math.Round(labelBounds.Y, 2).ToString(System.Globalization.CultureInfo.InvariantCulture),
                }));
                candidates.Add(new ProfileIdentityCandidate(inlineMatch.Groups[1].Value, structure[^1]));
            }
            var labelText = localName
                .Replace("：", ":", StringComparison.Ordinal)
                .TrimEnd(':')
                .Trim();
            if (labelText is not ("QQ号" or "账号" or "QQ ID" or "QQ"))
            {
                continue;
            }
            if (labelCurrent.IsOffscreen || labelBounds.IsEmpty || labelBounds.Width <= 0 || labelBounds.Height <= 0)
            {
                continue;
            }
            AutomationElement? parent;
            try
            {
                parent = TreeWalker.ControlViewWalker.GetParent(label);
            }
            catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException)
            {
                continue;
            }
            if (parent is null) continue;
            AutomationElementCollection siblings;
            try
            {
                siblings = parent.FindAll(TreeScope.Children, Condition.TrueCondition);
            }
            catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException)
            {
                continue;
            }
            var values = new List<(AutomationElement Element, string Id, AutomationElement.AutomationElementInformation Current)>();
            for (var siblingIndex = 0; siblingIndex < siblings.Count; siblingIndex += 1)
            {
                var sibling = siblings[siblingIndex];
                if (sibling.Equals(label)) continue;
                AutomationElement.AutomationElementInformation siblingCurrent;
                try
                {
                    siblingCurrent = sibling.Current;
                }
                catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException)
                {
                    continue;
                }
                var siblingBounds = siblingCurrent.BoundingRectangle;
                var siblingType = siblingCurrent.ControlType?.ProgrammaticName ?? string.Empty;
                if (siblingType is not ("ControlType.Text" or "ControlType.Document"))
                {
                    continue;
                }
                var candidateId = NormalizeLocalText(siblingCurrent.Name ?? string.Empty);
                var sameVisualRow = Math.Abs((siblingBounds.Top + siblingBounds.Height / 2d) -
                    (labelBounds.Top + labelBounds.Height / 2d)) <= Math.Max(labelBounds.Height, siblingBounds.Height);
                if (!siblingCurrent.IsOffscreen && !siblingBounds.IsEmpty && siblingBounds.Width > 0 &&
                    // QQ's visible "QQ:" label and adjacent digit glyphs have
                    // a one-pixel overlap in UIA bounds. Keep the same-parent,
                    // same-row and unique-numeric-value checks; permit only
                    // the small bounding-box overlap, never another column.
                    siblingBounds.Left >= labelBounds.Right - 2d && sameVisualRow &&
                    System.Text.RegularExpressions.Regex.IsMatch(candidateId, "^[0-9]{5,12}$"))
                {
                    values.Add((sibling, candidateId, siblingCurrent));
                }
            }
            if (values.Count != 1) continue;
            var value = values[0];
            var valueBounds = value.Current.BoundingRectangle;
            var structuralLine = string.Join("|", new[]
            {
                "explicit_labeled_qq_id",
                labelCurrent.ControlType?.ProgrammaticName ?? string.Empty,
                value.Current.ControlType?.ProgrammaticName ?? string.Empty,
                Math.Round(labelBounds.X, 2).ToString(System.Globalization.CultureInfo.InvariantCulture),
                Math.Round(labelBounds.Y, 2).ToString(System.Globalization.CultureInfo.InvariantCulture),
                Math.Round(valueBounds.X, 2).ToString(System.Globalization.CultureInfo.InvariantCulture),
                Math.Round(valueBounds.Y, 2).ToString(System.Globalization.CultureInfo.InvariantCulture),
            });
            structure.Add(structuralLine);
            candidates.Add(new ProfileIdentityCandidate(value.Id, structuralLine));
        }
    }
    catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException)
    {
        return new ProfileIdentityEvidence(0, Sha256("profile-unavailable"), Array.Empty<ProfileIdentityCandidate>());
    }
    var distinctCandidates = candidates
        .GroupBy(candidate => candidate.RawId, StringComparer.Ordinal)
        .Select(group => group.OrderBy(candidate => candidate.StructureLine, StringComparer.Ordinal).First())
        .OrderBy(candidate => candidate.RawId, StringComparer.Ordinal)
        .ToArray();
    return new ProfileIdentityEvidence(
        distinctCandidates.Length,
        Sha256(string.Join("\n", structure.OrderBy(line => line, StringComparer.Ordinal))),
        distinctCandidates);
}

static AvatarDiscovery FindInboundAvatarCandidates(
    AutomationElementCollection elements,
    double windowLeft,
    double windowTop,
    double windowWidth,
    double windowHeight)
{
    const int maximumSnapshotNodes = 5_000;
    const int maximumRawCandidates = 256;
    if (elements.Count > maximumSnapshotNodes)
    {
        return new AvatarDiscovery(Array.Empty<AvatarCandidate>(), true, false);
    }
    var metrics = new List<AvatarNodeMetric>(elements.Count);
    for (var index = 0; index < elements.Count; index += 1)
    {
        AutomationElement.AutomationElementInformation current;
        try
        {
            // UIA is crossed exactly once per element; all matching below is local memory only.
            current = elements[index].Current;
        }
        catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException)
        {
            continue;
        }
        var bounds = current.BoundingRectangle;
        metrics.Add(new AvatarNodeMetric(
            current.ControlType?.ProgrammaticName ?? string.Empty,
            current.IsOffscreen,
            bounds.X,
            bounds.Y,
            bounds.Width,
            bounds.Height,
            bounds.IsEmpty));
    }
    var candidates = new List<AvatarCandidate>();
    foreach (var metric in metrics)
    {
        var normalizedX = Normalize(metric.X - windowLeft, windowWidth);
        var normalizedY = Normalize(metric.Y - windowTop, windowHeight);
        var likelyAvatar = !metric.IsOffscreen && !metric.IsEmpty && metric.Width is >= 12 and <= 96 &&
            metric.Height is >= 12 and <= 96 && metric.ControlType == "ControlType.Custom" &&
            normalizedX >= 0.28 && normalizedX <= 0.315 && normalizedY >= 0.14 && normalizedY <= 0.75;
        if (!likelyAvatar) continue;
        var hasRightBubble = metrics.Any(other =>
        {
            var verticallyAdjacent = Math.Abs((other.Y + other.Height / 2d) -
                (metric.Y + metric.Height / 2d)) <= metric.Height * 1.75;
            return !other.IsOffscreen && !other.IsEmpty && other.Width >= 24 &&
                other.Height >= 12 && other.Height <= metric.Height * 3d &&
                other.X >= metric.X + metric.Width - 2 &&
                other.X <= windowLeft + windowWidth * 0.88 && verticallyAdjacent &&
                other.ControlType is "ControlType.Custom" or "ControlType.Pane" or "ControlType.Document" or "ControlType.Text";
        });
        if (!hasRightBubble) continue;
        var geometryKey = string.Join("|", new[]
        {
            Math.Round(normalizedX, 4).ToString(System.Globalization.CultureInfo.InvariantCulture),
            Math.Round(normalizedY, 4).ToString(System.Globalization.CultureInfo.InvariantCulture),
            Math.Round(Normalize(metric.Width, windowWidth), 4).ToString(System.Globalization.CultureInfo.InvariantCulture),
            Math.Round(Normalize(metric.Height, windowHeight), 4).ToString(System.Globalization.CultureInfo.InvariantCulture),
        });
        candidates.Add(new AvatarCandidate(
            metric.X,
            metric.Y,
            metric.Width,
            metric.Height,
            normalizedY,
            geometryKey,
            Sha256($"inbound_avatar|{geometryKey}|adjacent_right_bubble")));
        if (candidates.Count > maximumRawCandidates)
        {
            return new AvatarDiscovery(Array.Empty<AvatarCandidate>(), false, true);
        }
    }
    return new AvatarDiscovery(candidates
        .GroupBy(candidate => candidate.GeometryKey, StringComparer.Ordinal)
        .Select(group => group.First())
        .GroupBy(candidate => Math.Round(candidate.NormalizedY, 3))
        .Select(group => group.OrderBy(candidate => candidate.GeometryKey, StringComparer.Ordinal).First())
        .OrderBy(candidate => candidate.NormalizedY)
        .ToArray(), false, false);
}

static PrintWindowFrame? CapturePrintWindowFrame(IntPtr windowHandle, int width, int height)
{
    const int bytesPerPixel = 4;
    if (width < 300 || height < 200 || (long)width * height * bytesPerPixel > 64L * 1024 * 1024) return null;
    var memoryDc = IntPtr.Zero;
    var bitmap = IntPtr.Zero;
    var previousBitmap = IntPtr.Zero;
    try
    {
        memoryDc = NativeMethods.CreateCompatibleDC(IntPtr.Zero);
        if (memoryDc == IntPtr.Zero) return null;
        var bitmapInfo = new BitmapInfo
        {
            Header = new BitmapInfoHeader
            {
                Size = (uint)Marshal.SizeOf<BitmapInfoHeader>(),
                Width = width,
                Height = -height,
                Planes = 1,
                BitCount = 32,
                Compression = 0,
                ImageSize = (uint)(width * height * bytesPerPixel),
            },
        };
        bitmap = NativeMethods.CreateDIBSection(memoryDc, ref bitmapInfo, 0, out var bits, IntPtr.Zero, 0);
        if (bitmap == IntPtr.Zero || bits == IntPtr.Zero) return null;
        previousBitmap = NativeMethods.SelectObject(memoryDc, bitmap);
        if (previousBitmap == IntPtr.Zero || !NativeMethods.PrintWindow(windowHandle, memoryDc, 0)) return null;
        var pixels = new byte[width * height * bytesPerPixel];
        Marshal.Copy(bits, pixels, 0, pixels.Length);
        return new PrintWindowFrame(width, height, pixels);
    }
    finally
    {
        if (previousBitmap != IntPtr.Zero) NativeMethods.SelectObject(memoryDc, previousBitmap);
        if (bitmap != IntPtr.Zero) NativeMethods.DeleteObject(bitmap);
        if (memoryDc != IntPtr.Zero) NativeMethods.DeleteDC(memoryDc);
    }
}

static PrintWindowFrame? CaptureExactHwndBitBltFrame(IntPtr windowHandle, int width, int height)
{
    const int bytesPerPixel = 4;
    const uint srcCopyCaptureBlt = 0x00CC0020u | 0x40000000u;
    if (windowHandle == IntPtr.Zero || width < 300 || height < 200 ||
        (long)width * height * bytesPerPixel > 64L * 1024 * 1024) return null;
    var targetWindowDc = IntPtr.Zero;
    var memoryDc = IntPtr.Zero;
    var bitmap = IntPtr.Zero;
    var previousBitmap = IntPtr.Zero;
    try
    {
        // Exact-HWND only: never call GetDC(IntPtr.Zero) or enumerate/capture the desktop.
        targetWindowDc = NativeMethods.GetWindowDC(windowHandle);
        if (targetWindowDc == IntPtr.Zero) return null;
        memoryDc = NativeMethods.CreateCompatibleDC(targetWindowDc);
        if (memoryDc == IntPtr.Zero) return null;
        var bitmapInfo = new BitmapInfo
        {
            Header = new BitmapInfoHeader
            {
                Size = (uint)Marshal.SizeOf<BitmapInfoHeader>(),
                Width = width,
                Height = -height,
                Planes = 1,
                BitCount = 32,
                Compression = 0,
                ImageSize = (uint)(width * height * bytesPerPixel),
            },
        };
        bitmap = NativeMethods.CreateDIBSection(memoryDc, ref bitmapInfo, 0, out var bits, IntPtr.Zero, 0);
        if (bitmap == IntPtr.Zero || bits == IntPtr.Zero) return null;
        previousBitmap = NativeMethods.SelectObject(memoryDc, bitmap);
        if (previousBitmap == IntPtr.Zero || !NativeMethods.BitBlt(
            memoryDc, 0, 0, width, height, targetWindowDc, 0, 0, srcCopyCaptureBlt)) return null;
        var pixels = new byte[width * height * bytesPerPixel];
        Marshal.Copy(bits, pixels, 0, pixels.Length);
        return new PrintWindowFrame(width, height, pixels);
    }
    finally
    {
        if (previousBitmap != IntPtr.Zero) NativeMethods.SelectObject(memoryDc, previousBitmap);
        if (bitmap != IntPtr.Zero) NativeMethods.DeleteObject(bitmap);
        if (memoryDc != IntPtr.Zero) NativeMethods.DeleteDC(memoryDc);
        if (targetWindowDc != IntPtr.Zero) NativeMethods.ReleaseDC(windowHandle, targetWindowDc);
    }
}

static string? AvatarFrameHmac(
    PrintWindowFrame frame,
    AvatarCandidate candidate,
    double windowLeft,
    double windowTop,
    byte[] key)
{
    var left = (int)Math.Floor(candidate.X - windowLeft);
    var top = (int)Math.Floor(candidate.Y - windowTop);
    var width = (int)Math.Ceiling(candidate.Width);
    var height = (int)Math.Ceiling(candidate.Height);
    if (left < 0 || top < 0 || width is < 8 or > 128 || height is < 8 or > 128 ||
        left + width > frame.Width || top + height > frame.Height) return null;
    var minLuminance = 255;
    var maxLuminance = 0;
    var colors = new HashSet<int>();
    for (var y = top; y < top + height; y += 1)
    {
        for (var x = left; x < left + width; x += 1)
        {
            var offset = (y * frame.Width + x) * 4;
            var luminance = (frame.Pixels[offset] * 11 + frame.Pixels[offset + 1] * 59 + frame.Pixels[offset + 2] * 30) / 100;
            minLuminance = Math.Min(minLuminance, luminance);
            maxLuminance = Math.Max(maxLuminance, luminance);
            if (colors.Count < 16) colors.Add((frame.Pixels[offset] << 16) | (frame.Pixels[offset + 1] << 8) | frame.Pixels[offset + 2]);
        }
    }
    if (maxLuminance - minLuminance < 12 || colors.Count < 8) return null;
    const int normalizedSize = 16;
    var domain = Encoding.ASCII.GetBytes("personal-messenger-ai/avatar-hmac-v1");
    var material = new byte[domain.Length + normalizedSize * normalizedSize * 3];
    domain.CopyTo(material, 0);
    var cursor = domain.Length;
    for (var outputY = 0; outputY < normalizedSize; outputY += 1)
    {
        var sourceY = top + Math.Min(height - 1, (outputY * height + normalizedSize / 2) / normalizedSize);
        for (var outputX = 0; outputX < normalizedSize; outputX += 1)
        {
            var sourceX = left + Math.Min(width - 1, (outputX * width + normalizedSize / 2) / normalizedSize);
            var offset = (sourceY * frame.Width + sourceX) * 4;
            material[cursor++] = frame.Pixels[offset];
            material[cursor++] = frame.Pixels[offset + 1];
            material[cursor++] = frame.Pixels[offset + 2];
        }
    }
    try
    {
        using var hmac = new HMACSHA256(key);
        return Convert.ToHexString(hmac.ComputeHash(material)).ToLowerInvariant();
    }
    finally
    {
        Array.Clear(material, 0, material.Length);
    }
}

static bool IsUsablePrintWindowFrame(PrintWindowFrame frame)
{
    var minLuminance = 255;
    var maxLuminance = 0;
    var colors = new HashSet<int>();
    var sampleStride = Math.Max(1, frame.Pixels.Length / 16_384 / 4) * 4;
    for (var offset = 0; offset + 3 < frame.Pixels.Length; offset += sampleStride)
    {
        var luminance = (frame.Pixels[offset] * 11 + frame.Pixels[offset + 1] * 59 + frame.Pixels[offset + 2] * 30) / 100;
        minLuminance = Math.Min(minLuminance, luminance);
        maxLuminance = Math.Max(maxLuminance, luminance);
        if (colors.Count < 16) colors.Add((frame.Pixels[offset] << 16) | (frame.Pixels[offset + 1] << 8) | frame.Pixels[offset + 2]);
    }
    return maxLuminance - minLuminance >= 12 && colors.Count >= 8;
}

static bool IsCertifiedGuestIdentityEnvironment()
{
    if (!string.Equals(Environment.MachineName, "PMAI-QQVM", StringComparison.OrdinalIgnoreCase) ||
        !string.Equals(Environment.UserName, "qqbot", StringComparison.OrdinalIgnoreCase)) return false;
    try
    {
        var path = @"HKEY_LOCAL_MACHINE\HARDWARE\DESCRIPTION\System\BIOS";
        var manufacturer = Convert.ToString(Registry.GetValue(path, "SystemManufacturer", "")) ?? "";
        var product = Convert.ToString(Registry.GetValue(path, "SystemProductName", "")) ?? "";
        return (manufacturer + " " + product).Contains("VirtualBox", StringComparison.OrdinalIgnoreCase);
    }
    catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or System.Security.SecurityException) { return false; }
}

static ActiveHeaderEvidence FindGuestActiveHeader(AutomationElement root, double windowLeft,
    double windowTop, double windowWidth, double windowHeight)
{
    var candidates = new List<(AutomationElement Element, string Line, string StructureLine)>();
    try
    {
        var elements = root.FindAll(TreeScope.Descendants, Condition.TrueCondition);
        for (var index = 0; index < elements.Count && index < 5_000; index += 1)
        {
            var element = elements[index];
            AutomationElement.AutomationElementInformation current;
            try { current = element.Current; }
            catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException) { continue; }
            var bounds = current.BoundingRectangle;
            var classTokens = (current.ClassName ?? "").Split(' ', StringSplitOptions.RemoveEmptyEntries);
            var nx = Normalize(bounds.X - windowLeft, windowWidth);
            var ny = Normalize(bounds.Y - windowTop, windowHeight);
            var nw = Normalize(bounds.Width, windowWidth);
            var nh = Normalize(bounds.Height, windowHeight);
            var valid = current.ControlType == ControlType.Button && current.IsEnabled && !current.IsOffscreen &&
                !bounds.IsEmpty && classTokens.Contains("chat-header__contact-name", StringComparer.Ordinal) &&
                Supports(element, InvokePattern.Pattern) && nx >= 0.28 && ny >= 0.05 && ny <= 0.14 &&
                nw > 0 && nh > 0 && !string.IsNullOrWhiteSpace(current.Name);
            if (!valid) continue;
            var structure = $"ControlType.Button|chat-header__contact-name|{nx}|{ny}|{nw}|{nh}";
            candidates.Add((element, $"{structure}|{NormalizeLocalText(current.Name)}", structure));
        }
    }
    catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException)
    { return new ActiveHeaderEvidence(0, null, null, null); }
    return candidates.Count == 1
        ? new ActiveHeaderEvidence(1, Sha256(candidates[0].Line), candidates[0].Element,
            Sha256($"qq-guest-header-v1|{candidates[0].StructureLine}"))
        : new ActiveHeaderEvidence(candidates.Count, null, null, null);
}

static void EmitGuestHeaderResult(bool succeeded, string status, int count = 0,
    string? headerDigest = null, string? rightDigest = null, int? processId = null,
    long? windowHandle = null, bool guestCertified = false)
{
    Emit(new { probe_version = "qq-uia-guest-header-v1", mode = "guest_foreground_header_inspect",
        succeeded, status, header_candidate_count = count, active_header_digest = headerDigest,
        right_region_structure_digest = rightDigest, process_id = processId, window_handle = windowHandle,
        guest_environment = new { certified = guestCertified, machine = "PMAI-QQVM", user = "qqbot", hypervisor = "virtualbox" },
        privacy = new { exact_hwnd = true, emitted_control_names = false, emitted_chat_text = false,
            mouse_input_used = false, keyboard_input_used = false, clipboard_used = false,
            transient_navigation_performed = false, composer_or_send_accessed = false } });
}

static bool TryCloseTransientWindow(IntPtr windowHandle, int processId)
{
    NativeMethods.GetWindowThreadProcessId(windowHandle, out var ownerPid);
    if (ownerPid != processId || !NativeMethods.IsWindowVisible(windowHandle)) return false;
    try
    {
        var element = AutomationElement.FromHandle(windowHandle);
        if (element.TryGetCurrentPattern(WindowPattern.Pattern, out var raw) && raw is WindowPattern window)
        {
            window.Close();
            return WaitForWindowToClose(processId, windowHandle);
        }
    }
    catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException) { }
    return false;
}

var maxNodes = ReadMaxNodes(args);
var includeTopology = HasFlag(args, "--include-topology");
var warmupUiaEvents = HasFlag(args, "--warmup-uia-events");
var warmupMilliseconds = warmupUiaEvents ? ReadWarmupMilliseconds(args) : 0;
var foregroundWaitMilliseconds = ReadForegroundWaitMilliseconds(args);
var targetPidOption = ReadLongOption(args, "--target-qq-pid");
var targetHwndOption = ReadLongOption(args, "--target-qq-hwnd");
var selectionMode = HasFlag(args, "--match-from-stdin");
var selectionAuthorized = HasFlag(args, "--select-authorized");
var captureMode = HasFlag(args, "--capture-current-chat");
var captureAuthorized = HasFlag(args, "--capture-authorized");
var identityMode = HasFlag(args, "--capture-current-identity");
var identityAuthorized = HasFlag(args, "--identity-authorized");
var guestIdentityMode = HasFlag(args, "--capture-current-identity-guest-foreground");
var guestIdentityAuthorized = HasFlag(args, "--guest-identity-authorized");
var guestHeaderMode = HasFlag(args, "--inspect-guest-current-header");
var guestHeaderAuthorized = HasFlag(args, "--guest-header-authorized");
identityMode = identityMode || guestIdentityMode;
identityAuthorized = identityAuthorized || guestIdentityAuthorized;
var avatarMode = HasFlag(args, "--capture-current-avatar");
var avatarAuthorized = HasFlag(args, "--avatar-authorized");
var avatarDiscoveryMode = HasFlag(args, "--discover-current-avatar");
var avatarDiscoveryAuthorized = HasFlag(args, "--avatar-discovery-authorized");
var sessionInspectionMode = HasFlag(args, "--inspect-current-session");
var sessionInspectionAuthorized = HasFlag(args, "--session-inspection-authorized");
var selectionTarget = string.Empty;
var identityExpectedHeaderDigest = string.Empty;
var identityHmacKey = Array.Empty<byte>();
var avatarExpectedHeaderDigest = string.Empty;
var avatarHmacKey = Array.Empty<byte>();
var activeModeCount = new[]
{
    selectionMode || selectionAuthorized,
    captureMode || captureAuthorized,
    identityMode || identityAuthorized,
    avatarMode || avatarAuthorized,
    avatarDiscoveryMode || avatarDiscoveryAuthorized,
    sessionInspectionMode || sessionInspectionAuthorized,
    guestHeaderMode || guestHeaderAuthorized,
}.Count(enabled => enabled);
if (activeModeCount > 1)
{
    if (sessionInspectionMode || sessionInspectionAuthorized)
    {
        EmitCurrentSessionResult(false, "MODE_CONFLICT");
    }
    else if (avatarDiscoveryMode || avatarDiscoveryAuthorized)
    {
        EmitAvatarDiscoveryResult(false, "MODE_CONFLICT");
    }
    else if (avatarMode || avatarAuthorized)
    {
        EmitAvatarResult(false, "MODE_CONFLICT");
    }
    else if (identityMode || identityAuthorized)
    {
        EmitCurrentIdentityResult(false, "MODE_CONFLICT");
    }
    else if (captureMode || captureAuthorized)
    {
        EmitCurrentChatResult(false, "MODE_CONFLICT");
    }
    else
    {
        EmitSelectionResult(false, "MODE_CONFLICT", 0);
    }
    return 2;
}
if ((foregroundWaitMilliseconds > 0 || targetPidOption.Supplied || targetHwndOption.Supplied) &&
    activeModeCount != 0 && !guestIdentityMode && !guestHeaderMode)
{
    Emit(new
    {
        probe_version = "qq-uia-readonly-v1",
        succeeded = false,
        error_code = "STRUCTURAL_WAIT_MODE_CONFLICT",
        read_only = true,
    });
    return 2;
}
if (targetPidOption.Supplied != targetHwndOption.Supplied ||
    (targetPidOption.Supplied &&
     (targetPidOption.Value <= 0 || targetPidOption.Value > int.MaxValue || targetHwndOption.Value <= 0)))
{
    Emit(new
    {
        probe_version = "qq-uia-readonly-v1",
        succeeded = false,
        error_code = "TARGET_QQ_WINDOW_INVALID",
        read_only = true,
    });
    return 2;
}
if (sessionInspectionAuthorized && !sessionInspectionMode)
{
    EmitCurrentSessionResult(false, "SESSION_INSPECTION_MODE_REQUIRED");
    return 2;
}
if (sessionInspectionMode && !sessionInspectionAuthorized)
{
    EmitCurrentSessionResult(false, "SESSION_INSPECTION_AUTHORIZATION_REQUIRED");
    return 2;
}
if (identityAuthorized && !identityMode)
{
    EmitCurrentIdentityResult(false, "IDENTITY_MODE_REQUIRED");
    return 2;
}
if (identityMode && !identityAuthorized)
{
    EmitCurrentIdentityResult(false, "IDENTITY_AUTHORIZATION_REQUIRED", guestForeground: guestIdentityMode);
    return 2;
}
if (avatarAuthorized && !avatarMode)
{
    EmitAvatarResult(false, "AVATAR_MODE_REQUIRED");
    return 2;
}
if (avatarMode && !avatarAuthorized)
{
    EmitAvatarResult(false, "AVATAR_AUTHORIZATION_REQUIRED");
    return 2;
}
if (avatarDiscoveryAuthorized && !avatarDiscoveryMode)
{
    EmitAvatarDiscoveryResult(false, "AVATAR_DISCOVERY_MODE_REQUIRED");
    return 2;
}
if (avatarDiscoveryMode && !avatarDiscoveryAuthorized)
{
    EmitAvatarDiscoveryResult(false, "AVATAR_DISCOVERY_AUTHORIZATION_REQUIRED");
    return 2;
}
if (captureAuthorized && !captureMode)
{
    EmitCurrentChatResult(false, "CAPTURE_MODE_REQUIRED");
    return 2;
}
if (captureMode && !captureAuthorized)
{
    EmitCurrentChatResult(false, "CAPTURE_AUTHORIZATION_REQUIRED");
    return 2;
}
if (selectionAuthorized && !selectionMode)
{
    EmitSelectionResult(false, "SELECTION_MODE_REQUIRED", 0);
    return 2;
}
if (selectionMode)
{
    selectionTarget = Console.In.ReadToEnd().Trim();
    if (string.IsNullOrEmpty(selectionTarget) || selectionTarget.Length > 256)
    {
        EmitSelectionResult(false, "TARGET_STDIN_INVALID", 0);
        return 2;
    }
}
var windows = Process.GetProcessesByName("QQ")
    .Where(process => process.MainWindowHandle != IntPtr.Zero)
    .ToArray();

Process process;
IntPtr probeWindowHandle;
if (targetPidOption.Supplied)
{
    Process candidate;
    try
    {
        candidate = Process.GetProcessById(checked((int)targetPidOption.Value));
    }
    catch (Exception exception) when (exception is ArgumentException or OverflowException)
    {
        Emit(new { probe_version = "qq-uia-readonly-v1", succeeded = false,
            error_code = "TARGET_QQ_WINDOW_UNAVAILABLE", read_only = true });
        return 2;
    }
    probeWindowHandle = new IntPtr(targetHwndOption.Value);
    NativeMethods.GetWindowThreadProcessId(probeWindowHandle, out var ownerPid);
    if (!string.Equals(candidate.ProcessName, "QQ", StringComparison.OrdinalIgnoreCase) ||
        ownerPid != candidate.Id || !NativeMethods.IsWindowVisible(probeWindowHandle))
    {
        Emit(new { probe_version = "qq-uia-readonly-v1", succeeded = false,
            error_code = "TARGET_QQ_WINDOW_MISMATCH", read_only = true });
        return 2;
    }
    process = candidate;
}
if (guestIdentityMode != guestIdentityAuthorized ||
    (guestIdentityMode && HasFlag(args, "--capture-current-identity")))
{
    EmitCurrentIdentityResult(false, "GUEST_IDENTITY_MODE_CONFLICT", guestForeground: true);
    return 2;
}
if (guestIdentityMode && (!targetPidOption.Supplied || !targetHwndOption.Supplied))
{
    EmitCurrentIdentityResult(false, "GUEST_EXACT_TARGET_REQUIRED", guestForeground: true);
    return 2;
}
if (guestHeaderMode != guestHeaderAuthorized ||
    (guestHeaderMode && (!targetPidOption.Supplied || !targetHwndOption.Supplied)))
{
    EmitGuestHeaderResult(false, "GUEST_HEADER_AUTHORIZATION_OR_TARGET_REQUIRED");
    return 2;
}

else if (windows.Length != 1)
{
    if (sessionInspectionMode)
    {
        EmitCurrentSessionResult(false, "QQ_WINDOW_NOT_UNIQUE");
        return 2;
    }
    if (avatarDiscoveryMode)
    {
        EmitAvatarDiscoveryResult(false, "QQ_WINDOW_NOT_UNIQUE");
        return 2;
    }
    if (avatarMode)
    {
        EmitAvatarResult(false, "QQ_WINDOW_NOT_UNIQUE");
        return 2;
    }
    if (identityMode)
    {
        EmitCurrentIdentityResult(false, "QQ_WINDOW_NOT_UNIQUE");
        return 2;
    }
    if (captureMode)
    {
        EmitCurrentChatResult(false, "QQ_WINDOW_NOT_UNIQUE");
        return 2;
    }
    if (selectionMode)
    {
        EmitSelectionResult(false, "QQ_WINDOW_NOT_UNIQUE", 0);
        return 2;
    }
    Emit(new
    {
        probe_version = "qq-uia-readonly-v1",
        succeeded = false,
        error_code = "QQ_WINDOW_NOT_UNIQUE",
        window_count = windows.Length,
        read_only = true,
    });
    return 2;
}
else
{
    process = windows[0];
    probeWindowHandle = process.MainWindowHandle;
}
var foregroundWait = Stopwatch.StartNew();
long? foregroundStableSince = null;
while (foregroundWaitMilliseconds > 0 &&
       foregroundWait.ElapsedMilliseconds < foregroundWaitMilliseconds)
{
    process.Refresh();
    var ready = probeWindowHandle != IntPtr.Zero &&
        ForegroundRoot() == probeWindowHandle &&
        NativeMethods.IsZoomed(probeWindowHandle) &&
        !NativeMethods.IsIconic(probeWindowHandle);
    if (ready)
    {
        foregroundStableSince ??= foregroundWait.ElapsedMilliseconds;
        if (foregroundWait.ElapsedMilliseconds - foregroundStableSince >= 1_000)
        {
            break;
        }
    }
    else
    {
        foregroundStableSince = null;
    }
    Thread.Sleep(100);
}
process.Refresh();
var foregroundWaitSatisfied = probeWindowHandle != IntPtr.Zero &&
    ForegroundRoot() == probeWindowHandle &&
    NativeMethods.IsZoomed(probeWindowHandle) &&
    !NativeMethods.IsIconic(probeWindowHandle) &&
    foregroundStableSince is not null &&
    foregroundWait.ElapsedMilliseconds - foregroundStableSince >= 1_000;
if (foregroundWaitMilliseconds > 0 && !foregroundWaitSatisfied)
{
    Emit(new
    {
        probe_version = "qq-uia-readonly-v1",
        succeeded = false,
        error_code = "QQ_FOREGROUND_MAXIMIZED_TIMEOUT",
        read_only = true,
        foreground_wait_used = true,
        foreground_wait_ms = foregroundWaitMilliseconds,
    });
    return 2;
}
if (!NativeMethods.GetWindowRect(probeWindowHandle, out var nativeRectangle))
{
    if (sessionInspectionMode)
    {
        EmitCurrentSessionResult(false, "QQ_WINDOW_RECT_UNAVAILABLE");
        return 2;
    }
    if (avatarDiscoveryMode)
    {
        EmitAvatarDiscoveryResult(false, "QQ_WINDOW_RECT_UNAVAILABLE");
        return 2;
    }
    if (avatarMode)
    {
        EmitAvatarResult(false, "QQ_WINDOW_RECT_UNAVAILABLE");
        return 2;
    }
    if (identityMode)
    {
        EmitCurrentIdentityResult(false, "QQ_WINDOW_RECT_UNAVAILABLE");
        return 2;
    }
    if (captureMode)
    {
        EmitCurrentChatResult(false, "QQ_WINDOW_RECT_UNAVAILABLE");
        return 2;
    }
    if (selectionMode)
    {
        EmitSelectionResult(false, "QQ_WINDOW_RECT_UNAVAILABLE", 0);
        return 2;
    }
    Emit(new
    {
        probe_version = "qq-uia-readonly-v1",
        succeeded = false,
        error_code = "QQ_WINDOW_RECT_UNAVAILABLE",
        read_only = true,
    });
    return 2;
}
var windowLeft = (double)nativeRectangle.Left;
var windowTop = (double)nativeRectangle.Top;
var windowWidth = (double)(nativeRectangle.Right - nativeRectangle.Left);
var windowHeight = (double)(nativeRectangle.Bottom - nativeRectangle.Top);
var windowMinimized = NativeMethods.IsIconic(probeWindowHandle);
var windowMaximized = NativeMethods.IsZoomed(probeWindowHandle);
var windowForeground = ForegroundRoot() == probeWindowHandle;
var geometryUsable = !windowMinimized && windowWidth >= 300 && windowHeight >= 200;
var presentation = windowMinimized ? "minimized" : windowMaximized ? "maximized" : "normal";
var executablePath = string.Empty;
try
{
    executablePath = process.MainModule?.FileName ?? string.Empty;
}
catch (Exception exception) when (exception is InvalidOperationException or System.ComponentModel.Win32Exception)
{
    executablePath = string.Empty;
}
var executableSignature = string.IsNullOrWhiteSpace(executablePath)
    ? new string('0', 64)
    : FileSha256(executablePath);
var processSignature = Sha256($"{process.ProcessName}|{executableSignature}");
var dpiScale = Math.Round(Math.Max(96u, NativeMethods.GetDpiForWindow(probeWindowHandle)) / 96d, 4);
var monitorId = Sha256($"{windowLeft},{windowTop},{windowWidth},{windowHeight}");
var monitorTopologyDigest = Sha256(string.Join(",", new[]
{
    NativeMethods.GetSystemMetrics(76).ToString(),
    NativeMethods.GetSystemMetrics(77).ToString(),
    NativeMethods.GetSystemMetrics(78).ToString(),
    NativeMethods.GetSystemMetrics(79).ToString(),
}));
var otherVisibleProcessWindows = CountOtherVisibleProcessWindows(process.Id, probeWindowHandle);
AutomationElement root;
try
{
    root = AutomationElement.FromHandle(probeWindowHandle);
}
catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException)
{
    if (sessionInspectionMode)
    {
        EmitCurrentSessionResult(false, "QQ_UIA_ROOT_UNAVAILABLE");
        return 2;
    }
    if (avatarDiscoveryMode)
    {
        EmitAvatarDiscoveryResult(false, "QQ_UIA_ROOT_UNAVAILABLE");
        return 2;
    }
    if (avatarMode)
    {
        EmitAvatarResult(false, "QQ_UIA_ROOT_UNAVAILABLE");
        return 2;
    }
    if (identityMode)
    {
        EmitCurrentIdentityResult(false, "QQ_UIA_ROOT_UNAVAILABLE");
        return 2;
    }
    if (captureMode)
    {
        EmitCurrentChatResult(false, "QQ_UIA_ROOT_UNAVAILABLE");
        return 2;
    }
    if (selectionMode)
    {
        EmitSelectionResult(false, "QQ_UIA_ROOT_UNAVAILABLE", 0);
        return 2;
    }
    Emit(new
    {
        probe_version = "qq-uia-readonly-v1",
        succeeded = false,
        error_code = "QQ_UIA_ROOT_UNAVAILABLE",
        read_only = true,
    });
    return 2;
}

AutomationElementCollection elements;
StructureChangedEventHandler? warmupHandler = null;
try
{
    if (warmupUiaEvents)
    {
        // The handler is scoped to this already identified QQ HWND.  Its empty
        // callback neither reads nor records event data; registration only
        // advertises an active UIA client before the structural enumeration.
        warmupHandler = (_, _) => { };
        Automation.AddStructureChangedEventHandler(root, TreeScope.Subtree, warmupHandler);
        Thread.Sleep(warmupMilliseconds);
    }
    elements = root.FindAll(TreeScope.Descendants, Condition.TrueCondition);
}
catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException)
{
    if (sessionInspectionMode)
    {
        EmitCurrentSessionResult(false, "QQ_UIA_TREE_UNAVAILABLE");
        return 2;
    }
    if (avatarDiscoveryMode)
    {
        EmitAvatarDiscoveryResult(false, "QQ_UIA_TREE_UNAVAILABLE");
        return 2;
    }
    if (avatarMode)
    {
        EmitAvatarResult(false, "QQ_UIA_TREE_UNAVAILABLE");
        return 2;
    }
    if (identityMode)
    {
        EmitCurrentIdentityResult(false, "QQ_UIA_TREE_UNAVAILABLE");
        return 2;
    }
    if (captureMode)
    {
        EmitCurrentChatResult(false, "QQ_UIA_TREE_UNAVAILABLE");
        return 2;
    }
    if (selectionMode)
    {
        EmitSelectionResult(false, "QQ_UIA_TREE_UNAVAILABLE", 0);
        return 2;
    }
    Emit(new
    {
        probe_version = "qq-uia-readonly-v1",
        succeeded = false,
        error_code = "QQ_UIA_TREE_UNAVAILABLE",
        read_only = true,
    });
    return 2;
}
finally
{
    if (warmupHandler is not null)
    {
        try
        {
            Automation.RemoveStructureChangedEventHandler(root, warmupHandler);
        }
        catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException)
        {
            // The target may disappear during a read-only probe; there is no
            // broader desktop registration to remove.
        }
    }
}

if (guestHeaderMode)
{
    var guestCertified = IsCertifiedGuestIdentityEnvironment();
    if (!guestCertified || !windowForeground || windowMinimized || !windowMaximized || !geometryUsable)
    {
        EmitGuestHeaderResult(false, "GUEST_HEADER_ENVIRONMENT_NOT_CERTIFIED",
            processId: process.Id, windowHandle: probeWindowHandle.ToInt64(), guestCertified: guestCertified);
        return 2;
    }
    var header = FindGuestActiveHeader(root, windowLeft, windowTop, windowWidth, windowHeight);
    var rightDigest = Sha256(string.Join("\n", CaptureRightRegionStructureLines(elements,
        windowLeft, windowTop, windowWidth, windowHeight)));
    var ready = header.CandidateCount == 1 && header.Element is not null && !string.IsNullOrWhiteSpace(header.Digest);
    EmitGuestHeaderResult(ready, ready ? "HEADER_CAPTURED" : "ACTIVE_HEADER_AMBIGUOUS",
        header.CandidateCount, header.Digest, rightDigest,
        process.Id, probeWindowHandle.ToInt64(), guestCertified);
    return ready ? 0 : 2;
}

var controlTypes = new Dictionary<string, int>(StringComparer.Ordinal);
var classNames = new Dictionary<string, int>(StringComparer.Ordinal);
var automationIds = new Dictionary<string, int>(StringComparer.Ordinal);
var invokeCount = 0;
var selectionItemCount = 0;
var textCount = 0;
var valueCount = 0;
var scrollCount = 0;
var namedTextCount = 0;
var selectableConversationCandidates = 0;
var composerCandidates = 0;
var sendCandidates = 0;
var sendKeywordCandidates = 0;
var leftPaneInvokeCandidates = 0;
var bottomRightInvokeCandidates = 0;
var messageRegionNamedTextNodes = 0;
var chatSurfaceSignals = new HashSet<string>(StringComparer.Ordinal);
var sawLoginSignal = false;
var sawCaptchaSignal = false;
var sawUpdateSignal = false;
var sawAccountWarningSignal = false;
var composerDetails = new List<object>();
var sendTargetDetails = new List<object>();
var topologyDetails = new List<object>();
var topologyLines = new List<string>();
var conversationRows = new List<ConversationRowCandidate>();
var examined = Math.Min(elements.Count, maxNodes);
var knownRuntimeKeys = new HashSet<string>(StringComparer.Ordinal);
if (includeTopology && !guestIdentityMode)
{
    for (var index = 0; index < examined; index += 1)
    {
        var key = RuntimeKey(elements[index]);
        if (key is not null) knownRuntimeKeys.Add(key);
    }
}

// Guest identity performs its own exact header, selected-row, profile and
// restored-structure checks below. Generic readiness/topology statistics are
// consumed only by the other mutually exclusive modes; avoid their repeated
// pattern/property COM reads during this narrowly scoped acquisition.
for (var index = 0; index < examined && !guestIdentityMode; index += 1)
{
    var element = elements[index];
    AutomationElement.AutomationElementInformation current;
    try
    {
        current = element.Current;
    }
    catch (ElementNotAvailableException)
    {
        continue;
    }
    catch (InvalidOperationException)
    {
        continue;
    }

    var controlType = current.ControlType?.ProgrammaticName ?? string.Empty;
    var className = current.ClassName ?? string.Empty;
    var automationId = current.AutomationId ?? string.Empty;
    var name = current.Name ?? string.Empty;
    var rectangle = current.BoundingRectangle;
    var visibleRectangle = geometryUsable && !current.IsOffscreen && !rectangle.IsEmpty &&
        double.IsFinite(rectangle.X) && double.IsFinite(rectangle.Y) &&
        double.IsFinite(rectangle.Width) && double.IsFinite(rectangle.Height) &&
        rectangle.Width > 0 && rectangle.Height > 0;
    Increment(controlTypes, controlType);
    Increment(classNames, className);
    Increment(automationIds, automationId);

    var hasInvoke = Supports(element, InvokePattern.Pattern);
    var hasSelectionItem = Supports(element, SelectionItemPattern.Pattern);
    var hasText = Supports(element, TextPattern.Pattern);
    var hasValue = Supports(element, ValuePattern.Pattern);
    var hasScroll = Supports(element, ScrollPattern.Pattern);
    var isSearch = string.Equals(name.Trim(), "搜索", StringComparison.OrdinalIgnoreCase) ||
        string.Equals(name.Trim(), "search", StringComparison.OrdinalIgnoreCase);
    var hasSendKeyword = name.Contains("发送", StringComparison.OrdinalIgnoreCase) ||
        name.Contains("send", StringComparison.OrdinalIgnoreCase);
    var normalizedName = name.Trim();
    if (normalizedName is "消息" or "联系人" or "动态") chatSurfaceSignals.Add(normalizedName);
    if (normalizedName is "登录" or "登录QQ" or "扫码登录") sawLoginSignal = true;
    if (normalizedName.Contains("验证码", StringComparison.OrdinalIgnoreCase) ||
        normalizedName.Contains("安全验证", StringComparison.OrdinalIgnoreCase)) sawCaptchaSignal = true;
    if (normalizedName.Contains("版本更新", StringComparison.OrdinalIgnoreCase) ||
        normalizedName.Contains("立即更新", StringComparison.OrdinalIgnoreCase)) sawUpdateSignal = true;
    if (normalizedName.Contains("账号异常", StringComparison.OrdinalIgnoreCase) ||
        normalizedName.Contains("账号风险", StringComparison.OrdinalIgnoreCase)) sawAccountWarningSignal = true;
    var nameKind = string.IsNullOrWhiteSpace(name)
        ? "empty"
        : isSearch
            ? "search"
            : hasSendKeyword
                ? "send"
                : controlType == "ControlType.Text" || controlType == "ControlType.Document"
                    ? "named_text"
                    : "nonempty";
    var isBoundedDocument = controlType == "ControlType.Document" && visibleRectangle &&
        rectangle.Width < windowWidth * 0.75 && rectangle.Height < windowHeight * 0.55;
    var isComposerCandidate = current.IsEnabled && !current.IsPassword && hasValue &&
        (controlType == "ControlType.Edit" || isBoundedDocument) && !isSearch && visibleRectangle &&
        rectangle.X >= windowLeft + windowWidth * 0.25 &&
        rectangle.Y >= windowTop + windowHeight * 0.55;
    var isConversationItemCandidate = current.IsEnabled && hasInvoke && visibleRectangle &&
        rectangle.X < windowLeft + windowWidth * 0.35 &&
        rectangle.Y > windowTop + windowHeight * 0.08 &&
        rectangle.Y < windowTop + windowHeight * 0.90 &&
        rectangle.Height >= 20 && rectangle.Height <= 120 &&
        !string.IsNullOrWhiteSpace(name);
    var isMessageRegionCandidate = current.IsEnabled && visibleRectangle &&
        (controlType == "ControlType.Document" || controlType == "ControlType.Pane" ||
         controlType == "ControlType.Group") &&
        rectangle.X >= windowLeft + windowWidth * 0.25 &&
        rectangle.Y <= windowTop + windowHeight * 0.25 &&
        rectangle.Height >= windowHeight * 0.35 &&
        rectangle.Height <= windowHeight * 0.75;
    var semanticAnchors = new List<string>();
    if (isComposerCandidate) semanticAnchors.Add("composer");
    if (hasSendKeyword && hasInvoke) semanticAnchors.Add("send_button");
    if (isConversationItemCandidate) semanticAnchors.Add("conversation_item");
    if (isMessageRegionCandidate) semanticAnchors.Add("message_region");
    var ancestors = includeTopology ? AncestorControlTypes(element, root) : Array.Empty<string>();
    var normalizedX = Normalize(rectangle.X - windowLeft, windowWidth);
    var normalizedY = Normalize(rectangle.Y - windowTop, windowHeight);
    var normalizedRight = Normalize(rectangle.Right - windowLeft, windowWidth);
    var normalizedBottom = Normalize(rectangle.Bottom - windowTop, windowHeight);
    var normalizedWidth = Math.Round(Math.Max(0, normalizedRight - normalizedX), 6);
    var normalizedHeight = Math.Round(Math.Max(0, normalizedBottom - normalizedY), 6);
    var hasNormalizedBounds = visibleRectangle && normalizedWidth > 0 && normalizedHeight > 0;
    var normalizedBounds = hasNormalizedBounds
        ? new
        {
            x = normalizedX,
            y = normalizedY,
            width = normalizedWidth,
            height = normalizedHeight,
        }
        : null;
    var patternNames = new List<string>();
    if (hasInvoke) patternNames.Add("invokepattern");
    if (hasSelectionItem) patternNames.Add("selectionitempattern");
    if (hasText) patternNames.Add("textpattern");
    if (hasValue) patternNames.Add("valuepattern");
    if (hasScroll) patternNames.Add("scrollpattern");
    var structuralLine = string.Join("|", new[]
    {
        controlType,
        className,
        automationId,
        nameKind,
        string.Join(">", ancestors),
        string.Join(",", patternNames),
        hasNormalizedBounds ? $"{normalizedX},{normalizedY},{normalizedWidth},{normalizedHeight}" : "offscreen",
    });
    topologyLines.Add(structuralLine);
    if (includeTopology)
    {
        var runtimeKey = RuntimeKey(element) ?? Sha256($"fallback|{structuralLine}");
        var parentRuntimeKey = ParentRuntimeKey(element, root);
        if (parentRuntimeKey is not null && parentRuntimeKey != "__probe_root__" &&
            !knownRuntimeKeys.Contains(parentRuntimeKey))
        {
            parentRuntimeKey = "__probe_root__";
        }
        topologyDetails.Add(new
        {
            structural_id = Sha256(structuralLine),
            runtime_id = runtimeKey,
            parent_runtime_id = parentRuntimeKey,
            control_type = controlType,
            class_name = className,
            automation_id = automationId,
            name_kind = nameKind,
            ancestor_control_types = ancestors,
            patterns = patternNames,
            semantic_anchors = semanticAnchors,
            is_enabled = current.IsEnabled,
            is_offscreen = current.IsOffscreen,
            normalized_bounds = normalizedBounds,
        });
    }
    if (hasInvoke) invokeCount += 1;
    if (hasSelectionItem) selectionItemCount += 1;
    if (hasText) textCount += 1;
    if (hasValue) valueCount += 1;
    if (hasScroll) scrollCount += 1;

    if (!string.IsNullOrWhiteSpace(name) &&
        (controlType == "ControlType.Text" || controlType == "ControlType.Document"))
    {
        namedTextCount += 1;
        if (visibleRectangle && rectangle.X >= windowLeft + windowWidth * 0.28 &&
            rectangle.Y >= windowTop + windowHeight * 0.08 &&
            rectangle.Y <= windowTop + windowHeight * 0.75)
        {
            messageRegionNamedTextNodes += 1;
        }
    }
    if (hasSelectionItem &&
        (controlType == "ControlType.ListItem" || controlType == "ControlType.TreeItem" ||
         controlType == "ControlType.DataItem"))
    {
        selectableConversationCandidates += 1;
    }
    var isComposerControl = controlType == "ControlType.Edit" || isBoundedDocument;
    if (current.IsEnabled && !current.IsPassword && hasValue && isComposerControl &&
        !isSearch && visibleRectangle &&
        rectangle.X >= windowLeft + windowWidth * 0.25 &&
        rectangle.Y >= windowTop + windowHeight * 0.55)
    {
        composerCandidates += 1;
        composerDetails.Add(new
        {
            control_type = controlType,
            x = SafeCoordinate(rectangle.X - windowLeft),
            y = SafeCoordinate(rectangle.Y - windowTop),
            width = SafeCoordinate(rectangle.Width),
            height = SafeCoordinate(rectangle.Height),
            name_length = name.Length,
            name_category = string.IsNullOrWhiteSpace(name) ? "empty" : "nonempty",
        });
    }
    if (current.IsEnabled && hasInvoke && controlType == "ControlType.Button" &&
        (string.Equals(name.Trim(), "发送", StringComparison.OrdinalIgnoreCase) ||
         string.Equals(name.Trim(), "send", StringComparison.OrdinalIgnoreCase)))
    {
        sendCandidates += 1;
    }
    if (current.IsEnabled && hasInvoke && hasSendKeyword &&
        (controlType == "ControlType.Button" || controlType == "ControlType.Custom"))
    {
        sendKeywordCandidates += 1;
        if (sendTargetDetails.Count < 10)
        {
            sendTargetDetails.Add(new
            {
                source = "semantic_keyword",
                control_type = controlType,
                x = SafeCoordinate(rectangle.X - windowLeft),
                y = SafeCoordinate(rectangle.Y - windowTop),
                width = SafeCoordinate(rectangle.Width),
                height = SafeCoordinate(rectangle.Height),
                name_length = name.Length,
            });
        }
    }
    if (isConversationItemCandidate)
    {
        leftPaneInvokeCandidates += 1;
        var rowStructuralKey = Sha256(string.Join("|", new[]
        {
            controlType,
            className,
            string.Join(">", ancestors),
            string.Join(",", patternNames),
        }));
        conversationRows.Add(new ConversationRowCandidate(element, rowStructuralKey));
    }
    if (current.IsEnabled && hasInvoke && visibleRectangle &&
        rectangle.X > windowLeft + windowWidth * 0.60 &&
        rectangle.Y > windowTop + windowHeight * 0.65)
    {
        bottomRightInvokeCandidates += 1;
        if (sendTargetDetails.Count < 10)
        {
            sendTargetDetails.Add(new
            {
                source = "bottom_right_geometry",
                control_type = controlType,
                x = SafeCoordinate(rectangle.X - windowLeft),
                y = SafeCoordinate(rectangle.Y - windowTop),
                width = SafeCoordinate(rectangle.Width),
                height = SafeCoordinate(rectangle.Height),
                name_length = name.Length,
            });
        }
    }
}

string? clientVersion = null;
try
{
    clientVersion = process.MainModule?.FileVersionInfo.FileVersion;
}
catch (Exception exception) when (exception is InvalidOperationException or System.ComponentModel.Win32Exception)
{
    clientVersion = null;
}

var rootCurrent = root.Current;
var modalState = sawAccountWarningSignal
    ? "account_warning"
    : sawCaptchaSignal
        ? "captcha"
        : sawUpdateSignal
            ? "update"
            : sawLoginSignal
                ? "login"
                : otherVisibleProcessWindows > 0
                    ? "unknown"
                    : "none";
var structuralChatShell = geometryUsable && namedTextCount >= 10 &&
    leftPaneInvokeCandidates >= 5 && valueCount >= 1 &&
    controlTypes.GetValueOrDefault("ControlType.Window") >= 2;
var isLoggedIn = (chatSurfaceSignals.Count >= 2 || structuralChatShell) &&
    !sawLoginSignal && !sawCaptchaSignal && !sawUpdateSignal && !sawAccountWarningSignal;
if (sessionInspectionMode)
{
    void EmitSession(
        bool succeeded,
        string status,
        int headerCandidateCount = 0,
        string? activeHeaderDigest = null,
        string? structureDigest = null)
    {
        string? startedAt = null;
        try
        {
            startedAt = process.StartTime.ToUniversalTime().ToString("O");
        }
        catch (Exception exception) when (exception is InvalidOperationException or System.ComponentModel.Win32Exception)
        {
            startedAt = null;
        }
        EmitCurrentSessionResult(
            succeeded,
            status,
            headerCandidateCount,
            activeHeaderDigest,
            structureDigest,
            process.Id,
            probeWindowHandle.ToInt64(),
            startedAt,
            windowMaximized,
            ForegroundRoot() == probeWindowHandle);
    }
    if (windowMinimized || !windowMaximized || !geometryUsable)
    {
        EmitSession(false, "WINDOW_STATE_NOT_CERTIFIED");
        return 2;
    }
    if (!isLoggedIn || !structuralChatShell || modalState != "none" || otherVisibleProcessWindows != 0)
    {
        EmitSession(false, "CHAT_SHELL_NOT_CERTIFIED");
        return 2;
    }
    if (elements.Count > maxNodes)
    {
        EmitSession(false, "TREE_TRUNCATED");
        return 2;
    }
    if (!TryReadAvatarDiscoveryInput(out var expectedSessionHeaderDigest))
    {
        EmitSession(false, "SESSION_INSPECTION_STDIN_INVALID");
        return 2;
    }
    var headerEvidence = FindActiveHeader(root, windowLeft, windowTop, windowWidth, windowHeight);
    if (headerEvidence.CandidateCount != 1 || headerEvidence.Digest is null ||
        headerEvidence.StructureDigest is null)
    {
        EmitSession(
            false,
            "ACTIVE_HEADER_AMBIGUOUS",
            headerEvidence.CandidateCount,
            headerEvidence.Digest,
            headerEvidence.StructureDigest);
        return 2;
    }
    if (!string.Equals(headerEvidence.Digest, expectedSessionHeaderDigest, StringComparison.Ordinal))
    {
        EmitSession(
            false,
            "ACTIVE_HEADER_DIGEST_MISMATCH",
            headerEvidence.CandidateCount,
            headerEvidence.Digest,
            headerEvidence.StructureDigest);
        return 2;
    }
    EmitSession(
        true,
        "CURRENT_SESSION_INSPECTED",
        1,
        headerEvidence.Digest,
        headerEvidence.StructureDigest);
    return 0;
}
if (avatarDiscoveryMode)
{
    void EmitAvatarDiscovery(
        bool succeeded,
        string status,
        int candidateCount = 0,
        string? activeHeaderDigest = null,
        string? structureDigest = null,
        object[]? candidates = null)
    {
        EmitAvatarDiscoveryResult(
            succeeded,
            status,
            candidateCount,
            activeHeaderDigest,
            structureDigest,
            candidates,
            process.Id,
            probeWindowHandle.ToInt64(),
            windowMaximized,
            ForegroundRoot() == probeWindowHandle);
    }
    if (windowMinimized || !windowMaximized || !geometryUsable)
    {
        EmitAvatarDiscovery(false, "WINDOW_STATE_NOT_CERTIFIED");
        return 2;
    }
    if (windowForeground)
    {
        EmitAvatarDiscovery(false, "QQ_FOREGROUND");
        return 2;
    }
    if (!isLoggedIn || !structuralChatShell || modalState != "none" || otherVisibleProcessWindows != 0)
    {
        EmitAvatarDiscovery(false, "CHAT_SHELL_NOT_CERTIFIED");
        return 2;
    }
    if (elements.Count > maxNodes)
    {
        EmitAvatarDiscovery(false, "TREE_TRUNCATED");
        return 2;
    }
    if (!TryReadAvatarDiscoveryInput(out var expectedAvatarHeaderDigest))
    {
        EmitAvatarDiscovery(false, "AVATAR_DISCOVERY_STDIN_INVALID");
        return 2;
    }
    var headerEvidence = FindActiveHeader(root, windowLeft, windowTop, windowWidth, windowHeight);
    if (headerEvidence.CandidateCount != 1 || headerEvidence.Digest is null)
    {
        EmitAvatarDiscovery(
            false,
            "ACTIVE_HEADER_AMBIGUOUS",
            activeHeaderDigest: headerEvidence.Digest);
        return 2;
    }
    if (!string.Equals(headerEvidence.Digest, expectedAvatarHeaderDigest, StringComparison.Ordinal))
    {
        EmitAvatarDiscovery(
            false,
            "ACTIVE_HEADER_DIGEST_MISMATCH",
            activeHeaderDigest: headerEvidence.Digest);
        return 2;
    }
    var discovery = FindInboundAvatarCandidates(
        elements,
        windowLeft,
        windowTop,
        windowWidth,
        windowHeight);
    if (discovery.NodeLimitExceeded)
    {
        EmitAvatarDiscovery(false, "AVATAR_NODE_LIMIT_EXCEEDED", activeHeaderDigest: headerEvidence.Digest);
        return 2;
    }
    if (discovery.CandidateLimitExceeded)
    {
        EmitAvatarDiscovery(false, "AVATAR_CANDIDATE_LIMIT_EXCEEDED", activeHeaderDigest: headerEvidence.Digest);
        return 2;
    }
    var candidates = discovery.Candidates;
    var structureDigest = Sha256(string.Join("\n", candidates
        .Select(candidate => candidate.StructureDigest)
        .OrderBy(value => value, StringComparer.Ordinal)));
    var safeCandidates = candidates.Select(candidate => (object)new
    {
        normalized_x = Normalize(candidate.X - windowLeft, windowWidth),
        normalized_y = Normalize(candidate.Y - windowTop, windowHeight),
        normalized_width = Normalize(candidate.Width, windowWidth),
        normalized_height = Normalize(candidate.Height, windowHeight),
    }).ToArray();
    EmitAvatarDiscovery(
        candidates.Length >= 2,
        candidates.Length >= 2 ? "AVATAR_CANDIDATES_DISCOVERED" : "INSUFFICIENT_AVATAR_ROWS",
        candidates.Length,
        headerEvidence.Digest,
        structureDigest,
        safeCandidates);
    return candidates.Length >= 2 ? 0 : 2;
}
if (avatarMode)
{
    try
    {
        void EmitAvatar(
            bool succeeded,
            string status,
            int candidateRowCount = 0,
            int stableMatchCount = 0,
            string? avatarHmac = null,
            string? activeHeaderDigest = null,
            string? structureDigest = null,
            int headerCandidateCount = 0,
            IntPtr? foregroundBefore = null,
            string captureApi = "PrintWindow")
        {
            var foregroundAfter = ForegroundRoot();
            EmitAvatarResult(
                succeeded,
                status,
                candidateRowCount,
                stableMatchCount,
                avatarHmac,
                activeHeaderDigest,
                structureDigest,
                headerCandidateCount,
                process.Id,
                probeWindowHandle.ToInt64(),
                windowMaximized,
                windowForeground,
                foregroundAfter == probeWindowHandle,
                foregroundBefore.HasValue && foregroundAfter != foregroundBefore.Value,
                captureApi);
        }
        if (windowMinimized || !windowMaximized || !geometryUsable)
        {
            EmitAvatar(false, "WINDOW_STATE_NOT_CERTIFIED");
            return 2;
        }
        if (windowForeground)
        {
            EmitAvatar(false, "QQ_FOREGROUND");
            return 2;
        }
        if (!isLoggedIn || !structuralChatShell || modalState != "none" || otherVisibleProcessWindows != 0)
        {
            EmitAvatar(false, "CHAT_SHELL_NOT_CERTIFIED");
            return 2;
        }
        if (elements.Count > maxNodes)
        {
            EmitAvatar(false, "TREE_TRUNCATED");
            return 2;
        }
        if (!TryReadIdentityInput(out avatarExpectedHeaderDigest, out avatarHmacKey))
        {
            Array.Clear(avatarHmacKey, 0, avatarHmacKey.Length);
            EmitAvatar(false, "AVATAR_STDIN_INVALID");
            return 2;
        }
        var headerEvidence = FindActiveHeader(root, windowLeft, windowTop, windowWidth, windowHeight);
        if (headerEvidence.CandidateCount != 1 || headerEvidence.Digest is null)
        {
            EmitAvatar(
                false,
                "ACTIVE_HEADER_AMBIGUOUS",
                activeHeaderDigest: headerEvidence.Digest,
                headerCandidateCount: headerEvidence.CandidateCount);
            return 2;
        }
        if (!string.Equals(headerEvidence.Digest, avatarExpectedHeaderDigest, StringComparison.Ordinal))
        {
            EmitAvatar(
                false,
                "ACTIVE_HEADER_DIGEST_MISMATCH",
                activeHeaderDigest: headerEvidence.Digest,
                headerCandidateCount: headerEvidence.CandidateCount);
            return 2;
        }
        var avatarDiscovery = FindInboundAvatarCandidates(
            elements,
            windowLeft,
            windowTop,
            windowWidth,
            windowHeight);
        if (avatarDiscovery.NodeLimitExceeded)
        {
            EmitAvatar(
                false,
                "AVATAR_NODE_LIMIT_EXCEEDED",
                activeHeaderDigest: headerEvidence.Digest,
                headerCandidateCount: headerEvidence.CandidateCount);
            return 2;
        }
        if (avatarDiscovery.CandidateLimitExceeded)
        {
            EmitAvatar(
                false,
                "AVATAR_CANDIDATE_LIMIT_EXCEEDED",
                activeHeaderDigest: headerEvidence.Digest,
                headerCandidateCount: headerEvidence.CandidateCount);
            return 2;
        }
        var avatarCandidates = avatarDiscovery.Candidates;
        var structureDigest = Sha256(string.Join("\n", avatarCandidates
            .Select(candidate => candidate.StructureDigest)
            .OrderBy(value => value, StringComparer.Ordinal)));
        if (avatarCandidates.Length < 2)
        {
            EmitAvatar(
                false,
                "INSUFFICIENT_AVATAR_ROWS",
                avatarCandidates.Length,
                0,
                activeHeaderDigest: headerEvidence.Digest,
                structureDigest: structureDigest,
                headerCandidateCount: headerEvidence.CandidateCount);
            return 2;
        }
        var foregroundBefore = ForegroundRoot();
        var frameHashes = avatarCandidates.ToDictionary(
            candidate => candidate.GeometryKey,
            _ => new List<string>(),
            StringComparer.Ordinal);
        string? captureApi = null;
        for (var frameIndex = 0; frameIndex < 3; frameIndex += 1)
        {
            if (ForegroundRoot() != foregroundBefore)
            {
                EmitAvatar(
                    false,
                    "FOREGROUND_CHANGED",
                    avatarCandidates.Length,
                    0,
                    activeHeaderDigest: headerEvidence.Digest,
                    structureDigest: structureDigest,
                    headerCandidateCount: headerEvidence.CandidateCount,
                    foregroundBefore: foregroundBefore);
                return 2;
            }
            PrintWindowFrame? frame;
            if (frameIndex == 0)
            {
                frame = CapturePrintWindowFrame(probeWindowHandle, (int)windowWidth, (int)windowHeight);
                if (frame is not null && !IsUsablePrintWindowFrame(frame))
                {
                    Array.Clear(frame.Pixels, 0, frame.Pixels.Length);
                    frame = null;
                }
                if (frame is null)
                {
                    frame = CaptureExactHwndBitBltFrame(probeWindowHandle, (int)windowWidth, (int)windowHeight);
                    captureApi = "ExactHwndBitBlt";
                }
                else
                {
                    captureApi = "PrintWindow";
                }
            }
            else
            {
                frame = captureApi == "PrintWindow"
                    ? CapturePrintWindowFrame(probeWindowHandle, (int)windowWidth, (int)windowHeight)
                    : CaptureExactHwndBitBltFrame(probeWindowHandle, (int)windowWidth, (int)windowHeight);
            }
            if (frame is null || !IsUsablePrintWindowFrame(frame))
            {
                if (frame is not null) Array.Clear(frame.Pixels, 0, frame.Pixels.Length);
                EmitAvatar(
                    false,
                    "EXACT_HWND_CAPTURE_FAILED",
                    avatarCandidates.Length,
                    0,
                    activeHeaderDigest: headerEvidence.Digest,
                    structureDigest: structureDigest,
                    headerCandidateCount: headerEvidence.CandidateCount,
                    foregroundBefore: foregroundBefore,
                    captureApi: captureApi ?? "ExactHwndBitBlt");
                return 2;
            }
            try
            {
                foreach (var candidate in avatarCandidates)
                {
                    var hash = AvatarFrameHmac(frame, candidate, windowLeft, windowTop, avatarHmacKey);
                    if (hash is not null) frameHashes[candidate.GeometryKey].Add(hash);
                }
            }
            finally
            {
                Array.Clear(frame.Pixels, 0, frame.Pixels.Length);
            }
            if (frameIndex < 2) Thread.Sleep(75);
        }
        if (ForegroundRoot() != foregroundBefore)
        {
            EmitAvatar(
                false,
                "FOREGROUND_CHANGED",
                avatarCandidates.Length,
                0,
                activeHeaderDigest: headerEvidence.Digest,
                structureDigest: structureDigest,
                headerCandidateCount: headerEvidence.CandidateCount,
                foregroundBefore: foregroundBefore,
                captureApi: captureApi ?? "ExactHwndBitBlt");
            return 2;
        }
        var stableHashes = frameHashes.Values
            .Where(hashes => hashes.Count == 3 && hashes.Distinct(StringComparer.Ordinal).Count() == 1)
            .Select(hashes => hashes[0])
            .ToArray();
        var uniqueHashes = stableHashes.Distinct(StringComparer.Ordinal).ToArray();
        if (stableHashes.Length < 2)
        {
            EmitAvatar(
                false,
                "PENDING_AVATAR_UNSTABLE",
                avatarCandidates.Length,
                stableHashes.Length,
                activeHeaderDigest: headerEvidence.Digest,
                structureDigest: structureDigest,
                headerCandidateCount: headerEvidence.CandidateCount,
                foregroundBefore: foregroundBefore,
                captureApi: captureApi ?? "ExactHwndBitBlt");
            return 2;
        }
        if (uniqueHashes.Length != 1)
        {
            EmitAvatar(
                false,
                "PENDING_AVATAR_NOT_UNIQUE",
                avatarCandidates.Length,
                stableHashes.Length,
                activeHeaderDigest: headerEvidence.Digest,
                structureDigest: structureDigest,
                headerCandidateCount: headerEvidence.CandidateCount,
                foregroundBefore: foregroundBefore,
                captureApi: captureApi ?? "ExactHwndBitBlt");
            return 2;
        }
        EmitAvatar(
            true,
            "CURRENT_AVATAR_CAPTURED",
            avatarCandidates.Length,
            stableHashes.Length,
            uniqueHashes[0],
            headerEvidence.Digest,
            structureDigest,
            headerEvidence.CandidateCount,
            foregroundBefore,
            captureApi ?? "ExactHwndBitBlt");
        return 0;
    }
    finally
    {
        Array.Clear(avatarHmacKey, 0, avatarHmacKey.Length);
    }
}
if (identityMode)
{
    try
    {
        object? restorationDiagnostic = null;
        object? acquisitionMetadata = null;
        var identityStructureLines = CaptureRightRegionStructureLines(elements,
            windowLeft, windowTop, windowWidth, windowHeight);
        var identityRightRegionStructureDigest = guestIdentityMode
            ? Sha256(string.Join("\n", identityStructureLines)) : CaptureCurrentChatEvidence(
            root,
            elements,
            windowLeft,
            windowTop,
            windowWidth,
            windowHeight).RightRegionStructureDigest;
        void EmitIdentity(
            bool succeeded,
            string status,
            int headerCandidateCount = 0,
            string? activeHeaderDigest = null,
            int identityCandidateCount = 0,
            string? profileIdHmac = null,
            string? identityEvidenceType = null,
            string? profileStructureDigest = null,
            bool recoveryAttempted = false,
            bool originalViewRestored = false,
            bool foregroundChanged = false,
            bool transientNavigationPerformed = false)
        {
            EmitCurrentIdentityResult(
                succeeded,
                status,
                headerCandidateCount,
                activeHeaderDigest,
                identityCandidateCount,
                profileIdHmac,
                identityEvidenceType,
                profileStructureDigest,
                recoveryAttempted,
                originalViewRestored,
                foregroundChanged,
                process.Id,
                probeWindowHandle.ToInt64(),
                windowMaximized,
                windowForeground,
                ForegroundRoot() == probeWindowHandle,
                transientNavigationPerformed,
                identityRightRegionStructureDigest,
                guestIdentityMode,
                guestIdentityMode && IsCertifiedGuestIdentityEnvironment(),
                restorationDiagnostic,
                acquisitionMetadata);
        }
        if (windowMinimized || !windowMaximized || !geometryUsable)
        {
            EmitIdentity(false, "WINDOW_STATE_NOT_CERTIFIED");
            return 2;
        }
        if (guestIdentityMode && (!windowForeground || !IsCertifiedGuestIdentityEnvironment()))
        {
            EmitIdentity(false, "GUEST_FOREGROUND_ENVIRONMENT_NOT_CERTIFIED");
            return 2;
        }
        if (!guestIdentityMode && windowForeground)
        {
            EmitIdentity(false, "QQ_FOREGROUND");
            return 2;
        }
        if ((!guestIdentityMode && (!isLoggedIn || !structuralChatShell || modalState != "none")) ||
            otherVisibleProcessWindows != 0)
        {
            EmitIdentity(false, "CHAT_SHELL_NOT_CERTIFIED");
            return 2;
        }
        if (elements.Count > maxNodes)
        {
            EmitIdentity(false, "TREE_TRUNCATED");
            return 2;
        }
        if (!TryReadIdentityInput(out identityExpectedHeaderDigest, out identityHmacKey))
        {
            Array.Clear(identityHmacKey, 0, identityHmacKey.Length);
            EmitIdentity(false, "IDENTITY_STDIN_INVALID");
            return 2;
        }
        var headerEvidence = guestIdentityMode
            ? FindGuestActiveHeader(root, windowLeft, windowTop, windowWidth, windowHeight)
            : FindActiveHeader(root, windowLeft, windowTop, windowWidth, windowHeight);
        if (headerEvidence.CandidateCount != 1 || headerEvidence.Element is null)
        {
            EmitIdentity(
                false,
                "ACTIVE_HEADER_AMBIGUOUS",
                headerEvidence.CandidateCount,
                headerEvidence.Digest);
            return 2;
        }
        if (!string.Equals(headerEvidence.Digest, identityExpectedHeaderDigest, StringComparison.Ordinal))
        {
            EmitIdentity(
                false,
                "ACTIVE_HEADER_DIGEST_MISMATCH",
                headerEvidence.CandidateCount,
                headerEvidence.Digest);
            return 2;
        }
        if (!headerEvidence.Element.TryGetCurrentPattern(InvokePattern.Pattern, out var rawHeaderPattern) ||
            rawHeaderPattern is not InvokePattern headerInvokePattern)
        {
            EmitIdentity(
                false,
                "HEADER_INVOKE_UNAVAILABLE",
                headerEvidence.CandidateCount,
                headerEvidence.Digest);
            return 2;
        }
        var foregroundBefore = ForegroundRoot();
        var beforeFence = guestIdentityMode ? CaptureSelectedRowFence(root,
            headerEvidence.Digest, identityRightRegionStructureDigest,
            windowLeft, windowTop, windowWidth, windowHeight) : null;
        var windowsBefore = VisibleProcessWindowHandles(process.Id);
        if (windowsBefore.Length != 1 || windowsBefore[0] != probeWindowHandle)
        {
            EmitIdentity(
                false,
                "PROFILE_WINDOW_PRECONDITION_FAILED",
                headerEvidence.CandidateCount,
                headerEvidence.Digest);
            return 2;
        }
        try
        {
            // This is the one and only non-read UIA action in this explicitly authorized mode.
            headerInvokePattern.Invoke();
        }
        catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException)
        {
            EmitIdentity(
                false,
                "HEADER_INVOKE_FAILED",
                headerEvidence.CandidateCount,
                headerEvidence.Digest,
                transientNavigationPerformed: true);
            return 2;
        }
        var profileHandles = WaitForStableNewProcessWindows(process.Id, windowsBefore);
        var foregroundChangedAfterOpen = ForegroundRoot() != foregroundBefore;
        if (profileHandles.Length != 1)
        {
            EmitIdentity(
                false,
                profileHandles.Length == 0 ? "PROFILE_VIEW_NOT_SEPARATE_WINDOW" : "PROFILE_WINDOW_AMBIGUOUS",
                headerEvidence.CandidateCount,
                headerEvidence.Digest,
                recoveryAttempted: true,
                foregroundChanged: foregroundChangedAfterOpen,
                transientNavigationPerformed: true);
            return 2;
        }
        AutomationElement profileRoot;
        try
        {
            profileRoot = AutomationElement.FromHandle(profileHandles[0]);
        }
        catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException)
        {
            var cleanupSucceeded = TryCloseTransientWindow(profileHandles[0], process.Id);
            EmitIdentity(
                false,
                cleanupSucceeded ? "PROFILE_UIA_ROOT_UNAVAILABLE" : "RESTORATION_FAILED",
                headerEvidence.CandidateCount,
                headerEvidence.Digest,
                recoveryAttempted: true,
                originalViewRestored: cleanupSucceeded,
                foregroundChanged: foregroundChangedAfterOpen,
                transientNavigationPerformed: true);
            return 2;
        }
        var profileEvidence = CaptureExplicitProfileIdentityEvidence(profileRoot);
        var profileCapturedAt = DateTimeOffset.UtcNow.ToString("O");
        var profileCapturedMonotonicNs = MonotonicNanoseconds();
        var restored = false;
        try
        {
            if (profileRoot.TryGetCurrentPattern(WindowPattern.Pattern, out var rawWindowPattern) &&
                rawWindowPattern is WindowPattern profileWindowPattern)
            {
                // The target is a newly-created same-process top-level window, never the chat shell.
                profileWindowPattern.Close();
                restored = WaitForWindowToClose(process.Id, profileHandles[0]);
            }
        }
        catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException)
        {
            restored = false;
        }
        if (guestIdentityMode && ForegroundRoot() != probeWindowHandle)
        {
            NativeMethods.SetForegroundWindow(probeWindowHandle);
            Thread.Sleep(150);
        }
        var foregroundPreserved = ForegroundRoot() == foregroundBefore;
        if (!restored)
        {
            EmitIdentity(
                false,
                "RESTORATION_FAILED",
                headerEvidence.CandidateCount,
                headerEvidence.Digest,
                profileEvidence.CandidateCount,
                null,
                null,
                profileEvidence.StructureDigest,
                recoveryAttempted: true,
                originalViewRestored: false,
                foregroundChanged: !foregroundPreserved,
                transientNavigationPerformed: true);
            return 2;
        }
        if (!foregroundPreserved)
        {
            EmitIdentity(
                false,
                "FOREGROUND_CHANGED",
                headerEvidence.CandidateCount,
                headerEvidence.Digest,
                profileEvidence.CandidateCount,
                null,
                null,
                profileEvidence.StructureDigest,
                recoveryAttempted: true,
                originalViewRestored: true,
                foregroundChanged: true,
                transientNavigationPerformed: true);
            return 2;
        }
        if (guestIdentityMode)
        {
            NativeMethods.GetWindowThreadProcessId(probeWindowHandle, out var restoredOwnerPid);
            if (restoredOwnerPid != process.Id || !NativeMethods.IsWindowVisible(probeWindowHandle) ||
                !NativeMethods.IsZoomed(probeWindowHandle) || NativeMethods.IsIconic(probeWindowHandle) ||
                !NativeMethods.GetWindowRect(probeWindowHandle, out var restoredRectangle) ||
                restoredRectangle.Left != nativeRectangle.Left || restoredRectangle.Top != nativeRectangle.Top ||
                restoredRectangle.Right != nativeRectangle.Right || restoredRectangle.Bottom != nativeRectangle.Bottom)
            {
                EmitIdentity(false, "QQ_WINDOW_NOT_RESTORED", recoveryAttempted: true,
                    originalViewRestored: false, foregroundChanged: false,
                    transientNavigationPerformed: true);
                return 2;
            }
            var restoredRoot = AutomationElement.FromHandle(probeWindowHandle);
            var restoredElements = restoredRoot.FindAll(TreeScope.Descendants, Condition.TrueCondition);
            var restoredHeader = FindGuestActiveHeader(restoredRoot, windowLeft, windowTop, windowWidth, windowHeight);
            var restoredRightDigest = Sha256(string.Join("\n", CaptureRightRegionStructureLines(restoredElements,
                windowLeft, windowTop, windowWidth, windowHeight)));
            var afterFence = CaptureSelectedRowFence(restoredRoot,
                restoredHeader.Digest, restoredRightDigest,
                windowLeft, windowTop, windowWidth, windowHeight);
            var chatRestored = restoredHeader.CandidateCount == 1 && restoredHeader.Digest == headerEvidence.Digest &&
                restoredRightDigest == identityRightRegionStructureDigest;
            acquisitionMetadata = new
            {
                version = "qq_profile_acquisition_v2",
                process_started_at_100ns = process.StartTime.ToUniversalTime().ToFileTimeUtc(),
                profile_window_handle = profileHandles[0].ToInt64(),
                profile_process_id = process.Id,
                profile_window_candidate_count = profileHandles.Length,
                profile_window_was_new = !windowsBefore.Contains(profileHandles[0]),
                profile_captured_at = profileCapturedAt,
                profile_captured_monotonic_ns = profileCapturedMonotonicNs,
                profile_window_closed = restored,
                original_chat_restored = chatRestored,
                foreground_restored = foregroundPreserved,
                before = beforeFence,
                after = afterFence,
            };
            var restoredLines = CaptureRightRegionStructureLines(restoredElements,
                windowLeft, windowTop, windowWidth, windowHeight);
            restorationDiagnostic = new
            {
                restored_header_candidate_count = restoredHeader.CandidateCount,
                header_digest_equal = restoredHeader.Digest == headerEvidence.Digest,
                right_region_digest_equal = restoredRightDigest == identityRightRegionStructureDigest,
                before_node_count = identityStructureLines.Length,
                after_node_count = restoredLines.Length,
                before_diagnostic_digest = Sha256(string.Join("\n", identityStructureLines)),
                after_diagnostic_digest = Sha256(string.Join("\n", restoredLines)),
                removed_structure = identityStructureLines.Except(restoredLines, StringComparer.Ordinal).Take(20).ToArray(),
                added_structure = restoredLines.Except(identityStructureLines, StringComparer.Ordinal).Take(20).ToArray(),
            };
            if (restoredHeader.CandidateCount != 1 || restoredHeader.Digest != headerEvidence.Digest ||
                restoredRightDigest != identityRightRegionStructureDigest)
            {
                EmitIdentity(false, "ORIGINAL_CONVERSATION_NOT_RESTORED", headerEvidence.CandidateCount,
                    headerEvidence.Digest, profileEvidence.CandidateCount, null, null,
                    profileEvidence.StructureDigest, true, false, false, true);
                return 2;
            }
        }
        if (profileEvidence.CandidateCount != 1)
        {
            EmitIdentity(
                false,
                "PENDING_NO_STABLE_SIGNAL",
                headerEvidence.CandidateCount,
                headerEvidence.Digest,
                profileEvidence.CandidateCount,
                null,
                null,
                profileEvidence.StructureDigest,
                recoveryAttempted: true,
                originalViewRestored: true,
                foregroundChanged: false,
                transientNavigationPerformed: true);
            return 2;
        }
        EmitIdentity(
            true,
            "STABLE_IDENTITY_CAPTURED",
            headerEvidence.CandidateCount,
            headerEvidence.Digest,
            1,
            HmacSha256(profileEvidence.Candidates[0].RawId, identityHmacKey),
            "explicit_labeled_qq_id",
            profileEvidence.StructureDigest,
            recoveryAttempted: true,
            originalViewRestored: true,
            foregroundChanged: false,
            transientNavigationPerformed: true);
        return 0;
    }
    finally
    {
        Array.Clear(identityHmacKey, 0, identityHmacKey.Length);
    }
}
if (captureMode)
{
    if (windowMinimized || !windowMaximized || !geometryUsable)
    {
        EmitCurrentChatResult(false, "WINDOW_STATE_NOT_CERTIFIED");
        return 2;
    }
    if (!isLoggedIn || !structuralChatShell || modalState != "none")
    {
        EmitCurrentChatResult(false, "CHAT_SHELL_NOT_CERTIFIED");
        return 2;
    }
    if (elements.Count > maxNodes)
    {
        EmitCurrentChatResult(false, "TREE_TRUNCATED");
        return 2;
    }
    var chatEvidence = CaptureCurrentChatEvidence(
        root,
        elements,
        windowLeft,
        windowTop,
        windowWidth,
        windowHeight);
    if (chatEvidence.HeaderCandidateCount != 1)
    {
        EmitCurrentChatResult(
            false,
            "ACTIVE_HEADER_AMBIGUOUS",
            chatEvidence.HeaderCandidateCount,
            chatEvidence.ActiveHeaderDigest,
            chatEvidence.RightRegionStructureDigest);
        return 2;
    }
    if (chatEvidence.RepeatedCandidateCount == 0)
    {
        var status = chatEvidence.RawCandidateCount == 0
            ? "NO_MESSAGE_ROWS"
            : "MESSAGE_ROLE_AMBIGUOUS";
        EmitCurrentChatResult(
            false,
            status,
            chatEvidence.HeaderCandidateCount,
            chatEvidence.ActiveHeaderDigest,
            chatEvidence.RightRegionStructureDigest);
        return 2;
    }
    var messages = chatEvidence.Messages.Select(message => (object)new
    {
        text = message.Text,
        source_evidence_hash = message.SourceEvidenceHash,
        message_watermark = message.MessageWatermark,
        direction = message.Direction,
        direction_confidence = message.DirectionConfidence,
        observer_confidence = message.ObserverConfidence,
        normalized_y = message.NormalizedY,
        observed_at = (string?)null,
        time_confidence = 0,
    }).ToArray();
    EmitCurrentChatResult(
        true,
        "CURRENT_CHAT_CAPTURED",
        chatEvidence.HeaderCandidateCount,
        chatEvidence.ActiveHeaderDigest,
        chatEvidence.RightRegionStructureDigest,
        messages,
        process.Id,
        probeWindowHandle.ToInt64(),
        windowMinimized,
        DateTimeOffset.UtcNow.ToString("O"));
    return 0;
}
if (selectionMode)
{
    if (windowMinimized || !windowMaximized || !geometryUsable)
    {
        EmitSelectionResult(false, "WINDOW_STATE_NOT_CERTIFIED", 0);
        return 2;
    }
    if (windowForeground)
    {
        EmitSelectionResult(false, "QQ_FOREGROUND", 0);
        return 2;
    }
    if (!isLoggedIn || modalState != "none")
    {
        EmitSelectionResult(false, "QQ_MODAL_OR_LOGIN_STATE", 0);
        return 2;
    }
    if (elements.Count > maxNodes)
    {
        EmitSelectionResult(false, "TREE_TRUNCATED", 0);
        return 2;
    }
    var textMatchCounts = VisibleTextMatchCounts(
        elements,
        selectionTarget,
        windowLeft,
        windowTop,
        windowWidth,
        windowHeight);
    var repeatedRows = conversationRows
        .GroupBy(candidate => candidate.StructuralKey, StringComparer.Ordinal)
        .Where(group => group.Count() >= 2)
        .SelectMany(group => group)
        .ToArray();
    if (repeatedRows.Length < 2)
    {
        var status = textMatchCounts.Root == 1 && textMatchCounts.Right == 1
            ? "CURRENT_RIGHT_REGION_MATCH"
            : "NO_REPEATED_CONVERSATION_ROWS";
        var noRowsRightEvidence = status == "CURRENT_RIGHT_REGION_MATCH"
            ? CaptureRightRegionEvidence(
                root,
                windowLeft,
                windowTop,
                windowWidth,
                windowHeight,
                selectionTarget)
            : null;
        EmitSelectionResult(
            status == "CURRENT_RIGHT_REGION_MATCH",
            status,
            0,
            false,
            null,
            noRowsRightEvidence,
            textMatchCounts.Root,
            textMatchCounts.Left,
            textMatchCounts.Right);
        return status == "CURRENT_RIGHT_REGION_MATCH" ? 0 : 2;
    }
    var matches = new List<(ConversationRowCandidate Candidate, string EvidenceDigest)>();
    foreach (var candidate in repeatedRows)
    {
        if (HasVisibleLocalDescendantText(candidate.Element, selectionTarget, out var evidenceDigest))
        {
            matches.Add((candidate, evidenceDigest));
        }
    }
    var matchEvidenceDigest = Sha256(string.Join("\n", matches
        .Select(match => $"{match.Candidate.StructuralKey}|{match.EvidenceDigest}")
        .OrderBy(line => line, StringComparer.Ordinal)));
    if (matches.Count != 1)
    {
        var status = matches.Count == 0 &&
            textMatchCounts.Root == 1 && textMatchCounts.Right == 1
            ? "CURRENT_RIGHT_REGION_MATCH"
            : "MATCH_NOT_UNIQUE";
        var noMatchRightEvidence = status == "CURRENT_RIGHT_REGION_MATCH"
            ? CaptureRightRegionEvidence(
                root,
                windowLeft,
                windowTop,
                windowWidth,
                windowHeight,
                selectionTarget)
            : null;
        EmitSelectionResult(
            status == "CURRENT_RIGHT_REGION_MATCH",
            status,
            matches.Count,
            false,
            matchEvidenceDigest,
            noMatchRightEvidence,
            textMatchCounts.Root,
            textMatchCounts.Left,
            textMatchCounts.Right);
        return status == "CURRENT_RIGHT_REGION_MATCH" ? 0 : 2;
    }
    if (!selectionAuthorized)
    {
        EmitSelectionResult(
            true,
            "MATCH_READY_DRY_RUN",
            1,
            false,
            matchEvidenceDigest,
            null,
            textMatchCounts.Root,
            textMatchCounts.Left,
            textMatchCounts.Right);
        return 0;
    }
    if (!matches[0].Candidate.Element.TryGetCurrentPattern(InvokePattern.Pattern, out var rawPattern) ||
        rawPattern is not InvokePattern invokePattern)
    {
        EmitSelectionResult(
            false,
            "INVOKE_PATTERN_UNAVAILABLE",
            1,
            false,
            matchEvidenceDigest,
            null,
            textMatchCounts.Root,
            textMatchCounts.Left,
            textMatchCounts.Right);
        return 2;
    }
    try
    {
        invokePattern.Invoke();
    }
    catch (Exception exception) when (exception is ElementNotAvailableException or InvalidOperationException)
    {
        EmitSelectionResult(
            false,
            "INVOKE_FAILED",
            1,
            true,
            matchEvidenceDigest,
            null,
            textMatchCounts.Root,
            textMatchCounts.Left,
            textMatchCounts.Right);
        return 2;
    }
    Thread.Sleep(350);
    var selectedRightEvidence = CaptureRightRegionEvidence(
        root,
        windowLeft,
        windowTop,
        windowWidth,
        windowHeight,
        selectionTarget);
    if (selectedRightEvidence is null)
    {
        EmitSelectionResult(
            false,
            "POST_SELECT_EVIDENCE_UNAVAILABLE",
            1,
            true,
            matchEvidenceDigest,
            null,
            textMatchCounts.Root,
            textMatchCounts.Left,
            textMatchCounts.Right);
        return 2;
    }
    EmitSelectionResult(
        true,
        "SELECTED_WITH_POST_EVIDENCE",
        1,
        true,
        matchEvidenceDigest,
        selectedRightEvidence,
        textMatchCounts.Root,
        textMatchCounts.Left,
        textMatchCounts.Right);
    return 0;
}
Emit(new
{
    probe_version = "qq-uia-readonly-v1",
    succeeded = true,
    read_only = true,
    process_id = process.Id,
    window_handle = probeWindowHandle.ToInt64(),
    client_version = clientVersion,
    warmup_used = warmupUiaEvents,
    warmup_ms = warmupMilliseconds,
    foreground_wait_used = foregroundWaitMilliseconds > 0,
    foreground_wait_ms = foregroundWaitMilliseconds,
    foreground_wait_elapsed_ms = Math.Min(
        foregroundWait.ElapsedMilliseconds, foregroundWaitMilliseconds),
    root = new
    {
        control_type = rootCurrent.ControlType?.ProgrammaticName ?? string.Empty,
        class_name = rootCurrent.ClassName ?? string.Empty,
        automation_id = rootCurrent.AutomationId ?? string.Empty,
    },
    host_environment = new
    {
        executable_path = string.IsNullOrWhiteSpace(executablePath)
            ? "sha256:unknown"
            : $"sha256:{Sha256(executablePath.ToLowerInvariant())}",
        executable_signature = executableSignature,
        process_signature = processSignature,
        client_version = clientVersion ?? "unknown",
        windows_version = Environment.OSVersion.VersionString,
        dpi_scale = dpiScale,
        monitor_id = monitorId,
        monitor_topology_digest = monitorTopologyDigest,
        theme = ReadTheme(),
        window_class = rootCurrent.ClassName ?? string.Empty,
        presentation,
        is_foreground = windowForeground,
        is_occluded = !windowForeground,
        is_logged_in = isLoggedIn,
        modal_state = modalState,
        other_visible_process_windows = otherVisibleProcessWindows,
    },
    window_bounds = new
    {
        x = SafeCoordinate(windowLeft),
        y = SafeCoordinate(windowTop),
        width = windowWidth,
        height = windowHeight,
        minimized = windowMinimized,
        maximized = windowMaximized,
        foreground = windowForeground,
        geometry_usable = geometryUsable,
    },
    node_count_total = elements.Count,
    node_count_examined = examined,
    truncated = elements.Count > maxNodes,
    control_types = Top(controlTypes),
    class_names = Top(classNames),
    automation_ids = Top(automationIds),
    pattern_counts = new
    {
        invoke = invokeCount,
        selection_item = selectionItemCount,
        text = textCount,
        value = valueCount,
        scroll = scrollCount,
    },
    semantic_signals = new
    {
        named_text_nodes = namedTextCount,
        message_region_named_text_nodes = messageRegionNamedTextNodes,
        selectable_conversation_candidates = selectableConversationCandidates,
        left_pane_invoke_candidates = leftPaneInvokeCandidates,
        composer_candidates = composerCandidates,
        exact_send_button_candidates = sendCandidates,
        send_keyword_candidates = sendKeywordCandidates,
        bottom_right_invoke_candidates = bottomRightInvokeCandidates,
        structural_chat_shell_detected = structuralChatShell,
    },
    candidate_details = new
    {
        composers = composerDetails,
        send_targets = sendTargetDetails,
    },
    topology = new
    {
        included = includeTopology,
        digest = Sha256(string.Join("\n", topologyLines.OrderBy(line => line, StringComparer.Ordinal))),
        nodes = topologyDetails,
    },
    privacy = new
    {
        emitted_control_names = false,
        emitted_message_text = false,
        changed_window_state = false,
    },
});
return 0;

[StructLayout(LayoutKind.Sequential)]
internal struct NativeRect
{
    public int Left;
    public int Top;
    public int Right;
    public int Bottom;
}

internal sealed record ConversationRowCandidate(AutomationElement Element, string StructuralKey);

internal sealed record CurrentChatMessageCandidate(
    string Text,
    string Direction,
    double NormalizedY,
    string RowStructureDigest,
    string SourceEvidenceHash);

internal sealed record CurrentChatMessage(
    string Text,
    string SourceEvidenceHash,
    string MessageWatermark,
    string Direction,
    double DirectionConfidence,
    double ObserverConfidence,
    double NormalizedY);

internal sealed record CurrentChatEvidence(
    int HeaderCandidateCount,
    string? ActiveHeaderDigest,
    string RightRegionStructureDigest,
    CurrentChatMessage[] Messages,
    int RawCandidateCount,
    int RepeatedCandidateCount);

internal sealed record ActiveHeaderEvidence(
    int CandidateCount,
    string? Digest,
    AutomationElement? Element,
    string? StructureDigest);

internal sealed record ProfileIdentityCandidate(string RawId, string StructureLine);

internal sealed record ProfileIdentityEvidence(
    int CandidateCount,
    string StructureDigest,
    ProfileIdentityCandidate[] Candidates);

internal sealed record AvatarCandidate(
    double X,
    double Y,
    double Width,
    double Height,
    double NormalizedY,
    string GeometryKey,
    string StructureDigest);

internal sealed record AvatarNodeMetric(
    string ControlType,
    bool IsOffscreen,
    double X,
    double Y,
    double Width,
    double Height,
    bool IsEmpty);

internal sealed record AvatarDiscovery(
    AvatarCandidate[] Candidates,
    bool NodeLimitExceeded,
    bool CandidateLimitExceeded);

internal sealed record PrintWindowFrame(int Width, int Height, byte[] Pixels);

[StructLayout(LayoutKind.Sequential)]
internal struct BitmapInfoHeader
{
    public uint Size;
    public int Width;
    public int Height;
    public ushort Planes;
    public ushort BitCount;
    public uint Compression;
    public uint ImageSize;
    public int XPelsPerMeter;
    public int YPelsPerMeter;
    public uint ClrUsed;
    public uint ClrImportant;
}

[StructLayout(LayoutKind.Sequential)]
internal struct BitmapInfo
{
    public BitmapInfoHeader Header;
    public uint Colors;
}

internal static class NativeMethods
{
    internal delegate bool EnumWindowsProc(IntPtr windowHandle, IntPtr parameter);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool GetWindowRect(IntPtr windowHandle, out NativeRect rectangle);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool IsIconic(IntPtr windowHandle);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool IsZoomed(IntPtr windowHandle);

    [DllImport("user32.dll")]
    internal static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    internal static extern IntPtr GetAncestor(IntPtr hwnd, uint flags);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool SetForegroundWindow(IntPtr windowHandle);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool EnumWindows(EnumWindowsProc callback, IntPtr parameter);

    [DllImport("user32.dll")]
    internal static extern uint GetWindowThreadProcessId(IntPtr windowHandle, out int processId);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool IsWindowVisible(IntPtr windowHandle);

    [DllImport("user32.dll")]
    internal static extern uint GetDpiForWindow(IntPtr windowHandle);

    [DllImport("user32.dll")]
    internal static extern int GetSystemMetrics(int index);

    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool PrintWindow(IntPtr windowHandle, IntPtr deviceContext, uint flags);

    [DllImport("user32.dll", SetLastError = true)]
    internal static extern IntPtr GetWindowDC(IntPtr windowHandle);

    [DllImport("user32.dll", SetLastError = true)]
    internal static extern int ReleaseDC(IntPtr windowHandle, IntPtr deviceContext);

    [DllImport("gdi32.dll", SetLastError = true)]
    internal static extern IntPtr CreateCompatibleDC(IntPtr deviceContext);

    [DllImport("gdi32.dll", SetLastError = true)]
    internal static extern IntPtr CreateDIBSection(
        IntPtr deviceContext,
        [In] ref BitmapInfo bitmapInfo,
        uint usage,
        out IntPtr bits,
        IntPtr section,
        uint offset);

    [DllImport("gdi32.dll", SetLastError = true)]
    internal static extern IntPtr SelectObject(IntPtr deviceContext, IntPtr graphicsObject);

    [DllImport("gdi32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool DeleteObject(IntPtr graphicsObject);

    [DllImport("gdi32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool DeleteDC(IntPtr deviceContext);

    [DllImport("gdi32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool BitBlt(
        IntPtr destinationDc,
        int destinationX,
        int destinationY,
        int width,
        int height,
        IntPtr sourceDc,
        int sourceX,
        int sourceY,
        uint rasterOperation);

}
