from __future__ import annotations

from pathlib import Path

from sas_auto.sasplanet import (
    detect_executable_capabilities,
    discover_map_definitions,
    find_requested_provider,
)


def test_installed_provider_discovery(project_root):
    sas_exe = Path(r"C:\Users\anand.ts\Downloads\SAS.Planet.Release.260404.x64\SASPlanet.exe")
    definitions = discover_map_definitions(sas_exe)
    google = find_requested_provider(definitions, "Google Satellite")
    esri = find_requested_provider(definitions, "Esri World Imagery")
    assert google is not None and google.name == "Google - Satellite"
    assert esri is not None and esri.name == "ESRI ArcGIS.Imagery"


def test_executable_capabilities_are_detected_read_only():
    sas_exe = Path(r"C:\Users\anand.ts\Downloads\SAS.Planet.Release.260404.x64\SASPlanet.exe")
    capabilities = detect_executable_capabilities(sas_exe)
    assert capabilities["--sls-autostart"] is True
    assert capabilities["--map"] is True
    assert capabilities["--zoom"] is True
