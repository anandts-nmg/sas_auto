from __future__ import annotations

from pathlib import Path

from sas_auto.geometry import calculate_bounds, calculate_centroid
from sas_auto.models import AreaRecord, Coordinate, InspectionResult, PolygonPart
from sas_auto.sasplanet import (
    MapDefinition,
    SessionSettings,
    create_download_plan,
    create_sls_session,
    detect_executable_capabilities,
    discover_map_definitions,
    find_requested_provider,
    sasplanet_command,
    session_path_for,
    validate_sls_session,
)


def _settings(tmp_path: Path, *, missing_only: bool = True) -> SessionSettings:
    return SessionSettings(
        provider=MapDefinition(
            name="ESRI ArcGIS.Imagery",
            guid="{7B743985-BC5F-4AB6-8915-AC5DBBB8F552}",
            params_path="Maps/ESRI/params.txt",
            active=True,
        ),
        zoom_levels=(16,),
        download_missing_tiles_only=missing_only,
        auto_close_at_finish=False,
        workers_count=1,
        session_directory=tmp_path,
    )


def _fake_install(tmp_path: Path) -> Path:
    sas_exe = tmp_path / "SASPlanet.exe"
    sas_exe.write_bytes(b"--map --zoom --sls-autostart")
    definitions = (
        ("Google.zmp", "Google - Satellite", "{00000000-0000-0000-0000-000000000001}"),
        ("Esri.zmp", "ESRI ArcGIS.Imagery", "{00000000-0000-0000-0000-000000000002}"),
    )
    for directory, name, guid in definitions:
        params = tmp_path / "Maps" / directory / "params.txt"
        params.parent.mkdir(parents=True)
        params.write_text(f"[PARAMS]\nname={name}\nGUID={guid}\n", encoding="utf-8")
    return sas_exe


def test_provider_discovery_from_portable_fixture(tmp_path: Path) -> None:
    sas_exe = _fake_install(tmp_path)
    definitions = discover_map_definitions(sas_exe)
    google = find_requested_provider(definitions, "Google Satellite")
    esri = find_requested_provider(definitions, "Esri World Imagery")
    assert google is not None and google.name == "Google - Satellite"
    assert esri is not None and esri.name == "ESRI ArcGIS.Imagery"


def test_executable_capabilities_are_detected_read_only(tmp_path: Path) -> None:
    sas_exe = _fake_install(tmp_path)
    capabilities = detect_executable_capabilities(sas_exe)
    assert capabilities["--sls-autostart"] is True
    assert capabilities["--map"] is True
    assert capabilities["--zoom"] is True


def test_plan_uses_sas_one_based_zoom_and_enforces_scope_limits(
    actual_result: InspectionResult, tmp_path: Path
) -> None:
    sas_exe = _fake_install(tmp_path)
    settings = _settings(tmp_path)
    artifact = create_sls_session([actual_result.areas[0]], settings)
    plan = create_download_plan(
        artifact,
        [actual_result.areas[0]],
        settings,
        sas_exe,
        tmp_path,
        max_rectangle_tiles_per_feature=1,
        max_rectangle_tiles_total=1,
    )

    estimate = plan["rectangle_tile_estimates"][0]["zooms"][0]
    assert estimate["sasplanet_zoom"] == 16
    assert estimate["web_mercator_tile_matrix_zoom"] == 15
    assert plan["safety"]["within_limits"] is False
    assert plan["safety"]["violations"]


def test_single_area_sls_matches_installed_session_schema(actual_result: InspectionResult, tmp_path: Path) -> None:
    area = actual_result.areas[0]
    settings = _settings(tmp_path)
    artifact = create_sls_session([area], settings)
    path = Path(artifact.path)
    raw = path.read_bytes()
    text = raw.decode("ascii")

    assert path.name == "9101_ESRI_Z16.sls"
    assert raw.startswith(b"[Session]\r\n")
    assert f"MapGUID={settings.provider.guid}\r\n" in text
    assert "Zoom=16\r\nZoomArr=16\r\n" in text
    assert "ReplaceExistTiles=0\r\n" in text
    assert "AutoCloseAtFinish=0\r\n" in text
    assert text.count("PointLon_") == len(area.coordinates)
    assert text.count("PointLat_") == len(area.coordinates)
    assert "=NAN" not in text
    assert artifact.coordinate_count == len(area.coordinates)
    assert artifact.area_codes == ("9101",)
    assert len(artifact.sha256) == 64
    assert validate_sls_session(path) == []


def test_combined_sls_contains_only_polygons_and_nan_separators(
    actual_result: InspectionResult, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    artifact = create_sls_session(actual_result.areas, settings)
    text = Path(artifact.path).read_text(encoding="ascii")
    expected_coordinates = sum(len(area.coordinates) for area in actual_result.areas)

    assert Path(artifact.path).name == "ALL_KMZ_ESRI_Z16.sls"
    assert artifact.polygon_count == 20
    assert artifact.coordinate_count == expected_coordinates
    assert artifact.area_codes == tuple(str(value) for value in range(9101, 9121))
    assert text.count("=NAN") == 2 * (artifact.polygon_count - 1)
    assert text.count("PointLon_") == expected_coordinates + artifact.polygon_count - 1
    assert "9101_V001" not in text
    assert validate_sls_session(Path(artifact.path)) == []


def test_sls_preserves_multipolygon_parts_and_holes(tmp_path: Path) -> None:
    first_outer = [
        Coordinate(100, 45),
        Coordinate(102, 45),
        Coordinate(102, 47),
        Coordinate(100, 45),
    ]
    hole = [
        Coordinate(100.5, 45.5),
        Coordinate(101, 45.5),
        Coordinate(101, 46),
        Coordinate(100.5, 45.5),
    ]
    second_outer = [
        Coordinate(103, 45),
        Coordinate(104, 45),
        Coordinate(104, 46),
        Coordinate(103, 45),
    ]
    all_outer = [*first_outer, *second_outer]
    area = AreaRecord(
        tender_number="",
        area_code="multi",
        area_name="Multi polygon",
        placemark_name="Multi polygon",
        aimag="",
        soum="",
        area_hectares=0,
        declared_coordinate_count=6,
        coordinate_system="WGS 84 / EPSG:4326",
        source_url="",
        geometry_type="MultiPolygon",
        coordinates=first_outer,
        bounds=calculate_bounds(all_outer),
        center=calculate_centroid(first_outer),
        source_closed=True,
        polygons=[PolygonPart(first_outer, [hole]), PolygonPart(second_outer)],
        profile="generic_polygons",
    )

    artifact = create_sls_session([area], _settings(tmp_path))
    text = Path(artifact.path).read_text(encoding="ascii")

    assert artifact.polygon_count == 2
    assert artifact.coordinate_count == len(first_outer) + len(hole) + len(second_outer)
    assert text.count("=NAN") == 3
    assert "PointLat_4=-1" in text
    assert validate_sls_session(Path(artifact.path)) == []


def test_replace_existing_tiles_is_explicit(actual_result: InspectionResult, tmp_path: Path) -> None:
    artifact = create_sls_session([actual_result.areas[0]], _settings(tmp_path, missing_only=False))
    text = Path(artifact.path).read_text(encoding="ascii")
    assert "ReplaceExistTiles=1\n" in text


def test_session_path_and_launch_command_are_deterministic(actual_result: InspectionResult, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    session_path = session_path_for(settings, [actual_result.areas[0]])
    sas_exe = Path(r"C:\SASPlanet\SASPlanet.exe")
    assert sasplanet_command(sas_exe, session_path) == [
        str(sas_exe.resolve()),
        "--sls-autostart",
        str(session_path.resolve()),
    ]


def test_invalid_sls_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.sls"
    path.write_text("[Session]\nZoom=16\n", encoding="ascii")
    errors = validate_sls_session(path)
    assert any("MapGUID" in error for error in errors)
    assert any("polygon points" in error for error in errors)
