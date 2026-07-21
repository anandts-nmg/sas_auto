"""Data models shared by parsing, planning, validation, and state handling."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Coordinate:
    longitude: float
    latitude: float
    altitude: float | None = None

    def pair(self) -> tuple[float, float]:
        return (self.longitude, self.latitude)

    def geojson(self) -> list[float]:
        return [self.longitude, self.latitude]


@dataclass(frozen=True)
class Bounds:
    min_longitude: float
    min_latitude: float
    max_longitude: float
    max_latitude: float

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True)
class ValidationMessage:
    severity: str
    code: str
    message: str
    area_code: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PolygonPart:
    """One KML Polygon with an outer ring and zero or more inner rings."""

    outer: list[Coordinate]
    holes: list[list[Coordinate]] = field(default_factory=list)
    outer_source_closed: bool = True
    hole_source_closed: list[bool] = field(default_factory=list)

    @property
    def ring_count(self) -> int:
        return 1 + len(self.holes)

    @property
    def source_closed(self) -> bool:
        return self.outer_source_closed and all(self.hole_source_closed)

    def all_coordinates(self) -> list[Coordinate]:
        return [*self.outer, *(coordinate for hole in self.holes for coordinate in hole)]


@dataclass
class AreaRecord:
    tender_number: str
    area_code: str
    area_name: str
    placemark_name: str
    aimag: str
    soum: str
    area_hectares: float
    declared_coordinate_count: int
    coordinate_system: str
    source_url: str
    geometry_type: str
    coordinates: list[Coordinate]
    bounds: Bounds
    center: Coordinate
    source_closed: bool
    metadata: dict[str, str] = field(default_factory=dict)
    repairs: list[str] = field(default_factory=list)
    polygons: list[PolygonPart] = field(default_factory=list)
    profile: str = "generic_polygons"
    source_placemark_id: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        if not self.polygons:
            self.polygons = [
                PolygonPart(
                    outer=self.coordinates,
                    outer_source_closed=self.source_closed,
                )
            ]

    @property
    def feature_id(self) -> str:
        """Generic name for the backward-compatible area_code identifier."""
        return self.area_code

    @property
    def feature_name(self) -> str:
        return self.area_name

    @property
    def source_coordinate_count(self) -> int:
        return sum(len(polygon.outer) for polygon in self.polygons)

    @property
    def unique_coordinate_count(self) -> int:
        return sum(
            len(polygon.outer) - 1 if polygon.outer_source_closed and len(polygon.outer) > 1 else len(polygon.outer)
            for polygon in self.polygons
        )

    @property
    def hole_count(self) -> int:
        return sum(len(polygon.holes) for polygon in self.polygons)

    @property
    def hole_coordinate_count(self) -> int:
        return sum(len(hole) for polygon in self.polygons for hole in polygon.holes)

    @property
    def closure_note(self) -> str:
        if self.profile == "generic_polygons" and self.source_closed:
            return "All source polygon rings are closed."
        if self.source_closed and self.source_coordinate_count == self.declared_coordinate_count + len(self.polygons):
            return (
                "Each source outer ring contains a required closing coordinate that repeats "
                "its first vertex; the declared count excludes closing coordinates."
            )
        if not self.source_closed:
            return "The source ring is not closed; derived KML/GeoJSON output records a closure repair."
        return "Source and declared coordinate counts require review."

    def manifest_dict(self) -> dict[str, Any]:
        return {
            "tender_number": self.tender_number,
            "feature_id": self.feature_id,
            "feature_name": self.feature_name,
            "area_code": self.area_code,
            "area_name": self.area_name,
            "placemark_name": self.placemark_name,
            "aimag": self.aimag,
            "soum": self.soum,
            "area_hectares": self.area_hectares,
            "declared_coordinate_count": self.declared_coordinate_count,
            "source_coordinate_count": self.source_coordinate_count,
            "unique_coordinate_count": self.unique_coordinate_count,
            "geometry_type": self.geometry_type,
            "polygon_count": len(self.polygons),
            "hole_count": self.hole_count,
            "hole_coordinate_count": self.hole_coordinate_count,
            "profile": self.profile,
            "source_placemark_id": self.source_placemark_id,
            "description": self.description,
            "coordinate_system": self.coordinate_system,
            "source_url": self.source_url,
            "polygon_closed": self.source_closed,
            "closure_note": self.closure_note,
            "bounds": self.bounds.as_dict(),
            "center": {
                "longitude": self.center.longitude,
                "latitude": self.center.latitude,
            },
            "repairs": list(self.repairs),
            "metadata": dict(self.metadata),
        }


@dataclass
class InspectionResult:
    input_path: str
    input_sha256: str
    archive_entries: list[str]
    placemark_count: int
    polygon_placemark_count: int
    point_placemark_count: int
    vertex_point_count: int
    auxiliary_point_count: int
    areas: list[AreaRecord]
    validation_messages: list[ValidationMessage] = field(default_factory=list)
    dataset_id: str = "dataset"
    profile: str = "generic_polygons"
    kml_members: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[ValidationMessage]:
        return [item for item in self.validation_messages if item.severity == "error"]

    @property
    def warnings(self) -> list[ValidationMessage]:
        return [item for item in self.validation_messages if item.severity == "warning"]
