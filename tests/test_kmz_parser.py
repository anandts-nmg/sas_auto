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


def test_html_metadata_parsing_supports_bold_keys_in_td_cells() -> None:
    html = (
        "<table><tr><td><b>Сонгон шалгаруулалт:</b></td><td>92</td></tr>"
        "<tr><td><b>Аймаг:</b></td><td>Баян-Өлгий</td></tr></table>"
    )
    assert parse_html_metadata(html) == {"Сонгон шалгаруулалт": "92", "Аймаг": "Баян-Өлгий"}


def _write_kmz(path: Path, kml: str, member: str = "doc.kml") -> Path:
    with ZipFile(path, "w") as archive:
        archive.writestr(member, kml)
    return path


def _generic_kml(*placemarks: str, namespace: bool = True) -> str:
    xmlns = ' xmlns="http://www.opengis.net/kml/2.2"' if namespace else ""
    return f'<?xml version="1.0" encoding="UTF-8"?><kml{xmlns}><Document>{"".join(placemarks)}</Document></kml>'


def _polygon_placemark(name: str, coordinates: str, *, extra: str = "", placemark_id: str = "") -> str:
    identifier = f' id="{placemark_id}"' if placemark_id else ""
    return (
        f"<Placemark{identifier}><name>{name}</name>{extra}<Polygon><outerBoundaryIs><LinearRing>"
        f"<coordinates>{coordinates}</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>"
    )


def test_non_doc_kml_member_is_supported(tmp_path: Path) -> None:
    path = _write_kmz(
        tmp_path / "other_member.kmz",
        _generic_kml(_polygon_placemark("Area A", "100,45 101,45 101,46 100,45")),
        "nested/areas.kml",
    )
    result = inspect_kmz(path)
    assert result.kml_members == ["nested/areas.kml"]
    assert result.profile == "generic_polygons"
    assert [area.feature_id for area in result.areas] == ["area_a"]


def test_namespaceless_kml_is_supported_without_duplicate_placemarks(tmp_path: Path) -> None:
    path = _write_kmz(
        tmp_path / "namespaceless.kmz",
        _generic_kml(
            _polygon_placemark("Unicode Монгол", "100,45 101,45 101,46 100,45"),
            namespace=False,
        ),
    )
    result = inspect_kmz(path)
    assert result.placemark_count == 1
    assert len(result.areas) == 1
    assert result.areas[0].feature_name == "Unicode Монгол"


def test_archive_without_polygon_is_rejected(tmp_path: Path) -> None:
    path = _write_kmz(tmp_path / "points_only.kmz", _generic_kml("<Placemark><Point/></Placemark>"))
    with pytest.raises(ValueError, match="no Polygon"):
        inspect_kmz(path)


def test_auto_detects_non_91_tender_style_without_hardcoded_codes(tmp_path: Path) -> None:
    description = (
        "<description><![CDATA[<table><tr><td><b>Сонгон шалгаруулалт:</b></td><td>92</td></tr>"
        "<tr><td><b>Код:</b></td><td>СШ 9201</td></tr><tr><td><b>Талбай:</b></td><td>Шинэ талбай</td></tr>"
        "<tr><td><b>Аймаг:</b></td><td>Баян-Өлгий</td></tr>"
        "<tr><td><b>Сум:</b></td><td>Цэнгэл</td></tr>"
        "<tr><td><b>Координатын тоо:</b></td><td>3</td></tr></table>]]></description>"
        '<ExtendedData><Data name="selection_no"><value>92</value></Data>'
        '<Data name="area_code"><value>9201</value></Data>'
        '<Data name="area_name"><value>Шинэ талбай</value></Data></ExtendedData>'
    )
    path = _write_kmz(
        tmp_path / "Selection_92_All_Areas.kmz",
        _generic_kml(_polygon_placemark("92_0001_Шинэ талбай", "100,45 101,45 101,46 100,45", extra=description)),
    )
    result = inspect_kmz(path)
    assert result.profile == "tender_areas"
    assert result.dataset_id.startswith("selection_92_")
    assert len(result.dataset_id.rsplit("_", 1)[1]) == 8
    assert [area.feature_id for area in result.areas] == ["9201"]
    assert result.areas[0].tender_number == "92"
    assert result.areas[0].aimag == "Баян-Өлгий"
    assert result.areas[0].soum == "Цэнгэл"
    assert result.errors == []


def test_dash_number_point_names_are_classified_as_vertices(tmp_path: Path) -> None:
    path = _write_kmz(
        tmp_path / "dash_vertices.kmz",
        _generic_kml(
            _polygon_placemark("Area A", "100,45 101,45 101,46 100,45"),
            "<Placemark><name>area_a-001</name><Point><coordinates>100,45</coordinates></Point></Placemark>",
            "<Placemark><name>Area A - Centroid</name><Point><coordinates>100.5,45.5</coordinates></Point></Placemark>",
        ),
    )
    result = inspect_kmz(path)
    assert result.vertex_point_count == 1
    assert result.auxiliary_point_count == 1


def test_closed_tender_polygon_count_mismatch_with_all_markers_is_a_warning(tmp_path: Path) -> None:
    description = (
        "<description><![CDATA[<table><tr><th>Сонгон шалгаруулалт</th><td>92</td></tr>"
        "<tr><th>Код</th><td>9201</td></tr><tr><th>Талбай</th><td>Шинэ талбай</td></tr>"
        "<tr><th>Координатын тоо</th><td>4</td></tr></table>]]></description>"
    )
    points = tuple(
        f"<Placemark><name>9201-{number:03d}</name><Point><coordinates>100,45</coordinates></Point></Placemark>"
        for number in range(1, 5)
    )
    path = _write_kmz(
        tmp_path / "declared_mismatch.kmz",
        _generic_kml(
            _polygon_placemark("92_0001_Шинэ талбай", "100,45 101,45 101,46 100,45", extra=description),
            *points,
        ),
    )
    result = inspect_kmz(path)
    mismatch = [message for message in result.warnings if message.code == "declared_coordinate_count_mismatch"]
    assert len(mismatch) == 1
    assert "original polygon geometry is retained without repair" in mismatch[0].message.lower()
    assert result.errors == []


def test_multigeometry_holes_ids_and_namespaced_outputs(tmp_path: Path) -> None:
    multi = """
    <Placemark id="source-7"><name>Two Parts</name>
      <ExtendedData><Data name="code"><value>custom-7</value></Data></ExtendedData>
      <MultiGeometry>
        <Polygon><outerBoundaryIs><LinearRing><coordinates>100,45 102,45 102,47 100,47 100,45</coordinates></LinearRing></outerBoundaryIs>
          <innerBoundaryIs><LinearRing><coordinates>100.5,45.5 101,45.5 101,46 100.5,45.5</coordinates></LinearRing></innerBoundaryIs>
        </Polygon>
        <Polygon><outerBoundaryIs><LinearRing><coordinates>103,45 104,45 104,46 103,45</coordinates></LinearRing></outerBoundaryIs></Polygon>
      </MultiGeometry>
    </Placemark>
    """
    path = _write_kmz(tmp_path / "custom_data.kmz", _generic_kml(multi))
    result = inspect_kmz(path)
    area = result.areas[0]
    assert area.feature_id == "custom-7"
    assert area.geometry_type == "MultiPolygon"
    assert len(area.polygons) == 2
    assert area.hole_count == 1

    outputs = generate_outputs(result, tmp_path, namespace_outputs=True)
    assert Path(outputs["areas_json"]).parent == tmp_path / "generated" / result.dataset_id / "manifest"
    geojson = json.loads(Path(outputs["geojson"]).read_text(encoding="utf-8"))
    geometry = geojson["features"][0]["geometry"]
    assert geometry["type"] == "MultiPolygon"
    assert len(geometry["coordinates"]) == 2
    assert len(geometry["coordinates"][0]) == 2
    tree = ET.parse(outputs["individual_kml"][0])
    assert len(tree.findall(".//{*}Polygon")) == 2
    assert len(tree.findall(".//{*}innerBoundaryIs")) == 1
    source_fields = {
        node.get("name"): node.findtext("./{*}value") for node in tree.findall(".//{*}ExtendedData/{*}Data")
    }
    assert source_fields["source:code"] == "custom-7"


def test_generic_duplicate_names_receive_stable_unique_ids(tmp_path: Path) -> None:
    polygon = _polygon_placemark("Same Name", "100,45 101,45 101,46 100,45")
    path = _write_kmz(tmp_path / "duplicates.kmz", _generic_kml(polygon, polygon))
    result = inspect_kmz(path)
    assert [area.feature_id for area in result.areas] == ["same_name", "same_name_2"]


def test_long_duplicate_ids_remain_portable(tmp_path: Path) -> None:
    long_name = "a" * 120
    polygon = _polygon_placemark(long_name, "100,45 101,45 101,46 100,45")
    result = inspect_kmz(_write_kmz(tmp_path / "long.kmz", _generic_kml(polygon, polygon)))
    feature_ids = {area.feature_id for area in result.areas}
    assert all(len(feature_id) == 100 for feature_id in feature_ids)
    assert any(feature_id.endswith("_2") for feature_id in feature_ids)
    assert result.errors == []


def test_polygon_outside_web_mercator_is_rejected(tmp_path: Path) -> None:
    path = _write_kmz(
        tmp_path / "polar.kmz",
        _generic_kml(_polygon_placemark("Polar", "100,86 101,86 101,87 100,86")),
    )
    result = inspect_kmz(path)
    assert any(message.code == "outside_web_mercator" for message in result.errors)


def test_unclosed_source_ring_is_recorded_and_closed_only_in_derived_output(tmp_path: Path) -> None:
    path = _write_kmz(
        tmp_path / "unclosed.kmz",
        _generic_kml(_polygon_placemark("Unclosed", "100,45 101,45 101,46")),
    )
    result = inspect_kmz(path)
    area = result.areas[0]
    assert area.source_closed is False
    assert len(area.coordinates) == 3
    assert result.errors == []
    assert any(message.code == "unclosed_source_polygon" for message in result.warnings)

    outputs = generate_outputs(result, tmp_path)
    tree = ET.parse(outputs["individual_kml"][0])
    coordinate_text = tree.findtext(".//{*}coordinates") or ""
    assert len(coordinate_text.split()) == 4
    assert area.repairs


def test_reserved_windows_feature_name_is_safely_prefixed(tmp_path: Path) -> None:
    path = _write_kmz(
        tmp_path / "reserved.kmz",
        _generic_kml(_polygon_placemark("CON", "100,45 101,45 101,46 100,45")),
    )
    result = inspect_kmz(path)
    assert result.areas[0].feature_id == "feature_con"
    assert result.errors == []


def test_auto_dataset_ids_distinguish_same_filename_with_different_content(tmp_path: Path) -> None:
    first = tmp_path / "first" / "areas.kmz"
    second = tmp_path / "second" / "areas.kmz"
    first.parent.mkdir()
    second.parent.mkdir()
    _write_kmz(first, _generic_kml(_polygon_placemark("A", "100,45 101,45 101,46 100,45")))
    _write_kmz(second, _generic_kml(_polygon_placemark("B", "102,45 103,45 103,46 102,45")))

    first_result = inspect_kmz(first)
    second_result = inspect_kmz(second)
    repeated_first_result = inspect_kmz(first)
    assert first_result.dataset_id != second_result.dataset_id
    assert first_result.dataset_id == repeated_first_result.dataset_id
    assert first_result.dataset_id.startswith("areas_")
    assert second_result.dataset_id.startswith("areas_")


def test_manifest_geojson_and_individual_kml_generation(actual_result: InspectionResult, tmp_path: Path) -> None:
    outputs = generate_outputs(actual_result, tmp_path)
    manifest = json.loads(Path(outputs["areas_json"]).read_text(encoding="utf-8"))
    geojson = json.loads(Path(outputs["geojson"]).read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    assert len(manifest["areas"]) == 20
    assert manifest["areas"][0]["area_code"] == "9101"
    assert len(geojson["features"]) == 20
    assert len(outputs["individual_kml"]) == 20
    area_tree = ET.parse(tmp_path / "generated" / "kml" / "9101.kml")
    assert len(area_tree.findall(".//{*}Placemark")) == 1
    assert len(area_tree.findall(".//{*}Polygon")) == 1
    assert len(area_tree.findall(".//{*}Point")) == 0
