import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AssistantChatRequest(BaseModel):
    conversation_id: uuid.UUID | None = None
    message: str = Field(min_length=1, max_length=4000)


class AssistantChatResponse(BaseModel):
    conversation_id: uuid.UUID
    reply_text: str


class AssistantConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
