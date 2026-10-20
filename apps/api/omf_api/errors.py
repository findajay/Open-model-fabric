"""Error normalisation: every error response has the same shape.

``{"error": {"code", "message", "request_id", "retryable", "details"}}``

Domain errors map to HTTP status codes here (the only place that knows HTTP). Unexpected
exceptions become a generic 500 without internals; request validation errors never echo
the submitted input (which could contain prompts or secrets).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from omf.core.domain import errors as e
from omf.core.domain.base import DomainModel
from omf_api.context import current_request_id

log = logging.getLogger(__name__)

STATUS_BY_TYPE: list[tuple[type[e.OMFError], int]] = [
    (e.ContextLengthExceeded, 422),
    (e.ValidationFailed, 422),
    (e.EgressDenied, 422),
    (e.CapabilityNotSupported, 422),
    (e.NotFound, 404),
    (e.Conflict, 409),
    (e.ModelUnavailable, 409),
    (e.ChecksumMismatch, 409),
    (e.RequestCancelled, 409),
    (e.OperationNotSupported, 501),
    (e.ProviderTimeout, 504),
    (e.ProviderUnavailable, 503),
    (e.ProviderDisabled, 503),
    (e.NoRouteAvailable, 503),
    (e.ProviderAuthenticationFailed, 502),
    (e.ProviderError, 502),
    (e.StorageError, 502),
    (e.PolicyViolation, 403),
    (e.PermissionDenied, 403),
    (e.ComputeError, 502),
    (e.AuthenticationRequired, 401),
    (e.RateLimited, 429),
    (e.SecretNotFound, 500),
    (e.ConfigurationError, 500),
]


def status_for(error: e.OMFError) -> int:
    for cls, status in STATUS_BY_TYPE:
        if isinstance(error, cls):
            return status
    return 500


class ErrorDetail(DomainModel):
    code: str
    message: str
    request_id: str | None
    retryable: bool = False
    details: dict[str, Any] = {}


class ErrorResponse(DomainModel):
    error: ErrorDetail


def error_response(
    status: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=message,
            request_id=current_request_id(),
            retryable=retryable,
            details=details or {},
        )
    )
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"), headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(e.OMFError)
    async def omf_error(_req: Request, exc: e.OMFError) -> JSONResponse:
        status = status_for(exc)
        if status >= 500:
            log.warning("request failed: %s %s", exc.code, exc.message)
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
        if status == 429:
            headers = {"Retry-After": str(exc.details.get("retry_after_s", 1))}
        return error_response(
            status,
            exc.code,
            exc.message,
            retryable=exc.retryable,
            details=_jsonable(exc.details),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_req: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                "loc": [str(p) for p in err.get("loc", ())],
                "msg": err.get("msg", ""),
                "type": err.get("type", ""),
            }
            for err in exc.errors()
        ]
        return error_response(
            422, "validation_failed", "request validation failed", details={"errors": errors}
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_req: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            404: "not_found",
            405: "method_not_allowed",
            401: "authentication_required",
            403: "permission_denied",
        }.get(exc.status_code, "http_error")
        return error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def unexpected(_req: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error")
        return error_response(500, "internal_error", "internal server error")


def _jsonable(details: dict[str, Any]) -> dict[str, Any]:
    import json

    try:
        json.dumps(details)
        return details
    except (TypeError, ValueError):
        return {k: str(v) for k, v in details.items()}
