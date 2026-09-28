"""
Uniform error responses for the whole API (PRD §8).

Every error body has the shape::

    {"detail": "human readable message", "code": "machine_code"}

Validation errors on specific fields additionally include ``fields``, mapping
each field name to its list of messages, so forms can show inline errors::

    {"detail": "email: This field is required.", "code": "validation_error",
     "fields": {"email": ["This field is required."]}}
"""

import logging

from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from rest_framework import exceptions, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler, set_rollback

logger = logging.getLogger(__name__)

NON_FIELD_KEYS = ("non_field_errors", "detail")


def _messages(detail) -> list[str]:
    """Flatten a (possibly nested) DRF error detail into a list of strings."""
    if isinstance(detail, dict):
        return [msg for value in detail.values() for msg in _messages(value)]
    if isinstance(detail, list):
        return [msg for value in detail for msg in _messages(value)]
    return [str(detail)]


def _first_code(detail, fallback: str) -> str:
    if isinstance(detail, list) and detail:
        return _first_code(detail[0], fallback)
    return getattr(detail, "code", None) or fallback


def _validation_body(detail) -> dict:
    # ValidationError("message", code="some_code") -> a plain list of errors.
    if isinstance(detail, list):
        messages = _messages(detail)
        return {
            "detail": messages[0] if messages else "Invalid input.",
            "code": _first_code(detail, "invalid"),
        }

    fields = {key: _messages(value) for key, value in detail.items() if key not in NON_FIELD_KEYS}
    non_field = [msg for key in NON_FIELD_KEYS for msg in _messages(detail.get(key, []))]

    if non_field:
        message = non_field[0]
        code = _first_code(detail.get("non_field_errors") or detail.get("detail"), "invalid")
    elif fields:
        name, msgs = next(iter(fields.items()))
        message = f"{name}: {msgs[0]}" if msgs else "Invalid input."
        code = "validation_error"
    else:
        message, code = "Invalid input.", "validation_error"

    body = {"detail": message, "code": code}
    if fields:
        body["fields"] = fields
    return body


def _error_body(exc, detail) -> dict:
    if isinstance(exc, ValidationError):
        return _validation_body(detail)

    # simplejwt errors carry a dict like {"detail": ..., "code": ..., "messages": [...]}.
    if isinstance(detail, dict):
        message = detail.get("detail") or next(iter(_messages(detail)), "")
        code = detail.get("code") or _first_code(message, None) or exc.default_code
        return {"detail": str(message), "code": str(code)}

    if isinstance(detail, list):
        messages = _messages(detail)
        return {"detail": messages[0] if messages else "", "code": _first_code(detail, exc.default_code)}

    return {"detail": str(detail), "code": getattr(detail, "code", None) or exc.default_code}


def api_exception_handler(exc, context):
    # Mirror DRF's own conversion so `exc.detail` is always available below.
    if isinstance(exc, Http404):
        exc = exceptions.NotFound(*exc.args)
    elif isinstance(exc, PermissionDenied):
        exc = exceptions.PermissionDenied(*exc.args)

    response = exception_handler(exc, context)

    if response is None:
        # Unhandled exception: log it and still answer in the standard shape.
        logger.exception("Unhandled API error", exc_info=exc)
        set_rollback()
        return Response(
            {"detail": "Internal server error.", "code": "server_error"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    response.data = _error_body(exc, exc.detail)
    return response


# Django-level handlers (used when DEBUG is off) so non-DRF 404/500s are JSON too.

def json_404(request, exception=None):
    return JsonResponse({"detail": "Not found.", "code": "not_found"}, status=404)


def json_500(request):
    return JsonResponse({"detail": "Internal server error.", "code": "server_error"}, status=500)
