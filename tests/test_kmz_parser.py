from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile

import pytest

from sas_auto.kmz_parser import generate_outputs, inspect_kmz, parse_html_metadata
from sas_auto.models import InspectionResult


def test_archive_contains_doc_kml(actual_result: InspectionResult) -> None:
    assert actual_result.archive_entries == ["doc.kml"]


def test_namespace_and_verified_geometry_inventory(actual_result: InspectionResult) -> None:
    assert actual_result.placemark_count == 874
    assert actual_result.polygon_placemark_count == 20
    assert actual_result.point_placemark_count == 854
    assert actual_result.vertex_point_count == 814
    assert actual_result.auxiliary_point_count == 40


def test_unicode_names_are_preserved(actual_result: InspectionResult) -> None:
    assert actual_result.areas[0].area_name == "Уудавын булаг"
    assert actual_result.areas[-1].area_name == "Хөх хадны худаг"


def test_polygon_versus_point_classification(actual_result: InspectionResult) -> None:
    assert len(actual_result.areas) == 20
    assert all(area.geometry_type == "Polygon" for area in actual_result.areas)
    assert sum(area.declared_coordinate_count for area in actual_result.areas) == actual_result.vertex_point_count


def test_expected_codes_and_no_validation_errors(actual_result: InspectionResult) -> None:
    assert [area.area_code for area in actual_result.areas] == [str(value) for value in range(9101, 9121)]
    assert actual_result.errors == []
    assert actual_result.warnings == []


def test_html_metadata_parsing() -> None:
    html = "<table><tr><th>Талбай</th><td>Уудавын <b>булаг</b></td></tr><tr><th>Код</th><td>9101</td></tr></table>"
    assert parse_html_metadata(html) == {"Талбай": "Уудавын булаг", "Код": "9101"}


def test_missing_doc_kml_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.kmz"
    with ZipFile(path, "w") as archive:
        archive.writestr("other.kml", "<kml/>")
    with pytest.raises(ValueError, match=r"doc\.kml"):
        inspect_kmz(path)


def test_wrong_kml_namespace_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad_namespace.kmz"
    with ZipFile(path, "w") as archive:
        archive.writestr("doc.kml", "<kml><Document/></kml>")
    with pytest.raises(ValueError, match="namespace"):
        inspect_kmz(path)


def test_manifest_geojson_and_individual_kml_generation(actual_result: InspectionResult, tmp_path: Path) -> None:
    outputs = generate_outputs(actual_result, tmp_path)
    manifest = json.loads(Path(outputs["areas_json"]).read_text(encoding="utf-8"))
    geojson = json.loads(Path(outputs["geojson"]).read_text(encoding="utf-8"))
    assert len(manifest["areas"]) == 20
    assert manifest["areas"][0]["area_code"] == "9101"
    assert len(geojson["features"]) == 20
    assert len(outputs["individual_kml"]) == 20
    area_tree = ET.parse(tmp_path / "generated" / "kml" / "9101.kml")
    assert len(area_tree.findall(".//{*}Placemark")) == 1
    assert len(area_tree.findall(".//{*}Polygon")) == 1
    assert len(area_tree.findall(".//{*}Point")) == 0
