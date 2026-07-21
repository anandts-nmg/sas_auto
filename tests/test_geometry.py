from __future__ import annotations

import pytest

from sas_auto.geometry import (
    calculate_bounds,
    calculate_centroid,
    close_for_derived_output,
    estimate_bbox_tiles,
    is_closed,
    parse_coordinate_text,
)
from sas_auto.models import Bounds, Coordinate


def test_coordinate_parsing_and_closure() -> None:
    coordinates = parse_coordinate_text("100,45,0 101,45,0 101,46,0 100,45,0")
    assert is_closed(coordinates)
    assert coordinates[0] == Coordinate(100.0, 45.0, 0.0)


def test_derived_closure_is_explicit() -> None:
    coordinates = [Coordinate(100, 45), Coordinate(101, 45), Coordinate(101, 46)]
    closed, repaired = close_for_derived_output(coordinates)
    assert repaired is True
    assert len(closed) == 4
    assert closed[0] == closed[-1]
    assert len(coordinates) == 3


def test_bounds_and_centroid() -> None:
    coordinates = [
        Coordinate(100, 45),
        Coordinate(102, 45),
        Coordinate(102, 47),
        Coordinate(100, 47),
        Coordinate(100, 45),
    ]
    assert calculate_bounds(coordinates) == Bounds(100, 45, 102, 47)
    center = calculate_centroid(coordinates)
    assert center.longitude == pytest.approx(101)
    assert center.latitude == pytest.approx(46)


@pytest.mark.parametrize("text", ["181,45", "100,91", "not-a-number,45"])
def test_invalid_coordinates_are_rejected(text: str) -> None:
    with pytest.raises(ValueError):
        parse_coordinate_text(text)


def test_tile_estimate_is_positive_and_conservative() -> None:
    estimate = estimate_bbox_tiles(Bounds(92.35, 46.33, 92.42, 46.40), 15)
    assert estimate["tile_count"] > 0
    assert estimate["min_x"] <= estimate["max_x"]
    assert estimate["min_y"] <= estimate["max_y"]
