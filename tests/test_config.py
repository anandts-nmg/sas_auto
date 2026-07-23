from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import yaml

from sas_auto.config import load_config
from sas_auto.validation import validate_config


def _portable_config(project_root: Path, tmp_path: Path) -> tuple[dict[str, Any], Path]:
    config = yaml.safe_load((project_root / "config.yaml").read_text(encoding="utf-8"))
    assert isinstance(config, dict)
    config = cast(dict[str, Any], config)
    executable = tmp_path / "SASPlanet.exe"
    executable.write_bytes(b"test executable")
    config["sasplanet_exe"] = str(executable)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return config, path


def test_project_configuration_is_valid(project_root: Path, tmp_path: Path) -> None:
    _, path = _portable_config(project_root, tmp_path)
    config, detected_root = load_config(path)
    assert detected_root == project_root
    assert config["dry_run"] is True
    assert config["area_codes"] == ["9101"]
    assert config["dataset"]["profile"] == "selection_91"
    assert config["dataset"]["namespace_outputs"] is True
    assert config["imagery"]["source"] == "Esri World Imagery"
    assert config["imagery"]["zoom_levels"] == [16]
    assert config["imagery"]["download_missing_tiles_only"] is True
    assert config["safety"]["pilot_feature_id"] == "9101"
    assert config["safety"]["require_completed_pilot_before_multi_download"] is True
    assert config["sessions"]["directory"] == "generated/sls"
    assert config["export"]["enabled"] is True
    assert config["export"]["preferred_format"] == "GeoTIFF"
    assert config["export"]["include_georeferencing"] is True
    assert config["export"]["max_mosaic_pixels"] == 75_000_000


def test_configuration_validation_rejects_unsafe_values(project_root: Path, tmp_path: Path) -> None:
    config, _ = _portable_config(project_root, tmp_path)
    bad = deepcopy(config)
    bad["dry_run"] = "yes"
    bad["area_codes"] = ["bad/id"]
    bad["dataset"]["profile"] = "unsupported"
    bad["imagery"]["source"] = ""
    bad["imagery"]["zoom_levels"] = [0, 99]
    bad["sessions"]["workers_count"] = 0
    bad["safety"]["pilot_feature_id"] = "NUL"
    bad["safety"]["max_rectangle_tiles_total"] = 0
    bad["export"]["preferred_format"] = "BMP"
    bad["export"]["preview_max_size"] = 1
    bad["export"]["max_mosaic_pixels"] = 1
    errors = validate_config(bad, project_root)
    assert any("dry_run" in error for error in errors)
    assert any("portable feature IDs" in error for error in errors)
    assert any("dataset.profile" in error for error in errors)
    assert any("imagery.source" in error for error in errors)
    assert any("zoom_levels" in error for error in errors)
    assert any("workers_count" in error for error in errors)
    assert any("pilot_feature_id" in error for error in errors)
    assert any("max_rectangle_tiles_total" in error for error in errors)
    assert any("preferred_format" in error for error in errors)
    assert any("preview_max_size" in error for error in errors)
    assert any("max_mosaic_pixels" in error for error in errors)


def test_configuration_rejects_windows_device_names(project_root: Path, tmp_path: Path) -> None:
    config, _ = _portable_config(project_root, tmp_path)
    bad = deepcopy(config)
    bad["dataset"]["id"] = "CON"
    bad["area_codes"] = ["NUL"]
    errors = validate_config(bad, project_root)
    assert any("dataset.id" in error for error in errors)
    assert any("area_codes" in error for error in errors)
