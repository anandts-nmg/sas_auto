from __future__ import annotations

import json
from pathlib import Path

from sas_auto.state import WorkflowState, atomic_write_json


def test_atomic_json_write(tmp_path: Path):
    path = tmp_path / "state" / "value.json"
    atomic_write_json(path, {"name": "Монгол", "value": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {"name": "Монгол", "value": 1}
    assert list(path.parent.glob("*.tmp")) == []


def test_state_persistence_and_resume(tmp_path: Path):
    path = tmp_path / "workflow.json"
    state = WorkflowState(path)
    assert state.next_incomplete(["9101", "9102"]) == "9101"
    state.record_area("9101", "dry_run_completed", provider="Google - Satellite", zoom_levels=[15])
    reloaded = WorkflowState(path)
    assert reloaded.status_for("9101") == "dry_run_completed"
    assert reloaded.next_incomplete(["9101", "9102"]) == "9102"
    reloaded.record_area("9102", "completed", output_paths=["output/9102/result.tif"])
    assert WorkflowState(path).next_incomplete(["9101", "9102"]) is None
