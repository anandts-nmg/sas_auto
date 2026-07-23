"""Validation rules for source data and user configuration."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from .models import AreaRecord, ValidationMessage

EXPECTED_AREA_CODES = {str(value) for value in range(9101, 9121)}
WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
WEB_MERCATOR_MAX_LATITUDE = 85.05112878


def is_portable_windows_component(value: str) -> bool:
    """Return whether a value is safe as one cross-platform path component."""
    if not value or len(value) > 100 or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) is None:
        return False
    return value.split(".", 1)[0].casefold() not in WINDOWS_RESERVED_NAMES


def validate_areas(
    areas: list[AreaRecord],
    vertex_point_counts: Counter[str],
    *,
    profile: str = "tender_areas",
    expected_codes: set[str] | None = None,
) -> list[ValidationMessage]:
    messages: list[ValidationMessage] = []
    codes = [area.area_code for area in areas]
    code_counts = Counter(codes)

    for code, count in sorted(code_counts.items()):
        if count > 1:
            messages.append(
                ValidationMessage("error", "duplicate_area_code", f"Area code {code} occurs {count} times.", code)
            )
    if expected_codes is not None:
        actual_codes = set(codes)
        missing = sorted(expected_codes - actual_codes)
        unexpected = sorted(actual_codes - expected_codes)
        if missing:
            messages.append(
                ValidationMessage("error", "missing_area_codes", f"Missing expected area codes: {', '.join(missing)}")
            )
        if unexpected:
            messages.append(
                ValidationMessage("error", "unexpected_area_codes", f"Unexpected area codes: {', '.join(unexpected)}")
            )

    closure_differences = 0
    for area in areas:
        code = area.area_code
        if not code.strip():
            messages.append(ValidationMessage("error", "empty_feature_id", "Feature ID is empty.", code))
        if not is_portable_windows_component(code):
            messages.append(
                ValidationMessage(
                    "error",
                    "non_portable_feature_id",
                    f"Feature ID {code!r} is unsafe for portable output paths.",
                    code,
                )
            )
        if profile == "tender_areas":
            if not area.tender_number:
                messages.append(ValidationMessage("error", "missing_tender_number", "Tender number is missing.", code))
            expected_suffix = -1
            if code.isdigit() and area.tender_number.isdigit():
                tender_base = int(area.tender_number) * (10 ** max(2, len(code) - len(area.tender_number)))
                expected_suffix = int(code) - tender_base
            expected_prefix = (
                f"{area.tender_number}_{expected_suffix:04d}_" if area.tender_number and expected_suffix > 0 else ""
            )
            if expected_prefix and area.placemark_name and not area.placemark_name.startswith(expected_prefix):
                messages.append(
                    ValidationMessage(
                        "warning",
                        "placemark_name_mismatch",
                        f"Placemark {area.placemark_name!r} does not start with {expected_prefix!r}.",
                        code,
                    )
                )
        crs_compact = re.sub(r"\s+", "", area.coordinate_system).lower()
        if "wgs84" not in crs_compact or "epsg:4326" not in crs_compact:
            messages.append(
                ValidationMessage(
                    "error",
                    "unexpected_crs",
                    f"Coordinate system is {area.coordinate_system!r}; expected WGS 84 / EPSG:4326.",
                    code,
                )
            )
        for polygon_index, polygon in enumerate(area.polygons, start=1):
            if len({coordinate.pair() for coordinate in polygon.outer}) < 3:
                messages.append(
                    ValidationMessage(
                        "error",
                        "too_few_polygon_coordinates",
                        f"Polygon part {polygon_index} has fewer than three distinct outer-ring vertices.",
                        code,
                    )
                )
            for hole_index, hole in enumerate(polygon.holes, start=1):
                if len({coordinate.pair() for coordinate in hole}) < 3:
                    messages.append(
                        ValidationMessage(
                            "error",
                            "too_few_hole_coordinates",
                            f"Polygon part {polygon_index} hole {hole_index} has fewer than three distinct vertices.",
                            code,
                        )
                    )
            part_coordinates = [*polygon.outer, *(coordinate for hole in polygon.holes for coordinate in hole)]
            if any(abs(coordinate.latitude) > WEB_MERCATOR_MAX_LATITUDE for coordinate in part_coordinates):
                messages.append(
                    ValidationMessage(
                        "error",
                        "outside_web_mercator",
                        (
                            f"Polygon part {polygon_index} exceeds the Web Mercator latitude limit "
                            f"of +/-{WEB_MERCATOR_MAX_LATITUDE} degrees required by SAS.Planet imagery tiles."
                        ),
                        code,
                    )
                )
        if not area.source_closed:
            messages.append(
                ValidationMessage(
                    "warning",
                    "unclosed_source_polygon",
                    "Source polygon is not closed. Derived outputs will close it and record the repair.",
                    code,
                )
            )
        point_count = vertex_point_counts.get(code, 0)
        if (
            profile == "tender_areas"
            and area.source_closed
            and area.source_coordinate_count == area.declared_coordinate_count + len(area.polygons)
        ):
            closure_differences += 1
        elif profile == "tender_areas" and area.unique_coordinate_count != area.declared_coordinate_count:
            point_markers_match = point_count == area.declared_coordinate_count
            severity = "warning" if point_markers_match else "error"
            marker_context = (
                f" All {point_count} declared vertex-point placemarks are present."
                if point_markers_match
                else f" Found {point_count} vertex-point placemarks."
            )
            messages.append(
                ValidationMessage(
                    severity,
                    "declared_coordinate_count_mismatch",
                    (
                        f"Declared coordinate count is {area.declared_coordinate_count}, but the polygon has "
                        f"{area.unique_coordinate_count} non-closing vertices and {area.source_coordinate_count} source coordinates."
                        f"{marker_context} The original polygon geometry is retained without repair."
                    ),
                    code,
                )
            )
        if profile == "tender_areas" and point_count != area.declared_coordinate_count:
            messages.append(
                ValidationMessage(
                    "warning",
                    "vertex_point_count_mismatch",
                    f"Found {point_count} vertex-point placemarks; metadata declares {area.declared_coordinate_count} vertices.",
                    code,
                )
            )

    if closure_differences:
        messages.append(
            ValidationMessage(
                "info",
                "closing_coordinate_explained",
                (
                    f"{closure_differences} polygons contain one extra source coordinate because KML LinearRing "
                    "repeats the first vertex at the end; declared counts exclude that closing coordinate."
                ),
            )
        )
    return messages


def validate_config(config: dict[str, Any], project_root: Path) -> list[str]:
    errors: list[str] = []
    if not isinstance(config.get("dry_run"), bool):
        errors.append("dry_run must be true or false")
    dataset = config.get("dataset")
    if not isinstance(dataset, dict):
        errors.append("dataset must be a mapping")
    else:
        dataset_id = dataset.get("id")
        if not isinstance(dataset_id, str) or not dataset_id.strip():
            errors.append("dataset.id must be a non-empty string or 'auto'")
        elif dataset_id.casefold() != "auto" and not is_portable_windows_component(dataset_id):
            errors.append("dataset.id must be a portable, non-reserved Windows path component")
        if dataset.get("profile") not in {"auto", "selection_91", "tender_areas", "generic_polygons"}:
            errors.append("dataset.profile must be auto, selection_91, tender_areas, or generic_polygons")
        if not isinstance(dataset.get("namespace_outputs"), bool):
            errors.append("dataset.namespace_outputs must be true or false")
    parser = config.get("parser")
    if not isinstance(parser, dict):
        errors.append("parser must be a mapping")
    else:
        for key in ("id_fields", "name_fields"):
            fields = parser.get(key)
            if (
                not isinstance(fields, list)
                or not fields
                or not all(isinstance(item, str) and item.strip() for item in fields)
            ):
                errors.append(f"parser.{key} must be a non-empty list of field names")
    area_codes = config.get("area_codes")
    if not isinstance(area_codes, list) or not area_codes:
        errors.append("area_codes must be a non-empty list")
    elif not all(isinstance(code, str) and is_portable_windows_component(code) for code in area_codes):
        errors.append("area_codes must contain non-empty portable feature IDs")
    imagery = config.get("imagery")
    if not isinstance(imagery, dict):
        errors.append("imagery must be a mapping")
    else:
        source = imagery.get("source")
        if not isinstance(source, str) or not source.strip():
            errors.append("imagery.source must be a non-empty map name")
        zooms = imagery.get("zoom_levels")
        if not isinstance(zooms, list) or not zooms or not all(type(item) is int and 1 <= item <= 24 for item in zooms):
            errors.append("imagery.zoom_levels must contain integers from 1 through 24")
        if not isinstance(imagery.get("download_missing_tiles_only"), bool):
            errors.append("imagery.download_missing_tiles_only must be true or false")
    sessions = config.get("sessions")
    if not isinstance(sessions, dict):
        errors.append("sessions must be a mapping")
    else:
        directory = sessions.get("directory")
        if not isinstance(directory, str) or not directory.strip():
            errors.append("sessions.directory must be a non-empty path")
        if not isinstance(sessions.get("auto_close_at_finish"), bool):
            errors.append("sessions.auto_close_at_finish must be true or false")
        workers_count = sessions.get("workers_count")
        if type(workers_count) is not int or not 1 <= workers_count <= 32:
            errors.append("sessions.workers_count must be an integer from 1 through 32")
    safety = config.get("safety")
    if not isinstance(safety, dict):
        errors.append("safety must be a mapping")
    else:
        pilot_feature_id = safety.get("pilot_feature_id")
        if (
            not isinstance(pilot_feature_id, str)
            or not pilot_feature_id.strip()
            or (pilot_feature_id.casefold() != "auto" and not is_portable_windows_component(pilot_feature_id))
        ):
            errors.append("safety.pilot_feature_id must be 'auto' or a portable feature ID")
        if not isinstance(safety.get("require_completed_pilot_before_multi_download"), bool):
            errors.append("safety.require_completed_pilot_before_multi_download must be true or false")
        for key in ("max_rectangle_tiles_per_feature", "max_rectangle_tiles_total"):
            value = safety.get(key)
            if type(value) is not int or not 1 <= value <= 10_000_000:
                errors.append(f"safety.{key} must be an integer from 1 through 10000000")
    export = config.get("export")
    if not isinstance(export, dict):
        errors.append("export must be a mapping")
    else:
        if not isinstance(export.get("enabled"), bool):
            errors.append("export.enabled must be true or false")
        export_directory = export.get("directory")
        if not isinstance(export_directory, str) or not export_directory.strip():
            errors.append("export.directory must be a non-empty path")
        supported_formats = {"geotiff", "jpeg"}
        for key in ("preferred_format", "fallback_format"):
            format_value = export.get(key)
            if not isinstance(format_value, str) or format_value.casefold() not in supported_formats:
                errors.append(f"export.{key} must be GeoTIFF or JPEG")
        for key in ("include_georeferencing", "mask_to_polygon", "require_complete_cache"):
            if not isinstance(export.get(key), bool):
                errors.append(f"export.{key} must be true or false")
        preview_max_size = export.get("preview_max_size")
        if type(preview_max_size) is not int or not 128 <= preview_max_size <= 4096:
            errors.append("export.preview_max_size must be an integer from 128 through 4096")
        max_mosaic_pixels = export.get("max_mosaic_pixels")
        if type(max_mosaic_pixels) is not int or not 1_000_000 <= max_mosaic_pixels <= 1_000_000_000:
            errors.append("export.max_mosaic_pixels must be an integer from 1000000 through 1000000000")
        max_mosaic_dimension = export.get("max_mosaic_dimension")
        if type(max_mosaic_dimension) is not int or not 1024 <= max_mosaic_dimension <= 100_000:
            errors.append("export.max_mosaic_dimension must be an integer from 1024 through 100000")
    input_value = config.get("input_kmz")
    if not isinstance(input_value, str) or not input_value.strip():
        errors.append("input_kmz must be a path")
    else:
        input_path = Path(input_value)
        if not input_path.is_absolute():
            input_path = project_root / input_path
        if not input_path.is_file():
            errors.append(f"input_kmz does not exist: {input_path}")
    sasplanet_value = config.get("sasplanet_exe")
    if not isinstance(sasplanet_value, str) or not Path(sasplanet_value).is_file():
        errors.append(f"sasplanet_exe does not exist: {sasplanet_value}")
    return errors
