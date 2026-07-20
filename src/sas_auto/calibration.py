"""Read-only SAS.Planet GUI inspection and calibration diagnostics."""

from __future__ import annotations

import ctypes
import json
import subprocess
import time
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .sasplanet import detect_executable_capabilities
from .state import atomic_write_json


def _monitor_layout() -> list[dict[str, int]]:
    user32 = ctypes.windll.user32
    monitors: list[dict[str, int]] = []
    monitor_enum_proc = ctypes.WINFUNCTYPE(
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.RECT),
        ctypes.c_ssize_t,
    )

    def callback(handle: int, hdc: int, rect_pointer: Any, data: float) -> int:
        rect = rect_pointer.contents
        monitors.append(
            {"left": rect.left, "top": rect.top, "right": rect.right, "bottom": rect.bottom,
             "width": rect.right - rect.left, "height": rect.bottom - rect.top}
        )
        return 1

    user32.EnumDisplayMonitors(0, 0, monitor_enum_proc(callback), 0)
    return monitors


def _window_dpi(handle: int) -> dict[str, float]:
    user32 = ctypes.windll.user32
    dpi = 96
    if hasattr(user32, "GetDpiForWindow"):
        measured = int(user32.GetDpiForWindow(handle))
        if measured > 0:
            dpi = measured
    return {"dpi": dpi, "scale_percent": round(dpi / 96.0 * 100.0, 2)}


def _control_record(control: Any) -> dict[str, Any]:
    rectangle = control.rectangle()
    try:
        text = control.window_text()
    except Exception:
        text = ""
    info = getattr(control, "element_info", None)
    return {
        "text": text,
        "control_type": getattr(info, "control_type", None),
        "class_name": getattr(info, "class_name", None),
        "automation_id": getattr(info, "automation_id", None),
        "rectangle": {
            "left": rectangle.left, "top": rectangle.top, "right": rectangle.right, "bottom": rectangle.bottom
        },
    }


def calibrate_sasplanet(
    sasplanet_exe: Path,
    project_root: Path,
    launch_timeout_seconds: float,
) -> dict[str, Any]:
    """Launch and inspect controls without importing polygons or starting downloads."""
    if not sasplanet_exe.is_file():
        raise FileNotFoundError(f"SAS.Planet executable not found: {sasplanet_exe}")
    try:
        from pywinauto import Application, Desktop
        import pyautogui
    except ImportError as error:
        raise RuntimeError("Calibration requires pywinauto, pyautogui, and Pillow. Run scripts\\setup.ps1.") from error

    pyautogui.FAILSAFE = True
    started_by_toolkit = False
    process_id: int | None = None
    desktop = Desktop(backend="uia")
    existing = [window for window in desktop.windows() if "SAS.Planet" in window.window_text()]
    if existing:
        main_window = existing[0]
        process_id = main_window.process_id()
    else:
        process = subprocess.Popen([str(sasplanet_exe)], cwd=str(sasplanet_exe.parent))
        process_id = process.pid
        started_by_toolkit = True
        deadline = time.monotonic() + launch_timeout_seconds
        main_window = None
        while time.monotonic() < deadline:
            candidates = desktop.windows(process=process_id, visible_only=True)
            if candidates:
                main_window = candidates[0]
                break
            time.sleep(0.25)
        if main_window is None:
            raise TimeoutError(f"SAS.Planet did not expose a visible window within {launch_timeout_seconds} seconds")

    main_window.wait("visible", timeout=launch_timeout_seconds)
    rectangle = main_window.rectangle()
    handle = int(main_window.handle)
    descendants = main_window.descendants()
    controls = [_control_record(item) for item in descendants[:1000]]
    all_text = "\n".join(str(item.get("text") or "") for item in controls).casefold()
    import_terms = ("kml", "kmz", "import", "импорт")
    selection_terms = ("polygon", "полигон", "selection", "выдел")
    zoom_terms = ("zoom", "масштаб", "приблиз")

    screenshots_dir = project_root / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    screenshot_path = screenshots_dir / f"{stamp}_calibration.png"
    try:
        main_window.capture_as_image().save(screenshot_path)
    except Exception:
        pyautogui.screenshot(str(screenshot_path))

    visible_windows = desktop.windows(process=process_id, visible_only=True)
    window_records = []
    for window in visible_windows:
        try:
            window_records.append(_control_record(window))
        except Exception as error:
            window_records.append({"text": "<unreadable>", "error": str(error)})
    unexpected_dialog = len(visible_windows) > 1
    status = "unexpected_dialog_requires_human" if unexpected_dialog else "inspection_complete_requires_manual_verification"
    result = {
        "schema_version": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "sasplanet_exe": str(sasplanet_exe.resolve()),
        "process_id": process_id,
        "started_by_toolkit": started_by_toolkit,
        "launch_timeout_seconds": launch_timeout_seconds,
        "window": {
            "title": main_window.window_text(),
            "handle": handle,
            "left": rectangle.left,
            "top": rectangle.top,
            "right": rectangle.right,
            "bottom": rectangle.bottom,
            "width": rectangle.width(),
            "height": rectangle.height(),
            **_window_dpi(handle),
        },
        "monitors": _monitor_layout(),
        "control_count": len(descendants),
        "controls": controls,
        "visible_process_windows": window_records,
        "allowed_window_titles": [main_window.window_text()],
        "unexpected_dialog_detected": unexpected_dialog,
        "screenshot": str(screenshot_path.resolve()),
        "executable_capabilities": detect_executable_capabilities(sasplanet_exe),
        "kml_import_control_detected": any(term in all_text for term in import_terms),
        "polygon_selection_control_detected": any(term in all_text for term in selection_terms),
        "zoom_control_detected": any(term in all_text for term in zoom_terms),
        "polygon_import_verified": False,
        "polygon_select_and_zoom_verified": False,
        "download_workflow_verified": False,
        "workflow_actions": [],
        "manual_checks_required": [
            "Confirm no unexpected dialog is present; the toolkit will not dismiss one.",
            "Confirm the generated 9101.kml can be imported as a polygon.",
            "Confirm the imported polygon can be selected and framed without a broad rectangle.",
            "Confirm the requested map source and zoom controls by visible labels.",
            "Confirm download and export dialogs before any real action mapping is approved.",
        ],
        "safety": {
            "download_started": False,
            "polygon_import_attempted": False,
            "dialogs_dismissed": False,
            "pyautogui_failsafe_enabled": True,
        },
    }
    atomic_write_json(project_root / "state" / "calibration.json", result)
    return result
