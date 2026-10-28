import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from packages.database.models import WorkflowRunStatus, WorkflowTrigger


class WorkflowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    trigger: WorkflowTrigger
    is_active: bool = True
    definition: dict[str, Any]


class RecipeCreate(BaseModel):
    recipe: str = Field(min_length=1, max_length=80)
    answers: dict[str, str] = Field(default_factory=dict)
    name: str | None = Field(default=None, max_length=255)


class WorkflowUpdate(BaseModel):
    name: str | None = None
    is_active: bool | None = None
    definition: dict[str, Any] | None = None


class WorkflowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    trigger: WorkflowTrigger
    is_active: bool
    definition: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class WorkflowRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workflow_id: uuid.UUID
    status: WorkflowRunStatus
    trigger_payload: dict[str, Any]
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None
