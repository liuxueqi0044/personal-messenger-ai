from scripts.deployment.guest_focus_helper import _candidate_rows, _choose_window


def row(*, hwnd: int, foreground: bool = False, zoomed: bool = False,
        iconic: bool = False, width: int = 820, height: int = 612):
    return {
        "process_name": "QQ.exe",
        "class_name": "Chrome_WidgetWin_1",
        "is_visible": True,
        "is_foreground": foreground,
        "is_zoomed": zoomed,
        "is_iconic": iconic,
        "pid": 10596,
        "hwnd": hwnd,
        "rect": {"left": 90, "top": 60, "right": 90 + width, "bottom": 60 + height},
    }


def test_normal_foreground_window_is_selected_among_other_qq_windows() -> None:
    report = {"windows": [
        row(hwnd=11, foreground=False, width=840, height=600),
        row(hwnd=22, foreground=True, zoomed=False),
        row(hwnd=33, foreground=False, width=48, height=39),
    ]}

    assert _choose_window(report) == (10596, 22)


def test_maximized_foreground_window_remains_supported() -> None:
    assert _choose_window({"windows": [row(
        hwnd=22, foreground=True, zoomed=True, width=2560, height=1409
    )]}) == (10596, 22)


def test_nonforeground_multiple_usable_windows_fail_closed() -> None:
    assert _choose_window({"windows": [row(hwnd=11), row(hwnd=22)]}) is None


def test_iconic_and_tiny_windows_are_not_candidates() -> None:
    report = {"windows": [
        row(hwnd=11, foreground=True, iconic=True),
        row(hwnd=22, foreground=True, width=300, height=300),
    ]}

    assert _candidate_rows(report) == []
    assert _choose_window(report) is None
