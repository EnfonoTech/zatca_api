# zatca_api/utils/auth.py
# Copyright (c) 2026, Enfono Technologies and contributors
"""Request guards applied at the top of every endpoint.

Layering, outermost first:

1. **Frappe token auth** - handled by the framework before our code runs. Every
   endpoint is ``@frappe.whitelist()`` *without* ``allow_guest``, so an anonymous
   request is rejected by Frappe with 403 and never reaches this module.
2. **Guest rejection** - belt and braces, in case a future edit adds
   ``allow_guest=True`` by accident.
3. **Master switch** - ZATCA API Settings.enabled.
4. **Shared secret header** - optional second factor, compared in constant time.
5. **IP allowlist** - optional CIDR restriction.
6. **DocType permission** - the caller's roles must actually permit the operation.

Step 6 is the one that matters most: it is what makes this app safe to expose,
and it is exactly what the previous implementation skipped by calling
``frappe.set_user("Administrator")`` inside a whitelisted method.
"""

import hmac
import ipaddress

import frappe
from frappe import _

from zatca_api.services.envelope import ERR_DISABLED, ERR_FORBIDDEN, ERR_UNAUTHORIZED


class GuardError(Exception):
    """Raised when a request must be refused. Carries an envelope error code."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def get_client_ip() -> str | None:
    request = getattr(frappe.local, 'request', None)
    if request is None:
        return None

    # Honour the proxy header only for its left-most entry; anything further right
    # is attacker-controlled when the app sits behind a single reverse proxy.
    forwarded = request.headers.get('X-Forwarded-For')
    if forwarded:
        return forwarded.split(',')[0].strip()

    return request.headers.get('X-Real-IP') or request.remote_addr


def _check_enabled(settings) -> None:
    if not settings.enabled:
        raise GuardError(ERR_DISABLED, _('ZATCA API is disabled in ZATCA API Settings.'))


def _check_user() -> None:
    if frappe.session.user in ('Guest', None, ''):
        raise GuardError(
            ERR_UNAUTHORIZED,
            _('Authentication required. Send an Authorization header: token <api_key>:<api_secret>.'),
        )


def _check_shared_secret(settings) -> None:
    if not settings.require_shared_secret:
        return

    request = getattr(frappe.local, 'request', None)
    if request is None:
        # Server-side call (scheduler, bench execute, test). Transport guards do
        # not apply because there is no transport.
        return

    header_name = (settings.shared_secret_header or 'X-ZATCA-API-Secret').strip()
    provided = request.headers.get(header_name) or ''
    expected = settings.get_password('shared_secret', raise_exception=False) or ''

    if not expected:
        raise GuardError(ERR_FORBIDDEN, _('Shared secret is required but not configured on the server.'))

    if not hmac.compare_digest(str(provided), str(expected)):
        raise GuardError(
            ERR_FORBIDDEN,
            _('Invalid or missing {0} header.').format(header_name),
        )


def _check_ip(settings) -> None:
    networks = settings.get_allowed_networks()
    if not networks:
        return

    request = getattr(frappe.local, 'request', None)
    if request is None:
        return

    client_ip = get_client_ip()
    if not client_ip:
        raise GuardError(ERR_FORBIDDEN, _('Could not determine client IP address.'))

    try:
        address = ipaddress.ip_address(client_ip)
    except ValueError:
        raise GuardError(ERR_FORBIDDEN, _('Malformed client IP address.'))

    if not any(address in network for network in networks):
        raise GuardError(
            ERR_FORBIDDEN,
            _('IP address {0} is not allowed.').format(client_ip),
            {'ip': client_ip},
        )


def require_permission(doctype: str, ptype: str = 'create', doc: str | None = None) -> None:
    """Assert the *session user's* roles permit the operation.

    Note the deliberate absence of any ``ignore_permissions`` escape hatch here.
    Document writes downstream do pass ``ignore_permissions=True`` for master-data
    creation, which is why this check has to happen first and against the real user.
    """
    if not frappe.has_permission(doctype, ptype=ptype, doc=doc):
        raise GuardError(
            ERR_FORBIDDEN,
            _('User {0} is not permitted to {1} {2}.').format(frappe.session.user, ptype, doctype),
            {'doctype': doctype, 'permission': ptype},
        )


def guard_request(settings, doctype: str | None = None, ptype: str = 'create') -> None:
    """Run every guard in order. Raises :class:`GuardError` on the first failure."""
    _check_user()
    _check_enabled(settings)
    _check_shared_secret(settings)
    _check_ip(settings)
    if doctype:
        require_permission(doctype, ptype)
