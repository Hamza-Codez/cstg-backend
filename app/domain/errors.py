"""Domain errors.

Each maps to exactly one entry of the error taxonomy in docs/API.md §2. The
mapping is by type in `api/errors.py` — never by inspecting messages or paths,
so a reworded message can never change a status code.
"""


class DomainError(Exception):
    """Base class for all domain exceptions."""


class ValidationError(DomainError):
    """Input failed validation that Pydantic could not express. -> 400"""


class Unauthenticated(DomainError):
    """Missing, malformed, or rejected credentials. -> 401

    Distinct from Forbidden: this says "we do not know who you are", not "we know
    and you may not". Keeping them separate is what lets the handler decide the
    status from the type alone.
    """


class Forbidden(DomainError):
    """Authenticated, but not permitted to perform this action. -> 403"""


class NotFound(DomainError):
    """Resource does not exist, or is hidden from this principal. -> 404"""


class StateConflict(DomainError):
    """Illegal transition, or a guarded update that lost a race. -> 409"""


class BusinessRuleViolation(DomainError):
    """Shape is valid but a domain rule rejects it. -> 422"""
