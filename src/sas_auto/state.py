"""Atomic, interruption-safe workflow state."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


class WorkflowState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": 1, "updated_at": utc_now(), "areas": {}, "events": []}
        with self.path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        if data.get("schema_version") != 1 or not isinstance(data.get("areas"), dict):
            raise ValueError(f"Unsupported or malformed state file: {self.path}")
        return data

    def save(self) -> None:
        self.data["updated_at"] = utc_now()
        atomic_write_json(self.path, self.data)

    def record_area(
        self,
        area_code: str,
        status: str,
        *,
        provider: str | None = None,
        zoom_levels: list[int] | None = None,
        output_paths: list[str] | None = None,
        error: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        record = self.data["areas"].setdefault(area_code, {"attempts": []})
        record.update(
            {
                "status": status,
                "updated_at": utc_now(),
                "provider": provider,
                "zoom_levels": zoom_levels or [],
                "output_paths": output_paths or [],
                "error": error,
            }
        )
        attempt = {"timestamp": utc_now(), "status": status}
        if details:
            attempt["details"] = details
        if error:
            attempt["error"] = error
        record["attempts"].append(attempt)
        self.save()

    def record_event(self, event: str, details: dict[str, Any] | None = None) -> None:
        self.data.setdefault("events", []).append(
            {"timestamp": utc_now(), "event": event, "details": details or {}}
        )
        self.save()

    def status_for(self, area_code: str) -> str:
        return self.data.get("areas", {}).get(area_code, {}).get("status", "pending")

    def next_incomplete(self, area_codes: list[str]) -> str | None:
        for area_code in area_codes:
            if self.status_for(area_code) not in {"completed", "dry_run_completed"}:
                return area_code
        return None
