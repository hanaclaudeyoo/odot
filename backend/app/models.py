from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


AxisValue = int


class TaskBase(BaseModel):
    title: str
    importance: AxisValue = Field(ge=1, le=7)
    urgency: AxisValue = Field(ge=1, le=7)
    difficulty: AxisValue = Field(ge=1, le=7)
    time_estimate_minutes: int = Field(gt=0)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        title = value.strip()
        if not title:
            raise ValueError("title is required")
        return title


class TaskCreate(TaskBase):
    pass


class TaskUpdate(BaseModel):
    title: str | None = None
    importance: AxisValue | None = Field(default=None, ge=1, le=7)
    urgency: AxisValue | None = Field(default=None, ge=1, le=7)
    difficulty: AxisValue | None = Field(default=None, ge=1, le=7)
    time_estimate_minutes: int | None = Field(default=None, gt=0)

    @field_validator("title")
    @classmethod
    def clean_optional_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        title = value.strip()
        if not title:
            raise ValueError("title cannot be empty")
        return title


class Task(TaskBase):
    id: int
    status: Literal["active", "archived"]
    created_at: str
    archived_at: str | None = None
    actual_duration_seconds: int | None = None


class PullRequest(BaseModel):
    energy_level: AxisValue = Field(ge=1, le=7)


class ActiveSession(BaseModel):
    task: Task
    started_at: str
    decline_available_until: str


class DeclineEditRequest(BaseModel):
    action: Literal["update", "delete"]
    task: TaskUpdate | None = None
