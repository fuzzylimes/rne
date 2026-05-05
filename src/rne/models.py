from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Optional


class JobStatus(StrEnum):
    QUEUED = "queued"
    PAUSED = "paused"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


@dataclass
class HandbrakeArgs:
    encoder: str = "x265"
    quality: int = 20
    preset: str = "slow"
    audio_tracks: list[int] = field(default_factory=lambda: [1])
    audio_codec: str = "copy"
    subtitle_tracks: list[int] = field(default_factory=list)
    decomb: bool = False
    extra_args: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "encoder": self.encoder,
                "quality": self.quality,
                "preset": self.preset,
                "audio_tracks": self.audio_tracks,
                "audio_codec": self.audio_codec,
                "subtitle_tracks": self.subtitle_tracks,
                "decomb": self.decomb,
                "extra_args": self.extra_args,
            }
        )

    @classmethod
    def from_json(cls, s: str) -> HandbrakeArgs:
        return cls(**json.loads(s))


@dataclass
class Job:
    id: int
    source_path: str
    output_path: str
    handbrake_args: HandbrakeArgs
    status: JobStatus
    attempt_count: int
    priority: int
    show: Optional[str]
    season: Optional[int]
    episode: Optional[int]
    movie: Optional[str]
    progress_pct: Optional[float]
    progress_fps: Optional[float]
    progress_eta: Optional[int]
    created_at: str
    started_at: Optional[str]
    finished_at: Optional[str]
    exit_code: Optional[int]
    error_message: Optional[str]
    ingest_batch_id: Optional[int]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Job:
        return cls(
            id=row["id"],
            source_path=row["source_path"],
            output_path=row["output_path"],
            handbrake_args=HandbrakeArgs.from_json(row["handbrake_args"]),
            status=JobStatus(row["status"]),
            attempt_count=row["attempt_count"],
            priority=row["priority"],
            show=row["show"],
            season=row["season"],
            episode=row["episode"],
            movie=row["movie"],
            progress_pct=row["progress_pct"],
            progress_fps=row["progress_fps"],
            progress_eta=row["progress_eta"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            exit_code=row["exit_code"],
            error_message=row["error_message"],
            ingest_batch_id=row["ingest_batch_id"],
        )


@dataclass
class IngestBatch:
    id: int
    label: str
    show: Optional[str]
    movie: Optional[str]
    notes: Optional[str]
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> IngestBatch:
        return cls(
            id=row["id"],
            label=row["label"],
            show=row["show"],
            movie=row["movie"],
            notes=row["notes"],
            created_at=row["created_at"],
        )


@dataclass
class WorkerStatus:
    id: int
    pid: Optional[int]
    state: str
    current_job_id: Optional[int]
    last_seen: str
    started_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> WorkerStatus:
        return cls(
            id=row["id"],
            pid=row["pid"],
            state=row["state"],
            current_job_id=row["current_job_id"],
            last_seen=row["last_seen"],
            started_at=row["started_at"],
        )
