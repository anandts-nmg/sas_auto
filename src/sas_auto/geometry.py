"""Small dependency-free geometry helpers for WGS 84 polygons."""

from __future__ import annotations

from math import asinh, atan, cos, degrees, floor, pi, radians, sinh, tan

from .models import Bounds, Coordinate


def parse_coordinate_text(text: str) -> list[Coordinate]:
    """Parse a KML coordinate list without altering or closing the source ring."""
    result: list[Coordinate] = []
    for token in text.replace("\t", " ").replace("\n", " ").split():
        parts = token.split(",")
        if len(parts) < 2:
            raise ValueError(f"Malformed KML coordinate token: {token!r}")
        try:
            longitude = float(parts[0])
            latitude = float(parts[1])
            altitude = float(parts[2]) if len(parts) > 2 and parts[2] != "" else None
        except ValueError as error:
            raise ValueError(f"Non-numeric KML coordinate token: {token!r}") from error
        validate_coordinate(longitude, latitude)
        result.append(Coordinate(longitude, latitude, altitude))
    return result


def validate_coordinate(longitude: float, latitude: float) -> None:
    if not -180.0 <= longitude <= 180.0:
        raise ValueError(f"Longitude outside WGS 84 range: {longitude}")
    if not -90.0 <= latitude <= 90.0:
        raise ValueError(f"Latitude outside WGS 84 range: {latitude}")


def is_closed(coordinates: list[Coordinate]) -> bool:
    return len(coordinates) >= 2 and coordinates[0].pair() == coordinates[-1].pair()


def close_for_derived_output(coordinates: list[Coordinate]) -> tuple[list[Coordinate], bool]:
    """Return an output ring and whether a closure repair was made."""
    if not coordinates:
        return [], False
    if is_closed(coordinates):
        return list(coordinates), False
    return [*coordinates, coordinates[0]], True


def calculate_bounds(coordinates: list[Coordinate]) -> Bounds:
    if not coordinates:
        raise ValueError("Cannot calculate bounds for an empty coordinate sequence")
    return Bounds(
        min(item.longitude for item in coordinates),
        min(item.latitude for item in coordinates),
        max(item.longitude for item in coordinates),
        max(item.latitude for item in coordinates),
    )


def calculate_centroid(coordinates: list[Coordinate]) -> Coordinate:
    """Calculate a planar centroid suitable for these small WGS 84 tender polygons."""
    if len(coordinates) < 3:
        bounds = calculate_bounds(coordinates)
        return Coordinate(
            (bounds.min_longitude + bounds.max_longitude) / 2,
            (bounds.min_latitude + bounds.max_latitude) / 2,
        )
    ring = coordinates[:-1] if is_closed(coordinates) else coordinates
    twice_area = 0.0
    longitude_sum = 0.0
    latitude_sum = 0.0
    for first, second in zip(ring, ring[1:] + ring[:1]):
        cross = first.longitude * second.latitude - second.longitude * first.latitude
        twice_area += cross
        longitude_sum += (first.longitude + second.longitude) * cross
        latitude_sum += (first.latitude + second.latitude) * cross
    if abs(twice_area) < 1e-15:
        bounds = calculate_bounds(coordinates)
        return Coordinate(
            (bounds.min_longitude + bounds.max_longitude) / 2,
            (bounds.min_latitude + bounds.max_latitude) / 2,
        )
    return Coordinate(
        longitude_sum / (3.0 * twice_area),
        latitude_sum / (3.0 * twice_area),
    )


def buffered_bounds(bounds: Bounds, percent: float) -> Bounds:
    if percent < 0:
        raise ValueError("Buffer percent cannot be negative")
    longitude_pad = (bounds.max_longitude - bounds.min_longitude) * percent / 100.0
    latitude_pad = (bounds.max_latitude - bounds.min_latitude) * percent / 100.0
    return Bounds(
        max(-180.0, bounds.min_longitude - longitude_pad),
        max(-85.05112878, bounds.min_latitude - latitude_pad),
        min(180.0, bounds.max_longitude + longitude_pad),
        min(85.05112878, bounds.max_latitude + latitude_pad),
    )


def _longitude_to_tile_x(longitude: float, zoom: int) -> int:
    count = 1 << zoom
    return min(count - 1, max(0, floor((longitude + 180.0) / 360.0 * count)))


def _latitude_to_tile_y(latitude: float, zoom: int) -> int:
    count = 1 << zoom
    latitude = min(85.05112878, max(-85.05112878, latitude))
    value = (1.0 - asinh(tan(radians(latitude))) / pi) / 2.0 * count
    return min(count - 1, max(0, floor(value)))


def estimate_bbox_tiles(bounds: Bounds, zoom: int) -> dict[str, int]:
    """Return a conservative Web Mercator rectangle estimate, not a download."""
    if not 0 <= zoom <= 24:
        raise ValueError(f"Unsupported zoom level: {zoom}")
    min_x = _longitude_to_tile_x(bounds.min_longitude, zoom)
    max_x = _longitude_to_tile_x(bounds.max_longitude, zoom)
    min_y = _latitude_to_tile_y(bounds.max_latitude, zoom)
    max_y = _latitude_to_tile_y(bounds.min_latitude, zoom)
    return {
        "min_x": min_x,
        "max_x": max_x,
        "min_y": min_y,
        "max_y": max_y,
        "tile_count": (max_x - min_x + 1) * (max_y - min_y + 1),
    }


def format_number(value: float) -> str:
    text = f"{value:.12f}".rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text
