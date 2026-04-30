"""Resume support: persist per-stage state.

Each run gets a directory under `.lattice/runs/<run_id>/`. After every
stage, the ResumeManager writes the current state.json. On resume, the
latest run's state.json tells us which stage to restart from.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field


class Stage(str, Enum):
    ingest = "ingest"
    index = "index"
    enrich = "enrich"
    shadow = "shadow"
    differ = "differ"
    review = "review"
    plan = "plan"
    render = "render"
    audit = "audit"
    flags = "flags"
    propose = "propose"
    edits = "edits"
    apply = "apply"


class StageStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    interrupted = "interrupted"


_STAGE_ORDER: list[Stage] = [
    Stage.ingest,
    Stage.index,
    Stage.enrich,
    Stage.shadow,
    Stage.differ,
    Stage.review,
    Stage.plan,
    Stage.render,
    Stage.audit,
    Stage.flags,
    Stage.propose,
    Stage.edits,
    Stage.apply,
]


class RunState(BaseModel):
    run_id: str
    started_at: datetime
    last_updated_at: datetime
    voice: str | None = None
    stage_status: dict[Stage, StageStatus] = Field(default_factory=dict)
    last_completed_stage: Stage | None = None
    error: str | None = None


class ResumeManager:
    def __init__(self, project_path: Path) -> None:
        self.runs_dir = Path(project_path) / ".lattice" / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def start_run(self, voice: str | None = None) -> RunState:
        now = datetime.now(timezone.utc)
        run_id = now.strftime("%Y-%m-%dT%H-%M-%S")
        state = RunState(run_id=run_id, started_at=now, last_updated_at=now, voice=voice)
        self._write(state)
        return state

    def update_stage(self, run_id: str, stage: Stage, status: StageStatus) -> None:
        path = self._state_path(run_id)
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        state = RunState.model_validate(data)
        state.stage_status[stage] = status
        state.last_updated_at = datetime.now(timezone.utc)
        if status == StageStatus.completed:
            state.last_completed_stage = stage
        self._write(state)

    def latest_run(self) -> RunState | None:
        candidates = sorted(p for p in self.runs_dir.iterdir() if p.is_dir())
        if not candidates:
            return None
        path = candidates[-1] / "state.json"
        if not path.exists():
            return None
        return RunState.model_validate_json(path.read_text(encoding="utf-8"))

    def next_stage_after(self, run_state: RunState) -> Stage | None:
        last = run_state.last_completed_stage
        if last is None:
            return _STAGE_ORDER[0]
        try:
            idx = _STAGE_ORDER.index(last)
        except ValueError:
            return _STAGE_ORDER[0]
        return _STAGE_ORDER[idx + 1] if idx + 1 < len(_STAGE_ORDER) else None

    def _state_path(self, run_id: str) -> Path:
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir / "state.json"

    def _write(self, state: RunState) -> None:
        path = self._state_path(state.run_id)
        path.write_text(state.model_dump_json(indent=2), encoding="utf-8")


__all__ = ["Stage", "StageStatus", "RunState", "ResumeManager"]
