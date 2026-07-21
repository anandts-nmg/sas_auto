"""SAS.Planet map discovery, saved-session generation, and CLI launching."""

from __future__ import annotations

import configparser
import hashlib
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .geometry import close_for_derived_output, estimate_bbox_tiles, format_number
from .models import AreaRecord
from .state import atomic_write_json, atomic_write_text

GUID_PATTERN = re.compile(r"\{[0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}\}")


@dataclass(frozen=True)
class MapDefinition:
    name: str
    guid: str
    params_path: str
    active: bool


@dataclass(frozen=True)
class SessionSettings:
    provider: MapDefinition
    zoom_levels: tuple[int, ...]
    download_missing_tiles_only: bool
    auto_close_at_finish: bool
    workers_count: int
    session_directory: Path


@dataclass(frozen=True)
class SessionArtifact:
    path: str
    sha256: str
    provider_name: str
    provider_guid: str
    zoom_levels: tuple[int, ...]
    area_codes: tuple[str, ...]
    polygon_count: int
    coordinate_count: int
    missing_tiles_only: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


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


def resolve_requested_provider(sasplanet_exe: Path, requested: str) -> MapDefinition:
    definitions = discover_map_definitions(sasplanet_exe)
    provider = find_requested_provider(definitions, requested)
    if provider is not None:
        return provider
    active_names = [definition.name for definition in definitions if definition.active]
    raise ValueError(
        f"Requested provider {requested!r} is unavailable; no provider was substituted. "
        f"Active SAS.Planet maps include: {', '.join(active_names[:25]) or 'none'}"
    )


def detect_executable_capabilities(sasplanet_exe: Path) -> dict[str, bool]:
    """Read known command tokens from the executable without launching it."""
    data = sasplanet_exe.read_bytes()
    flags = ["--map", "--zoom", "--move", "--navigate", "--sls-autostart"]
    return {flag: flag.encode("utf-16le") in data or flag.encode("ascii") in data for flag in flags}


def resolve_session_settings(config: dict[str, Any], project_root: Path) -> SessionSettings:
    sasplanet_exe = Path(config["sasplanet_exe"])
    provider = resolve_requested_provider(sasplanet_exe, str(config["imagery"]["source"]))
    zoom_levels = tuple(sorted({int(value) for value in config["imagery"]["zoom_levels"]}))
    directory_value = Path(str(config["sessions"]["directory"]))
    session_directory = directory_value if directory_value.is_absolute() else project_root / directory_value
    return SessionSettings(
        provider=provider,
        zoom_levels=zoom_levels,
        download_missing_tiles_only=bool(config["imagery"]["download_missing_tiles_only"]),
        auto_close_at_finish=bool(config["sessions"]["auto_close_at_finish"]),
        workers_count=int(config["sessions"]["workers_count"]),
        session_directory=session_directory.resolve(),
    )


def _provider_slug(provider: MapDefinition) -> str:
    normalized = _normal_name(provider.name)
    if "esri" in normalized:
        return "ESRI"
    if "google" in normalized:
        return "GOOGLE"
    slug = re.sub(r"[^A-Z0-9]+", "_", provider.name.upper()).strip("_")
    return slug or "MAP"


def _zoom_array_text(zoom_levels: tuple[int, ...]) -> str:
    ranges: list[str] = []
    start = previous = zoom_levels[0]
    for zoom in zoom_levels[1:]:
        if zoom == previous + 1:
            previous = zoom
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = zoom
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def _zoom_slug(zoom_levels: tuple[int, ...]) -> str:
    return "Z" + _zoom_array_text(zoom_levels).replace(",", "_")


def session_path_for(settings: SessionSettings, areas: list[AreaRecord]) -> Path:
    scope = areas[0].area_code if len(areas) == 1 else "ALL_KMZ"
    name = f"{scope}_{_provider_slug(settings.provider)}_{_zoom_slug(settings.zoom_levels)}.sls"
    return settings.session_directory / name


def _session_lines(areas: list[AreaRecord], settings: SessionSettings) -> tuple[list[str], int]:
    lines = [
        "[Session]",
        f"MapGUID={settings.provider.guid}",
        "VersionDownload=",
        "VersionCheck=",
        "VersionCheckOther=0",
        f"Zoom={settings.zoom_levels[0]}",
        f"ZoomArr={_zoom_array_text(settings.zoom_levels)}",
        f"ReplaceExistTiles={0 if settings.download_missing_tiles_only else 1}",
        "CheckExistTileSize=0",
        "CheckExistTileDate=0",
        "SecondLoadTNE=0",
        "ProcessedTileCount=0",
        "Processed=0",
        "ProcessedFromLastSuccessfulPoint=0",
        "LastProcessedCount=0",
        "ProcessedSize=0",
        "StartX=-1",
        "StartY=-1",
        "LastSuccessfulStartX=-1",
        "LastSuccessfulStartY=-1",
        "ElapsedTime=0",
        f"AutoCloseAtFinish={1 if settings.auto_close_at_finish else 0}",
        "AutoSaveInterval=0",
        "AutoSavePrefix=",
        f"WorkersCount={settings.workers_count}",
        "WorkerIndex=0",
    ]
    point_index = 0
    coordinate_count = 0
    for area_index, area in enumerate(areas):
        if area_index > 0:
            lines.append(f"PointLon_{point_index}=NAN")
            lines.append(f"PointLat_{point_index}=NAN")
            point_index += 1
        ring, _ = close_for_derived_output(area.coordinates)
        for coordinate in ring:
            lines.append(f"PointLon_{point_index}={format_number(coordinate.longitude)}")
            lines.append(f"PointLat_{point_index}={format_number(coordinate.latitude)}")
            point_index += 1
            coordinate_count += 1
    return lines, coordinate_count


def create_sls_session(
    areas: list[AreaRecord],
    settings: SessionSettings,
    output_path: Path | None = None,
) -> SessionArtifact:
    if not areas:
        raise ValueError("At least one verified polygon area is required")
    if not settings.zoom_levels or any(zoom < 1 or zoom > 24 for zoom in settings.zoom_levels):
        raise ValueError(f"Invalid SAS.Planet zoom levels: {settings.zoom_levels}")
    if not GUID_PATTERN.fullmatch(settings.provider.guid):
        raise ValueError(f"Invalid SAS.Planet map GUID: {settings.provider.guid}")
    codes = tuple(area.area_code for area in areas)
    if len(set(codes)) != len(codes):
        raise ValueError(f"Duplicate area codes in session: {codes}")

    path = (output_path or session_path_for(settings, areas)).resolve()
    lines, coordinate_count = _session_lines(areas, settings)
    atomic_write_text(path, "\r\n".join(lines) + "\r\n", encoding="ascii")
    errors = validate_sls_session(path)
    if errors:
        raise RuntimeError("Generated SLS validation failed: " + "; ".join(errors))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return SessionArtifact(
        path=str(path),
        sha256=digest,
        provider_name=settings.provider.name,
        provider_guid=settings.provider.guid,
        zoom_levels=settings.zoom_levels,
        area_codes=codes,
        polygon_count=len(areas),
        coordinate_count=coordinate_count,
        missing_tiles_only=settings.download_missing_tiles_only,
    )


def _parse_zoom_array(value: str) -> list[int]:
    result: list[int] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            start_text, end_text = item.split("-", 1)
            start, end = int(start_text), int(end_text)
            result.extend(range(start, end + 1))
        else:
            result.append(int(item))
    return result


def validate_sls_session(path: Path) -> list[str]:
    errors: list[str] = []
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, configparser.Error) as error:
        return [f"Cannot read SLS session: {error}"]
    if "Session" not in parser:
        return ["SLS has no [Session] section"]
    session = parser["Session"]
    required = {"mapguid", "zoom", "zoomarr", "replaceexisttiles", "workerscount", "workerindex"}
    for key in sorted(required - set(session)):
        errors.append(f"SLS is missing {key}")
    guid = session.get("mapguid", "").upper()
    if not GUID_PATTERN.fullmatch(guid):
        errors.append(f"SLS MapGUID is invalid: {guid!r}")
    try:
        zoom = int(session.get("zoom", "0"))
        zooms = _parse_zoom_array(session.get("zoomarr", ""))
        if not zooms or zoom not in zooms or any(value < 1 or value > 24 for value in zooms):
            errors.append(f"SLS zoom fields are inconsistent: Zoom={zoom}, ZoomArr={zooms}")
    except ValueError:
        errors.append("SLS zoom fields are not numeric")

    longitude_indexes = {
        int(match.group(1)) for key in session if (match := re.fullmatch(r"pointlon_(\d+)", key)) is not None
    }
    latitude_indexes = {
        int(match.group(1)) for key in session if (match := re.fullmatch(r"pointlat_(\d+)", key)) is not None
    }
    if longitude_indexes != latitude_indexes:
        errors.append("SLS longitude and latitude point indexes differ")
    if not longitude_indexes:
        errors.append("SLS contains no polygon points")
        return errors
    expected_indexes = set(range(max(longitude_indexes) + 1))
    if longitude_indexes != expected_indexes:
        errors.append("SLS point indexes are not contiguous from zero")
    coordinate_count = 0
    for index in sorted(longitude_indexes & latitude_indexes):
        longitude_text = session[f"pointlon_{index}"].strip()
        latitude_text = session[f"pointlat_{index}"].strip()
        if longitude_text.casefold() == "nan" or latitude_text.casefold() == "nan":
            continue
        try:
            longitude = float(longitude_text)
            latitude = float(latitude_text)
        except ValueError:
            errors.append(f"SLS point {index} is not numeric")
            continue
        if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
            errors.append(f"SLS point {index} is outside WGS 84 ranges")
        coordinate_count += 1
    if coordinate_count < 4:
        errors.append("SLS polygon contains fewer than four coordinates")
    return errors


def sasplanet_command(sasplanet_exe: Path, session_path: Path) -> list[str]:
    return [str(sasplanet_exe.resolve()), "--sls-autostart", str(session_path.resolve())]


def create_download_plan(
    artifact: SessionArtifact,
    areas: list[AreaRecord],
    settings: SessionSettings,
    sasplanet_exe: Path,
    project_root: Path,
) -> dict[str, Any]:
    estimates = [
        {
            "area_code": area.area_code,
            "bounds": area.bounds.as_dict(),
            "zooms": [{"zoom": zoom, **estimate_bbox_tiles(area.bounds, zoom)} for zoom in settings.zoom_levels],
        }
        for area in areas
    ]
    command = sasplanet_command(sasplanet_exe, Path(artifact.path))
    plan: dict[str, Any] = {
        "schema_version": 2,
        "mode": "sls-autostart",
        "network_download_started": False,
        "session": artifact.as_dict(),
        "sasplanet_exe": str(sasplanet_exe.resolve()),
        "command": command,
        "executable_capabilities": detect_executable_capabilities(sasplanet_exe),
        "selection": "exact polygon geometry encoded in SLS",
        "rectangle_tile_estimates": estimates,
        "resume_behavior": (
            "Relaunching the same SLS re-enumerates the polygon but skips cached tiles because ReplaceExistTiles=0."
        ),
        "desktop_automation": False,
    }
    plan_path = project_root / "state" / "plans" / f"{Path(artifact.path).stem}.json"
    plan["plan_path"] = str(plan_path.resolve())
    atomic_write_json(plan_path, plan)
    return plan


def launch_sls_session(sasplanet_exe: Path, session_path: Path) -> int:
    if not sasplanet_exe.is_file():
        raise FileNotFoundError(f"SAS.Planet executable not found: {sasplanet_exe}")
    if not session_path.is_file():
        raise FileNotFoundError(f"SLS session not found: {session_path}")
    errors = validate_sls_session(session_path)
    if errors:
        raise ValueError("Refusing to launch invalid SLS: " + "; ".join(errors))
    if not detect_executable_capabilities(sasplanet_exe).get("--sls-autostart", False):
        raise ValueError(f"SAS.Planet executable does not advertise --sls-autostart: {sasplanet_exe}")
    process = subprocess.Popen(sasplanet_command(sasplanet_exe, session_path), cwd=str(sasplanet_exe.parent))
    return process.pid
