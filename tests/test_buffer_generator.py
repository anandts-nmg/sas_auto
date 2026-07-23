from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from sas_auto.buffer_generator import METHOD, create_buffered_kmz, default_output_path, derive_footprints
from sas_auto.geometry import calculate_bounds, calculate_centroid
from sas_auto.kmz_parser import inspect_kmz
from sas_auto.models import AreaRecord, Coordinate, InspectionResult, PolygonPart

REFERENCE_9101 = """
92.3831861111,46.3938638889 92.3831861111,46.3726138889 92.3905027778,46.3726138889
92.3855944444,46.358725 92.413725,46.358725 92.4007888889,46.3505472222
92.3842916667,46.3401166667 92.3600611111,46.358725 92.3593361111,46.358725
92.3593361111,46.3627277778 92.3581916667,46.3627277778 92.3581833333,46.3731277778
92.3581916667,46.3731277778 92.3616666667,46.3731277778 92.3616666667,46.358725
92.3731944444,46.358725 92.3731944444,46.3746666667 92.3616833333,46.3746666667
92.3616833333,46.3768638889 92.3581833333,46.3768638889 92.3581833333,46.3932305556
92.3729555556,46.3936055556 92.3729555556,46.3841722222 92.3720916667,46.3841722222
92.3720916667,46.3834 92.3732222222,46.3834 92.3732222222,46.3936111111
92.3831861111,46.3938638889
"""


def _reference_area() -> AreaRecord:
    coordinates = [
        Coordinate(float(longitude), float(latitude))
        for token in REFERENCE_9101.split()
        for longitude, latitude in [token.split(",")]
    ]
    return AreaRecord(
        tender_number="91",
        area_code="9101",
        area_name="Уудавын булаг",
        placemark_name="91_0001_Уудавын_булаг - Boundary Polygon",
        aimag="Ховд",
        soum="Алтай",
        area_hectares=1054.29,
        declared_coordinate_count=27,
        coordinate_system="WGS84 (EPSG:4326)",
        source_url="https://example.test/91",
        geometry_type="Polygon",
        coordinates=coordinates,
        bounds=calculate_bounds(coordinates),
        center=calculate_centroid(coordinates),
        source_closed=True,
        polygons=[PolygonPart(outer=coordinates, outer_source_closed=True)],
        profile="tender_areas",
    )


def _result(area: AreaRecord, source_path: Path) -> InspectionResult:
    return InspectionResult(
        input_path=str(source_path),
        input_sha256=hashlib.sha256(source_path.read_bytes()).hexdigest(),
        archive_entries=["doc.kml"],
        placemark_count=1,
        polygon_placemark_count=1,
        point_placemark_count=0,
        vertex_point_count=0,
        auxiliary_point_count=0,
        areas=[area],
        dataset_id="selection_91_test",
        profile="tender_areas",
        kml_members=["doc.kml"],
    )


def test_reference_rectangle_matches_selection_91_local_aeqd_result() -> None:
    derived = derive_footprints(_reference_area(), 1000.0)
    expected = [
        (92.376940469, 46.401043717),
        (92.417773406, 46.354062779),
        (92.377848686, 46.337431036),
        (92.336993916, 46.384397848),
    ]
    actual = list(derived.rectangle.exterior.coords)[:-1]
    for longitude, latitude in actual:
        nearest_error = min(abs(longitude - x) + abs(latitude - y) for x, y in expected)
        assert nearest_error < 2e-8
    assert derived.rectangle_area_hectares == pytest.approx(2185.80, abs=0.01)
    assert derived.buffer_area_hectares == pytest.approx(4522.00, abs=0.01)
    assert derived.base_name == "91_0001_Уудавын_булаг"


def test_create_buffered_kmz_is_separate_parseable_and_metadata_rich(tmp_path: Path) -> None:
    source = tmp_path / "Selection_91_All_Areas.kmz"
    source.write_bytes(b"immutable-source-sentinel")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    output = tmp_path / "Selection_91_Rectangles_1km_Buffer.kmz"

    created = create_buffered_kmz(_result(_reference_area(), source), output, buffer_meters=1000.0)

    assert output.is_file()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    assert created["source_feature_count"] == 1
    assert created["derived_feature_count"] == 2
    parsed = inspect_kmz(output, profile="generic_polygons", dataset_id="buffer_test")
    assert not parsed.errors
    assert len(parsed.areas) == 2
    assert {area.metadata["derived_kind"] for area in parsed.areas} == {"rectangle", "buffer"}
    assert all(area.metadata["Арга"] == METHOD for area in parsed.areas)
    assert all(area.aimag == "Ховд" and area.soum == "Алтай" for area in parsed.areas)
    assert all(area.area_hectares > 0 for area in parsed.areas)


def test_default_output_path_removes_all_areas_suffix() -> None:
    source = Path("inputs/Selection_92_All_Areas.kmz")
    assert default_output_path(source, 1000.0) == Path("inputs/Selection_92_Rectangles_1km_Buffer.kmz")
