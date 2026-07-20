"""Safe Google Earth Pro launching for human visual verification only."""

from __future__ import annotations

import subprocess
from pathlib import Path


STANDARD_GOOGLE_EARTH_PATHS = (
    Path(r"C:\Program Files\Google\Google Earth Pro\client\googleearth.exe"),
    Path(r"C:\Program Files (x86)\Google\Google Earth Pro\client\googleearth.exe"),
)


def locate_google_earth(configured: str | None = None) -> Path | None:
    candidates = [Path(configured)] if configured else []
    candidates.extend(STANDARD_GOOGLE_EARTH_PATHS)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def generated_kml_path(project_root: Path, area_code: str | None, open_all: bool) -> Path:
    if open_all:
        return project_root / "generated" / "kml" / "selection_91_areas.kml"
    if not area_code:
        raise ValueError("An area code is required unless --all is used")
    return project_root / "generated" / "kml" / f"{area_code}.kml"


def open_for_verification(executable: Path, kml_path: Path) -> int:
    """Open a generated KML. No license, authentication, or dialog automation occurs."""
    if not executable.is_file():
        raise FileNotFoundError(f"Google Earth Pro executable not found: {executable}")
    if not kml_path.is_file():
        raise FileNotFoundError(f"Generated KML not found: {kml_path}. Run generate first.")
    process = subprocess.Popen([str(executable), str(kml_path)], cwd=str(executable.parent))
    return process.pid
