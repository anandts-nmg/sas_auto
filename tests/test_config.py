from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import yaml

from sas_auto.config import load_config
from sas_auto.validation import validate_config


def test_project_configuration_is_valid(project_root: Path) -> None:
    config, detected_root = load_config(project_root / "config.yaml")
    assert detected_root == project_root
    assert config["dry_run"] is True
    assert config["area_codes"] == ["9101"]
    assert config["imagery"]["source"] == "Esri World Imagery"
    assert config["imagery"]["zoom_levels"] == [16]
    assert config["imagery"]["download_missing_tiles_only"] is True
    assert config["sessions"]["directory"] == "generated/sls"
    assert config["export"]["enabled"] is True
    assert config["export"]["preferred_format"] == "GeoTIFF"
    assert config["export"]["include_georeferencing"] is True


def test_configuration_validation_rejects_unsafe_values(project_root: Path) -> None:
    config = yaml.safe_load((project_root / "config.yaml").read_text(encoding="utf-8"))
    bad = deepcopy(config)
    bad["dry_run"] = "yes"
    bad["area_codes"] = ["9999"]
    bad["imagery"]["source"] = ""
    bad["imagery"]["zoom_levels"] = [0, 99]
    bad["sessions"]["workers_count"] = 0
    bad["export"]["preferred_format"] = "BMP"
    bad["export"]["preview_max_size"] = 1
    errors = validate_config(bad, project_root)
    assert any("dry_run" in error for error in errors)
    assert any("unsupported" in error for error in errors)
    assert any("imagery.source" in error for error in errors)
    assert any("zoom_levels" in error for error in errors)
    assert any("workers_count" in error for error in errors)
    assert any("preferred_format" in error for error in errors)
    assert any("preview_max_size" in error for error in errors)
