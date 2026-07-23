"""Read-only KMZ inspection and clean artifact generation."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import unicodedata
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
from .models import AreaRecord, Coordinate, InspectionResult, PolygonPart
from .validation import EXPECTED_AREA_CODES, is_portable_windows_component, validate_areas

KML_NAMESPACE = "http://www.opengis.net/kml/2.2"
VERTEX_POINT_PATTERN = re.compile(r"^(?P<code>[A-Za-z0-9._-]+?)(?:_V|-)(?P<number>\d+)$", re.IGNORECASE)
SUPPORTED_PROFILES = {"auto", "selection_91", "tender_areas", "generic_polygons"}
DEFAULT_ID_FIELDS = ("area_code", "code", "Код", "id")
DEFAULT_NAME_FIELDS = ("area_name", "name", "Талбай")
MAX_ARCHIVE_ENTRIES = 10_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_KML_BYTES = 50 * 1024 * 1024


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
        self._current_row: list[tuple[str, str]] | None = None
        self._rows: list[list[tuple[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.lower()
        if normalized_tag == "tr":
            self._current_row = []
        elif normalized_tag in {"th", "td"}:
            self._current_tag = normalized_tag
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._current_tag is not None:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        current_tag = self._current_tag
        if current_tag is not None and current_tag == normalized_tag:
            value = " ".join("".join(self._buffer).split())
            cell = (current_tag, value)
            self._cells.append(cell)
            if self._current_row is not None:
                self._current_row.append(cell)
            self._current_tag = None
            self._buffer = []
        elif normalized_tag == "tr" and self._current_row is not None:
            if self._current_row:
                self._rows.append(self._current_row)
            self._current_row = None

    def as_mapping(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for row in self._rows:
            cells = [value for _, value in row if value]
            if len(cells) >= 2:
                key = cells[0].rstrip(":").strip()
                if key:
                    mapping[key] = cells[1]
        if mapping:
            return mapping

        pending_key: str | None = None
        for tag, value in self._cells:
            if tag == "th":
                pending_key = value.rstrip(":").strip()
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


def _required(metadata: dict[str, str], fields: tuple[str, ...], label: str, placemark_name: str) -> str:
    value = _metadata_value(metadata, fields)
    if not value:
        raise ValueError(f"Polygon {placemark_name!r} is missing description field {label!r}")
    return value


def _extended_data(placemark: ET.Element) -> dict[str, str]:
    values: dict[str, str] = {}
    for data in placemark.findall(".//{*}ExtendedData/{*}Data"):
        name = (data.get("name") or "").strip()
        value = (data.findtext("./{*}value") or "").strip()
        if name and value:
            values[name] = value
    for data in placemark.findall(".//{*}ExtendedData/{*}SchemaData/{*}SimpleData"):
        name = (data.get("name") or "").strip()
        value = (data.text or "").strip()
        if name and value:
            values[name] = value
    return values


def _metadata_value(metadata: dict[str, str], fields: Iterable[str]) -> str:
    folded = {key.casefold(): value for key, value in metadata.items()}
    for field in fields:
        value = folded.get(field.casefold(), "").strip()
        if value:
            return value
    return ""


def _portable_id(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", errors="ignore").decode("ascii")
    identifier = re.sub(r"[^A-Za-z0-9._-]+", "_", normalized).strip("._-").lower()
    identifier = identifier[:100]
    if identifier and not is_portable_windows_component(identifier):
        identifier = f"feature_{identifier}"[:100]
    return identifier


def _unique_id(candidate: str, fallback: str, used_ids: set[str]) -> str:
    base = _portable_id(candidate) or fallback
    identifier = base
    suffix = 2
    while identifier.casefold() in used_ids:
        suffix_text = f"_{suffix}"
        identifier = f"{base[: 100 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    used_ids.add(identifier.casefold())
    return identifier


def _parse_optional_float(value: str) -> float:
    if not value.strip():
        return 0.0
    return float(value.replace(",", "").strip())


def _parse_optional_int(value: str, default: int) -> int:
    if not value.strip():
        return default
    return int(value.replace(",", "").strip())


def _polygon_part(polygon: ET.Element, placemark_name: str, polygon_index: int) -> PolygonPart:
    outer_node = polygon.find("./{*}outerBoundaryIs/{*}LinearRing/{*}coordinates")
    if outer_node is None or not (outer_node.text or "").strip():
        raise ValueError(f"Placemark {placemark_name!r} polygon {polygon_index} has no outer LinearRing coordinates")
    outer = parse_coordinate_text(outer_node.text or "")
    if len(outer) < 3:
        raise ValueError(f"Placemark {placemark_name!r} polygon {polygon_index} has too few outer coordinates")
    holes: list[list[Coordinate]] = []
    hole_closed: list[bool] = []
    for hole_index, hole_node in enumerate(
        polygon.findall("./{*}innerBoundaryIs/{*}LinearRing/{*}coordinates"), start=1
    ):
        hole = parse_coordinate_text(hole_node.text or "")
        if len(hole) < 3:
            raise ValueError(
                f"Placemark {placemark_name!r} polygon {polygon_index} hole {hole_index} has too few coordinates"
            )
        holes.append(hole)
        hole_closed.append(is_closed(hole))
    return PolygonPart(
        outer=outer,
        holes=holes,
        outer_source_closed=is_closed(outer),
        hole_source_closed=hole_closed,
    )


def _area_from_placemark(
    placemark: ET.Element,
    *,
    profile: str,
    index: int,
    used_ids: set[str],
    id_fields: tuple[str, ...],
    name_fields: tuple[str, ...],
) -> AreaRecord:
    placemark_name = (placemark.findtext("{*}name") or "").strip()
    description = placemark.findtext("{*}description") or ""
    metadata = {**parse_html_metadata(description), **_extended_data(placemark)}
    polygon_nodes = placemark.findall(".//{*}Polygon")
    if not polygon_nodes:
        raise ValueError(f"Placemark {placemark_name!r} has no Polygon elements")
    polygons = [_polygon_part(node, placemark_name, number) for number, node in enumerate(polygon_nodes, start=1)]
    all_outer_coordinates = [coordinate for polygon in polygons for coordinate in polygon.outer]
    source_closed = all(polygon.source_closed for polygon in polygons)
    try:
        area_hectares = _parse_optional_float(_metadata_value(metadata, ("Талбай (га)", "area_hectares", "area_ha")))
        unique_outer_count = sum(
            len(polygon.outer) - 1 if polygon.outer_source_closed else len(polygon.outer) for polygon in polygons
        )
        declared_count = _parse_optional_int(
            _metadata_value(metadata, ("Координатын тоо", "coordinate_count", "coord_count")),
            unique_outer_count,
        )
    except ValueError as error:
        raise ValueError(f"Polygon {placemark_name!r} has invalid numeric metadata: {error}") from error
    repairs: list[str] = []
    if not source_closed:
        repairs.append(
            "Closed unclosed source LinearRing values in derived outputs by repeating their first coordinate."
        )
    placemark_id = (placemark.get("id") or "").strip()
    if profile == "tender_areas":
        feature_id = _required(metadata, ("area_code", "Код", "code"), "Код", placemark_name)
        area_name = _required(metadata, ("area_name", "Талбай", "name"), "Талбай", placemark_name)
        tender_number = _required(
            metadata,
            ("selection_no", "Сонгон шалгаруулалт", "tender_number"),
            "Сонгон шалгаруулалт",
            placemark_name,
        )
        used_ids.add(feature_id.casefold())
    else:
        candidate = _metadata_value(metadata, id_fields) or placemark_id or placemark_name
        feature_id = _unique_id(candidate, f"feature_{index:04d}", used_ids)
        area_name = _metadata_value(metadata, name_fields) or placemark_name or feature_id
        tender_number = _metadata_value(metadata, ("Сонгон шалгаруулалт", "tender_number"))
    return AreaRecord(
        tender_number=tender_number,
        area_code=feature_id,
        area_name=area_name,
        placemark_name=placemark_name,
        aimag=_metadata_value(metadata, ("Аймаг", "aimag", "province")),
        soum=_metadata_value(metadata, ("Сум", "soum", "district")),
        area_hectares=area_hectares,
        declared_coordinate_count=declared_count,
        coordinate_system=_metadata_value(metadata, ("Coordinate System", "coordinate_system", "crs"))
        or "WGS 84 / EPSG:4326",
        source_url=_metadata_value(metadata, ("Эх сурвалж", "source_url", "url")),
        geometry_type="Polygon" if len(polygons) == 1 else "MultiPolygon",
        coordinates=polygons[0].outer,
        bounds=calculate_bounds(all_outer_coordinates),
        center=calculate_centroid(polygons[0].outer),
        source_closed=source_closed,
        metadata=metadata,
        repairs=repairs,
        polygons=polygons,
        profile=profile,
        source_placemark_id=placemark_id,
        description=description,
    )


def _detect_profile(placemarks: list[ET.Element], requested: str) -> str:
    if requested not in SUPPORTED_PROFILES:
        raise ValueError(f"Unsupported parser profile {requested!r}; choose one of {sorted(SUPPORTED_PROFILES)}")
    if requested == "selection_91":
        return "tender_areas"
    if requested != "auto":
        return requested
    polygon_metadata = []
    for placemark in placemarks:
        if placemark.findall(".//{*}Polygon"):
            description = placemark.findtext("{*}description") or ""
            polygon_metadata.append({**parse_html_metadata(description), **_extended_data(placemark)})
    required_fields = (
        ("Сонгон шалгаруулалт", "selection_no", "tender_number"),
        ("Код", "area_code", "code"),
        ("Талбай", "area_name", "name"),
    )
    return (
        "tender_areas"
        if polygon_metadata
        and all(all(_metadata_value(item, fields) for fields in required_fields) for item in polygon_metadata)
        else "generic_polygons"
    )


def _dataset_id(path: Path, configured: str | None) -> str:
    if configured and configured.casefold() != "auto":
        identifier = _portable_id(configured)
    else:
        base = _portable_id(re.sub(r"(?i)_all_areas$", "", path.stem)) or "dataset"
        identifier = f"{base}_{_sha256(path)[:8]}"
    return identifier or "dataset"


def inspect_kmz(
    path: Path,
    *,
    profile: str = "auto",
    dataset_id: str | None = None,
    id_fields: tuple[str, ...] = DEFAULT_ID_FIELDS,
    name_fields: tuple[str, ...] = DEFAULT_NAME_FIELDS,
) -> InspectionResult:
    """Inspect a KMZ in read-only mode and return its verified inventory."""
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(f"KMZ not found: {path}")
    try:
        with ZipFile(path, "r") as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise ValueError(f"KMZ has {len(infos)} entries; safety limit is {MAX_ARCHIVE_ENTRIES}")
            total_uncompressed = sum(info.file_size for info in infos)
            if total_uncompressed > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise ValueError(
                    f"KMZ expands to {total_uncompressed} bytes; safety limit is {MAX_ARCHIVE_UNCOMPRESSED_BYTES}"
                )
            bad_member = archive.testzip()
            if bad_member:
                raise ValueError(f"KMZ contains a corrupt archive entry: {bad_member}")
            entries = archive.namelist()
            kml_members = [name for name in entries if name.casefold().endswith(".kml")]
            if not kml_members:
                raise ValueError("KMZ contains no KML members")
            kml_members.sort(key=lambda name: (Path(name).name.casefold() != "doc.kml", name.casefold()))
            kml_documents: list[tuple[str, bytes]] = []
            for member in kml_members:
                data = archive.read(member)
                if len(data) > MAX_KML_BYTES:
                    raise ValueError(f"KML member {member!r} exceeds the {MAX_KML_BYTES}-byte safety limit")
                kml_documents.append((member, data))
    except BadZipFile as error:
        raise ValueError(f"Input is not a valid ZIP/KMZ archive: {path}") from error

    placemarks: list[ET.Element] = []
    for member, kml_bytes in kml_documents:
        try:
            root = ET.fromstring(kml_bytes)
        except ET.ParseError as error:
            raise ValueError(f"Malformed KML member {member!r}: {error}") from error
        local_root_name = root.tag.rsplit("}", 1)[-1]
        if local_root_name.casefold() != "kml":
            raise ValueError(f"Unexpected KML root element in {member!r}: {root.tag}")
        member_placemarks = root.findall(".//{*}Placemark")
        if not member_placemarks and not root.tag.startswith("{"):
            member_placemarks = root.findall(".//Placemark")
        placemarks.extend(member_placemarks)
    detected_profile = _detect_profile(placemarks, profile)
    areas: list[AreaRecord] = []
    polygon_count = 0
    point_count = 0
    vertex_point_counts: Counter[str] = Counter()
    auxiliary_point_count = 0
    used_ids: set[str] = set()

    for placemark in placemarks:
        polygon_nodes = placemark.findall(".//{*}Polygon")
        point_nodes = placemark.findall(".//{*}Point")
        if polygon_nodes:
            polygon_count += 1
            areas.append(
                _area_from_placemark(
                    placemark,
                    profile=detected_profile,
                    index=len(areas) + 1,
                    used_ids=used_ids,
                    id_fields=id_fields,
                    name_fields=name_fields,
                )
            )
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

    if not areas:
        raise ValueError("KMZ contains no Polygon or MultiPolygon placemarks")

    areas.sort(key=lambda area: area.area_code)
    expected_codes = EXPECTED_AREA_CODES if profile == "selection_91" else None
    messages = validate_areas(
        areas,
        vertex_point_counts,
        profile=detected_profile,
        expected_codes=expected_codes,
    )
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
        dataset_id=_dataset_id(path, dataset_id),
        profile=detected_profile,
        kml_members=kml_members,
    )


def _output_ring(area: AreaRecord, source_ring: list[Coordinate]) -> list[Coordinate]:
    ring, repaired = close_for_derived_output(source_ring)
    if repaired:
        repair = "Closed the source LinearRing in derived outputs by repeating the first coordinate."
        if repair not in area.repairs:
            area.repairs.append(repair)
    return ring


def _append_polygon(parent: ET.Element, area: AreaRecord, part: PolygonPart) -> None:
    polygon = ET.SubElement(parent, f"{{{KML_NAMESPACE}}}Polygon")
    ET.SubElement(polygon, f"{{{KML_NAMESPACE}}}tessellate").text = "1"
    boundary = ET.SubElement(polygon, f"{{{KML_NAMESPACE}}}outerBoundaryIs")
    ring_node = ET.SubElement(boundary, f"{{{KML_NAMESPACE}}}LinearRing")
    coordinates_node = ET.SubElement(ring_node, f"{{{KML_NAMESPACE}}}coordinates")
    coordinates_node.text = " ".join(
        f"{format_number(item.longitude)},{format_number(item.latitude)},0" for item in _output_ring(area, part.outer)
    )
    for hole in part.holes:
        inner_boundary = ET.SubElement(polygon, f"{{{KML_NAMESPACE}}}innerBoundaryIs")
        inner_ring = ET.SubElement(inner_boundary, f"{{{KML_NAMESPACE}}}LinearRing")
        inner_coordinates = ET.SubElement(inner_ring, f"{{{KML_NAMESPACE}}}coordinates")
        inner_coordinates.text = " ".join(
            f"{format_number(item.longitude)},{format_number(item.latitude)},0" for item in _output_ring(area, hole)
        )


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
        description_lines = [
            f"Feature ID: {area.feature_id}",
            f"Name: {area.feature_name}",
            f"Geometry: {area.geometry_type}",
            f"CRS: {area.coordinate_system}",
        ]
        if area.tender_number:
            description_lines.append(f"Tender: {area.tender_number}")
        if area.aimag:
            description_lines.append(f"Aimag: {area.aimag}")
        if area.soum:
            description_lines.append(f"Soum: {area.soum}")
        if area.area_hectares:
            description_lines.append(f"Area (ha): {area.area_hectares}")
        if area.source_url:
            description_lines.append(f"Source: {area.source_url}")
        description_lines.append(area.closure_note)
        description = "\n".join(description_lines)
        ET.SubElement(placemark, f"{{{KML_NAMESPACE}}}description").text = description
        extended = ET.SubElement(placemark, f"{{{KML_NAMESPACE}}}ExtendedData")
        for key, value in area.manifest_dict().items():
            if isinstance(value, (str, int, float, bool)):
                data = ET.SubElement(extended, f"{{{KML_NAMESPACE}}}Data", {"name": key})
                ET.SubElement(data, f"{{{KML_NAMESPACE}}}value").text = str(value)
        for key, value in sorted(area.metadata.items()):
            data = ET.SubElement(extended, f"{{{KML_NAMESPACE}}}Data", {"name": f"source:{key}"})
            ET.SubElement(data, f"{{{KML_NAMESPACE}}}value").text = value
        geometry_parent = (
            ET.SubElement(placemark, f"{{{KML_NAMESPACE}}}MultiGeometry") if len(area.polygons) > 1 else placemark
        )
        for part in area.polygons:
            _append_polygon(geometry_parent, area, part)
    return ET.ElementTree(root)


def _write_kml(path: Path, areas: Iterable[AreaRecord], document_name: str) -> None:
    tree = _kml_document(areas, document_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(path, encoding="utf-8", xml_declaration=True)


def generate_outputs(
    result: InspectionResult,
    project_root: Path,
    *,
    namespace_outputs: bool = False,
) -> GeneratedOutputs:
    """Generate clean, code-based outputs. The source KMZ is never opened for writing."""
    if result.errors:
        joined = "; ".join(item.message for item in result.errors)
        raise ValueError(f"Generation refused because validation has errors: {joined}")
    generated_root = project_root / "generated"
    output_root = project_root / "output"
    if namespace_outputs:
        generated_root = generated_root / result.dataset_id
        output_root = output_root / result.dataset_id
    manifest_dir = generated_root / "manifest"
    kml_dir = generated_root / "kml"
    geojson_dir = generated_root / "geojson"
    for directory in (manifest_dir, kml_dir, geojson_dir):
        directory.mkdir(parents=True, exist_ok=True)

    individual_kml: list[str] = []
    for area in result.areas:
        (output_root / area.feature_id).mkdir(parents=True, exist_ok=True)
        output_path = kml_dir / f"{area.area_code}.kml"
        _write_kml(output_path, [area], f"{area.area_code} - {area.area_name}")
        individual_kml.append(str(output_path.resolve()))
    combined_kml = kml_dir / f"{result.dataset_id}_areas.kml"
    _write_kml(combined_kml, result.areas, f"{result.dataset_id} clean polygon features")

    manifest_payload = {
        "schema_version": 2,
        "dataset_id": result.dataset_id,
        "profile": result.profile,
        "input": {
            "path": result.input_path,
            "sha256": result.input_sha256,
            "archive_entries": result.archive_entries,
            "kml_members": result.kml_members,
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
        "feature_id",
        "feature_name",
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
        "polygon_count",
        "hole_count",
        "hole_coordinate_count",
        "profile",
        "source_placemark_id",
        "description",
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

    geojson_path = geojson_dir / f"{result.dataset_id}_areas.geojson"
    features = []
    for area in result.areas:
        properties = area.manifest_dict()
        properties.pop("bounds")
        properties.pop("center")
        properties.pop("metadata")
        polygon_coordinates = [
            [
                [coordinate.geojson() for coordinate in _output_ring(area, part.outer)],
                *[[coordinate.geojson() for coordinate in _output_ring(area, hole)] for hole in part.holes],
            ]
            for part in area.polygons
        ]
        features.append(
            {
                "type": "Feature",
                "id": area.feature_id,
                "properties": properties,
                "geometry": {
                    "type": area.geometry_type,
                    "coordinates": polygon_coordinates[0] if area.geometry_type == "Polygon" else polygon_coordinates,
                },
            }
        )
    geojson_payload = {
        "type": "FeatureCollection",
        "name": f"{result.dataset_id}_areas",
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
