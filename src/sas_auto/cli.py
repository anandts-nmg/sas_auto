"""Command-line interface for inspection, generation, planning, and safe execution."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .calibration import calibrate_sasplanet
from .config import load_config, resolve_project_path
from .earth_pro import generated_kml_path, locate_google_earth, open_for_verification
from .kmz_parser import generate_outputs, inspect_kmz
from .logging_setup import configure_logging
from .models import AreaRecord, InspectionResult
from .sasplanet import create_area_plan
from .state import WorkflowState
from .ui_driver import CalibrationRequired, SASPlanetUIDriver, UnexpectedWindow
from .validation import EXPECTED_AREA_CODES, validate_output_files


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _configure_windows_console() -> None:
    """Preserve Mongolian output when Python inherits a legacy Windows code page."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _load_inventory(config: dict[str, Any], root: Path) -> InspectionResult:
    return inspect_kmz(resolve_project_path(config["input_kmz"], root))


def _find_area(result: InspectionResult, area_code: str) -> AreaRecord:
    for area in result.areas:
        if area.area_code == area_code:
            return area
    raise ValueError(f"Area code not found in verified KMZ polygons: {area_code}")


def _inventory_summary(result: InspectionResult) -> dict[str, Any]:
    return {
        "input_path": result.input_path,
        "sha256": result.input_sha256,
        "archive_entries": result.archive_entries,
        "placemarks": result.placemark_count,
        "polygon_placemarks": result.polygon_placemark_count,
        "point_placemarks": result.point_placemark_count,
        "vertex_point_placemarks": result.vertex_point_count,
        "auxiliary_point_placemarks": result.auxiliary_point_count,
        "area_codes": [area.area_code for area in result.areas],
        "warnings": [item.as_dict() for item in result.warnings],
        "errors": [item.as_dict() for item in result.errors],
        "notes": [item.as_dict() for item in result.validation_messages if item.severity == "info"],
    }


def _plan_one(
    result: InspectionResult, config: dict[str, Any], root: Path, area_code: str
) -> dict[str, Any]:
    if area_code not in EXPECTED_AREA_CODES:
        raise ValueError(f"Unsupported area code: {area_code}")
    kml_path = root / "generated" / "kml" / f"{area_code}.kml"
    if not kml_path.is_file():
        raise FileNotFoundError(f"Generated KML missing: {kml_path}. Run `python -m sas_auto.cli generate`.")
    area = _find_area(result, area_code)
    sasplanet_exe = Path(config["sasplanet_exe"])
    return create_area_plan(area, config, root, sasplanet_exe)


def command_inspect(args: argparse.Namespace, config: dict[str, Any], root: Path) -> int:
    result = _load_inventory(config, root)
    print(_json(_inventory_summary(result)))
    return 2 if result.errors else 0


def command_generate(args: argparse.Namespace, config: dict[str, Any], root: Path) -> int:
    result = _load_inventory(config, root)
    outputs = generate_outputs(result, root)
    print(_json({"generated": outputs, "inventory": _inventory_summary(result)}))
    return 0


def command_earth_open(args: argparse.Namespace, config: dict[str, Any], root: Path) -> int:
    executable = locate_google_earth(config.get("google_earth_exe"))
    if executable is None:
        raise FileNotFoundError("Google Earth Pro was not found. Run scripts\\inspect_environment.ps1 and update config.yaml.")
    path = generated_kml_path(root, args.area, args.all)
    process_id = open_for_verification(executable, path)
    print(_json({"executable": str(executable), "kml": str(path.resolve()), "process_id": process_id,
                 "purpose": "manual visual verification only"}))
    return 0


def command_calibrate(args: argparse.Namespace, config: dict[str, Any], root: Path) -> int:
    result = calibrate_sasplanet(
        Path(config["sasplanet_exe"]), root, float(config["automation"]["launch_timeout_seconds"])
    )
    print(_json({key: value for key, value in result.items() if key != "controls"}))
    return 3 if result["status"].startswith("unexpected_dialog") else 0


def command_plan(args: argparse.Namespace, config: dict[str, Any], root: Path) -> int:
    result = _load_inventory(config, root)
    plan = _plan_one(result, config, root, args.area)
    print(_json(plan))
    return 0


def _run_dry(
    result: InspectionResult,
    config: dict[str, Any],
    root: Path,
    state: WorkflowState,
    area_code: str,
) -> dict[str, Any]:
    plan = _plan_one(result, config, root, area_code)
    state.record_area(
        area_code,
        "dry_run_completed",
        provider=plan["resolved_provider"]["name"],
        zoom_levels=plan["zoom_levels"],
        details={"plan_path": plan["plan_path"], "network_download_started": False},
    )
    return plan


def command_run(args: argparse.Namespace, config: dict[str, Any], root: Path) -> int:
    result = _load_inventory(config, root)
    state = WorkflowState(root / "state" / "workflow.json")
    explicit_real = bool(args.confirm_download)
    dry_run = bool(args.dry_run) or (not explicit_real and bool(config["dry_run"]))
    if dry_run:
        plan = _run_dry(result, config, root, state, args.area)
        print(_json({"status": "dry_run_completed", "network_download_started": False, "plan": plan}))
        return 0

    if not explicit_real:
        raise ValueError("A real run requires the explicit --confirm-download flag")
    if args.area != "9101":
        raise ValueError("The first real validation run is restricted to area 9101")
    plan = _plan_one(result, config, root, args.area)
    state.record_area(
        args.area,
        "confirmed_pending_calibrated_actions",
        provider=plan["resolved_provider"]["name"],
        zoom_levels=plan["zoom_levels"],
        details={"explicit_confirmation_received": True, "network_download_started": False},
    )
    try:
        driver = SASPlanetUIDriver(root / "state" / "calibration.json", root / "screenshots")
        driver_result = driver.execute_confirmed_workflow(plan)
    except CalibrationRequired as error:
        state.record_area(
            args.area,
            "awaiting_calibration",
            provider=plan["resolved_provider"]["name"],
            zoom_levels=plan["zoom_levels"],
            error=str(error),
            details={"explicit_confirmation_received": True, "network_download_started": False},
        )
        print(_json({"status": "blocked_safely", "reason": str(error), "network_download_started": False}))
        return 3
    except UnexpectedWindow as error:
        state.record_area(
            args.area,
            "failed_unexpected_window",
            provider=plan["resolved_provider"]["name"],
            zoom_levels=plan["zoom_levels"],
            error=str(error),
            details={"network_download_started": "unknown; inspect the diagnostic screenshot"},
        )
        print(_json({"status": "stopped_on_unexpected_window", "reason": str(error)}))
        return 4
    except Exception as error:
        state.record_area(
            args.area,
            "failed",
            provider=plan["resolved_provider"]["name"],
            zoom_levels=plan["zoom_levels"],
            error=str(error),
            details={"network_download_started": "unknown; inspect SAS.Planet and logs"},
        )
        raise
    output_paths: list[Path] = []
    if config["export"]["enabled"]:
        output_directory = root / "output" / args.area
        output_paths = [path for path in output_directory.rglob("*") if path.is_file()]
        output_errors = validate_output_files(output_paths)
        if output_errors:
            state.record_area(
                args.area,
                "failed",
                provider=plan["resolved_provider"]["name"],
                zoom_levels=plan["zoom_levels"],
                output_paths=[str(path.resolve()) for path in output_paths],
                error="; ".join(output_errors),
                details=driver_result,
            )
            raise RuntimeError("Output validation failed: " + "; ".join(output_errors))
    state.record_area(
        args.area,
        "completed",
        provider=plan["resolved_provider"]["name"],
        zoom_levels=plan["zoom_levels"],
        output_paths=[str(path.resolve()) for path in output_paths],
        details={**driver_result, "export_enabled": bool(config["export"]["enabled"])},
    )
    print(_json({"status": "completed", "area_code": args.area, "outputs": [str(path) for path in output_paths]}))
    return 0


def command_run_all(args: argparse.Namespace, config: dict[str, Any], root: Path) -> int:
    if not args.dry_run:
        raise ValueError("Bulk real downloads are not implemented or permitted in this initial toolkit; use --dry-run")
    result = _load_inventory(config, root)
    state = WorkflowState(root / "state" / "workflow.json")
    if state.status_for("9101") != "dry_run_completed":
        raise ValueError("Run and validate the area 9101 dry run before planning all areas")
    plans = []
    for area in result.areas:
        plans.append(_run_dry(result, config, root, state, area.area_code))
    print(_json({"status": "dry_run_completed", "areas": [plan["area"]["area_code"] for plan in plans],
                 "network_download_started": False}))
    return 0


def command_status(args: argparse.Namespace, config: dict[str, Any], root: Path) -> int:
    state = WorkflowState(root / "state" / "workflow.json")
    print(_json(state.data))
    return 0


def command_resume(args: argparse.Namespace, config: dict[str, Any], root: Path) -> int:
    if not config["dry_run"]:
        raise ValueError("Automatic resume is allowed only while config.yaml has dry_run: true")
    result = _load_inventory(config, root)
    state = WorkflowState(root / "state" / "workflow.json")
    configured_codes = [str(code) for code in config["area_codes"]]
    area_code = state.next_incomplete(configured_codes)
    if area_code is None:
        print(_json({"status": "nothing_to_resume", "area_codes": configured_codes}))
        return 0
    plan = _run_dry(result, config, root, state, area_code)
    print(_json({"status": "dry_run_resumed", "area_code": area_code, "plan": plan,
                 "network_download_started": False}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Configuration YAML path (default: project config.yaml)")
    parser.add_argument("--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("inspect", help="Inspect and validate the source KMZ")
    subparsers.add_parser("generate", help="Generate manifests, GeoJSON, and clean KML files")

    earth = subparsers.add_parser("earth-open", help="Open generated KML in Google Earth Pro")
    earth_group = earth.add_mutually_exclusive_group(required=True)
    earth_group.add_argument("--area", choices=sorted(EXPECTED_AREA_CODES))
    earth_group.add_argument("--all", action="store_true")

    subparsers.add_parser("calibrate", help="Safely inspect SAS.Planet UI without downloading")
    plan = subparsers.add_parser("plan", help="Create a no-network plan for one area")
    plan.add_argument("--area", required=True, choices=sorted(EXPECTED_AREA_CODES))

    run = subparsers.add_parser("run", help="Run one area; dry-run unless explicitly confirmed")
    run.add_argument("--area", required=True, choices=sorted(EXPECTED_AREA_CODES))
    run_mode = run.add_mutually_exclusive_group()
    run_mode.add_argument("--dry-run", action="store_true")
    run_mode.add_argument("--confirm-download", action="store_true")

    run_all = subparsers.add_parser("run-all", help="Plan all areas after the 9101 pilot dry run")
    run_all.add_argument("--dry-run", action="store_true", required=True)
    subparsers.add_parser("status", help="Show persisted workflow state")
    subparsers.add_parser("resume", help="Safely resume configured dry-run areas")
    return parser


COMMANDS = {
    "inspect": command_inspect,
    "generate": command_generate,
    "earth-open": command_earth_open,
    "calibrate": command_calibrate,
    "plan": command_plan,
    "run": command_run,
    "run-all": command_run_all,
    "status": command_status,
    "resume": command_resume,
}


def main(argv: list[str] | None = None) -> int:
    _configure_windows_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config, root = load_config(args.config)
        logger, log_path = configure_logging(root, args.command, args.verbose)
        logger.debug("Using configuration %s", args.config or (root / "config.yaml"))
        code = COMMANDS[args.command](args, config, root)
        logger.info("Log: %s", log_path)
        return code
    except KeyboardInterrupt:
        print("Stopped safely by Ctrl+C. No unknown dialog was dismissed.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
