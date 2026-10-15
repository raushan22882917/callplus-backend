import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from packages.database.models import TicketPriority, TicketStatus


class TicketCreate(BaseModel):
    customer_id: uuid.UUID
    subject: str = Field(min_length=1, max_length=255)
    priority: TicketPriority = TicketPriority.MEDIUM


class TicketRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    customer_id: uuid.UUID
    assigned_to_user_id: uuid.UUID | None
    subject: str
    status: TicketStatus
    priority: TicketPriority
    created_at: datetime
    updated_at: datetime


class TicketUpdate(BaseModel):
    status: TicketStatus | None = None
    priority: TicketPriority | None = None
    assigned_to_user_id: uuid.UUID | None = None


class TicketCommentCreate(BaseModel):
    body: str = Field(min_length=1)


class TicketCommentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ticket_id: uuid.UUID
    author_user_id: uuid.UUID | None
    body: str
    created_at: datetime
