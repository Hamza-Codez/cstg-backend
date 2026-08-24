from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.domain.errors import (
    BusinessRuleViolation,
    DomainError,
    Forbidden,
    NotFound,
    StateConflict,
    Unauthenticated,
    ValidationError,
)
from app.schemas.common import ErrorDetail, ErrorEnvelope

# One entry per row of the taxonomy in docs/API.md §2.
_STATUS_BY_ERROR: list[tuple[type[DomainError], int, str]] = [
    (ValidationError, status.HTTP_400_BAD_REQUEST, "VALIDATION_ERROR"),
    (Unauthenticated, status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED"),
    (Forbidden, status.HTTP_403_FORBIDDEN, "FORBIDDEN"),
    (NotFound, status.HTTP_404_NOT_FOUND, "NOT_FOUND"),
    (StateConflict, status.HTTP_409_CONFLICT, "STATE_CONFLICT"),
    (BusinessRuleViolation, status.HTTP_422_UNPROCESSABLE_CONTENT, "BUSINESS_RULE_VIOLATION"),
]


async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    """Map a domain error to its taxonomy entry by type.

    Dispatch is on the exception class alone — an earlier version keyed off the
    request path and a substring of the message, which meant rewording an error
    could silently change its status code.
    """
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    code = "INTERNAL_ERROR"
    for error_type, mapped_status, mapped_code in _STATUS_BY_ERROR:
        if isinstance(exc, error_type):
            status_code, code = mapped_status, mapped_code
            break

    envelope = ErrorEnvelope(
        error=ErrorDetail(code=code, message=str(exc) or "An error occurred", details={})
    )
    return JSONResponse(status_code=status_code, content=envelope.model_dump())


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    envelope = ErrorEnvelope(
        error=ErrorDetail(
            code="VALIDATION_ERROR",
            message="Request failed schema validation.",
            details={"errors": exc.errors()},
        )
    )
    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content=envelope.model_dump())


async def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
    import logging
    logging.exception("Unhandled exception:")
    envelope = ErrorEnvelope(
        error=ErrorDetail(
            code="INTERNAL_ERROR", message="An unexpected error occurred.", details={}
        )
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content=envelope.model_dump()
    )
