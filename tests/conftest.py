from __future__ import annotations

from pathlib import Path

import pytest

from sas_auto.kmz_parser import inspect_kmz
from sas_auto.models import InspectionResult

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def actual_result(project_root: Path) -> InspectionResult:
    return inspect_kmz(project_root / "inputs" / "Selection_91_All_Areas.kmz")
