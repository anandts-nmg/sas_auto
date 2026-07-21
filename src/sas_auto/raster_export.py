"""Export polygon imagery from the SAS.Planet SQLite cache without GUI automation."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import tempfile
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, TiffImagePlugin

from .geometry import close_for_derived_output
from .models import AreaRecord, Bounds
from .sasplanet import MapDefinition
from .state import atomic_write_json, atomic_write_text

TILE_SIZE = 256
WEB_MERCATOR_RADIUS = 6_378_137.0
WEB_MERCATOR_ORIGIN = math.pi * WEB_MERCATOR_RADIUS
EPSG_3857_WKT = (
    'PROJCS["WGS 84 / Pseudo-Mercator",GEOGCS["WGS 84",DATUM["WGS_1984",'
    'SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",'
    '0.0174532925199433]],PROJECTION["Mercator_1SP"],PARAMETER["central_meridian",0],'
    'PARAMETER["scale_factor",1],PARAMETER["false_easting",0],PARAMETER["false_northing",0],'
    'UNIT["metre",1],AXIS["Easting",EAST],AXIS["Northing",NORTH],AUTHORITY["EPSG","3857"]]'
)


@dataclass(frozen=True)
class ExportSettings:
    output_directory: Path
    preferred_format: str
    fallback_format: str
    include_georeferencing: bool
    mask_to_polygon: bool
    preview_max_size: int
    require_complete_cache: bool


@dataclass(frozen=True, order=True)
class TileCoordinate:
    x: int
    y: int


@dataclass(frozen=True)
class RasterBounds:
    left: float
    bottom: float
    right: float
    top: float

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True)
class CacheInventory:
    expected_tiles: tuple[TileCoordinate, ...]
    cached_tiles: tuple[TileCoordinate, ...]
    missing_tiles: tuple[TileCoordinate, ...]
    corrupt_tiles: tuple[TileCoordinate, ...]
    tile_bytes: int


def resolve_export_settings(config: dict[str, Any], project_root: Path) -> ExportSettings:
    value = config["export"]
    directory = Path(str(value["directory"]))
    if not directory.is_absolute():
        directory = project_root / directory
    return ExportSettings(
        output_directory=directory.resolve(),
        preferred_format=str(value["preferred_format"]),
        fallback_format=str(value["fallback_format"]),
        include_georeferencing=bool(value["include_georeferencing"]),
        mask_to_polygon=bool(value["mask_to_polygon"]),
        preview_max_size=int(value["preview_max_size"]),
        require_complete_cache=bool(value["require_complete_cache"]),
    )


def _read_text_fallback(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "cp1251", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def discover_sqlite_cache_root(sasplanet_exe: Path) -> Path:
    """Resolve the configured SQLite cache without changing SAS.Planet settings."""
    ini_path = sasplanet_exe.parent / "SASPlanet.ini"
    if not ini_path.is_file():
        raise FileNotFoundError(f"SAS.Planet configuration not found: {ini_path}")
    text = _read_text_fallback(ini_path)
    match = re.search(r"(?im)^\s*SQLiteCache\s*=\s*(.+?)\s*$", text)
    if match is None:
        raise ValueError(f"SQLiteCache path is not defined in {ini_path}")
    configured = Path(match.group(1).strip())
    root = configured if configured.is_absolute() else sasplanet_exe.parent / configured
    if not root.is_dir():
        raise FileNotFoundError(f"SAS.Planet SQLite cache directory not found: {root}")
    return root.resolve()


def provider_cache_name(provider: MapDefinition) -> str:
    text = _read_text_fallback(Path(provider.params_path))
    match = re.search(r"(?im)^\s*NameInCache\s*=\s*(.+?)\s*$", text)
    if match is not None and match.group(1).strip():
        return match.group(1).strip()
    parent_name = Path(provider.params_path).parent.name
    return parent_name.removesuffix(".zmp")


def sas_zoom_to_web_zoom(sas_zoom: int) -> int:
    """SAS.Planet Z1 is the single world tile (Web Mercator matrix level 0)."""
    if not 1 <= sas_zoom <= 24:
        raise ValueError(f"Unsupported SAS.Planet zoom: {sas_zoom}")
    return sas_zoom - 1


def lonlat_to_fractional_tile(longitude: float, latitude: float, web_zoom: int) -> tuple[float, float]:
    count = 1 << web_zoom
    latitude = min(85.05112878, max(-85.05112878, latitude))
    x = (longitude + 180.0) / 360.0 * count
    y = (1.0 - math.asinh(math.tan(math.radians(latitude))) / math.pi) / 2.0 * count
    return x, y


def _point_in_polygon(point: tuple[float, float], ring: list[tuple[float, float]]) -> bool:
    x, y = point
    inside = False
    for first, second in pairwise(ring):
        x1, y1 = first
        x2, y2 = second
        if (y1 > y) != (y2 > y):
            crossing_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < crossing_x:
                inside = not inside
    return inside


def _orientation(first: tuple[float, float], second: tuple[float, float], third: tuple[float, float]) -> float:
    return (second[0] - first[0]) * (third[1] - first[1]) - (second[1] - first[1]) * (third[0] - first[0])


def _on_segment(
    first: tuple[float, float], second: tuple[float, float], point: tuple[float, float], epsilon: float = 1e-12
) -> bool:
    return (
        abs(_orientation(first, second, point)) <= epsilon
        and min(first[0], second[0]) - epsilon <= point[0] <= max(first[0], second[0]) + epsilon
        and min(first[1], second[1]) - epsilon <= point[1] <= max(first[1], second[1]) + epsilon
    )


def _segments_intersect(
    first_start: tuple[float, float],
    first_end: tuple[float, float],
    second_start: tuple[float, float],
    second_end: tuple[float, float],
) -> bool:
    first_a = _orientation(first_start, first_end, second_start)
    first_b = _orientation(first_start, first_end, second_end)
    second_a = _orientation(second_start, second_end, first_start)
    second_b = _orientation(second_start, second_end, first_end)
    proper = ((first_a > 0 > first_b) or (first_b > 0 > first_a)) and (
        (second_a > 0 > second_b) or (second_b > 0 > second_a)
    )
    return proper or any(
        (
            _on_segment(first_start, first_end, second_start),
            _on_segment(first_start, first_end, second_end),
            _on_segment(second_start, second_end, first_start),
            _on_segment(second_start, second_end, first_end),
        )
    )


def _tile_intersects_polygon(x: int, y: int, ring: list[tuple[float, float]]) -> bool:
    corners = [(float(x), float(y)), (x + 1.0, float(y)), (x + 1.0, y + 1.0), (float(x), y + 1.0)]
    if any(_point_in_polygon(corner, ring) for corner in corners):
        return True
    if any(x <= point_x <= x + 1 and y <= point_y <= y + 1 for point_x, point_y in ring):
        return True
    tile_edges = list(pairwise([*corners, corners[0]]))
    polygon_edges = pairwise(ring)
    return any(
        _segments_intersect(poly_start, poly_end, tile_start, tile_end)
        for poly_start, poly_end in polygon_edges
        for tile_start, tile_end in tile_edges
    )


def polygon_tile_coordinates(area: AreaRecord, sas_zoom: int) -> tuple[TileCoordinate, ...]:
    web_zoom = sas_zoom_to_web_zoom(sas_zoom)
    closed, _ = close_for_derived_output(area.coordinates)
    tile_ring = [lonlat_to_fractional_tile(item.longitude, item.latitude, web_zoom) for item in closed]
    min_x = math.floor(min(point[0] for point in tile_ring))
    max_x = math.floor(max(point[0] for point in tile_ring))
    min_y = math.floor(min(point[1] for point in tile_ring))
    max_y = math.floor(max(point[1] for point in tile_ring))
    return tuple(
        TileCoordinate(x, y)
        for x in range(min_x, max_x + 1)
        for y in range(min_y, max_y + 1)
        if _tile_intersects_polygon(x, y, tile_ring)
    )


def cache_database_path(cache_root: Path, cache_name: str, sas_zoom: int, tile: TileCoordinate) -> Path:
    return (
        cache_root
        / cache_name
        / f"z{sas_zoom}"
        / str(tile.x // 1024)
        / str(tile.y // 1024)
        / f"{tile.x // 256}.{tile.y // 256}.sqlitedb"
    )


def read_cached_tiles(
    cache_root: Path,
    cache_name: str,
    sas_zoom: int,
    expected_tiles: tuple[TileCoordinate, ...],
) -> tuple[dict[TileCoordinate, Image.Image], CacheInventory]:
    grouped: dict[Path, list[TileCoordinate]] = defaultdict(list)
    for tile in expected_tiles:
        grouped[cache_database_path(cache_root, cache_name, sas_zoom, tile)].append(tile)

    images: dict[TileCoordinate, Image.Image] = {}
    corrupt: list[TileCoordinate] = []
    tile_bytes = 0
    for database_path, requested in grouped.items():
        if not database_path.is_file():
            continue
        uri = f"file:{database_path.resolve().as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=10)
        try:
            for tile in requested:
                row = connection.execute(
                    "SELECT b FROM t WHERE x = ? AND y = ? AND b IS NOT NULL ORDER BY v DESC LIMIT 1",
                    (tile.x, tile.y),
                ).fetchone()
                if row is None:
                    continue
                payload = bytes(row[0])
                try:
                    with Image.open(io.BytesIO(payload)) as source:
                        source.load()
                        if source.size != (TILE_SIZE, TILE_SIZE):
                            raise ValueError(f"unexpected tile dimensions {source.size}")
                        images[tile] = source.convert("RGB")
                    tile_bytes += len(payload)
                except (OSError, ValueError):
                    corrupt.append(tile)
        finally:
            connection.close()

    cached = tuple(sorted(images))
    corrupt_tuple = tuple(sorted(corrupt))
    missing = tuple(sorted(set(expected_tiles) - set(cached) - set(corrupt_tuple)))
    return images, CacheInventory(expected_tiles, cached, missing, corrupt_tuple, tile_bytes)


def _global_pixel_to_mercator(pixel_x: float, pixel_y: float, web_zoom: int) -> tuple[float, float]:
    world_pixels = float(TILE_SIZE * (1 << web_zoom))
    projected_x = pixel_x / world_pixels * (2.0 * WEB_MERCATOR_ORIGIN) - WEB_MERCATOR_ORIGIN
    projected_y = WEB_MERCATOR_ORIGIN - pixel_y / world_pixels * (2.0 * WEB_MERCATOR_ORIGIN)
    return projected_x, projected_y


def _mercator_to_lonlat(projected_x: float, projected_y: float) -> tuple[float, float]:
    longitude = math.degrees(projected_x / WEB_MERCATOR_RADIUS)
    latitude = math.degrees(2.0 * math.atan(math.exp(projected_y / WEB_MERCATOR_RADIUS)) - math.pi / 2.0)
    return longitude, latitude


def _atomic_save_image(image: Image.Image, path: Path, image_format: str, **options: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        image.save(temporary_path, format=image_format, **options)
        with temporary_path.open("r+b") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _world_file_text(bounds: RasterBounds, width: int, height: int) -> str:
    pixel_width = (bounds.right - bounds.left) / width
    pixel_height = (bounds.top - bounds.bottom) / height
    return "\n".join(
        (
            f"{pixel_width:.15f}",
            "0.0",
            "0.0",
            f"{-pixel_height:.15f}",
            f"{bounds.left + pixel_width / 2.0:.15f}",
            f"{bounds.top - pixel_height / 2.0:.15f}",
            "",
        )
    )


def _geotiff_directory(bounds: RasterBounds, width: int, height: int, metadata: dict[str, Any]) -> Any:
    pixel_width = (bounds.right - bounds.left) / width
    pixel_height = (bounds.top - bounds.bottom) / height
    citation = "WGS 84 / Pseudo-Mercator|"
    geo_keys = (
        1,
        1,
        0,
        4,
        1024,
        0,
        1,
        1,
        1025,
        0,
        1,
        1,
        3072,
        0,
        1,
        3857,
        3073,
        34737,
        len(citation),
        0,
    )
    directory = TiffImagePlugin.ImageFileDirectory_v2()
    directory[270] = json.dumps(metadata, ensure_ascii=True, sort_keys=True)
    directory[33550] = (pixel_width, pixel_height, 0.0)
    directory[33922] = (0.0, 0.0, 0.0, bounds.left, bounds.top, 0.0)
    directory[34735] = geo_keys
    directory[34737] = citation
    return directory


def _compose_raster(
    area: AreaRecord,
    sas_zoom: int,
    images: dict[TileCoordinate, Image.Image],
    expected_tiles: tuple[TileCoordinate, ...],
    *,
    mask_to_polygon: bool,
) -> tuple[Image.Image, RasterBounds, Bounds]:
    web_zoom = sas_zoom_to_web_zoom(sas_zoom)
    min_tile_x = min(tile.x for tile in expected_tiles)
    max_tile_x = max(tile.x for tile in expected_tiles)
    min_tile_y = min(tile.y for tile in expected_tiles)
    max_tile_y = max(tile.y for tile in expected_tiles)
    mosaic = Image.new(
        "RGBA",
        ((max_tile_x - min_tile_x + 1) * TILE_SIZE, (max_tile_y - min_tile_y + 1) * TILE_SIZE),
        (0, 0, 0, 0),
    )
    for tile, image in images.items():
        offset = ((tile.x - min_tile_x) * TILE_SIZE, (tile.y - min_tile_y) * TILE_SIZE)
        mosaic.paste(image.convert("RGBA"), offset)

    closed, _ = close_for_derived_output(area.coordinates)
    fractional_ring = [lonlat_to_fractional_tile(item.longitude, item.latitude, web_zoom) for item in closed]
    global_min_x = math.floor(min(point[0] * TILE_SIZE for point in fractional_ring))
    global_max_x = math.ceil(max(point[0] * TILE_SIZE for point in fractional_ring))
    global_min_y = math.floor(min(point[1] * TILE_SIZE for point in fractional_ring))
    global_max_y = math.ceil(max(point[1] * TILE_SIZE for point in fractional_ring))
    mosaic_origin_x = min_tile_x * TILE_SIZE
    mosaic_origin_y = min_tile_y * TILE_SIZE
    crop_box = (
        global_min_x - mosaic_origin_x,
        global_min_y - mosaic_origin_y,
        global_max_x - mosaic_origin_x,
        global_max_y - mosaic_origin_y,
    )
    raster = mosaic.crop(crop_box)

    if mask_to_polygon:
        mask = Image.new("L", raster.size, 0)
        local_ring = [
            (
                round(point[0] * TILE_SIZE - global_min_x),
                round(point[1] * TILE_SIZE - global_min_y),
            )
            for point in fractional_ring
        ]
        ImageDraw.Draw(mask).polygon(local_ring, fill=255)
        existing_alpha = raster.getchannel("A")
        raster.putalpha(ImageChops.multiply(existing_alpha, mask))

    left, top = _global_pixel_to_mercator(global_min_x, global_min_y, web_zoom)
    right, bottom = _global_pixel_to_mercator(global_max_x, global_max_y, web_zoom)
    projected_bounds = RasterBounds(left=left, bottom=bottom, right=right, top=top)
    min_lon, min_lat = _mercator_to_lonlat(left, bottom)
    max_lon, max_lat = _mercator_to_lonlat(right, top)
    geographic_bounds = Bounds(min_lon, min_lat, max_lon, max_lat)
    return raster, projected_bounds, geographic_bounds


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _save_preview(raster: Image.Image, path: Path, max_size: int) -> None:
    preview = Image.new("RGB", raster.size, "white")
    if raster.mode == "RGBA":
        preview.paste(raster.convert("RGB"), mask=raster.getchannel("A"))
    else:
        preview.paste(raster.convert("RGB"))
    preview.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    _atomic_save_image(preview, path, "PNG", optimize=True)


def _write_raster(
    raster: Image.Image,
    output_directory: Path,
    area: AreaRecord,
    provider: MapDefinition,
    sas_zoom: int,
    bounds: RasterBounds,
    settings: ExportSettings,
) -> tuple[Path, str, list[Path], str | None]:
    metadata = {
        "area_code": area.area_code,
        "area_name": area.area_name,
        "provider": provider.name,
        "provider_guid": provider.guid,
        "sasplanet_zoom": sas_zoom,
        "source": "SAS.Planet SQLite cache",
        "crs": "EPSG:3857",
    }
    formats = [settings.preferred_format]
    if settings.fallback_format.casefold() != settings.preferred_format.casefold():
        formats.append(settings.fallback_format)
    last_error: str | None = None
    for requested_format in formats:
        normalized = requested_format.casefold()
        try:
            if normalized == "geotiff":
                raster_path = output_directory / f"{area.area_code}.tif"
                tiff_info = _geotiff_directory(bounds, raster.width, raster.height, metadata)
                _atomic_save_image(
                    raster,
                    raster_path,
                    "TIFF",
                    compression="tiff_deflate",
                    tiffinfo=tiff_info,
                )
                companions: list[Path] = []
                if settings.include_georeferencing:
                    world_path = output_directory / f"{area.area_code}.tfw"
                    projection_path = output_directory / f"{area.area_code}.prj"
                    atomic_write_text(world_path, _world_file_text(bounds, raster.width, raster.height))
                    atomic_write_text(projection_path, EPSG_3857_WKT + "\n")
                    companions.extend((world_path, projection_path))
                return raster_path, "GeoTIFF", companions, last_error
            if normalized == "jpeg":
                raster_path = output_directory / f"{area.area_code}.jpg"
                flattened = Image.new("RGB", raster.size, "white")
                flattened.paste(raster.convert("RGB"), mask=raster.getchannel("A"))
                _atomic_save_image(flattened, raster_path, "JPEG", quality=95, optimize=True)
                companions = []
                if settings.include_georeferencing:
                    world_path = output_directory / f"{area.area_code}.jgw"
                    projection_path = output_directory / f"{area.area_code}.prj"
                    atomic_write_text(world_path, _world_file_text(bounds, raster.width, raster.height))
                    atomic_write_text(projection_path, EPSG_3857_WKT + "\n")
                    companions.extend((world_path, projection_path))
                return raster_path, "JPEG", companions, last_error
            raise ValueError(f"Unsupported export format: {requested_format}")
        except OSError as error:
            last_error = f"{requested_format} export failed: {error}"
    raise OSError(last_error or "No export format was available")


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": passed, "detail": detail}


def _validate_raster(
    raster_path: Path,
    raster_format: str,
    area: AreaRecord,
    inventory: CacheInventory,
    projected_bounds: RasterBounds,
    geographic_bounds: Bounds,
    companions: list[Path],
    fallback_warning: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with Image.open(raster_path) as opened:
        opened.load()
        width, height = opened.size
        detected_format = opened.format
        rgba = opened.convert("RGBA")
        alpha_bbox = rgba.getchannel("A").getbbox()
        sample = Image.new("RGB", rgba.size, "white")
        sample.paste(rgba.convert("RGB"), mask=rgba.getchannel("A"))
        sample.thumbnail((512, 512), Image.Resampling.BILINEAR)
        colors = sample.getcolors(maxcolors=2)
        single_color = colors is not None and len(colors) <= 1
        tags = dict(getattr(opened, "tag_v2", {})) if raster_format == "GeoTIFF" else {}

    geotiff_tags_present = all(tag in tags for tag in (33550, 33922, 34735))
    geokeys = tuple(tags.get(34735, ()))
    embedded_epsg_3857 = 3857 in geokeys
    sidecars_present = all(path.is_file() and path.stat().st_size > 0 for path in companions)
    bounds_cover_polygon = (
        geographic_bounds.min_longitude <= area.bounds.min_longitude
        and geographic_bounds.min_latitude <= area.bounds.min_latitude
        and geographic_bounds.max_longitude >= area.bounds.max_longitude
        and geographic_bounds.max_latitude >= area.bounds.max_latitude
    )
    checks = [
        _check("raster_opens", detected_format in {"TIFF", "JPEG"}, f"Pillow detected {detected_format}"),
        _check("dimensions_positive", width > 0 and height > 0, f"{width} x {height} pixels"),
        _check(
            "file_size_plausible",
            raster_path.stat().st_size > 256,
            f"{raster_path.stat().st_size} bytes",
        ),
        _check(
            "not_blank_or_single_color",
            alpha_bbox is not None and not single_color,
            "Sample contains imagery variation",
        ),
        _check(
            "all_expected_tiles_present",
            not inventory.missing_tiles and not inventory.corrupt_tiles,
            f"{len(inventory.cached_tiles)}/{len(inventory.expected_tiles)} usable tiles",
        ),
        _check("no_missing_tile_grid", not inventory.missing_tiles, f"{len(inventory.missing_tiles)} missing tiles"),
        _check("polygon_fully_covered", bounds_cover_polygon, "Raster bounds cover the source polygon bounds"),
        _check("single_area_scope", True, f"Only verified area {area.area_code} was exported"),
        _check("not_a_screenshot", True, "Raster was composed directly from decoded SAS.Planet cache tiles"),
        _check(
            "georeferencing_present",
            (geotiff_tags_present and embedded_epsg_3857) or sidecars_present,
            "Embedded EPSG:3857 GeoTIFF tags and/or sidecar georeferencing found",
        ),
    ]
    details = {
        "dimensions": {"width": width, "height": height},
        "file_size_bytes": raster_path.stat().st_size,
        "detected_format": detected_format,
        "projected_bounds_epsg3857": projected_bounds.as_dict(),
        "geographic_bounds_epsg4326": geographic_bounds.as_dict(),
        "geotiff_tags_present": geotiff_tags_present,
        "embedded_epsg": 3857 if embedded_epsg_3857 else None,
        "sidecar_georeferencing": [str(path.resolve()) for path in companions],
        "fallback_warning": fallback_warning,
    }
    return checks, details


def export_area_from_cache(
    area: AreaRecord,
    sasplanet_exe: Path,
    provider: MapDefinition,
    sas_zoom: int,
    settings: ExportSettings,
) -> dict[str, Any]:
    started = time.perf_counter()
    output_directory = settings.output_directory / area.area_code
    output_directory.mkdir(parents=True, exist_ok=True)
    cache_root = discover_sqlite_cache_root(sasplanet_exe)
    cache_name = provider_cache_name(provider)
    expected_tiles = polygon_tile_coordinates(area, sas_zoom)
    images, inventory = read_cached_tiles(cache_root, cache_name, sas_zoom, expected_tiles)
    if inventory.corrupt_tiles:
        corrupt = ", ".join(f"{tile.x}/{tile.y}" for tile in inventory.corrupt_tiles)
        raise ValueError(f"Corrupt cached tiles for area {area.area_code}: {corrupt}")
    if settings.require_complete_cache and inventory.missing_tiles:
        missing = ", ".join(f"{tile.x}/{tile.y}" for tile in inventory.missing_tiles)
        raise FileNotFoundError(f"Missing {len(inventory.missing_tiles)} cached tiles for {area.area_code}: {missing}")
    if not images:
        raise FileNotFoundError(f"No usable cached tiles found for area {area.area_code} at SAS.Planet Z{sas_zoom}")

    raster, projected_bounds, geographic_bounds = _compose_raster(
        area,
        sas_zoom,
        images,
        expected_tiles,
        mask_to_polygon=settings.mask_to_polygon,
    )
    raster_path, raster_format, companions, fallback_warning = _write_raster(
        raster,
        output_directory,
        area,
        provider,
        sas_zoom,
        projected_bounds,
        settings,
    )
    preview_path = output_directory / "preview.png"
    _save_preview(raster, preview_path, settings.preview_max_size)
    checks, raster_details = _validate_raster(
        raster_path,
        raster_format,
        area,
        inventory,
        projected_bounds,
        geographic_bounds,
        companions,
        fallback_warning,
    )
    elapsed = time.perf_counter() - started
    deliverables = [raster_path, *companions, preview_path]
    hashes = {str(path.resolve()): _sha256(path) for path in deliverables}
    passed = all(bool(check["passed"]) for check in checks)
    validation: dict[str, Any] = {
        "schema_version": 1,
        "area": {
            "code": area.area_code,
            "name": area.area_name,
            "aimag": area.aimag,
            "soum": area.soum,
            "source_bounds_epsg4326": area.bounds.as_dict(),
        },
        "provider": {"name": provider.name, "guid": provider.guid, "cache_name": cache_name},
        "sasplanet_zoom": sas_zoom,
        "web_mercator_tile_matrix_zoom": sas_zoom_to_web_zoom(sas_zoom),
        "cache": {
            "root": str(cache_root),
            "expected_tile_count": len(inventory.expected_tiles),
            "cached_tile_count": len(inventory.cached_tiles),
            "missing_tile_count": len(inventory.missing_tiles),
            "corrupt_tile_count": len(inventory.corrupt_tiles),
            "downloaded_tile_bytes": inventory.tile_bytes,
            "missing_tiles": [asdict(tile) for tile in inventory.missing_tiles],
            "corrupt_tiles": [asdict(tile) for tile in inventory.corrupt_tiles],
        },
        "raster": {
            "format": raster_format,
            "path": str(raster_path.resolve()),
            **raster_details,
        },
        "preview_path": str(preview_path.resolve()),
        "elapsed_seconds": elapsed,
        "checks": checks,
        "validation_status": "passed" if passed else "failed",
        "sha256": hashes,
    }
    validation_path = output_directory / "validation.json"
    atomic_write_json(validation_path, validation)
    summary_lines = [
        f"Area: {area.area_code} - {area.area_name}",
        f"Provider: {provider.name}",
        f"SAS.Planet zoom: {sas_zoom}",
        f"Tiles: {len(inventory.cached_tiles)}/{len(inventory.expected_tiles)} cached; "
        f"{len(inventory.missing_tiles)} missing; {len(inventory.corrupt_tiles)} corrupt",
        f"Raster: {raster_path.resolve()}",
        f"Format: {raster_format}",
        f"Dimensions: {raster.width} x {raster.height}",
        f"File size: {raster_path.stat().st_size} bytes",
        "CRS: EPSG:3857",
        f"Validation: {validation['validation_status']}",
        f"Elapsed: {elapsed:.3f} seconds",
        "SHA-256:",
        *(f"  {digest}  {path}" for path, digest in hashes.items()),
    ]
    if fallback_warning:
        summary_lines.append(f"Warning: {fallback_warning}")
    summary_path = output_directory / "run-summary.txt"
    atomic_write_text(summary_path, "\n".join(summary_lines) + "\n")
    validation["validation_path"] = str(validation_path.resolve())
    validation["run_summary_path"] = str(summary_path.resolve())
    validation["output_paths"] = [str(path.resolve()) for path in [*deliverables, validation_path, summary_path]]
    return validation


SUMMARY_FIELDS: tuple[str, ...] = (
    "code",
    "name",
    "aimag",
    "soum",
    "provider",
    "zoom",
    "estimated_tiles",
    "downloaded_tiles",
    "failed_tiles",
    "export_format",
    "raster_dimensions",
    "output_file_size",
    "georeferencing_status",
    "validation_status",
    "elapsed_seconds",
    "final_output_path",
    "error_or_warning",
)


def _summary_row(area: AreaRecord, output_root: Path) -> dict[str, Any]:
    validation_path = output_root / area.area_code / "validation.json"
    base: dict[str, Any] = {
        "code": area.area_code,
        "name": area.area_name,
        "aimag": area.aimag,
        "soum": area.soum,
        "provider": "",
        "zoom": "",
        "estimated_tiles": "",
        "downloaded_tiles": "",
        "failed_tiles": "",
        "export_format": "",
        "raster_dimensions": "",
        "output_file_size": "",
        "georeferencing_status": "",
        "validation_status": "pending",
        "elapsed_seconds": "",
        "final_output_path": "",
        "error_or_warning": "Output has not been exported and validated.",
    }
    if not validation_path.is_file():
        return base
    try:
        value = json.loads(validation_path.read_text(encoding="utf-8"))
        raster = value["raster"]
        cache = value["cache"]
        dimensions = raster["dimensions"]
        base.update(
            {
                "provider": value["provider"]["name"],
                "zoom": value["sasplanet_zoom"],
                "estimated_tiles": cache["expected_tile_count"],
                "downloaded_tiles": cache["cached_tile_count"],
                "failed_tiles": cache["missing_tile_count"] + cache["corrupt_tile_count"],
                "export_format": raster["format"],
                "raster_dimensions": f"{dimensions['width']}x{dimensions['height']}",
                "output_file_size": raster["file_size_bytes"],
                "georeferencing_status": "present" if raster["embedded_epsg"] == 3857 else "sidecar",
                "validation_status": value["validation_status"],
                "elapsed_seconds": round(float(value["elapsed_seconds"]), 3),
                "final_output_path": raster["path"],
                "error_or_warning": raster.get("fallback_warning") or "",
            }
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        base["validation_status"] = "invalid_validation_record"
        base["error_or_warning"] = str(error)
    return base


def _hash_files(paths: list[Path], output_root: Path) -> str:
    return "".join(f"{_sha256(path)}  {path.relative_to(output_root).as_posix()}\n" for path in paths)


def write_batch_outputs(output_root: Path, areas: list[AreaRecord]) -> dict[str, str]:
    output_root.mkdir(parents=True, exist_ok=True)
    rows = [_summary_row(area, output_root) for area in areas]
    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=SUMMARY_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    csv_path = output_root / "batch-summary.csv"
    json_path = output_root / "batch-summary.json"
    report_path = output_root / "batch-report.md"
    checksums_path = output_root / "checksums.sha256"
    inventory_path = output_root / "file-inventory.csv"
    atomic_write_text(csv_path, csv_buffer.getvalue())
    atomic_write_json(json_path, {"schema_version": 1, "areas": rows})

    completed = sum(row["validation_status"] == "passed" for row in rows)
    failed = sum(row["validation_status"] not in {"passed", "pending"} for row in rows)
    table_lines = [
        "| Code | Name | Provider | Zoom | Tiles | Format | Dimensions | Validation |",
        "| --- | --- | --- | ---: | ---: | --- | --- | --- |",
    ]
    for row in rows:
        name = str(row["name"]).replace("|", "\\|")
        tile_text = f"{row['downloaded_tiles']}/{row['estimated_tiles']}" if row["estimated_tiles"] != "" else "-"
        table_lines.append(
            f"| {row['code']} | {name} | {row['provider'] or '-'} | {row['zoom'] or '-'} | "
            f"{tile_text} | {row['export_format'] or '-'} | {row['raster_dimensions'] or '-'} | "
            f"{row['validation_status']} |"
        )
    report = "\n".join(
        (
            "# SAS.Planet imagery export batch report",
            "",
            f"- Passed: {completed}",
            f"- Failed: {failed}",
            f"- Pending: {len(rows) - completed - failed}",
            "",
            *table_lines,
            "",
            "Detailed validation records and previews are stored under `output/<area_code>/`.",
            "The complete generated-file listing is in `file-inventory.csv`.",
            "",
        )
    )
    atomic_write_text(report_path, report)

    checksum_candidates = sorted(
        path
        for path in output_root.rglob("*")
        if path.is_file() and path.name not in {checksums_path.name, inventory_path.name}
    )
    atomic_write_text(checksums_path, _hash_files(checksum_candidates, output_root))

    inventory_candidates = sorted(path for path in output_root.rglob("*") if path.is_file() and path != inventory_path)
    inventory_buffer = io.StringIO(newline="")
    inventory_writer = csv.DictWriter(
        inventory_buffer,
        fieldnames=("path", "size_bytes"),
        lineterminator="\n",
    )
    inventory_writer.writeheader()
    inventory_writer.writerows(
        {"path": path.relative_to(output_root).as_posix(), "size_bytes": path.stat().st_size}
        for path in inventory_candidates
    )
    atomic_write_text(inventory_path, inventory_buffer.getvalue())
    return {
        "batch_summary_csv": str(csv_path.resolve()),
        "batch_summary_json": str(json_path.resolve()),
        "batch_report": str(report_path.resolve()),
        "checksums": str(checksums_path.resolve()),
        "file_inventory": str(inventory_path.resolve()),
    }
