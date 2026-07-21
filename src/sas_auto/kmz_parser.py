"""Read-only KMZ inspection and clean artifact generation."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterable
from html.parser import HTMLParser
from pathlib import Path
from typing import TypedDict
from zipfile import BadZipFile, ZipFile

from .geometry import (
    calculate_bounds,
    calculate_centroid,
    close_for_derived_output,
    format_number,
    is_closed,
    parse_coordinate_text,
)
from .models import AreaRecord, InspectionResult
from .validation import validate_areas

KML_NAMESPACE = "http://www.opengis.net/kml/2.2"
VERTEX_POINT_PATTERN = re.compile(r"^(?P<code>\d{4})_V(?P<number>\d+)$")


class GeneratedOutputs(TypedDict):
    areas_json: str
    areas_csv: str
    geojson: str
    combined_kml: str
    individual_kml: list[str]


class MetadataTableParser(HTMLParser):
    """Extract key/value pairs from the description's HTML table."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._current_tag: str | None = None
        self._buffer: list[str] = []
        self._cells: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"th", "td"}:
            self._current_tag = tag.lower()
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._current_tag is not None:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        current_tag = self._current_tag
        if current_tag is not None and current_tag == tag.lower():
            value = " ".join("".join(self._buffer).split())
            self._cells.append((current_tag, value))
            self._current_tag = None
            self._buffer = []

    def as_mapping(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        pending_key: str | None = None
        for tag, value in self._cells:
            if tag == "th":
                pending_key = value
            elif tag == "td" and pending_key is not None:
                mapping[pending_key] = value
                pending_key = None
        return mapping


def parse_html_metadata(description: str) -> dict[str, str]:
    parser = MetadataTableParser()
    parser.feed(description)
    parser.close()
    return parser.as_mapping()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _required(metadata: dict[str, str], key: str, placemark_name: str) -> str:
    value = metadata.get(key, "").strip()
    if not value:
        raise ValueError(f"Polygon {placemark_name!r} is missing description field {key!r}")
    return value


def _area_from_placemark(placemark: ET.Element) -> AreaRecord:
    placemark_name = (placemark.findtext("{*}name") or "").strip()
    description = placemark.findtext("{*}description") or ""
    metadata = parse_html_metadata(description)
    polygon_nodes = placemark.findall(".//{*}Polygon")
    if len(polygon_nodes) != 1:
        raise ValueError(
            f"Polygon area {placemark_name!r} has {len(polygon_nodes)} Polygon elements; exactly one is required"
        )
    coordinates_node = polygon_nodes[0].find("./{*}outerBoundaryIs/{*}LinearRing/{*}coordinates")
    if coordinates_node is None or not (coordinates_node.text or "").strip():
        raise ValueError(f"Polygon {placemark_name!r} has no outer LinearRing coordinates")
    coordinates = parse_coordinate_text(coordinates_node.text or "")
    if len(coordinates) < 3:
        raise ValueError(f"Polygon {placemark_name!r} has too few coordinates")
    try:
        area_hectares = float(_required(metadata, "Талбай (га)", placemark_name))
        declared_count = int(_required(metadata, "Координатын тоо", placemark_name))
    except ValueError as error:
        raise ValueError(f"Polygon {placemark_name!r} has invalid numeric metadata: {error}") from error
    source_closed = is_closed(coordinates)
    repairs: list[str] = []
    if not source_closed:
        repairs.append("Closed the source LinearRing in derived outputs by repeating the first coordinate.")
    return AreaRecord(
        tender_number=_required(metadata, "Сонгон шалгаруулалт", placemark_name),
        area_code=_required(metadata, "Код", placemark_name),
        area_name=_required(metadata, "Талбай", placemark_name),
        placemark_name=placemark_name,
        aimag=_required(metadata, "Аймаг", placemark_name),
        soum=_required(metadata, "Сум", placemark_name),
        area_hectares=area_hectares,
        declared_coordinate_count=declared_count,
        coordinate_system=_required(metadata, "Coordinate System", placemark_name),
        source_url=_required(metadata, "Эх сурвалж", placemark_name),
        geometry_type="Polygon",
        coordinates=coordinates,
        bounds=calculate_bounds(coordinates),
        center=calculate_centroid(coordinates),
        source_closed=source_closed,
        metadata=metadata,
        repairs=repairs,
    )


def inspect_kmz(path: Path) -> InspectionResult:
    """Inspect a KMZ in read-only mode and return its verified inventory."""
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(f"KMZ not found: {path}")
    try:
        with ZipFile(path, "r") as archive:
            bad_member = archive.testzip()
            if bad_member:
                raise ValueError(f"KMZ contains a corrupt archive entry: {bad_member}")
            entries = archive.namelist()
            if "doc.kml" not in entries:
                raise ValueError("KMZ must contain doc.kml at the archive root")
            kml_bytes = archive.read("doc.kml")
    except BadZipFile as error:
        raise ValueError(f"Input is not a valid ZIP/KMZ archive: {path}") from error

    root = ET.fromstring(kml_bytes)
    if root.tag != f"{{{KML_NAMESPACE}}}kml":
        raise ValueError(f"Unexpected KML root element or namespace: {root.tag}")

    placemarks = root.findall(".//{*}Placemark")
    areas: list[AreaRecord] = []
    polygon_count = 0
    point_count = 0
    vertex_point_counts: Counter[str] = Counter()
    auxiliary_point_count = 0

    for placemark in placemarks:
        polygon_nodes = placemark.findall(".//{*}Polygon")
        point_nodes = placemark.findall(".//{*}Point")
        if polygon_nodes:
            polygon_count += 1
            areas.append(_area_from_placemark(placemark))
        elif point_nodes:
            point_count += 1
            name = (placemark.findtext("{*}name") or "").strip()
            match = VERTEX_POINT_PATTERN.fullmatch(name)
            if match:
                vertex_point_counts[match.group("code")] += 1
            else:
                auxiliary_point_count += 1
            for point_node in point_nodes:
                coordinate_text = point_node.findtext("./{*}coordinates") or ""
                if coordinate_text.strip():
                    parse_coordinate_text(coordinate_text)

    areas.sort(key=lambda area: area.area_code)
    messages = validate_areas(areas, vertex_point_counts)
    return InspectionResult(
        input_path=str(path),
        input_sha256=_sha256(path),
        archive_entries=entries,
        placemark_count=len(placemarks),
        polygon_placemark_count=polygon_count,
        point_placemark_count=point_count,
        vertex_point_count=sum(vertex_point_counts.values()),
        auxiliary_point_count=auxiliary_point_count,
        areas=areas,
        validation_messages=messages,
    )


def _output_ring(area: AreaRecord) -> list:
    ring, repaired = close_for_derived_output(area.coordinates)
    if repaired:
        repair = "Closed the source LinearRing in derived outputs by repeating the first coordinate."
        if repair not in area.repairs:
            area.repairs.append(repair)
    return ring


def _kml_document(areas: Iterable[AreaRecord], document_name: str) -> ET.ElementTree:
    ET.register_namespace("", KML_NAMESPACE)
    root = ET.Element(f"{{{KML_NAMESPACE}}}kml")
    document = ET.SubElement(root, f"{{{KML_NAMESPACE}}}Document")
    ET.SubElement(document, f"{{{KML_NAMESPACE}}}name").text = document_name
    style = ET.SubElement(document, f"{{{KML_NAMESPACE}}}Style", {"id": "tender-area"})
    line_style = ET.SubElement(style, f"{{{KML_NAMESPACE}}}LineStyle")
    ET.SubElement(line_style, f"{{{KML_NAMESPACE}}}color").text = "ff00ffff"
    ET.SubElement(line_style, f"{{{KML_NAMESPACE}}}width").text = "2"
    poly_style = ET.SubElement(style, f"{{{KML_NAMESPACE}}}PolyStyle")
    ET.SubElement(poly_style, f"{{{KML_NAMESPACE}}}color").text = "4000ffff"
    for area in areas:
        placemark = ET.SubElement(document, f"{{{KML_NAMESPACE}}}Placemark")
        ET.SubElement(placemark, f"{{{KML_NAMESPACE}}}name").text = f"{area.area_code} - {area.area_name}"
        ET.SubElement(placemark, f"{{{KML_NAMESPACE}}}styleUrl").text = "#tender-area"
        description = (
            f"Tender: {area.tender_number}\nCode: {area.area_code}\nArea: {area.area_name}\n"
            f"Aimag: {area.aimag}\nSoum: {area.soum}\nArea (ha): {area.area_hectares}\n"
            f"CRS: {area.coordinate_system}\nSource: {area.source_url}\n{area.closure_note}"
        )
        ET.SubElement(placemark, f"{{{KML_NAMESPACE}}}description").text = description
        extended = ET.SubElement(placemark, f"{{{KML_NAMESPACE}}}ExtendedData")
        for key, value in area.manifest_dict().items():
            if isinstance(value, (str, int, float, bool)):
                data = ET.SubElement(extended, f"{{{KML_NAMESPACE}}}Data", {"name": key})
                ET.SubElement(data, f"{{{KML_NAMESPACE}}}value").text = str(value)
        polygon = ET.SubElement(placemark, f"{{{KML_NAMESPACE}}}Polygon")
        ET.SubElement(polygon, f"{{{KML_NAMESPACE}}}tessellate").text = "1"
        boundary = ET.SubElement(polygon, f"{{{KML_NAMESPACE}}}outerBoundaryIs")
        ring_node = ET.SubElement(boundary, f"{{{KML_NAMESPACE}}}LinearRing")
        coordinates_node = ET.SubElement(ring_node, f"{{{KML_NAMESPACE}}}coordinates")
        coordinates_node.text = " ".join(
            f"{format_number(item.longitude)},{format_number(item.latitude)},0" for item in _output_ring(area)
        )
    return ET.ElementTree(root)


def _write_kml(path: Path, areas: Iterable[AreaRecord], document_name: str) -> None:
    tree = _kml_document(areas, document_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(path, encoding="utf-8", xml_declaration=True)


def generate_outputs(result: InspectionResult, project_root: Path) -> GeneratedOutputs:
    """Generate clean, code-based outputs. The source KMZ is never opened for writing."""
    if result.errors:
        joined = "; ".join(item.message for item in result.errors)
        raise ValueError(f"Generation refused because validation has errors: {joined}")
    manifest_dir = project_root / "generated" / "manifest"
    kml_dir = project_root / "generated" / "kml"
    geojson_dir = project_root / "generated" / "geojson"
    for directory in (manifest_dir, kml_dir, geojson_dir):
        directory.mkdir(parents=True, exist_ok=True)

    individual_kml: list[str] = []
    for area in result.areas:
        (project_root / "output" / area.area_code).mkdir(parents=True, exist_ok=True)
        output_path = kml_dir / f"{area.area_code}.kml"
        _write_kml(output_path, [area], f"{area.area_code} - {area.area_name}")
        individual_kml.append(str(output_path.resolve()))
    combined_kml = kml_dir / "selection_91_areas.kml"
    _write_kml(combined_kml, result.areas, "Tender 91 clean area polygons")

    manifest_payload = {
        "schema_version": 1,
        "input": {
            "path": result.input_path,
            "sha256": result.input_sha256,
            "archive_entries": result.archive_entries,
        },
        "inventory": {
            "placemarks": result.placemark_count,
            "polygon_placemarks": result.polygon_placemark_count,
            "point_placemarks": result.point_placemark_count,
            "vertex_point_placemarks": result.vertex_point_count,
            "auxiliary_point_placemarks": result.auxiliary_point_count,
        },
        "validation": [item.as_dict() for item in result.validation_messages],
        "areas": [area.manifest_dict() for area in result.areas],
    }
    json_path = manifest_dir / "areas.json"
    json_path.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    csv_path = manifest_dir / "areas.csv"
    csv_fields = [
        "tender_number",
        "area_code",
        "area_name",
        "placemark_name",
        "aimag",
        "soum",
        "area_hectares",
        "declared_coordinate_count",
        "source_coordinate_count",
        "unique_coordinate_count",
        "geometry_type",
        "coordinate_system",
        "polygon_closed",
        "bbox_min_longitude",
        "bbox_min_latitude",
        "bbox_max_longitude",
        "bbox_max_latitude",
        "center_longitude",
        "center_latitude",
        "source_url",
        "closure_note",
        "repairs",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=csv_fields)
        writer.writeheader()
        for area in result.areas:
            row = area.manifest_dict()
            bounds = row.pop("bounds")
            center = row.pop("center")
            row.pop("metadata")
            row["bbox_min_longitude"] = bounds["min_longitude"]
            row["bbox_min_latitude"] = bounds["min_latitude"]
            row["bbox_max_longitude"] = bounds["max_longitude"]
            row["bbox_max_latitude"] = bounds["max_latitude"]
            row["center_longitude"] = center["longitude"]
            row["center_latitude"] = center["latitude"]
            row["repairs"] = "; ".join(row["repairs"])
            writer.writerow({key: row.get(key, "") for key in csv_fields})

    geojson_path = geojson_dir / "selection_91_areas.geojson"
    features = []
    for area in result.areas:
        properties = area.manifest_dict()
        properties.pop("bounds")
        properties.pop("center")
        properties.pop("metadata")
        features.append(
            {
                "type": "Feature",
                "id": area.area_code,
                "properties": properties,
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[coordinate.geojson() for coordinate in _output_ring(area)]],
                },
            }
        )
    geojson_payload = {
        "type": "FeatureCollection",
        "name": "selection_91_areas",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": features,
    }
    geojson_path.write_text(json.dumps(geojson_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {
        "areas_json": str(json_path.resolve()),
        "areas_csv": str(csv_path.resolve()),
        "geojson": str(geojson_path.resolve()),
        "combined_kml": str(combined_kml.resolve()),
        "individual_kml": individual_kml,
    }
