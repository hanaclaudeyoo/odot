from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


AxisValue = float


class TaskBase(BaseModel):
    title: str
    importance: AxisValue = Field(ge=0, le=10)
    difficulty: AxisValue = Field(ge=0, le=10)
    time_estimate_minutes: int = Field(gt=0)
    deadline_at: str | None = None
    category_id: int | None = None
    tag_ids: list[int] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        title = value.strip()
        if not title:
            raise ValueError("title is required")
        return title

    @field_validator("deadline_at")
    @classmethod
    def validate_deadline_at(cls, value: str | None) -> str | None:
        if value is None:
            return None
        deadline = value.strip()
        if not deadline:
            return None
        try:
            datetime.fromisoformat(deadline)
        except ValueError as exc:
            raise ValueError("deadline must be a valid ISO datetime") from exc
        return deadline


class TaskCreate(TaskBase):
    # Reject unknown keys so a client still sending urgency fails loudly.
    model_config = ConfigDict(extra="forbid")


class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    importance: AxisValue | None = Field(default=None, ge=0, le=10)
    difficulty: AxisValue | None = Field(default=None, ge=0, le=10)
    time_estimate_minutes: int | None = Field(default=None, gt=0)
    deadline_at: str | None = None
    category_id: int | None = None
    tag_ids: list[int] | None = None

    @field_validator("title")
    @classmethod
    def clean_optional_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        title = value.strip()
        if not title:
            raise ValueError("title cannot be empty")
        return title

    @field_validator("deadline_at")
    @classmethod
    def validate_optional_deadline_at(cls, value: str | None) -> str | None:
        if value is None:
            return None
        deadline = value.strip()
        if not deadline:
            return None
        try:
            datetime.fromisoformat(deadline)
        except ValueError as exc:
            raise ValueError("deadline must be a valid ISO datetime") from exc
        return deadline


class Task(TaskBase):
    id: int
    # Derived from deadline_at on read; never accepted as input.
    urgency: AxisValue
    status: Literal["active", "archived", "deleted"]
    created_at: str
    archived_at: str | None = None
    actual_duration_seconds: int | None = None
    category_snapshot: str | None = None


class Tag(BaseModel):
    id: int
    name: str
    sort_order: int


class CategoryBase(BaseModel):
    name: str = Field(max_length=32)
    parent_id: int | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("name is required")
        if len(name) > 32:
            raise ValueError("name must be 32 characters or fewer")
        return name


class CategoryCreate(CategoryBase):
    pass


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=32)
    parent_id: int | None = None
    sort_order: int | None = Field(default=None, ge=0)

    @field_validator("name")
    @classmethod
    def clean_optional_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        name = value.strip()
        if not name:
            raise ValueError("name cannot be empty")
        if len(name) > 32:
            raise ValueError("name must be 32 characters or fewer")
        return name


class Category(CategoryBase):
    id: int
    sort_order: int
    created_at: str


class CategoryDeletePreview(BaseModel):
    active_task_count: int
    category_count: int


class PullRequest(BaseModel):
    energy_level: AxisValue = Field(ge=0, le=10)


class ActiveSession(BaseModel):
    task: Task
    started_at: str
    decline_available_until: str


class DeclineEditRequest(BaseModel):
    action: Literal["update", "delete"]
    task: TaskUpdate | None = None
