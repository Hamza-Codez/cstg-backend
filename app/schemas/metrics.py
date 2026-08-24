from pydantic import BaseModel, ConfigDict

from app.models.enums import Priority


class PriorityMetrics(BaseModel):
    open: int
    in_progress: int
    resolved: int
    closed: int
    breached_open: int
    breach_rate: float
    avg_resolution_seconds: int

    model_config = ConfigDict(from_attributes=True)


class MetricsOverview(BaseModel):
    open: int
    in_progress: int
    resolved: int
    closed: int
    breached_open: int
    breach_rate: float
    avg_resolution_seconds: int
    by_priority: dict[Priority, PriorityMetrics]

    model_config = ConfigDict(from_attributes=True)
