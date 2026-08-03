# zatca_api/services/envelope.py
# Copyright (c) 2026, Enfono Technologies and contributors
"""Uniform response envelope for every ZATCA API endpoint.

Frappe wraps a whitelisted function's return value in ``{"message": ...}``, so a
caller sees::

    {"message": {"success": true, "data": {...}, "errors": []}}

That wrapper is Frappe's, not ours, and is documented in the user guide. The
envelope below is what lives inside it. Keeping the shape identical for success
and failure means an integrator writes one parser, not two.
"""

import frappe
from frappe.utils import now

# Machine-readable error codes. Callers should branch on these, never on message
# text, which is translated and may change.
ERR_DISABLED = 'app_disabled'
ERR_UNAUTHORIZED = 'unauthorized'
ERR_FORBIDDEN = 'forbidden'
ERR_VALIDATION = 'validation_error'
ERR_NOT_FOUND = 'not_found'
ERR_DUPLICATE = 'duplicate'
ERR_IMMUTABLE = 'immutable_document'
ERR_ZATCA_UNAVAILABLE = 'zatca_unavailable'
ERR_UPSTREAM = 'upstream_error'
ERR_INTERNAL = 'internal_error'

HTTP_FOR_CODE = {
    ERR_DISABLED: 503,
    ERR_UNAUTHORIZED: 401,
    ERR_FORBIDDEN: 403,
    ERR_VALIDATION: 400,
    ERR_NOT_FOUND: 404,
    ERR_DUPLICATE: 409,
    ERR_IMMUTABLE: 409,
    ERR_ZATCA_UNAVAILABLE: 424,
    ERR_UPSTREAM: 502,
    ERR_INTERNAL: 500,
}


def new_request_id() -> str:
    """Correlation id echoed in the response and stored on the request log."""
    return frappe.generate_hash(length=12)


def success_response(data: dict, request_id: str | None = None, warnings: list | None = None) -> dict:
    return {
        'success': True,
        'request_id': request_id or new_request_id(),
        'timestamp': now(),
        'data': data or {},
        'warnings': warnings or [],
        'errors': [],
    }


def error_response(
    code: str,
    message: str,
    request_id: str | None = None,
    details: dict | None = None,
    set_http_status: bool = True,
) -> dict:
    """Build an error envelope and set the HTTP status code to match.

    ``set_http_status`` is False for per-item errors inside a batch response, where
    the overall request succeeded.
    """
    if set_http_status and getattr(frappe.local, 'response', None) is not None:
        frappe.local.response['http_status_code'] = HTTP_FOR_CODE.get(code, 400)

    return {
        'success': False,
        'request_id': request_id or new_request_id(),
        'timestamp': now(),
        'data': {},
        'warnings': [],
        'errors': [
            {
                'code': code,
                'message': message,
                'details': details or {},
            }
        ],
    }


def item_error(code: str, message: str, details: dict | None = None) -> dict:
    """An error entry for one item of a batch, without touching the HTTP status."""
    return {'code': code, 'message': message, 'details': details or {}}
