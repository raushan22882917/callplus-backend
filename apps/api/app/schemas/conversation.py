import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from packages.database.models import MessageDirection, MessageSender


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    direction: MessageDirection
    sender: MessageSender
    text: str | None
    media_url: str | None
    created_at: datetime
