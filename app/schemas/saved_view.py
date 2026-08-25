import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.ticket_filters import TicketFilters


class SavedViewCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    filters: TicketFilters


class SavedViewResponse(BaseModel):
    id: uuid.UUID
    name: str
    filters: TicketFilters
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
