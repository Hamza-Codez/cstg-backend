from enum import StrEnum


class TicketStatus(StrEnum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class Priority(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Role(StrEnum):
    CUSTOMER = "CUSTOMER"
    AGENT = "AGENT"
    DISPATCHER = "DISPATCHER"
    ADMIN = "ADMIN"


class CustomerTier(StrEnum):
    ENTERPRISE = "ENTERPRISE"
    BUSINESS = "BUSINESS"
    FREE = "FREE"


class Category(StrEnum):
    OUTAGE = "OUTAGE"
    BILLING = "BILLING"
    TECHNICAL = "TECHNICAL"
    GENERAL = "GENERAL"


class CommentType(StrEnum):
    INTERNAL_NOTE = "INTERNAL_NOTE"
    PUBLIC_REPLY = "PUBLIC_REPLY"


class EventType(StrEnum):
    CREATED = "CREATED"
    STATUS_CHANGE = "STATUS_CHANGE"
    ASSIGNMENT = "ASSIGNMENT"
    COMMENT = "COMMENT"
    SLA_BREACH = "SLA_BREACH"
    # P14: attachment upload was the one state change with no audit record,
    # so INV-5 did not hold for it. Added by migration 0010.
    ATTACHMENT = "ATTACHMENT"


class ActorType(StrEnum):
    CUSTOMER = "CUSTOMER"
    USER = "USER"
    SYSTEM = "SYSTEM"
