"""Read-only SAS.Planet discovery and dry-run planning."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .geometry import buffered_bounds, estimate_bbox_tiles
from .models import AreaRecord
from .state import atomic_write_json


@dataclass(frozen=True)
class MapDefinition:
    name: str
    guid: str
    params_path: str
    active: bool


def _read_text_fallback(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def discover_map_definitions(sasplanet_exe: Path) -> list[MapDefinition]:
    maps_root = sasplanet_exe.parent / "Maps"
    if not maps_root.is_dir():
        raise FileNotFoundError(f"SAS.Planet Maps directory not found: {maps_root}")
    definitions: list[MapDefinition] = []
    for params_path in maps_root.rglob("params.txt"):
        text = _read_text_fallback(params_path)
        name_match = re.search(r"(?im)^\s*name\s*=\s*(.+?)\s*$", text)
        guid_match = re.search(r"(?im)^\s*guid\s*=\s*(\{[0-9a-f-]{36}\})\s*$", text)
        if not name_match or not guid_match:
            continue
        relative_parts = [part.casefold() for part in params_path.relative_to(maps_root).parts]
        active = "_broken_maps" not in relative_parts
        definitions.append(
            MapDefinition(
                name=name_match.group(1).strip(),
                guid=guid_match.group(1).upper(),
                params_path=str(params_path.resolve()),
                active=active,
            )
        )
    return sorted(definitions, key=lambda item: item.name.casefold())


def _normal_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


SOURCE_ALIASES = {
    "googlesatellite": {"googlesatellite"},
    "esriworldimagery": {"esriworldimagery", "esriarcgisimagery"},
}


def find_requested_provider(definitions: list[MapDefinition], requested: str) -> MapDefinition | None:
    requested_key = _normal_name(requested)
    accepted = SOURCE_ALIASES.get(requested_key, {requested_key})
    for definition in definitions:
        if definition.active and _normal_name(definition.name) in accepted:
            return definition
    return None


def detect_executable_capabilities(sasplanet_exe: Path) -> dict[str, bool]:
    """Read command tokens from the executable without launching or modifying it."""
    data = sasplanet_exe.read_bytes()
    flags = [
        "--map",
        "--zoom",
        "--move",
        "--move-xyz",
        "--navigate",
        "--show-placemarks",
        "--insert-placemark",
        "--insert-placemark-with-icon",
        "--sls-autostart",
    ]
    return {flag: flag.encode("utf-16le") in data or flag.encode("ascii") in data for flag in flags}


def create_area_plan(
    area: AreaRecord,
    config: dict[str, Any],
    project_root: Path,
    sasplanet_exe: Path,
) -> dict[str, Any]:
    definitions = discover_map_definitions(sasplanet_exe)
    requested = config["imagery"]["preferred_sources"][0]
    provider = find_requested_provider(definitions, requested)
    if provider is None:
        active_names = [item.name for item in definitions if item.active]
        preferred_alternatives = [
            name for name in config["imagery"]["preferred_sources"][1:]
            if find_requested_provider(definitions, name) is not None
        ]
        raise ValueError(
            f"Requested provider {requested!r} is unavailable. No provider was substituted. "
            f"Configured alternatives detected: {preferred_alternatives or 'none'}. "
            f"Review available definitions in the plan diagnostics ({len(active_names)} active maps)."
        )
    buffer_percent = float(config["imagery"]["buffer_percent"])
    scope_bounds = buffered_bounds(area.bounds, buffer_percent)
    estimates = []
    for zoom in config["imagery"]["zoom_levels"]:
        estimate = estimate_bbox_tiles(scope_bounds, zoom)
        estimates.append({"zoom": zoom, **estimate})
    output_directory = project_root / "output" / area.area_code
    calibration_path = project_root / "state" / "calibration.json"
    calibration_status = "not_run"
    if calibration_path.is_file():
        try:
            calibration_status = json.loads(calibration_path.read_text(encoding="utf-8")).get("status", "unknown")
        except (OSError, json.JSONDecodeError):
            calibration_status = "invalid"
    plan = {
        "schema_version": 1,
        "mode": "dry-run-plan",
        "network_download_started": False,
        "area": area.manifest_dict(),
        "generated_kml": str((project_root / "generated" / "kml" / f"{area.area_code}.kml").resolve()),
        "sasplanet_exe": str(sasplanet_exe.resolve()),
        "executable_capabilities": detect_executable_capabilities(sasplanet_exe),
        "requested_provider": requested,
        "resolved_provider": asdict(provider),
        "zoom_levels": list(config["imagery"]["zoom_levels"]),
        "buffer_percent": buffer_percent,
        "buffered_bounds": scope_bounds.as_dict(),
        "conservative_rectangle_tile_estimates": estimates,
        "download_missing_tiles_only": bool(config["imagery"]["download_missing_tiles_only"]),
        "export": dict(config["export"]),
        "output_directory": str(output_directory.resolve()),
        "calibration_status": calibration_status,
        "steps": [
            "Load/import the generated one-area KML.",
            "Verify the visible code and Mongolian area name.",
            "Frame the polygon using the configured buffer.",
            f"Select exactly the configured provider: {provider.name}.",
            "Prefer polygon selection; stop if only an unexpectedly broad rectangle is selected.",
            "Review the tile estimate and capture a pre-operation screenshot.",
            "Require --confirm-download before the first real network operation.",
            "Download only the configured zoom levels and missing tiles.",
            "Export only when export.enabled is true and georeferencing is verified.",
            "Validate non-empty outputs, capture a post-operation screenshot, and atomically record state.",
        ],
        "safety_notes": [
            "The tile estimate is a conservative bounding rectangle; actual polygon selection may use fewer tiles.",
            "No imagery provider fallback is automatic.",
            "No SAS.Planet cache or Maps files will be modified by this toolkit.",
            "Real GUI steps remain blocked until calibration records a verified workflow.",
        ],
    }
    plan_path = project_root / "state" / "plans" / f"{area.area_code}.json"
    atomic_write_json(plan_path, plan)
    plan["plan_path"] = str(plan_path.resolve())
    return plan
