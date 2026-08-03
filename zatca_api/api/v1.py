# zatca_api/api/v1.py
# Copyright (c) 2026, Enfono Technologies and contributors
"""Public REST surface, version 1.

Every function here is reachable at::

    POST|GET  /api/method/zatca_api.api.v1.<function>

Frappe wraps the return value in ``{"message": ...}``; the envelope documented in
``services/envelope.py`` lives inside that.

Security posture:

* No endpoint uses ``allow_guest``. Authentication is Frappe token auth
  (``Authorization: token <api_key>:<api_secret>``), so an anonymous request is
  rejected by the framework before reaching this module.
* Every endpoint asserts the *session user's* DocType permissions. There is no
  ``frappe.set_user('Administrator')`` anywhere in this app: doing that inside a
  whitelisted method hands full system privileges to any authenticated caller.
* Every list endpoint is paginated with a hard ceiling, so no single call can be
  made to serialise the whole invoice table.
"""

import time

import frappe
from frappe import _
from frappe.utils import cint, cstr, strip_html

from zatca_api.services import invoice as invoice_service
from zatca_api.services import puller, zatca
from zatca_api.services.envelope import (
    ERR_INTERNAL,
    ERR_NOT_FOUND,
    ERR_VALIDATION,
    ERR_ZATCA_UNAVAILABLE,
    error_response,
    new_request_id,
    success_response,
)
from zatca_api.services.payload import PayloadError, normalise_invoice
from zatca_api.utils.auth import GuardError, get_client_ip, guard_request, require_permission
from zatca_api.zatca_api.doctype.zatca_api_settings.zatca_api_settings import get_settings

MAX_PAGE_LIMIT = 200
DEFAULT_PAGE_LIMIT = 20


def _log_request(
    endpoint,
    status,
    request_id,
    payload=None,
    response=None,
    error=None,
    external_id=None,
    reference_name=None,
    http_status=None,
    duration_ms=None,
    zatca_block=None,
):
    """Persist a request log row. Never raises - logging must not break the request."""
    try:
        settings = get_settings()
        if not cint(settings.log_requests):
            return

        log = frappe.new_doc('ZATCA API Request Log')
        log.endpoint = endpoint
        log.direction = 'Inbound'
        log.status = status
        log.http_status = cint(http_status) or None
        log.duration_ms = cint(duration_ms) or None
        log.user = frappe.session.user
        log.ip_address = get_client_ip()
        log.external_id = external_id
        log.reference_doctype = 'Sales Invoice' if reference_name else None
        log.reference_name = reference_name

        if zatca_block:
            log.zatca_phase = zatca_block.get('phase')
            log.zatca_uuid = zatca_block.get('uuid')
            log.zatca_integration_status = zatca_block.get('integration_status')

        if cint(settings.log_payloads):
            if payload is not None:
                log.request_payload = frappe.as_json(_redact(payload))[:1000000]
            if response is not None:
                log.response_payload = frappe.as_json(_strip_heavy(response))[:1000000]

        if error:
            log.error = cstr(error)[:100000]

        log.insert(ignore_permissions=True)
    except Exception:
        frappe.log_error(title='ZATCA API: request log failed', message=frappe.get_traceback())


def _redact(payload):
    """Drop anything secret-shaped before a payload is written to the log."""
    if not isinstance(payload, dict):
        return payload
    redacted = {}
    for key, value in payload.items():
        if any(token in cstr(key).lower() for token in ('secret', 'password', 'token', 'apikey', 'api_key')):
            redacted[key] = '***redacted***'
        else:
            redacted[key] = value
    return redacted


def _strip_heavy(response):
    """Keep the log readable: base64 images and signed XML are recorded by size only."""
    if not isinstance(response, dict):
        return response

    import copy

    trimmed = copy.deepcopy(response)
    block = (trimmed.get('data') or {}).get('zatca')
    if isinstance(block, dict):
        for key in ('qr_png_base64', 'qr_png_data_uri', 'signed_xml'):
            if block.get(key):
                block[key] = f'<{len(block[key])} chars omitted from log>'
    return trimmed


def _reset_request_state() -> None:
    """Clear per-request message state before doing any work.

    In a real HTTP request ``frappe.message_log`` starts empty, but a scheduled
    job, a test run or a `bench execute` call reuses one long-lived process. Some
    validation code - ksa_compliance's Sales Invoice validate hook among it -
    builds its error text by joining the *whole* accumulated message log, so a
    stale entry from an earlier call would surface inside an unrelated caller's
    error response. Clearing it keeps each response about its own request.
    """
    frappe.clear_messages()


def _body() -> dict:
    """Read the request body as a dict.

    ``frappe.form_dict`` already merges a JSON body, the query string and form
    fields, so it covers every content type. The internal ``cmd`` key that Frappe
    adds for its own dispatch is dropped so it cannot collide with a payload key.
    """
    data = dict(frappe.form_dict or {})
    data.pop('cmd', None)
    return data


def _handle_guard_error(endpoint, exc, request_id, payload=None):
    response = error_response(exc.code, exc.message, request_id, exc.details)
    _log_request(
        endpoint,
        'Failed',
        request_id,
        payload=payload,
        response=response,
        error=exc.message,
        http_status=frappe.local.response.get('http_status_code'),
    )
    return response


# ---------------------------------------------------------------------------
# Write endpoints
# ---------------------------------------------------------------------------


@frappe.whitelist(methods=['POST'])
def create_invoice(**kwargs):
    """Create (and by default submit) a Sales Invoice, returning the ZATCA QR.

    Minimal body::

        {
          "external_id": "INV-2026-0001",
          "customer": "Al Rajhi Trading",
          "company": "Enfono KSA",
          "posting_date": "2026-08-04",
          "items": [{"item_code": "SVC-01", "qty": 2, "rate": 500}]
        }

    The response ``data`` carries ``invoice`` (all header, item and tax values) and
    ``zatca`` (phase, uuid, invoice hash, QR content and QR PNG).
    """
    return _create(kwargs, is_return=False, endpoint='create_invoice')


@frappe.whitelist(methods=['POST'])
def create_credit_note(**kwargs):
    """Create a credit note (``is_return = 1``).

    Item quantities are sign-normalised, so send either positive or negative
    figures. ``return_against`` is optional but, when given, must reference a
    submitted Sales Invoice - ZATCA requires a credit note to identify its original.
    """
    return _create(kwargs, is_return=True, endpoint='create_credit_note')


def _create(kwargs: dict, is_return: bool, endpoint: str):
    request_id = new_request_id()
    started = time.monotonic()
    _reset_request_state()
    raw = kwargs or _body()

    try:
        settings = get_settings()
        guard_request(settings, doctype='Sales Invoice', ptype='create')
    except GuardError as exc:
        return _handle_guard_error(endpoint, exc, request_id, raw)

    try:
        payload = normalise_invoice(raw, is_return=is_return)
        result, warnings = invoice_service.build_invoice(payload, settings)

        should_submit = payload['submit']
        if should_submit is None:
            should_submit = bool(cint(settings.auto_submit_invoices))

        submitted_now = False
        queued = False

        if should_submit and result.docstatus == 0 and result.action != 'duplicate':
            if settings.submit_mode == 'Queued':
                invoice_service.enqueue_submission(result.name)
                queued = True
            else:
                invoice_service.submit_invoice(result.name)
                submitted_now = True

        wait_info = None
        if submitted_now and cint(settings.wait_for_zatca_seconds):
            wait_info = zatca.wait_for_clearance(
                result.name, 'Sales Invoice', settings.wait_for_zatca_seconds
            )

        summary = invoice_service.invoice_summary(result.name)
        zatca_block = zatca.get_zatca_details(
            result.name, 'Sales Invoice', settings=settings, company=summary['company']
        )

        if queued:
            zatca_block.setdefault('reason', 'Submission is queued; poll get_status for the QR.')

        data = {
            'action': result.action,
            'duplicate': result.action == 'duplicate',
            'submitted': summary['docstatus'] == 1,
            'submission_queued': queued,
            'invoice': summary,
            'zatca': zatca_block,
        }
        if wait_info:
            data['clearance_wait'] = wait_info

        response = success_response(data, request_id, warnings)
        _log_request(
            endpoint,
            'Success' if not warnings else 'Partial',
            request_id,
            payload=raw,
            response=response,
            external_id=payload.get('external_id'),
            reference_name=result.name,
            http_status=200,
            duration_ms=int((time.monotonic() - started) * 1000),
            zatca_block=zatca_block,
        )
        return response

    except PayloadError as exc:
        frappe.db.rollback()
        response = error_response(exc.code, exc.message, request_id, exc.details)
        _log_request(
            endpoint,
            'Failed',
            request_id,
            payload=raw,
            response=response,
            error=exc.message,
            external_id=raw.get('external_id'),
            http_status=frappe.local.response.get('http_status_code'),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return response

    except frappe.ValidationError as exc:
        # ERPNext / ksa_compliance validation. The message is meaningful to the
        # integrator, so it is passed through rather than masked as a 500.
        frappe.db.rollback()
        message = _clean_message(exc)
        response = error_response(ERR_VALIDATION, message, request_id, {'source': 'erpnext_validation'})
        _log_request(
            endpoint,
            'Failed',
            request_id,
            payload=raw,
            response=response,
            error=frappe.get_traceback(),
            external_id=raw.get('external_id'),
            http_status=frappe.local.response.get('http_status_code'),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return response

    except Exception:
        frappe.db.rollback()
        traceback = frappe.get_traceback()
        frappe.log_error(title=f'ZATCA API {endpoint} failed'[:140], message=traceback)
        response = error_response(
            ERR_INTERNAL,
            _('Unexpected server error. Reference this request id when reporting it.'),
            request_id,
            {'request_id': request_id},
        )
        _log_request(
            endpoint,
            'Failed',
            request_id,
            payload=raw,
            response=response,
            error=traceback,
            external_id=raw.get('external_id'),
            http_status=500,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return response


def _clean_message(exc) -> str:
    """Flatten a frappe validation error into a single readable line.

    ``frappe.throw(msg)`` passes the message to the exception, so ``str(exc)`` is
    the most reliable source. ``message_log`` is only consulted when the exception
    itself carries nothing, and only its last entry - earlier entries belong to
    unrelated msgprints from the same request.
    """
    text = cstr(exc).strip()
    if not text and frappe.message_log:
        last = frappe.message_log[-1]
        text = cstr(last.get('message') if isinstance(last, dict) else last)

    text = strip_html(text).strip()

    # Validation code that joins a message log can repeat the same line. Collapse
    # duplicates while preserving order so the response reads as one clear error.
    seen = set()
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if line and line not in seen:
            seen.add(line)
            lines.append(line)

    return ' '.join(lines)[:2000] or _('Validation failed.')


@frappe.whitelist(methods=['POST'])
def submit_invoice(invoice: str | None = None, external_id: str | None = None):
    """Submit an existing draft invoice and return its ZATCA QR.

    For the ``Submit Mode = Queued`` flow, or to submit a draft created some other
    way.
    """
    request_id = new_request_id()
    endpoint = 'submit_invoice'
    _reset_request_state()

    try:
        settings = get_settings()
        guard_request(settings, doctype='Sales Invoice', ptype='submit')
    except GuardError as exc:
        return _handle_guard_error(endpoint, exc, request_id)

    name = _resolve_invoice(invoice, external_id)
    if not name:
        return error_response(
            ERR_NOT_FOUND,
            _('Invoice not found.'),
            request_id,
            {'invoice': invoice, 'external_id': external_id},
        )

    try:
        require_permission('Sales Invoice', 'submit', doc=name)
        invoice_service.submit_invoice(name)
    except GuardError as exc:
        return _handle_guard_error(endpoint, exc, request_id)
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        return error_response(ERR_VALIDATION, _clean_message(exc), request_id)

    summary = invoice_service.invoice_summary(name)
    zatca_block = zatca.get_zatca_details(
        name, 'Sales Invoice', settings=settings, company=summary['company']
    )

    response = success_response(
        {
            'action': 'submitted',
            'submitted': summary['docstatus'] == 1,
            'invoice': summary,
            'zatca': zatca_block,
        },
        request_id,
    )
    _log_request(
        endpoint,
        'Success',
        request_id,
        response=response,
        external_id=summary['external_id'],
        reference_name=name,
        http_status=200,
        zatca_block=zatca_block,
    )
    return response


@frappe.whitelist(methods=['POST'])
def resubmit_to_zatca(invoice: str | None = None, external_id: str | None = None):
    """Retry a ZATCA filing that came back Rejected or Resend.

    Delegates to `ksa_compliance`'s own ``fix_rejection``, which creates a fresh
    Sales Invoice Additional Fields document. Re-signing outside that routine would
    break the previous-invoice-hash chain for every subsequent invoice.
    """
    request_id = new_request_id()
    endpoint = 'resubmit_to_zatca'
    _reset_request_state()

    try:
        settings = get_settings()
        # ksa_compliance.fix_rejection itself only requires *read* on SIAF; the
        # meaningful gate is the right to submit the underlying invoice.
        guard_request(settings, doctype='Sales Invoice', ptype='submit')
        require_permission('Sales Invoice Additional Fields', 'read')
    except GuardError as exc:
        return _handle_guard_error(endpoint, exc, request_id)

    name = _resolve_invoice(invoice, external_id)
    if not name:
        return error_response(
            ERR_NOT_FOUND,
            _('Invoice not found.'),
            request_id,
            {'invoice': invoice, 'external_id': external_id},
        )

    if not zatca.is_ksa_compliance_installed():
        return error_response(
            ERR_ZATCA_UNAVAILABLE,
            _('The ksa_compliance app is not installed on this site.'),
            request_id,
        )

    try:
        outcome = zatca.resubmit(name, 'Sales Invoice')
    except frappe.DoesNotExistError as exc:
        return error_response(ERR_NOT_FOUND, cstr(exc), request_id)
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        return error_response(ERR_VALIDATION, _clean_message(exc), request_id)

    zatca_block = zatca.get_zatca_details(name, 'Sales Invoice', settings=settings)
    response = success_response({'invoice': name, 'resubmission': outcome, 'zatca': zatca_block}, request_id)
    _log_request(
        endpoint,
        'Success',
        request_id,
        response=response,
        reference_name=name,
        http_status=200,
        zatca_block=zatca_block,
    )
    return response


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------


@frappe.whitelist(methods=['GET'])
def get_invoice(invoice: str | None = None, external_id: str | None = None, include_xml: int = 0):
    """Fetch one invoice with its ZATCA block. Look it up by name or by external id."""
    request_id = new_request_id()

    try:
        settings = get_settings()
        guard_request(settings, doctype='Sales Invoice', ptype='read')
    except GuardError as exc:
        return _handle_guard_error('get_invoice', exc, request_id)

    name = _resolve_invoice(invoice, external_id)
    if not name:
        return error_response(
            ERR_NOT_FOUND,
            _('Invoice not found.'),
            request_id,
            {'invoice': invoice, 'external_id': external_id},
        )

    try:
        require_permission('Sales Invoice', 'read', doc=name)
    except GuardError as exc:
        return _handle_guard_error('get_invoice', exc, request_id)

    summary = invoice_service.invoice_summary(name)
    return success_response(
        {
            'invoice': summary,
            # include_xml is passed as a flag rather than by mutating the cached
            # Single document, which is shared for the life of the process.
            'zatca': zatca.get_zatca_details(
                name,
                'Sales Invoice',
                settings=settings,
                company=summary['company'],
                include_xml=bool(cint(include_xml)) or None,
            ),
        },
        request_id,
    )


@frappe.whitelist(methods=['GET'])
def get_status(invoice: str | None = None, external_id: str | None = None):
    """Cheap poll for ZATCA clearance state.

    The QR, UUID and invoice hash are available the moment the invoice is
    submitted; only ``integration_status`` moves asynchronously. Poll this rather
    than holding an HTTP connection open.
    """
    request_id = new_request_id()

    try:
        settings = get_settings()
        guard_request(settings, doctype='Sales Invoice', ptype='read')
    except GuardError as exc:
        return _handle_guard_error('get_status', exc, request_id)

    name = _resolve_invoice(invoice, external_id)
    if not name:
        return error_response(
            ERR_NOT_FOUND,
            _('Invoice not found.'),
            request_id,
            {'invoice': invoice, 'external_id': external_id},
        )

    try:
        require_permission('Sales Invoice', 'read', doc=name)
    except GuardError as exc:
        return _handle_guard_error('get_status', exc, request_id)

    row = frappe.db.get_value('Sales Invoice', name, ['docstatus', 'status', 'company'], as_dict=True)
    zatca_block = zatca.get_zatca_details(name, 'Sales Invoice', settings=settings, company=row['company'])

    return success_response(
        {
            'invoice': name,
            'docstatus': cint(row['docstatus']),
            'status': row['status'],
            'zatca': {
                'available': zatca_block.get('available'),
                'phase': zatca_block.get('phase'),
                'uuid': zatca_block.get('uuid'),
                'integration_status': zatca_block.get('integration_status'),
                'is_cleared': zatca_block.get('is_cleared'),
                'is_pending': zatca_block.get('is_pending'),
                'reason': zatca_block.get('reason'),
            },
        },
        request_id,
    )


@frappe.whitelist(methods=['GET'])
def list_invoices(
    company: str | None = None,
    customer: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    integration_status: str | None = None,
    docstatus: int = 1,
    limit: int = DEFAULT_PAGE_LIMIT,
    start: int = 0,
    include_qr: int = 0,
):
    """Paginated invoice list, optionally with each invoice's QR.

    ``limit`` is capped at 200. ``include_qr`` renders one QR per row, which is
    expensive - it is off by default and further limited to 50 rows per call.
    """
    request_id = new_request_id()

    try:
        settings = get_settings()
        guard_request(settings, doctype='Sales Invoice', ptype='read')
    except GuardError as exc:
        return _handle_guard_error('list_invoices', exc, request_id)

    limit = min(max(cint(limit) or DEFAULT_PAGE_LIMIT, 1), MAX_PAGE_LIMIT)
    start = max(cint(start), 0)
    include_qr = cint(include_qr)

    if include_qr and limit > 50:
        limit = 50

    filters = {'docstatus': cint(docstatus)}
    if company:
        filters['company'] = company
    if customer:
        filters['customer'] = customer
    if from_date and to_date:
        filters['posting_date'] = ['between', [from_date, to_date]]
    elif from_date:
        filters['posting_date'] = ['>=', from_date]
    elif to_date:
        filters['posting_date'] = ['<=', to_date]

    if integration_status:
        # Filter before paginating, not after, so count and has_more stay truthful.
        matching = _invoices_with_integration_status(integration_status)
        if not matching:
            return success_response(
                {'count': 0, 'total': 0, 'start': start, 'limit': limit, 'has_more': False, 'invoices': []},
                request_id,
            )
        filters['name'] = ['in', matching]

    # get_list, not get_all: this endpoint must respect the caller's permissions,
    # including User Permissions restricting them to one company.
    rows = frappe.get_list(
        'Sales Invoice',
        filters=filters,
        fields=[
            'name',
            'customer',
            'customer_name',
            'company',
            'posting_date',
            'currency',
            'net_total',
            'total_taxes_and_charges',
            'grand_total',
            'outstanding_amount',
            'status',
            'is_return',
            invoice_service.EXTERNAL_ID_FIELD,
        ],
        order_by='posting_date desc, creation desc',
        limit_page_length=limit,
        limit_start=start,
    )

    # Aggregate count, so the total does not require materialising every row.
    count_row = frappe.get_list(
        'Sales Invoice', filters=filters, fields=['count(name) as total'], limit_page_length=0
    )
    total_count = cint(count_row[0]['total']) if count_row else 0

    if include_qr:
        for row in rows:
            row['zatca'] = zatca.get_zatca_details(
                row['name'], 'Sales Invoice', settings=settings, company=row['company']
            )
    else:
        statuses = _integration_statuses([row['name'] for row in rows])
        for row in rows:
            row['integration_status'] = statuses.get(row['name'])

    return success_response(
        {
            'count': len(rows),
            'total': total_count,
            'start': start,
            'limit': limit,
            'has_more': start + len(rows) < total_count,
            'invoices': rows,
        },
        request_id,
    )


def _integration_statuses(invoice_names: list) -> dict:
    """Batch-fetch integration statuses. One query, not one per invoice."""
    if not invoice_names or not zatca.is_ksa_compliance_installed():
        return {}

    rows = frappe.get_all(
        zatca.SIAF_DOCTYPE,
        filters={'sales_invoice': ['in', invoice_names], 'invoice_doctype': 'Sales Invoice', 'is_latest': 1},
        fields=['sales_invoice', 'integration_status'],
    )
    return {row['sales_invoice']: row['integration_status'] for row in rows}


def _invoices_with_integration_status(integration_status: str) -> list:
    """Invoice names whose latest ZATCA record carries this integration status.

    Sales Invoice Additional Fields has no ``company`` column (verified against
    ksa_compliance 0.58.0), so company scoping is left to the Sales Invoice query
    that consumes this list.
    """
    if not zatca.is_ksa_compliance_installed():
        return []

    return frappe.get_all(
        zatca.SIAF_DOCTYPE,
        filters={
            'integration_status': integration_status,
            'invoice_doctype': 'Sales Invoice',
            'is_latest': 1,
        },
        pluck='sales_invoice',
        limit_page_length=0,
    )


@frappe.whitelist(methods=['GET'])
def ping():
    """Health and capability probe.

    Reports what this app can actually do on this site - whether
    `ksa_compliance` is installed, which phase each company resolves to, and
    whether the invoice custom fields have been migrated. Run this first when an
    integration misbehaves.
    """
    request_id = new_request_id()

    try:
        settings = get_settings()
        guard_request(settings)
    except GuardError as exc:
        return _handle_guard_error('ping', exc, request_id)

    from zatca_api import __version__

    meta = frappe.get_meta('Sales Invoice')
    return success_response(
        {
            'app': 'zatca_api',
            'version': __version__,
            'site': frappe.local.site,
            'user': frappe.session.user,
            'enabled': bool(cint(settings.enabled)),
            'auto_submit_invoices': bool(cint(settings.auto_submit_invoices)),
            'submit_mode': settings.submit_mode,
            'zatca_phase_setting': settings.zatca_phase,
            'custom_fields_installed': bool(meta.get_field(invoice_service.EXTERNAL_ID_FIELD)),
            'readiness': zatca.readiness_report(settings.default_company),
        },
        request_id,
    )


@frappe.whitelist(methods=['POST'])
def pull_now(source: str | None = None):
    """Trigger a configured pull source immediately instead of waiting for the cron."""
    request_id = new_request_id()

    try:
        settings = get_settings()
        guard_request(settings, doctype='Sales Invoice', ptype='create')
        require_permission('ZATCA API Settings', 'read')
    except GuardError as exc:
        return _handle_guard_error('pull_now', exc, request_id)

    try:
        if source:
            result = puller.import_source(source)
        else:
            result = puller.pull_all_sources()
    except frappe.ValidationError as exc:
        return error_response(ERR_VALIDATION, _clean_message(exc), request_id)

    return success_response(result, request_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_invoice(invoice: str | None, external_id: str | None) -> str | None:
    """Resolve an invoice by ERPNext name or by the external id it was created with."""
    invoice = cstr(invoice or '').strip()
    external_id = cstr(external_id or '').strip()

    if invoice and frappe.db.exists('Sales Invoice', invoice):
        return invoice

    if external_id:
        row = invoice_service.find_by_external_id(external_id)
        if row:
            return row['name']

    return None
