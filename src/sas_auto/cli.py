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


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _configure_windows_console() -> None:
    """Preserve Mongolian output when Python inherits a legacy Windows code page."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _load_inventory(config: dict[str, Any], root: Path) -> InspectionResult:
    dataset = config["dataset"]
    parser = config["parser"]
    return inspect_kmz(
        resolve_project_path(str(config["input_kmz"]), root),
        profile=str(dataset["profile"]),
        dataset_id=str(dataset["id"]),
        id_fields=tuple(str(value) for value in parser["id_fields"]),
        name_fields=tuple(str(value) for value in parser["name_fields"]),
    )


def _state_path(config: dict[str, Any], root: Path, dataset_id: str) -> Path:
    if bool(config["dataset"]["namespace_outputs"]):
        return root / "state" / "datasets" / f"{dataset_id}.json"
    return root / "state" / "workflow.json"


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
        "dataset_id": result.dataset_id,
        "profile": result.profile,
        "sha256": result.input_sha256,
        "archive_entries": result.archive_entries,
        "kml_members": result.kml_members,
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
    dataset_id: str,
    settings: SessionSettings | None = None,
) -> tuple[SessionArtifact, SessionSettings, dict[str, Any]]:
    resolved_settings = settings or resolve_session_settings(config, root, dataset_id)
    artifact = create_sls_session(areas, resolved_settings)
    plan_directory = root / "state" / "plans"
    if bool(config["dataset"]["namespace_outputs"]):
        plan_directory /= dataset_id
    plan = create_download_plan(
        artifact,
        areas,
        resolved_settings,
        Path(config["sasplanet_exe"]),
        root,
        plan_directory,
        max_rectangle_tiles_per_feature=int(config["safety"]["max_rectangle_tiles_per_feature"]),
        max_rectangle_tiles_total=int(config["safety"]["max_rectangle_tiles_total"]),
    )
    return artifact, resolved_settings, plan


def _record_scope(
    state: WorkflowState,
    areas: list[AreaRecord],
    status: str,
    artifact: SessionArtifact,
    details: dict[str, Any],
) -> None:
    for area in areas:
        if state.status_for(area.area_code) == "completed":
            state.record_event(
                "completed_area_status_preserved",
                {
                    "area_code": area.area_code,
                    "requested_status": status,
                    "details": details,
                },
            )
            continue
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
    outputs = generate_outputs(
        result,
        root,
        namespace_outputs=bool(config["dataset"]["namespace_outputs"]),
    )
    settings = resolve_session_settings(config, root, result.dataset_id)
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
    artifact, _, plan = _prepare_scope(areas, config, root, result.dataset_id)
    print(_json({"status": "session_ready", "session": artifact.as_dict(), "plan": plan}))
    return 0


def command_plan(args: argparse.Namespace, config: dict[str, Any], root: Path) -> int:
    result = _load_inventory(config, root)
    _require_valid_inventory(result)
    areas = _scope_from_args(args, result, config)
    _, _, plan = _prepare_scope(areas, config, root, result.dataset_id)
    print(_json(plan))
    return 0


def _run_scope(
    args: argparse.Namespace,
    config: dict[str, Any],
    root: Path,
    areas: list[AreaRecord],
    dataset_id: str,
) -> int:
    artifact, _, plan = _prepare_scope(areas, config, root, dataset_id)
    state = WorkflowState(_state_path(config, root, dataset_id))
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

    safety = plan["safety"]
    if not bool(safety["within_limits"]):
        violations = "; ".join(str(item) for item in safety["violations"])
        raise ValueError(
            "Refusing to launch a download outside configured tile-scope safety limits: "
            f"{violations} Review the plan and adjust safety limits explicitly only if the scope is intended."
        )

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
    return _run_scope(args, config, root, [_find_area(result, args.area)], result.dataset_id)


def _pilot_feature_id(config: dict[str, Any], result: InspectionResult) -> str:
    configured = str(config["safety"]["pilot_feature_id"])
    if configured.casefold() != "auto":
        return _find_area(result, configured).area_code
    for configured_code in config["area_codes"]:
        code = str(configured_code)
        if any(area.area_code == code for area in result.areas):
            return code
    if not result.areas:
        raise ValueError("Cannot choose a pilot because the KMZ contains no verified polygons")
    return result.areas[0].area_code


def _require_completed_pilot(
    config: dict[str, Any],
    root: Path,
    result: InspectionResult,
    areas: list[AreaRecord],
) -> None:
    if len(areas) <= 1 or not bool(config["safety"]["require_completed_pilot_before_multi_download"]):
        return
    pilot = _pilot_feature_id(config, result)
    state_path = _state_path(config, root, result.dataset_id)
    status = WorkflowState(state_path).status_for(pilot)
    if status != "completed":
        raise ValueError(
            f"Refusing a multi-feature download until pilot {pilot} has a validated raster export. "
            f"Current pilot status is {status!r} in {state_path}. Run and export the pilot first."
        )


def command_run_all(args: argparse.Namespace, config: dict[str, Any], root: Path) -> int:
    result = _load_inventory(config, root)
    _require_valid_inventory(result)
    areas = list(result.areas)
    if bool(args.confirm_download):
        _require_completed_pilot(config, root, result, areas)
    return _run_scope(args, config, root, areas, result.dataset_id)


def command_resume(args: argparse.Namespace, config: dict[str, Any], root: Path) -> int:
    result = _load_inventory(config, root)
    _require_valid_inventory(result)
    areas = _scope_from_args(args, result, config)
    if bool(args.confirm_download):
        _require_completed_pilot(config, root, result, areas)
    settings = resolve_session_settings(config, root, result.dataset_id)
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
    state = WorkflowState(_state_path(config, root, result.dataset_id))
    digest = hashlib.sha256(session_path.read_bytes()).hexdigest()
    for area in areas:
        if state.status_for(area.area_code) == "completed":
            state.record_event(
                "completed_area_status_preserved",
                {
                    "area_code": area.area_code,
                    "requested_status": "download_relaunched",
                    "process_id": process_id,
                    "session_sha256": digest,
                },
            )
            continue
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
    result = _load_inventory(config, root)
    state = WorkflowState(_state_path(config, root, result.dataset_id))
    print(_json(state.data))
    return 0


def command_list_features(args: argparse.Namespace, config: dict[str, Any], root: Path) -> int:
    result = _load_inventory(config, root)
    print(
        _json(
            {
                "dataset_id": result.dataset_id,
                "profile": result.profile,
                "features": [
                    {
                        "feature_id": area.feature_id,
                        "feature_name": area.feature_name,
                        "geometry_type": area.geometry_type,
                        "bounds": area.bounds.as_dict(),
                    }
                    for area in result.areas
                ],
            }
        )
    )
    return 0


def command_export(args: argparse.Namespace, config: dict[str, Any], root: Path) -> int:
    if not bool(config["export"]["enabled"]):
        raise ValueError("Raster export is disabled by export.enabled in config.yaml")
    result = _load_inventory(config, root)
    _require_valid_inventory(result)
    areas = _scope_from_args(args, result, config)
    session_settings = resolve_session_settings(config, root, result.dataset_id)
    if len(session_settings.zoom_levels) != 1:
        raise ValueError("Raster export currently requires exactly one configured imagery.zoom_levels value")
    export_settings = resolve_export_settings(config, root, result.dataset_id)
    state = WorkflowState(_state_path(config, root, result.dataset_id))
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
    group.add_argument("--area", "--feature", dest="area", help="Feature ID from the KMZ inventory")
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
    subparsers.add_parser("list-features", help="List detected polygon feature IDs and names")
    subparsers.add_parser("generate", help="Generate manifests, clean KML/GeoJSON, and all SLS files")

    session = subparsers.add_parser("session", help="Generate and validate an SLS without launching SAS.Planet")
    _add_scope_arguments(session)
    plan = subparsers.add_parser("plan", help="Generate an SLS and a no-network download plan")
    _add_scope_arguments(plan)

    run = subparsers.add_parser("run", help="Prepare one area; launch only with --confirm-download")
    run.add_argument("--area", "--feature", dest="area", required=True, help="Feature ID from the KMZ inventory")
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
    "list-features": command_list_features,
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
