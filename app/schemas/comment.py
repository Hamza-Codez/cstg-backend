import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import CommentType


class CommentCreate(BaseModel):
    type: CommentType
    body: str = Field(..., min_length=1, max_length=10000)


class CommentResponse(BaseModel):
    id: uuid.UUID
    ticket_id: uuid.UUID
    author_id: uuid.UUID
    type: CommentType
    body: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
