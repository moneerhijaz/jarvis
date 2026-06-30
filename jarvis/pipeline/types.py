"""Run request/result DTOs for the pipeline runtime.

Neutral envelope types shared by the app and the pipeline runner. (Formerly lived in the
old agent loop; relocated here so nothing depends on that removed module.)
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class RunRequest(BaseModel):
    run_id: str
    thread_id: str | None = None
    message: str
    working_directory: str | None = None
    project: str | None = None
    model_role: str = "brain"
    model_id: str | None = None   # explicit model for this run (auto-routing)


class RunResult(BaseModel):
    run_id: str
    status: str
    final_answer: str | None = None
    steps: int = 0
    artifacts: list[str] = Field(default_factory=list)
    error: str | None = None
