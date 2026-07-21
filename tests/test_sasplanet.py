from __future__ import annotations

from pathlib import Path

from sas_auto.models import InspectionResult
from sas_auto.sasplanet import (
    MapDefinition,
    SessionSettings,
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


def test_installed_provider_discovery() -> None:
    sas_exe = Path(r"C:\Users\anand.ts\Downloads\SAS.Planet.Release.260404.x64\SASPlanet.exe")
    definitions = discover_map_definitions(sas_exe)
    google = find_requested_provider(definitions, "Google Satellite")
    esri = find_requested_provider(definitions, "Esri World Imagery")
    assert google is not None and google.name == "Google - Satellite"
    assert esri is not None and esri.name == "ESRI ArcGIS.Imagery"


def test_executable_capabilities_are_detected_read_only() -> None:
    sas_exe = Path(r"C:\Users\anand.ts\Downloads\SAS.Planet.Release.260404.x64\SASPlanet.exe")
    capabilities = detect_executable_capabilities(sas_exe)
    assert capabilities["--sls-autostart"] is True
    assert capabilities["--map"] is True
    assert capabilities["--zoom"] is True


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
