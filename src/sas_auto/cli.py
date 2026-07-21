"""Command-line interface for KMZ validation and SAS.Planet SLS downloads."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from .config import load_config, resolve_project_path
from .kmz_parser import generate_outputs, inspect_kmz
from .logging_setup import configure_logging
from .models import AreaRecord, InspectionResult
from .raster_export import export_area_from_cache, resolve_export_settings, write_batch_outputs
from .sasplanet import (
    SessionArtifact,
    SessionSettings,
    create_download_plan,
    create_sls_session,
    launch_sls_session,
    resolve_session_settings,
    sasplanet_command,
    session_path_for,
    validate_sls_session,
)
from .state import WorkflowState
from .validation import EXPECTED_AREA_CODES


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _configure_windows_console() -> None:
    """Preserve Mongolian output when Python inherits a legacy Windows code page."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _load_inventory(config: dict[str, Any], root: Path) -> InspectionResult:
    return inspect_kmz(resolve_project_path(str(config["input_kmz"]), root))


def _require_valid_inventory(result: InspectionResult) -> None:
    if result.errors:
        messages = "; ".join(message.message for message in result.errors)
        raise ValueError(f"SLS generation refused because KMZ validation failed: {messages}")


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


def _select_areas(
    result: InspectionResult,
    config: dict[str, Any],
    *,
    area_code: str | None,
    all_areas: bool,
    configured: bool,
) -> list[AreaRecord]:
    if all_areas:
        return list(result.areas)
    if configured:
        return [_find_area(result, str(code)) for code in config["area_codes"]]
    if area_code is None:
        raise ValueError("Choose --area, --configured, or --all")
    return [_find_area(result, area_code)]


def _scope_from_args(args: argparse.Namespace, result: InspectionResult, config: dict[str, Any]) -> list[AreaRecord]:
    return _select_areas(
        result,
        config,
        area_code=getattr(args, "area", None),
        all_areas=bool(getattr(args, "all", False)),
        configured=bool(getattr(args, "configured", False)),
    )


def _prepare_scope(
    areas: list[AreaRecord],
    config: dict[str, Any],
    root: Path,
    settings: SessionSettings | None = None,
) -> tuple[SessionArtifact, SessionSettings, dict[str, Any]]:
    resolved_settings = settings or resolve_session_settings(config, root)
    artifact = create_sls_session(areas, resolved_settings)
    plan = create_download_plan(artifact, areas, resolved_settings, Path(config["sasplanet_exe"]), root)
    return artifact, resolved_settings, plan


def _record_scope(
    state: WorkflowState,
    areas: list[AreaRecord],
    status: str,
    artifact: SessionArtifact,
    details: dict[str, Any],
) -> None:
    for area in areas:
        state.record_area(
            area.area_code,
            status,
            provider=artifact.provider_name,
            zoom_levels=list(artifact.zoom_levels),
            output_paths=[artifact.path],
            details=details,
        )


def command_inspect(args: argparse.Namespace, config: dict[str, Any], root: Path) -> int:
    result = _load_inventory(config, root)
    print(_json(_inventory_summary(result)))
    return 2 if result.errors else 0


def command_generate(args: argparse.Namespace, config: dict[str, Any], root: Path) -> int:
    result = _load_inventory(config, root)
    _require_valid_inventory(result)
    outputs = generate_outputs(result, root)
    settings = resolve_session_settings(config, root)
    individual = [create_sls_session([area], settings).as_dict() for area in result.areas]
    combined = create_sls_session(result.areas, settings).as_dict()
    print(
        _json(
            {
                "generated": outputs,
                "sls": {"individual": individual, "combined": combined},
                "inventory": _inventory_summary(result),
                "network_download_started": False,
            }
        )
    )
    return 0


def command_session(args: argparse.Namespace, config: dict[str, Any], root: Path) -> int:
    result = _load_inventory(config, root)
    _require_valid_inventory(result)
    areas = _scope_from_args(args, result, config)
    artifact, _, plan = _prepare_scope(areas, config, root)
    print(_json({"status": "session_ready", "session": artifact.as_dict(), "plan": plan}))
    return 0


def command_plan(args: argparse.Namespace, config: dict[str, Any], root: Path) -> int:
    result = _load_inventory(config, root)
    _require_valid_inventory(result)
    areas = _scope_from_args(args, result, config)
    _, _, plan = _prepare_scope(areas, config, root)
    print(_json(plan))
    return 0


def _run_scope(
    args: argparse.Namespace,
    config: dict[str, Any],
    root: Path,
    areas: list[AreaRecord],
) -> int:
    artifact, _, plan = _prepare_scope(areas, config, root)
    state = WorkflowState(root / "state" / "workflow.json")
    if not bool(args.confirm_download):
        _record_scope(
            state,
            areas,
            "session_ready",
            artifact,
            {
                "plan_path": plan["plan_path"],
                "network_download_started": False,
                "explicit_confirmation_required": True,
            },
        )
        print(
            _json(
                {
                    "status": "dry_run",
                    "network_download_started": False,
                    "session": artifact.as_dict(),
                    "command_after_review": plan["command"],
                }
            )
        )
        return 0

    process_id = launch_sls_session(Path(config["sasplanet_exe"]), Path(artifact.path))
    details = {
        "plan_path": plan["plan_path"],
        "process_id": process_id,
        "command": plan["command"],
        "network_download_started": True,
        "completion_is_reported_by_sasplanet": True,
    }
    _record_scope(state, areas, "download_launched", artifact, details)
    print(
        _json(
            {
                "status": "download_launched",
                "process_id": process_id,
                "session": artifact.as_dict(),
                "command": plan["command"],
            }
        )
    )
    return 0


def command_run(args: argparse.Namespace, config: dict[str, Any], root: Path) -> int:
    result = _load_inventory(config, root)
    _require_valid_inventory(result)
    return _run_scope(args, config, root, [_find_area(result, args.area)])


def command_run_all(args: argparse.Namespace, config: dict[str, Any], root: Path) -> int:
    result = _load_inventory(config, root)
    _require_valid_inventory(result)
    return _run_scope(args, config, root, list(result.areas))


def command_resume(args: argparse.Namespace, config: dict[str, Any], root: Path) -> int:
    result = _load_inventory(config, root)
    _require_valid_inventory(result)
    areas = _scope_from_args(args, result, config)
    settings = resolve_session_settings(config, root)
    session_path = session_path_for(settings, areas)
    if not session_path.is_file():
        raise FileNotFoundError(
            f"Prepared SLS session not found: {session_path}. Run the matching session command first."
        )
    errors = validate_sls_session(session_path)
    if errors:
        raise ValueError("Stored SLS session is invalid: " + "; ".join(errors))
    command = sasplanet_command(Path(config["sasplanet_exe"]), session_path)
    if not bool(args.confirm_download):
        print(
            _json(
                {
                    "status": "resume_dry_run",
                    "network_download_started": False,
                    "session": str(session_path),
                    "command_after_review": command,
                    "note": "Existing cached tiles will be skipped because ReplaceExistTiles=0.",
                }
            )
        )
        return 0
    process_id = launch_sls_session(Path(config["sasplanet_exe"]), session_path)
    state = WorkflowState(root / "state" / "workflow.json")
    digest = hashlib.sha256(session_path.read_bytes()).hexdigest()
    for area in areas:
        state.record_area(
            area.area_code,
            "download_relaunched",
            provider=settings.provider.name,
            zoom_levels=list(settings.zoom_levels),
            output_paths=[str(session_path.resolve())],
            details={"process_id": process_id, "session_sha256": digest, "command": command},
        )
    print(_json({"status": "download_relaunched", "process_id": process_id, "command": command}))
    return 0


def command_status(args: argparse.Namespace, config: dict[str, Any], root: Path) -> int:
    state = WorkflowState(root / "state" / "workflow.json")
    print(_json(state.data))
    return 0


def command_export(args: argparse.Namespace, config: dict[str, Any], root: Path) -> int:
    if not bool(config["export"]["enabled"]):
        raise ValueError("Raster export is disabled by export.enabled in config.yaml")
    result = _load_inventory(config, root)
    _require_valid_inventory(result)
    areas = _scope_from_args(args, result, config)
    session_settings = resolve_session_settings(config, root)
    if len(session_settings.zoom_levels) != 1:
        raise ValueError("Raster export currently requires exactly one configured imagery.zoom_levels value")
    export_settings = resolve_export_settings(config, root)
    state = WorkflowState(root / "state" / "workflow.json")
    completed: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    for area in areas:
        try:
            validation = export_area_from_cache(
                area,
                Path(config["sasplanet_exe"]),
                session_settings.provider,
                session_settings.zoom_levels[0],
                export_settings,
            )
            if validation["validation_status"] != "passed":
                raise ValueError(f"Raster validation failed for area {area.area_code}")
            output_paths = [str(path) for path in validation["output_paths"]]
            state.record_area(
                area.area_code,
                "completed",
                provider=session_settings.provider.name,
                zoom_levels=list(session_settings.zoom_levels),
                output_paths=output_paths,
                details={
                    "validation_path": validation["validation_path"],
                    "raster_path": validation["raster"]["path"],
                    "cached_tile_count": validation["cache"]["cached_tile_count"],
                    "missing_tile_count": validation["cache"]["missing_tile_count"],
                },
            )
            completed.append(validation)
        except Exception as error:
            state.record_area(
                area.area_code,
                "failed",
                provider=session_settings.provider.name,
                zoom_levels=list(session_settings.zoom_levels),
                error=str(error),
                details={"stage": "cache_to_raster_export"},
            )
            failed.append({"area_code": area.area_code, "error": str(error)})
            if len(areas) == 1:
                raise

    batch_outputs = write_batch_outputs(export_settings.output_directory, list(result.areas))
    print(
        _json(
            {
                "status": "completed" if not failed else "completed_with_failures",
                "network_download_started": False,
                "completed": completed,
                "failed": failed,
                "batch_outputs": batch_outputs,
            }
        )
    )
    return 1 if failed else 0


def _add_scope_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--area", choices=sorted(EXPECTED_AREA_CODES))
    group.add_argument("--configured", action="store_true", help="Use area_codes from config.yaml")
    group.add_argument("--all", action="store_true", help="Use every verified polygon in the KMZ")


def _add_run_mode(parser: argparse.ArgumentParser) -> None:
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Prepare and validate the SLS without launching")
    mode.add_argument(
        "--confirm-download",
        action="store_true",
        help="Explicitly launch SAS.Planet with --sls-autostart (may use the network)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Configuration YAML path (default: project config.yaml)")
    parser.add_argument("--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("inspect", help="Inspect and validate the source KMZ")
    subparsers.add_parser("generate", help="Generate manifests, clean KML/GeoJSON, and all SLS files")

    session = subparsers.add_parser("session", help="Generate and validate an SLS without launching SAS.Planet")
    _add_scope_arguments(session)
    plan = subparsers.add_parser("plan", help="Generate an SLS and a no-network download plan")
    _add_scope_arguments(plan)

    run = subparsers.add_parser("run", help="Prepare one area; launch only with --confirm-download")
    run.add_argument("--area", required=True, choices=sorted(EXPECTED_AREA_CODES))
    _add_run_mode(run)
    run_all = subparsers.add_parser("run-all", help="Prepare all polygons in one SLS session")
    _add_run_mode(run_all)

    resume = subparsers.add_parser("resume", help="Relaunch an existing SLS; cached tiles are skipped")
    _add_scope_arguments(resume)
    resume.add_argument("--confirm-download", action="store_true")
    export = subparsers.add_parser("export", help="Export and validate cached tiles as georeferenced rasters")
    _add_scope_arguments(export)
    subparsers.add_parser("status", help="Show persisted launch state")
    return parser


COMMANDS = {
    "inspect": command_inspect,
    "generate": command_generate,
    "session": command_session,
    "plan": command_plan,
    "run": command_run,
    "run-all": command_run_all,
    "resume": command_resume,
    "export": command_export,
    "status": command_status,
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
        print("Stopped by Ctrl+C.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
