# zatca_api/services/zatca.py
# Copyright (c) 2026, Enfono Technologies and contributors
"""Adapter over `ksa_compliance` that assembles the ZATCA block of an API response.

**This app performs no ZATCA cryptography.** No TLV encoding, no XML generation,
no SHA-256 invoice hashing, no XAdES signing, no certificate handling. All of that
is delegated to `ksa_compliance`, which is the audited implementation of a spec
that is unforgiving about byte-level detail. This module only reads what that app
produced and shapes it for the HTTP response.

How the two phases differ, and why the response shape differs with them:

**Phase 1** (`ZATCA Phase 1 Business Settings`) is a local QR only. There is no
submission to ZATCA and no UUID or hash. ``ksa_compliance.jinja.
get_zatca_phase_1_qr_for_invoice()`` returns a base64 **PNG image**, so
``qr_content`` is unavailable for Phase 1.

**Phase 2** (`ZATCA Business Settings`) signs the invoice and files it with
ZATCA. On ``Sales Invoice.on_submit``, `ksa_compliance` creates a
`Sales Invoice Additional Fields` (SIAF) document whose ``before_insert`` already
performs the signing. That is the important timing fact for this API: **the UUID,
invoice hash, PIH and QR exist the moment the invoice is submitted** and can be
returned synchronously. Only ``integration_status`` - the outcome of the network
call to ZATCA for clearance or reporting - is asynchronous, because it runs in a
background job. Here ``qr_code`` on the SIAF is the base64 **TLV string**, which
this module can additionally render to a PNG.

Verified against ksa_compliance 0.58.0 / frappe 15.68.1.
"""

import base64
import time
from io import BytesIO

import frappe
from frappe import _
from frappe.utils import cint

SIAF_DOCTYPE = 'Sales Invoice Additional Fields'

# ZATCA has finished with the invoice once it leaves this status. `ksa_compliance`
# sets 'Ready For Batch' at SIAF before_insert and overwrites it with the mapped
# HTTP outcome after the ZATCA call.
PENDING_STATUSES = ('', None, 'Ready For Batch')

TERMINAL_OK_STATUSES = ('Accepted', 'Accepted with warnings', 'Clearance switched off')

MAX_WAIT_SECONDS = 30


def is_ksa_compliance_installed() -> bool:
    """True when the `ksa_compliance` app is installed on this site.

    Checked at call time rather than by importing at module scope. A top-level
    ``from ksa_compliance...`` import makes the whole module - and therefore every
    endpoint and every scheduled job in this app - fail to import on a site
    without that app installed.
    """
    try:
        return 'ksa_compliance' in frappe.get_installed_apps()
    except Exception:
        return False


def get_phase_for_company(company: str) -> str:
    """Resolve which ZATCA phase applies to a company: 'Phase 2', 'Phase 1' or 'None'."""
    if not company or not is_ksa_compliance_installed():
        return 'None'

    if frappe.db.exists('ZATCA Business Settings', {'company': company, 'status': 'Active'}):
        return 'Phase 2'

    phase_1 = frappe.db.get_value('ZATCA Phase 1 Business Settings', {'company': company}, ['name', 'status'])
    if phase_1 and phase_1[1] != 'Disabled':
        return 'Phase 1'

    return 'None'


def _render_qr_png(qr_content: str) -> dict:
    """Render a QR payload string to PNG. Returns {} when rendering is unavailable."""
    if not qr_content:
        return {}
    try:
        import pyqrcode
    except ImportError:
        # pyqrcode ships with erpnext's KSA region code; if it is genuinely absent
        # the caller still has qr_content and can render it itself.
        return {}

    try:
        qr = pyqrcode.create(qr_content)
        with BytesIO() as buffer:
            qr.png(buffer, scale=4)
            buffer.seek(0)
            encoded = base64.b64encode(buffer.getvalue()).decode('utf-8')
    except Exception:
        frappe.log_error(title='ZATCA API: QR render failed', message=frappe.get_traceback())
        return {}

    return {
        'qr_png_base64': encoded,
        'qr_png_data_uri': f'data:image/png;base64,{encoded}',
    }


def _phase_2_details(invoice_name: str, doctype: str, flags: dict) -> dict | None:
    """Read the latest SIAF row for this invoice and shape it for the response."""
    row = frappe.db.get_value(
        SIAF_DOCTYPE,
        {'sales_invoice': invoice_name, 'invoice_doctype': doctype, 'is_latest': 1},
        [
            'name',
            'uuid',
            'invoice_counter',
            'invoice_hash',
            'previous_invoice_hash',
            'qr_code',
            'integration_status',
            'invoice_type_code',
            'invoice_type_transaction',
        ],
        as_dict=True,
    )

    if not row:
        # Fall back to any SIAF row: on very old records `is_latest` may be unset.
        row = frappe.db.get_value(
            SIAF_DOCTYPE,
            {'sales_invoice': invoice_name, 'invoice_doctype': doctype},
            [
                'name',
                'uuid',
                'invoice_counter',
                'invoice_hash',
                'previous_invoice_hash',
                'qr_code',
                'integration_status',
                'invoice_type_code',
                'invoice_type_transaction',
            ],
            as_dict=True,
            order_by='creation desc',
        )

    if not row:
        return None

    status = row.get('integration_status') or ''
    details = {
        'available': True,
        'phase': 'Phase 2',
        'additional_fields_doc': row['name'],
        'uuid': row.get('uuid'),
        'invoice_counter': cint(row.get('invoice_counter')) or None,
        'invoice_hash': row.get('invoice_hash'),
        'previous_invoice_hash': row.get('previous_invoice_hash'),
        'qr_content': row.get('qr_code'),
        'qr_format': 'base64-tlv',
        'integration_status': status,
        # 'standard' invoices are cleared with ZATCA; 'simplified' are reported.
        'invoice_type_transaction': row.get('invoice_type_transaction'),
        'invoice_type_code': row.get('invoice_type_code'),
        'is_cleared': status in TERMINAL_OK_STATUSES,
        'is_pending': status in PENDING_STATUSES,
    }

    if flags['include_png']:
        details.update(_render_qr_png(row.get('qr_code')))

    if flags['include_xml']:
        details['signed_xml'] = frappe.db.get_value(SIAF_DOCTYPE, row['name'], 'invoice_xml')

    return details


def _phase_1_details(invoice_name: str, flags: dict) -> dict | None:
    """Phase 1 QR via the public ksa_compliance jinja helper."""
    try:
        from ksa_compliance.jinja import get_zatca_phase_1_qr_for_invoice
    except ImportError:
        return None

    try:
        png_base64 = get_zatca_phase_1_qr_for_invoice(invoice_name)
    except Exception:
        frappe.log_error(
            title=f'ZATCA API: Phase 1 QR failed for {invoice_name}'[:140],
            message=frappe.get_traceback(),
        )
        return None

    if not png_base64:
        return None

    details = {
        'available': True,
        'phase': 'Phase 1',
        'uuid': None,
        'invoice_hash': None,
        # The Phase 1 helper returns a rendered image, not the underlying TLV
        # string, so there is no qr_content to hand back.
        'qr_content': None,
        'qr_format': 'png-only',
        'integration_status': 'Not Applicable',
        'is_cleared': True,
        'is_pending': False,
    }

    if flags['include_png']:
        details['qr_png_base64'] = png_base64
        details['qr_png_data_uri'] = f'data:image/png;base64,{png_base64}'

    return details


def get_zatca_details(
    invoice_name: str,
    doctype: str = 'Sales Invoice',
    settings=None,
    company: str | None = None,
    include_png: bool | None = None,
    include_xml: bool | None = None,
) -> dict:
    """Return the ZATCA block for an invoice.

    Always returns a dict. ``available`` is False, with a ``reason``, whenever no
    ZATCA data could be produced - an unsubmitted invoice, a company with no ZATCA
    settings, or `ksa_compliance` not installed. The endpoint still succeeds in
    that case: creating the invoice worked, and the caller can tell the difference
    from the flag rather than from a failed request.
    """
    if settings is None:
        from zatca_api.zatca_api.doctype.zatca_api_settings.zatca_api_settings import get_settings

        settings = get_settings()

    # Per-request overrides. Resolved into plain flags rather than by copying the
    # cached Single document, which must never be mutated.
    flags = {
        'include_png': cint(settings.include_qr_png) if include_png is None else bool(include_png),
        'include_xml': cint(settings.include_signed_xml) if include_xml is None else bool(include_xml),
    }

    phase_setting = settings.zatca_phase or 'Auto'
    if phase_setting == 'Disabled':
        return {'available': False, 'phase': 'Disabled', 'reason': 'ZATCA reporting disabled in settings.'}

    if not is_ksa_compliance_installed():
        return {
            'available': False,
            'phase': 'None',
            'reason': 'The ksa_compliance app is not installed on this site, so no ZATCA QR can be produced.',
        }

    docstatus = frappe.db.get_value(doctype, invoice_name, 'docstatus')
    if docstatus is None:
        return {'available': False, 'phase': 'None', 'reason': f'{doctype} {invoice_name} not found.'}
    if cint(docstatus) == 0:
        return {
            'available': False,
            'phase': 'None',
            'reason': 'Invoice is in Draft. A ZATCA QR only exists for a submitted invoice.',
        }

    if phase_setting in ('Auto', 'Phase 2 Only'):
        details = _phase_2_details(invoice_name, doctype, flags)
        if details:
            return details
        if phase_setting == 'Phase 2 Only':
            return {
                'available': False,
                'phase': 'Phase 2',
                'reason': (
                    'No Sales Invoice Additional Fields document exists for this invoice. Check that '
                    'ZATCA Business Settings for the company are Active and that the invoice date is on '
                    'or after the configured start date.'
                ),
            }

    if phase_setting in ('Auto', 'Phase 1 Only'):
        details = _phase_1_details(invoice_name, flags)
        if details:
            return details

    return {
        'available': False,
        'phase': get_phase_for_company(company or frappe.db.get_value(doctype, invoice_name, 'company')),
        'reason': (
            'No ZATCA settings resolved for this company. Configure ZATCA Business Settings (Phase 2) '
            'or ZATCA Phase 1 Business Settings.'
        ),
    }


def wait_for_clearance(invoice_name: str, doctype: str, seconds: int) -> dict | None:
    """Block until ZATCA clearance leaves the pending state, or the budget expires.

    Opt-in and capped, because holding an HTTP worker on a third-party network call
    is how a queue backs up. The default is 0; the QR is already available without
    waiting, so callers should poll :func:`zatca_api.api.v1.get_status` instead.

    A commit is required before the loop: `ksa_compliance` enqueues its submission
    with ``enqueue_after_commit=True``, so the job does not exist until this
    transaction commits. Each iteration rolls back to start a fresh read snapshot -
    without it, this transaction's isolation level would keep returning the value
    read at the start.
    """
    seconds = min(max(cint(seconds), 0), MAX_WAIT_SECONDS)
    if not seconds:
        return None

    frappe.db.commit()

    deadline = time.monotonic() + seconds
    poll_interval = 0.5
    status = None

    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        frappe.db.rollback()
        status = frappe.db.get_value(
            SIAF_DOCTYPE,
            {'sales_invoice': invoice_name, 'invoice_doctype': doctype, 'is_latest': 1},
            'integration_status',
        )
        if status not in PENDING_STATUSES:
            return {'waited': True, 'integration_status': status}
        poll_interval = min(poll_interval * 1.5, 3.0)

    return {'waited': True, 'timed_out': True, 'integration_status': status}


def readiness_report(company: str | None = None) -> dict:
    """Diagnostics for the ping endpoint: what is configured, what is missing."""
    report = {
        'ksa_compliance_installed': is_ksa_compliance_installed(),
        'ksa_compliance_version': None,
        'companies': [],
    }

    if not report['ksa_compliance_installed']:
        return report

    try:
        import ksa_compliance

        report['ksa_compliance_version'] = getattr(ksa_compliance, '__version__', None)
    except ImportError:
        pass

    companies = [company] if company else frappe.get_all('Company', pluck='name')
    for name in companies:
        phase = get_phase_for_company(name)
        entry = {'company': name, 'phase': phase}
        if phase == 'Phase 2':
            entry['business_settings'] = frappe.db.get_value(
                'ZATCA Business Settings', {'company': name, 'status': 'Active'}, 'name'
            )
            entry['sync_with_zatca'] = frappe.db.get_value(
                'ZATCA Business Settings', entry['business_settings'], 'sync_with_zatca'
            )
        report['companies'].append(entry)

    return report


def resubmit(invoice_name: str, doctype: str = 'Sales Invoice') -> dict:
    """Queue a fresh ZATCA submission for an invoice whose filing failed.

    Delegates to `ksa_compliance`'s own ``fix_rejection`` so the invoice counter and
    previous-invoice-hash chain stay consistent. Re-signing an invoice outside that
    routine would corrupt the hash chain for every later invoice.
    """
    row = frappe.db.get_value(
        SIAF_DOCTYPE,
        {'sales_invoice': invoice_name, 'invoice_doctype': doctype, 'is_latest': 1},
        ['name', 'integration_status'],
        as_dict=True,
    )
    if not row:
        frappe.throw(
            _('No ZATCA record exists for {0}. Nothing to resubmit.').format(invoice_name),
            frappe.DoesNotExistError,
        )

    if row['integration_status'] in TERMINAL_OK_STATUSES:
        return {
            'queued': False,
            'integration_status': row['integration_status'],
            'message': _('Invoice is already accepted by ZATCA; resubmission skipped.'),
        }

    from ksa_compliance.ksa_compliance.doctype.sales_invoice_additional_fields.sales_invoice_additional_fields import (
        fix_rejection,
    )

    fix_rejection(id=row['name'])

    return {
        'queued': True,
        'previous_integration_status': row['integration_status'],
        'message': _('Resubmission queued with ksa_compliance.'),
    }
