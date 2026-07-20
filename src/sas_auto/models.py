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

    @property
    def source_coordinate_count(self) -> int:
        return len(self.coordinates)

    @property
    def unique_coordinate_count(self) -> int:
        if self.source_closed and len(self.coordinates) > 1:
            return len(self.coordinates) - 1
        return len(self.coordinates)

    @property
    def closure_note(self) -> str:
        if self.source_closed and self.source_coordinate_count == self.declared_coordinate_count + 1:
            return (
                "The source ring contains one required closing coordinate that repeats "
                "the first vertex; the declared count excludes it."
            )
        if not self.source_closed:
            return "The source ring is not closed; derived KML/GeoJSON output records a closure repair."
        return "Source and declared coordinate counts require review."

    def manifest_dict(self) -> dict[str, Any]:
        return {
            "tender_number": self.tender_number,
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

    @property
    def errors(self) -> list[ValidationMessage]:
        return [item for item in self.validation_messages if item.severity == "error"]

    @property
    def warnings(self) -> list[ValidationMessage]:
        return [item for item in self.validation_messages if item.severity == "warning"]
