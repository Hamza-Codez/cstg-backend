import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import CommentType


class CommentCreate(BaseModel):
    type: CommentType
    body: str = Field(..., min_length=1, max_length=10000)


class CommentAuthor(BaseModel):
    type: str  # "CUSTOMER" or "USER"
    id: uuid.UUID
    name: str


class CommentResponse(BaseModel):
    id: uuid.UUID
    ticket_id: uuid.UUID
    author: CommentAuthor
    type: CommentType
    body: str
    created_at: datetime
