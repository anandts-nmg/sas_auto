from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from sas_auto.geometry import calculate_bounds, calculate_centroid
from sas_auto.models import AreaRecord, Coordinate
from sas_auto.raster_export import (
    ExportSettings,
    cache_database_path,
    discover_sqlite_cache_root,
    export_area_from_cache,
    polygon_tile_coordinates,
    sas_zoom_to_web_zoom,
    write_batch_outputs,
)
from sas_auto.sasplanet import MapDefinition


def _area() -> AreaRecord:
    coordinates = [
        Coordinate(1.0, 2.0),
        Coordinate(2.0, 2.0),
        Coordinate(2.0, 1.0),
        Coordinate(1.0, 1.0),
        Coordinate(1.0, 2.0),
    ]
    return AreaRecord(
        tender_number="91",
        area_code="9101",
        area_name="Туршилтын талбай",
        placemark_name="91_0001_Туршилтын талбай",
        aimag="Туршилтын аймаг",
        soum="Туршилтын сум",
        area_hectares=100.0,
        declared_coordinate_count=4,
        coordinate_system="WGS 84 / EPSG:4326",
        source_url="https://example.test/9101",
        geometry_type="Polygon",
        coordinates=coordinates,
        bounds=calculate_bounds(coordinates),
        center=calculate_centroid(coordinates),
        source_closed=True,
    )


def _provider(tmp_path: Path) -> MapDefinition:
    params_path = tmp_path / "Maps" / "ArcGIS.Imagery.zmp" / "params.txt"
    params_path.parent.mkdir(parents=True)
    params_path.write_text(
        "[PARAMS]\nname=ESRI ArcGIS.Imagery\nNameInCache=ArcGIS.Imagery\nGUID={7B743985-BC5F-4AB6-8915-AC5DBBB8F552}\n",
        encoding="utf-8",
    )
    return MapDefinition(
        name="ESRI ArcGIS.Imagery",
        guid="{7B743985-BC5F-4AB6-8915-AC5DBBB8F552}",
        params_path=str(params_path),
        active=True,
    )


def _sasplanet(tmp_path: Path) -> Path:
    executable = tmp_path / "SASPlanet.exe"
    executable.write_bytes(b"test executable")
    (tmp_path / "cache_sqlite").mkdir()
    (tmp_path / "SASPlanet.ini").write_text("[PathToCache]\nSQLiteCache=cache_sqlite\n", encoding="utf-8")
    return executable


def _jpeg_payload() -> bytes:
    image = Image.new("RGB", (256, 256), (40, 80, 30))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 127, 127), fill=(170, 140, 80))
    draw.ellipse((64, 64, 220, 220), fill=(30, 90, 180))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _populate_cache(cache_root: Path, cache_name: str, sas_zoom: int, area: AreaRecord) -> int:
    tiles = polygon_tile_coordinates(area, sas_zoom)
    grouped: dict[Path, list[tuple[int, int]]] = {}
    for tile in tiles:
        grouped.setdefault(cache_database_path(cache_root, cache_name, sas_zoom, tile), []).append((tile.x, tile.y))
    payload = _jpeg_payload()
    for database_path, coordinates in grouped.items():
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path)
        try:
            connection.execute(
                "CREATE TABLE t (x INTEGER, y INTEGER, v INTEGER, c INTEGER, s INTEGER, h INTEGER, d BLOB, b BLOB)"
            )
            connection.executemany(
                "INSERT INTO t (x, y, v, c, s, h, d, b) VALUES (?, ?, 0, 0, 0, 0, ?, ?)",
                ((x, y, b"metadata", payload) for x, y in coordinates),
            )
            connection.commit()
        finally:
            connection.close()
    return len(tiles)


def _settings(tmp_path: Path) -> ExportSettings:
    return ExportSettings(
        output_directory=tmp_path / "output",
        preferred_format="GeoTIFF",
        fallback_format="JPEG",
        include_georeferencing=True,
        mask_to_polygon=True,
        preview_max_size=512,
        require_complete_cache=True,
    )


def test_zoom_and_polygon_cache_coverage() -> None:
    area = _area()
    assert sas_zoom_to_web_zoom(6) == 5
    tiles = polygon_tile_coordinates(area, 6)
    assert tiles
    assert len(tiles) == len(set(tiles))


def test_export_georeferenced_raster_from_sqlite_cache(tmp_path: Path) -> None:
    area = _area()
    executable = _sasplanet(tmp_path)
    provider = _provider(tmp_path)
    cache_root = discover_sqlite_cache_root(executable)
    tile_count = _populate_cache(cache_root, "ArcGIS.Imagery", 6, area)

    result = export_area_from_cache(area, executable, provider, 6, _settings(tmp_path))

    assert result["validation_status"] == "passed"
    assert result["cache"]["expected_tile_count"] == tile_count
    assert result["cache"]["cached_tile_count"] == tile_count
    assert result["cache"]["missing_tile_count"] == 0
    raster_path = Path(result["raster"]["path"])
    assert raster_path.name == "9101.tif"
    assert raster_path.stat().st_size > 256
    with Image.open(raster_path) as raster:
        assert raster.width > 0 and raster.height > 0
        tags = getattr(raster, "tag_v2", {})
        assert all(tag in tags for tag in (33550, 33922, 34735))
        assert 3857 in tuple(tags[34735])
    assert (tmp_path / "output" / "9101" / "9101.tfw").is_file()
    assert (tmp_path / "output" / "9101" / "9101.prj").is_file()
    assert (tmp_path / "output" / "9101" / "preview.png").is_file()
    validation = json.loads((tmp_path / "output" / "9101" / "validation.json").read_text(encoding="utf-8"))
    assert validation["area"]["name"] == "Туршилтын талбай"


def test_export_refuses_incomplete_cache(tmp_path: Path) -> None:
    area = _area()
    executable = _sasplanet(tmp_path)
    provider = _provider(tmp_path)
    with pytest.raises(FileNotFoundError, match="Missing"):
        export_area_from_cache(area, executable, provider, 6, _settings(tmp_path))


def test_batch_outputs_include_pending_areas(tmp_path: Path) -> None:
    areas = [_area()]
    paths = write_batch_outputs(tmp_path / "output", areas)
    summary = json.loads(Path(paths["batch_summary_json"]).read_text(encoding="utf-8"))
    assert summary["areas"][0]["code"] == "9101"
    assert summary["areas"][0]["validation_status"] == "pending"
    assert Path(paths["batch_report"]).is_file()
    assert Path(paths["checksums"]).is_file()
    assert Path(paths["file_inventory"]).is_file()
