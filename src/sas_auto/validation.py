"""Validation rules for source data and user configuration."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from .models import AreaRecord, ValidationMessage

EXPECTED_AREA_CODES = {str(value) for value in range(9101, 9121)}


def validate_areas(areas: list[AreaRecord], vertex_point_counts: Counter[str]) -> list[ValidationMessage]:
    messages: list[ValidationMessage] = []
    codes = [area.area_code for area in areas]
    code_counts = Counter(codes)

    for code, count in sorted(code_counts.items()):
        if count > 1:
            messages.append(
                ValidationMessage("error", "duplicate_area_code", f"Area code {code} occurs {count} times.", code)
            )
    actual_codes = set(codes)
    missing = sorted(EXPECTED_AREA_CODES - actual_codes)
    unexpected = sorted(actual_codes - EXPECTED_AREA_CODES)
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
        if not re.fullmatch(r"\d{4}", code):
            messages.append(ValidationMessage("error", "malformed_area_code", f"Malformed area code: {code!r}", code))
        if area.tender_number != "91":
            messages.append(
                ValidationMessage(
                    "error",
                    "unexpected_tender_number",
                    f"Tender number is {area.tender_number!r}; expected '91'.",
                    code,
                )
            )
        expected_suffix = int(code) - 9100 if code.isdigit() else -1
        expected_prefix = f"91_{expected_suffix:04d}_" if expected_suffix > 0 else ""
        if expected_prefix and not area.placemark_name.startswith(expected_prefix):
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
        if len(area.coordinates) < 4:
            messages.append(
                ValidationMessage(
                    "error", "too_few_polygon_coordinates", "Polygon has fewer than four coordinates.", code
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
        if area.source_closed and area.source_coordinate_count == area.declared_coordinate_count + 1:
            closure_differences += 1
        elif area.unique_coordinate_count != area.declared_coordinate_count:
            messages.append(
                ValidationMessage(
                    "error",
                    "declared_coordinate_count_mismatch",
                    (
                        f"Declared coordinate count is {area.declared_coordinate_count}, but the polygon has "
                        f"{area.unique_coordinate_count} non-closing vertices and {area.source_coordinate_count} source coordinates."
                    ),
                    code,
                )
            )
        point_count = vertex_point_counts.get(code, 0)
        if point_count != area.declared_coordinate_count:
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
    area_codes = config.get("area_codes")
    if not isinstance(area_codes, list) or not area_codes:
        errors.append("area_codes must be a non-empty list")
    else:
        invalid_codes = [str(code) for code in area_codes if str(code) not in EXPECTED_AREA_CODES]
        if invalid_codes:
            errors.append(f"area_codes contains unsupported values: {', '.join(invalid_codes)}")
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
