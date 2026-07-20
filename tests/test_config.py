from __future__ import annotations

from copy import deepcopy

import yaml

from sas_auto.config import load_config
from sas_auto.validation import validate_config, validate_output_files


def test_project_configuration_is_valid(project_root):
    config, detected_root = load_config(project_root / "config.yaml")
    assert detected_root == project_root
    assert config["dry_run"] is True
    assert config["area_codes"] == ["9101"]
    assert config["imagery"]["zoom_levels"] == [15]


def test_configuration_validation_rejects_unsafe_values(project_root):
    config = yaml.safe_load((project_root / "config.yaml").read_text(encoding="utf-8"))
    bad = deepcopy(config)
    bad["dry_run"] = "yes"
    bad["area_codes"] = ["9999"]
    bad["imagery"]["zoom_levels"] = [99]
    errors = validate_config(bad, project_root)
    assert any("dry_run" in error for error in errors)
    assert any("unsupported" in error for error in errors)
    assert any("zoom_levels" in error for error in errors)


def test_output_validation(tmp_path):
    good = tmp_path / "good.tif"
    empty = tmp_path / "empty.jpg"
    missing = tmp_path / "missing.tif"
    good.write_bytes(b"content")
    empty.write_bytes(b"")
    assert validate_output_files([good]) == []
    errors = validate_output_files([good, empty, missing])
    assert any("empty" in error.lower() for error in errors)
    assert any("does not exist" in error for error in errors)
