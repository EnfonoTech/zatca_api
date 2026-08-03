# zatca_api/services/dryrun.py
# Copyright (c) 2026, Enfono Technologies and contributors
"""Validate a payload without keeping anything.

This exists for the onboarding problem. In pull mode the external system never
calls us -- we poll it -- so a malformed feed produces failures in *our* Error Log
where the vendor cannot see them. They would be integrating blind.

The dry run gives them a self-test loop: POST the JSON they intend to publish, get
back every missing or invalid field, the real ERPNext totals, what master data
would be created, and which ZATCA track the invoice would land on. Then iterate
until clean, before a single real invoice exists.

**How "without keeping anything" works.** The payload is run through the exact same
code path as a real request -- inside a database savepoint that is always rolled
back. That matters: a validator that reimplemented the rules would drift from the
real one and pass payloads that later fail. Here ERPNext's own
``calculate_taxes_and_totals`` produces the totals, and ERPNext's and
``ksa_compliance``'s own ``validate`` hooks produce the errors.

The invoice is inserted as a **draft only** -- never submitted -- so no GL entry is
written, no ZATCA document is created, and nothing is filed with ZATCA.
"""

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt

from zatca_api.services import invoice as invoice_service
from zatca_api.services import zatca
from zatca_api.services.envelope import ERR_VALIDATION, item_error
from zatca_api.services.payload import PayloadError, normalise_invoice

SAVEPOINT = 'zatca_api_dry_run'


def _existing_masters(payload: dict) -> dict:
    """Snapshot which referenced master records already exist.

    Taken before the savepoint work so the report can distinguish 'you referenced
    an existing item' from 'you are about to create a new one' -- 40 unexpected new
    items usually means the item codes are wrong, not that 40 items are missing.
    """
    customer = cstr(payload.get('customer')).strip()
    item_codes = [cstr(row.get('item_code')).strip() for row in payload.get('items') or []]
    uoms = {cstr(row.get('uom')).strip() for row in payload.get('items') or [] if row.get('uom')}
    project = cstr(payload.get('project')).strip()

    return {
        'customer_exists': bool(
            customer
            and (
                frappe.db.exists('Customer', customer)
                or frappe.db.get_value('Customer', {'customer_name': customer}, 'name')
            )
        ),
        'existing_items': [code for code in item_codes if code and frappe.db.exists('Item', code)],
        'new_items': sorted({code for code in item_codes if code and not frappe.db.exists('Item', code)}),
        'new_uoms': sorted({uom for uom in uoms if not frappe.db.exists('UOM', uom)}),
        'project_exists': bool(project and frappe.db.exists('Project', project)),
    }


def dry_run(raw: dict, settings, is_return: bool = False) -> dict:
    """Validate ``raw`` and report. Returns a plain dict; never raises PayloadError.

    Guaranteed to leave the database exactly as it found it.
    """
    report = {
        'valid': False,
        'document_type': 'Credit Note' if is_return else 'Sales Invoice',
        'errors': [],
        'warnings': [],
        'would_create': {},
        'resolved': {},
        'totals': {},
        'zatca': {},
        'dry_run': True,
    }

    # Phase 1: normalise and check the shape. No database writes at all.
    try:
        payload = normalise_invoice(raw, is_return=is_return)
    except PayloadError as exc:
        report['errors'].append(item_error(exc.code, exc.message, exc.details))
        return report

    report['resolved'] = {
        'external_id': payload.get('external_id'),
        'customer': payload.get('customer'),
        'company': payload.get('company') or settings.default_company,
        'posting_date': cstr(payload.get('posting_date') or ''),
        'item_count': len(payload.get('items') or []),
        'is_return': bool(payload.get('is_return')),
    }
    report['would_create'] = _existing_masters(payload)

    existing = invoice_service.find_by_external_id(payload.get('external_id'))
    if existing:
        report['warnings'].append(
            f'external_id {payload["external_id"]!r} already maps to invoice {existing["name"]} '
            f'(docstatus {cint(existing["docstatus"])}). A real request would return it as a '
            f'duplicate rather than creating a new invoice.'
        )
        report['resolved']['existing_invoice'] = existing['name']

    # Phase 2: build for real inside a savepoint, then throw the work away. This is
    # what makes the totals and the error list trustworthy -- they come from
    # ERPNext and ksa_compliance, not from a parallel reimplementation.
    frappe.db.savepoint(SAVEPOINT)
    try:
        result, warnings = invoice_service.build_invoice(payload, settings)
        report['warnings'].extend(warnings)

        doc = frappe.get_doc('Sales Invoice', result.name)
        report['totals'] = {
            'currency': doc.currency,
            'net_total': flt(doc.net_total),
            'total_taxes_and_charges': flt(doc.total_taxes_and_charges),
            'grand_total': flt(doc.grand_total),
            'rounded_total': flt(doc.rounded_total),
            'tax_rows': [
                {
                    'account_head': row.account_head,
                    'rate': flt(row.rate),
                    'tax_amount': flt(row.tax_amount),
                }
                for row in doc.taxes
            ],
        }
        report['zatca'] = zatca.classify_invoice_type(doc.company, doc.customer)
        report['valid'] = True

    except PayloadError as exc:
        report['errors'].append(item_error(exc.code, exc.message, exc.details))

    except frappe.ValidationError as exc:
        # ERPNext / ksa_compliance validation. Exactly what a real request would hit.
        report['errors'].append(item_error(ERR_VALIDATION, _clean(exc), {'source': 'erpnext_validation'}))

    except Exception:
        frappe.log_error(title='ZATCA API dry run failed', message=frappe.get_traceback())
        report['errors'].append(
            item_error('internal_error', _('Unexpected error while validating. See the Error Log.'))
        )

    finally:
        # Unconditional. Nothing this function did survives, on any path.
        frappe.db.rollback(save_point=SAVEPOINT)

    if not report['zatca']:
        company = report['resolved'].get('company')
        if company:
            report['zatca'] = zatca.classify_invoice_type(company, payload.get('customer'))

    report['zatca_readiness'] = _zatca_readiness(report, payload)
    return report


def _zatca_readiness(report: dict, payload: dict) -> dict:
    """Turn the buyer-address and identifier warnings into a ZATCA-shaped verdict.

    A payload can be perfectly valid for ERPNext and still be rejected by ZATCA for
    a *standard* invoice. Splitting the two makes the vendor's next action obvious.
    """
    invoice_type = (report.get('zatca') or {}).get('invoice_type')
    blocking = []
    advisory = []

    address_warnings = [w for w in report['warnings'] if 'Buyer address' in w or 'Buyer postal' in w]
    if invoice_type == 'Standard':
        blocking.extend(address_warnings)
        if not payload.get('tax_id') and not payload.get('buyer_id_value'):
            blocking.append(
                'A standard invoice needs a buyer identifier: send tax_id, or '
                'buyer_id_type + buyer_id_value.'
            )
    else:
        advisory.extend(address_warnings)

    return {
        'invoice_type': invoice_type,
        'would_be_rejected_by_zatca': bool(blocking),
        'blocking': blocking,
        'advisory': advisory,
    }


def _clean(exc) -> str:
    from frappe.utils import strip_html

    text = strip_html(cstr(exc)).strip()
    seen = set()
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if line and line not in seen:
            seen.add(line)
            lines.append(line)
    return ' '.join(lines)[:2000] or _('Validation failed.')
