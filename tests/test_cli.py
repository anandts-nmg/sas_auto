from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from sas_auto.cli import _record_scope, _require_completed_pilot, _run_scope, _state_path, build_parser
from sas_auto.kmz_parser import inspect_kmz
from sas_auto.raster_export import resolve_export_settings
from sas_auto.sasplanet import SessionArtifact
from sas_auto.state import WorkflowState


def _config(project_root: Path) -> dict[str, Any]:
    value = yaml.safe_load((project_root / "config.yaml").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def test_feature_alias_accepts_arbitrary_portable_id() -> None:
    args = build_parser().parse_args(["run", "--feature", "custom-feature_7", "--dry-run"])
    assert args.area == "custom-feature_7"


def test_dataset_namespaces_state_and_export_paths(project_root: Path) -> None:
    config = _config(project_root)
    namespaced = deepcopy(config)
    namespaced["dataset"]["namespace_outputs"] = True

    state_path = _state_path(namespaced, project_root, "selection_92")
    export = resolve_export_settings(namespaced, project_root, "selection_92")

    assert state_path == project_root / "state" / "datasets" / "selection_92.json"
    assert export.output_directory == project_root / "output" / "selection_92"


def test_multi_download_requires_completed_pilot(project_root: Path, tmp_path: Path) -> None:
    config = _config(project_root)
    isolated = deepcopy(config)
    isolated["dataset"]["namespace_outputs"] = False
    result = inspect_kmz(
        project_root / "inputs" / "Selection_91_All_Areas.kmz",
        profile="selection_91",
        dataset_id="selection_91",
    )
    areas = list(result.areas[:2])

    with pytest.raises(ValueError, match="pilot 9101"):
        _require_completed_pilot(isolated, tmp_path, result, areas)

    state = WorkflowState(_state_path(isolated, tmp_path, result.dataset_id))
    state.record_area("9101", "completed")
    _require_completed_pilot(isolated, tmp_path, result, areas)


def test_preparation_does_not_reset_completed_state(tmp_path: Path, project_root: Path) -> None:
    result = inspect_kmz(project_root / "inputs" / "Selection_91_All_Areas.kmz")
    area = result.areas[0]
    state = WorkflowState(tmp_path / "workflow.json")
    state.record_area(area.area_code, "completed")
    artifact = SessionArtifact(
        path=str(tmp_path / "9101.sls"),
        sha256="0" * 64,
        provider_name="ESRI ArcGIS.Imagery",
        provider_guid="{7B743985-BC5F-4AB6-8915-AC5DBBB8F552}",
        zoom_levels=(16,),
        area_codes=(area.area_code,),
        polygon_count=1,
        coordinate_count=len(area.coordinates),
        missing_tiles_only=True,
    )

    _record_scope(state, [area], "session_ready", artifact, {"network_download_started": False})

    assert state.status_for(area.area_code) == "completed"
    assert state.data["events"][-1]["event"] == "completed_area_status_preserved"


def test_confirmed_run_refuses_unsafe_tile_scope_before_launch(
    tmp_path: Path, project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    area = inspect_kmz(project_root / "inputs" / "Selection_91_All_Areas.kmz").areas[0]
    artifact = SessionArtifact(
        path=str(tmp_path / "9101.sls"),
        sha256="0" * 64,
        provider_name="ESRI ArcGIS.Imagery",
        provider_guid="{7B743985-BC5F-4AB6-8915-AC5DBBB8F552}",
        zoom_levels=(16,),
        area_codes=(area.area_code,),
        polygon_count=1,
        coordinate_count=len(area.coordinates),
        missing_tiles_only=True,
    )
    unsafe_plan = {
        "plan_path": str(tmp_path / "plan.json"),
        "command": ["SASPlanet.exe", "--sls-autostart", artifact.path],
        "safety": {"within_limits": False, "violations": ["test scope violation"]},
    }

    def fake_prepare(*args: Any, **kwargs: Any) -> tuple[SessionArtifact, None, dict[str, Any]]:
        return artifact, None, unsafe_plan

    def fail_launch(*args: Any, **kwargs: Any) -> int:
        raise AssertionError("launcher must not run")

    monkeypatch.setattr("sas_auto.cli._prepare_scope", fake_prepare)
    monkeypatch.setattr("sas_auto.cli.launch_sls_session", fail_launch)
    config = {"dataset": {"namespace_outputs": False}, "sasplanet_exe": str(tmp_path / "SASPlanet.exe")}

    with pytest.raises(ValueError, match="outside configured tile-scope safety limits"):
        _run_scope(argparse.Namespace(confirm_download=True), config, tmp_path, [area], "selection_91")
