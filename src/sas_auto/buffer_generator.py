"""Create metric minimum-rectangle and buffered-footprint KMZ datasets."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from pyproj import CRS, Geod, Transformer
from shapely.geometry import Polygon as ShapelyPolygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union
from shapely.validation import explain_validity

from .models import AreaRecord, InspectionResult

KML_NAMESPACE = "http://www.opengis.net/kml/2.2"
BOUNDARY_SUFFIX = re.compile(r"\s*-\s*Boundary Polygon\s*$", re.IGNORECASE)
METHOD = "Minimum rotated rectangle + metric mitre buffer in a local azimuthal-equidistant CRS"


@dataclass(frozen=True)
class DerivedFootprints:
    """The two projected derivatives generated from one source area."""

    source: AreaRecord
    base_name: str
    rectangle: ShapelyPolygon
    buffered_rectangle: ShapelyPolygon
    source_area_hectares: float
    rectangle_area_hectares: float
    buffer_area_hectares: float
    local_crs: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_geometry(area: AreaRecord) -> BaseGeometry:
    if not area.source_closed:
        raise ValueError(
            f"Area {area.area_code} has an unclosed source ring; metric derivation refuses to repair it silently"
        )
    polygons: list[ShapelyPolygon] = []
    for part in area.polygons:
        outer = [coordinate.pair() for coordinate in part.outer]
        holes = [[coordinate.pair() for coordinate in hole] for hole in part.holes]
        polygon = ShapelyPolygon(outer, holes)
        if polygon.is_empty or polygon.area <= 0:
            raise ValueError(f"Area {area.area_code} contains an empty or zero-area polygon")
        if not polygon.is_valid:
            raise ValueError(f"Area {area.area_code} contains invalid geometry: {explain_validity(polygon)}")
        polygons.append(polygon)
    geometry = unary_union(polygons)
    if geometry.is_empty or geometry.area <= 0:
        raise ValueError(f"Area {area.area_code} has no usable polygon area")
    if not geometry.is_valid:
        raise ValueError(f"Area {area.area_code} union is invalid: {explain_validity(geometry)}")
    return geometry


def _base_name(area: AreaRecord) -> str:
    value = BOUNDARY_SUFFIX.sub("", area.placemark_name).strip()
    if not value:
        value = f"{area.tender_number}_{area.area_code}_{area.area_name}" if area.tender_number else area.area_name
    return re.sub(r"\s+", "_", value)


def derive_footprints(area: AreaRecord, buffer_meters: float) -> DerivedFootprints:
    """Derive the same local-AEQD rectangle and mitre buffer used by the Selection 91 reference."""
    if buffer_meters <= 0:
        raise ValueError("buffer_meters must be greater than zero")
    source = _source_geometry(area)
    center = source.centroid
    local_crs = CRS.from_proj4(
        f"+proj=aeqd +lat_0={center.y:.12f} +lon_0={center.x:.12f} +datum=WGS84 +units=m +no_defs"
    )
    forward = Transformer.from_crs(CRS.from_epsg(4326), local_crs, always_xy=True)
    inverse = Transformer.from_crs(local_crs, CRS.from_epsg(4326), always_xy=True)
    projected_source = transform(forward.transform, source)
    projected_rectangle = projected_source.minimum_rotated_rectangle
    if not isinstance(projected_rectangle, ShapelyPolygon) or projected_rectangle.is_empty:
        raise ValueError(f"Area {area.area_code} did not produce a polygonal minimum rotated rectangle")
    projected_buffer = projected_rectangle.buffer(buffer_meters, join_style="mitre")
    if not isinstance(projected_buffer, ShapelyPolygon) or projected_buffer.is_empty:
        raise ValueError(f"Area {area.area_code} did not produce a polygonal metric buffer")
    rectangle = transform(inverse.transform, projected_rectangle)
    buffered_rectangle = transform(inverse.transform, projected_buffer)
    if not isinstance(rectangle, ShapelyPolygon) or not isinstance(buffered_rectangle, ShapelyPolygon):
        raise ValueError(f"Area {area.area_code} inverse projection did not return polygons")
    source_area = area.area_hectares
    if source_area <= 0:
        source_area = abs(Geod(ellps="WGS84").geometry_area_perimeter(source)[0]) / 10_000
    return DerivedFootprints(
        source=area,
        base_name=_base_name(area),
        rectangle=rectangle,
        buffered_rectangle=buffered_rectangle,
        source_area_hectares=source_area,
        rectangle_area_hectares=projected_rectangle.area / 10_000,
        buffer_area_hectares=projected_buffer.area / 10_000,
        local_crs=local_crs.to_string(),
    )


def _format_number(value: float) -> str:
    text = f"{value:.9f}".rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _add_style(document: ET.Element, style_id: str, line_color: str, polygon_color: str) -> None:
    style = ET.SubElement(document, f"{{{KML_NAMESPACE}}}Style", {"id": style_id})
    line = ET.SubElement(style, f"{{{KML_NAMESPACE}}}LineStyle")
    ET.SubElement(line, f"{{{KML_NAMESPACE}}}color").text = line_color
    ET.SubElement(line, f"{{{KML_NAMESPACE}}}width").text = "2.5"
    polygon = ET.SubElement(style, f"{{{KML_NAMESPACE}}}PolyStyle")
    ET.SubElement(polygon, f"{{{KML_NAMESPACE}}}color").text = polygon_color
    ET.SubElement(polygon, f"{{{KML_NAMESPACE}}}fill").text = "1"
    ET.SubElement(polygon, f"{{{KML_NAMESPACE}}}outline").text = "1"


def _add_data(parent: ET.Element, name: str, value: str) -> None:
    data = ET.SubElement(parent, f"{{{KML_NAMESPACE}}}Data", {"name": name})
    ET.SubElement(data, f"{{{KML_NAMESPACE}}}value").text = value


def _add_placemark(
    folder: ET.Element,
    item: DerivedFootprints,
    *,
    geometry: ShapelyPolygon,
    suffix: str,
    style_id: str,
    buffer_meters: float,
) -> None:
    is_buffer = suffix.startswith("Buffer_")
    area_hectares = item.buffer_area_hectares if is_buffer else item.rectangle_area_hectares
    name = f"{item.base_name}_{suffix}"
    placemark = ET.SubElement(folder, f"{{{KML_NAMESPACE}}}Placemark")
    ET.SubElement(placemark, f"{{{KML_NAMESPACE}}}name").text = name
    ET.SubElement(placemark, f"{{{KML_NAMESPACE}}}styleUrl").text = f"#{style_id}"
    if is_buffer:
        description = (
            f"<b>{item.base_name}</b><br/>Тэгш өнцөгтөөс гадагш {buffer_meters:,.0f} м buffer."
            f"<br/>Buffer-ийн нийт талбай: {item.buffer_area_hectares:,.2f} га"
        )
    else:
        description = (
            f"<b>{item.base_name}</b><br/>Эх полигоныг бүрэн хамрах хамгийн бага эргүүлсэн тэгш өнцөгт."
            f"<br/>Эх талбай: {item.source_area_hectares:,.2f} га"
            f"<br/>Тэгш өнцөгт: {item.rectangle_area_hectares:,.2f} га"
        )
    ET.SubElement(placemark, f"{{{KML_NAMESPACE}}}description").text = description
    extended = ET.SubElement(placemark, f"{{{KML_NAMESPACE}}}ExtendedData")
    metadata = {
        "Эх_талбайн_нэр": item.base_name,
        "Эх_талбайн_код": item.source.area_code,
        "Эх_талбай_га": f"{item.source_area_hectares:.2f}",
        "Тэгш_өнцөгт_га": f"{item.rectangle_area_hectares:.2f}",
        "Buffer_га": f"{item.buffer_area_hectares:.2f}",
        "Buffer_метр": f"{buffer_meters:g}",
        "Арга": METHOD,
        "Local_CRS": item.local_crs,
        "derived_kind": "buffer" if is_buffer else "rectangle",
        "area_ha": f"{area_hectares:.2f}",
        "Аймаг": item.source.aimag,
        "Сум": item.source.soum,
        "source_url": item.source.source_url,
    }
    for key, value in metadata.items():
        if value:
            _add_data(extended, key, value)
    polygon = ET.SubElement(placemark, f"{{{KML_NAMESPACE}}}Polygon")
    ET.SubElement(polygon, f"{{{KML_NAMESPACE}}}tessellate").text = "1"
    ET.SubElement(polygon, f"{{{KML_NAMESPACE}}}altitudeMode").text = "clampToGround"
    boundary = ET.SubElement(polygon, f"{{{KML_NAMESPACE}}}outerBoundaryIs")
    ring = ET.SubElement(boundary, f"{{{KML_NAMESPACE}}}LinearRing")
    coordinate_text = " ".join(
        f"{_format_number(longitude)},{_format_number(latitude)},0" for longitude, latitude in geometry.exterior.coords
    )
    ET.SubElement(ring, f"{{{KML_NAMESPACE}}}coordinates").text = coordinate_text


def _buffer_label(buffer_meters: float) -> str:
    if buffer_meters.is_integer() and int(buffer_meters) % 1000 == 0:
        return f"{int(buffer_meters) // 1000}km"
    return f"{buffer_meters:g}m".replace(".", "p")


def default_output_path(source_path: Path, buffer_meters: float) -> Path:
    """Return a sibling KMZ name without modifying the source path."""
    base = re.sub(r"(?i)_all_areas$", "", source_path.stem)
    return source_path.with_name(f"{base}_Rectangles_{_buffer_label(buffer_meters)}_Buffer.kmz")


def create_buffered_kmz(
    result: InspectionResult,
    output_path: Path,
    *,
    buffer_meters: float = 1000.0,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Write a derived KMZ atomically while leaving the source archive untouched."""
    if result.errors:
        messages = "; ".join(item.message for item in result.errors)
        raise ValueError(f"Buffer generation refused because source validation failed: {messages}")
    if buffer_meters <= 0:
        raise ValueError("buffer_meters must be greater than zero")
    output_path = output_path.resolve()
    source_path = Path(result.input_path).resolve()
    if output_path == source_path:
        raise ValueError("Derived KMZ output must not overwrite the source KMZ")
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"Derived KMZ already exists: {output_path}; pass --overwrite to replace it")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    items = [derive_footprints(area, buffer_meters) for area in result.areas]

    ET.register_namespace("", KML_NAMESPACE)
    root = ET.Element(f"{{{KML_NAMESPACE}}}kml")
    document = ET.SubElement(root, f"{{{KML_NAMESPACE}}}Document")
    tender = items[0].source.tender_number if items else result.dataset_id
    ET.SubElement(
        document, f"{{{KML_NAMESPACE}}}name"
    ).text = f"Selection {tender} - Rectangles and {_buffer_label(buffer_meters)} Buffer"
    _add_style(document, "rectangleStyle", "ff00a5ff", "3300a5ff")
    _add_style(document, "bufferStyle", "ffff0000", "3314a0ff")
    rectangles = ET.SubElement(document, f"{{{KML_NAMESPACE}}}Folder")
    ET.SubElement(rectangles, f"{{{KML_NAMESPACE}}}name").text = "01_Тэгш_өнцөгт_талбай"
    ET.SubElement(rectangles, f"{{{KML_NAMESPACE}}}open").text = "0"
    buffers = ET.SubElement(document, f"{{{KML_NAMESPACE}}}Folder")
    ET.SubElement(buffers, f"{{{KML_NAMESPACE}}}name").text = f"02_{_buffer_label(buffer_meters)}_Buffer"
    ET.SubElement(buffers, f"{{{KML_NAMESPACE}}}open").text = "0"
    label = _buffer_label(buffer_meters)
    for item in items:
        _add_placemark(
            rectangles,
            item,
            geometry=item.rectangle,
            suffix="Rectangle",
            style_id="rectangleStyle",
            buffer_meters=buffer_meters,
        )
    for item in items:
        _add_placemark(
            buffers,
            item,
            geometry=item.buffered_rectangle,
            suffix=f"Buffer_{label}",
            style_id="bufferStyle",
            buffer_meters=buffer_meters,
        )
    kml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    with tempfile.NamedTemporaryFile(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent, delete=False
    ) as stream:
        temporary_path = Path(stream.name)
    try:
        info = ZipInfo("doc.kml", date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = ZIP_DEFLATED
        with ZipFile(temporary_path, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr(info, kml_bytes)
        os.replace(temporary_path, output_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    return {
        "source_path": str(source_path),
        "source_sha256": result.input_sha256,
        "output_path": str(output_path),
        "output_sha256": _sha256(output_path),
        "source_feature_count": len(items),
        "derived_feature_count": len(items) * 2,
        "buffer_meters": buffer_meters,
        "method": METHOD,
        "network_download_started": False,
    }
