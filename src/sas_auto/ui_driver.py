"""Calibrated GUI abstraction; never uses unrecorded screen coordinates."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class CalibrationRequired(RuntimeError):
    pass


class UnexpectedWindow(RuntimeError):
    pass


def load_calibration(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise CalibrationRequired(f"Calibration is missing: {path}. Run `python -m sas_auto.cli calibrate`.")
    try:
        calibration = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise CalibrationRequired(f"Calibration JSON is invalid: {path}") from error
    if not calibration.get("download_workflow_verified", False):
        raise CalibrationRequired(
            "Calibration has not verified polygon import/selection/download/export controls. "
            "No real GUI automation will run."
        )
    return calibration


class SASPlanetUIDriver:
    """A narrow real-run boundary kept separate from GIS parsing and planning."""

    def __init__(self, calibration_path: Path, screenshots_dir: Path) -> None:
        self.calibration_path = calibration_path
        self.screenshots_dir = screenshots_dir
        self.calibration = load_calibration(calibration_path)
        try:
            import pyautogui
        except ImportError as error:
            raise RuntimeError("pyautogui is required for calibrated GUI fail-safe support") from error
        pyautogui.FAILSAFE = True
        self.pyautogui = pyautogui

    def capture_screen(self, name: str) -> Path:
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        path = self.screenshots_dir / f"{name}.png"
        self.pyautogui.screenshot(str(path))
        return path

    @staticmethod
    def _rectangle_dict(window: Any) -> dict[str, int]:
        rectangle = window.rectangle()
        return {
            "left": rectangle.left,
            "top": rectangle.top,
            "right": rectangle.right,
            "bottom": rectangle.bottom,
            "width": rectangle.width(),
            "height": rectangle.height(),
        }

    def _assert_display_signature(self, window: Any) -> None:
        from .calibration import _monitor_layout, _window_dpi

        saved = self.calibration.get("window", {})
        current = self._rectangle_dict(window)
        for key in ("width", "height"):
            if saved.get(key) != current.get(key):
                raise CalibrationRequired(
                    f"Window {key} changed from calibrated {saved.get(key)} to {current.get(key)}. Recalibrate."
                )
        saved_monitors = self.calibration.get("monitors")
        if not isinstance(saved_monitors, list) or not saved_monitors:
            raise CalibrationRequired("Calibration has no monitor signature; coordinate fallback is unsafe")
        if saved_monitors != _monitor_layout():
            raise CalibrationRequired("Monitor layout differs from calibration; coordinate fallback is unsafe")
        current_dpi = _window_dpi(int(window.handle))
        if saved.get("dpi") != current_dpi.get("dpi"):
            raise CalibrationRequired(
                f"Window DPI changed from calibrated {saved.get('dpi')} to {current_dpi.get('dpi')}. Recalibrate."
            )

    def _assert_no_unknown_windows(self, desktop: Any, process_id: int, allowed_titles: list[str]) -> None:
        unknown = []
        for window in desktop.windows(process=process_id, visible_only=True):
            title = window.window_text()
            if title and not any(allowed.casefold() in title.casefold() for allowed in allowed_titles):
                unknown.append(title)
        if unknown:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            diagnostic = self.capture_screen(f"{stamp}_unexpected_window")
            raise UnexpectedWindow(f"Unexpected visible window(s): {unknown}. Screenshot: {diagnostic}")

    def _perform_action(self, action: dict[str, Any], window: Any) -> None:
        kind = action.get("kind")
        if kind in {"uia_click", "win32_click"}:
            criteria = action.get("criteria")
            if not isinstance(criteria, dict) or not criteria:
                raise CalibrationRequired(f"Control action has no calibrated criteria: {action}")
            control = window.child_window(**criteria)
            control.wait("visible enabled", timeout=float(action.get("timeout_seconds", 15)))
            control.click_input()
            return
        if kind == "menu_select":
            path = action.get("path")
            if not isinstance(path, str) or not path:
                raise CalibrationRequired("menu_select action is missing a calibrated menu path")
            window.menu_select(path)
            return
        if kind == "send_keys":
            keys = action.get("keys")
            if not isinstance(keys, str) or not keys:
                raise CalibrationRequired("send_keys action is missing keys")
            from pywinauto.keyboard import send_keys

            send_keys(keys, pause=float(action.get("key_pause_seconds", 0.05)), with_spaces=True)
            return
        if kind == "image_anchor":
            image_path = Path(str(action.get("image_path", "")))
            if not image_path.is_absolute():
                image_path = self.calibration_path.parent / image_path
            if not image_path.is_file():
                raise CalibrationRequired(f"Calibrated image anchor is missing: {image_path}")
            confidence = action.get("confidence")
            kwargs = {"confidence": confidence} if confidence is not None else {}
            location = self.pyautogui.locateCenterOnScreen(str(image_path), **kwargs)
            if location is None:
                raise CalibrationRequired(f"Image anchor was not found: {image_path}")
            self.pyautogui.click(location.x, location.y)
            return
        if kind == "coordinate_click":
            self._assert_display_signature(window)
            point = action.get("point")
            if not isinstance(point, dict) or not isinstance(point.get("x"), int) or not isinstance(point.get("y"), int):
                raise CalibrationRequired("coordinate_click lacks a calibrated integer point")
            self.pyautogui.click(point["x"], point["y"])
            return
        raise CalibrationRequired(f"Unsupported calibrated action kind: {kind!r}")

    def execute_confirmed_workflow(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Execute only explicitly calibrated actions after the CLI confirmation gate.

        Calibration data must contain a workflow_actions list whose entries identify
        UIA/Win32 controls or named image anchors. Absolute coordinates are accepted
        only when calibration records display geometry and DPI for comparison.
        """
        actions = self.calibration.get("workflow_actions")
        if not isinstance(actions, list) or not actions:
            raise CalibrationRequired(
                "Calibration does not contain verified workflow_actions. "
                "The confirmed download remains intentionally blocked."
            )
        try:
            from pywinauto import Desktop
        except ImportError as error:
            raise RuntimeError("pywinauto is required for verified UI action playback") from error

        executable = Path(plan["sasplanet_exe"])
        process = subprocess.Popen([str(executable)], cwd=str(executable.parent))
        desktop = Desktop(backend="uia")
        deadline = time.monotonic() + float(self.calibration.get("launch_timeout_seconds", 30))
        window = None
        while time.monotonic() < deadline:
            candidates = desktop.windows(process=process.pid, visible_only=True)
            if candidates:
                window = candidates[0]
                break
            time.sleep(0.25)
        if window is None:
            raise TimeoutError("SAS.Planet did not expose a visible window before the calibrated timeout")

        allowed_titles = list(self.calibration.get("allowed_window_titles") or [])
        main_title = window.window_text()
        if main_title:
            allowed_titles.append(main_title)
        screenshots: list[str] = []
        for index, action in enumerate(actions, start=1):
            if not isinstance(action, dict):
                raise CalibrationRequired(f"Workflow action {index} is not a mapping")
            for title in action.get("allowed_window_titles", []):
                if title not in allowed_titles:
                    allowed_titles.append(title)
            self._assert_no_unknown_windows(desktop, process.pid, allowed_titles)
            if action.get("network_boundary"):
                screenshots.append(str(self.capture_screen(f"{plan['area']['area_code']}_pre_network")))
            self._perform_action(action, window)
            time.sleep(float(action.get("delay_seconds", 1.0)))
            if action.get("capture_after", True):
                screenshots.append(str(self.capture_screen(f"{plan['area']['area_code']}_step_{index:02d}")))
            self._assert_no_unknown_windows(desktop, process.pid, allowed_titles)
        return {"process_id": process.pid, "screenshots": screenshots, "action_count": len(actions)}
