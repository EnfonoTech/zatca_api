# zatca_api/services/invoice.py
# Copyright (c) 2026, Enfono Technologies and contributors
"""Build, persist and submit a Sales Invoice from a normalised payload.

Design rules, each one a defect this replaces:

**Idempotency is keyed on an external id, not on the document name.**
``frappe.model.naming.set_new_name`` executes ``doc.name = None`` for any DocType
whose ``autoname`` is a naming series, which Sales Invoice's is. Passing
``{"name": "INV-001"}`` to ``frappe.get_doc`` is therefore silently discarded and
the invoice is named ``ACC-SINV-YYYY-#####`` instead. Any later existence check
against the external number can never match, so a repeating import creates a fresh
duplicate invoice on every run. The external number is stored in an indexed custom
field and looked up ``FOR UPDATE``, which also serialises two concurrent requests
carrying the same id.

**A submitted invoice is never modified.** Re-posting a submitted invoice rewrites
GL entries behind the ledger's back, and once ZATCA has cleared it the document is
legally immutable. A repeat request for a submitted invoice returns the existing
document, flagged ``duplicate``.

**Taxes are computed by ERPNext, not flattened by hand.** Collapsing every Item
Tax Template into one ``On Net Total`` row - as the previous implementation did -
charges standard-rate VAT on zero-rated and exempt lines. Here the per-row
``item_tax_template`` is set and ERPNext's own
``calculate_taxes_and_totals`` fills ``item_wise_tax_detail``, which is exactly the
field `ksa_compliance` reads to build per-line VAT categories for the ZATCA XML.

**Money and dates go through frappe.utils.** ``flt``/``cint``/``getdate`` absorb
``None``, ``''`` and locale-formatted numbers; bare ``float()``/``int()`` raise.
"""

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, getdate, now, nowtime

from zatca_api.services import masters
from zatca_api.services.envelope import ERR_IMMUTABLE
from zatca_api.services.payload import PayloadError, apply_field_mappings, validate_invoice

EXTERNAL_ID_FIELD = 'zatca_api_external_id'
SOURCE_FIELD = 'zatca_api_source'
SYNCED_ON_FIELD = 'zatca_api_synced_on'


class InvoiceResult:
    """Outcome of one create/update call."""

    def __init__(self, name: str, action: str, docstatus: int, warnings: list | None = None):
        self.name = name
        self.action = action  # created | updated | duplicate | submitted
        self.docstatus = docstatus
        self.warnings = warnings or []


def find_by_external_id(external_id: str, lock: bool = False) -> dict | None:
    """Look up an invoice previously created for this external id.

    ``lock=True`` takes a ``FOR UPDATE`` lock. Because the column is indexed, that
    is a gap lock on the key, so a second concurrent request for the same external
    id blocks here instead of racing to insert a second invoice.
    """
    if not external_id:
        return None

    if not frappe.get_meta('Sales Invoice').get_field(EXTERNAL_ID_FIELD):
        # The custom field ships as a fixture; if migrate has not run yet, fall
        # back to no dedup rather than crashing.
        frappe.log_error(
            title='ZATCA API: external id field missing',
            message=f'Sales Invoice.{EXTERNAL_ID_FIELD} not found. Run bench migrate.',
        )
        return None

    return frappe.db.get_value(
        'Sales Invoice',
        {EXTERNAL_ID_FIELD: external_id},
        ['name', 'docstatus', 'is_return', 'grand_total'],
        as_dict=True,
        for_update=lock,
        order_by='creation desc',
    )


def _resolve_taxes(payload: dict, company: str, doc) -> list:
    """Return the rows for Sales Invoice.taxes, in precedence order.

    1. Explicit ``taxes`` in the payload - the caller knows exactly what it wants.
    2. A named template in ``tax_template``.
    3. The company's default Sales Taxes and Charges Template.

    In all three cases the per-item ``item_tax_template`` set in
    :func:`_append_items` still governs which lines are actually taxed and at what
    rate; these rows only declare the account and charge basis.
    """
    if payload.get('taxes'):
        rows = []
        for tax in payload['taxes']:
            account = tax.get('account_head')
            if not account:
                raise PayloadError(_('Tax row is missing account_head.'), {'field': 'taxes'})

            account_company = frappe.db.get_value('Account', account, 'company')
            if not account_company:
                raise PayloadError(_('Tax account {0} does not exist.').format(account), {'field': 'taxes'})
            if account_company != company:
                raise PayloadError(
                    _('Tax account {0} belongs to company {1}, not {2}.').format(
                        account, account_company, company
                    ),
                    {'field': 'taxes'},
                )

            charge_type = tax.get('charge_type') or 'On Net Total'
            row = {
                'charge_type': charge_type,
                'account_head': account,
                'description': tax.get('description') or account,
                'rate': flt(tax.get('rate')),
                'included_in_print_rate': cint(tax.get('included_in_print_rate')),
            }
            # 'Actual' takes an amount and ignores the rate; every other charge
            # type takes a rate and computes the amount.
            if charge_type == 'Actual':
                row['tax_amount'] = flt(tax.get('tax_amount'))
            rows.append(row)
        return rows

    template = payload.get('tax_template')
    if template:
        template_name = frappe.db.get_value(
            'Sales Taxes and Charges Template', {'name': template, 'company': company}, 'name'
        ) or frappe.db.get_value(
            'Sales Taxes and Charges Template', {'title': template, 'company': company}, 'name'
        )
        if not template_name:
            raise PayloadError(
                _('Sales Taxes and Charges Template {0} not found for company {1}.').format(
                    template, company
                ),
                {'field': 'tax_template'},
            )
        doc.taxes_and_charges = template_name
        return _rows_from_template(template_name)

    default_template = frappe.db.get_value(
        'Sales Taxes and Charges Template', {'company': company, 'is_default': 1}, 'name'
    )
    if default_template:
        doc.taxes_and_charges = default_template
        return _rows_from_template(default_template)

    # No tax rows at all is legal in a zero-VAT jurisdiction. It is *not* legal
    # once ZATCA is switched on: ksa_compliance's Sales Invoice validate hook
    # rejects an invoice with an empty taxes table. Failing here with a specific,
    # actionable message beats letting that hook raise a generic one.
    from zatca_api.services.zatca import get_phase_for_company

    if get_phase_for_company(company) != 'None':
        raise PayloadError(
            _(
                'No tax rows could be resolved for company {0}, but ZATCA e-invoicing is enabled '
                'for it and requires at least one. Either send a "taxes" array or a "tax_template" '
                'in the payload, or set a default Sales Taxes and Charges Template for the company.'
            ).format(company),
            {'field': 'taxes', 'company': company},
        )

    return []


def _rows_from_template(template_name: str) -> list:
    template = frappe.get_cached_doc('Sales Taxes and Charges Template', template_name)
    return [
        {
            'charge_type': row.charge_type,
            'account_head': row.account_head,
            'description': row.description or row.account_head,
            'rate': flt(row.rate),
            'tax_amount': flt(row.tax_amount) if row.charge_type == 'Actual' else 0,
            'included_in_print_rate': cint(row.included_in_print_rate),
            'cost_center': row.cost_center,
        }
        for row in template.taxes
    ]


def _inherited_item_tax_templates(payload: dict) -> dict:
    """``item_code`` -> ``item_tax_template`` taken from the invoice being credited.

    A credit note whose lines omit ``item_tax_template`` does **not** inherit the original
    invoice's tax treatment. ERPNext re-resolves each line from the Item master, its Item
    Group chain, and failing both the header tax row -- so crediting a zero-rated or exempt
    sale reclaims standard VAT that was never charged.

    Measured on the test site: a zero-rated invoice of 1,000 charged 0 VAT; crediting it
    without the template produced VAT of -150, i.e. a 150 SAR VAT refund against a sale that
    carried none. Resending the template gave the correct 0.

    So the original's per-line templates are inherited whenever the caller did not name one.
    An explicit value in the payload always wins, because a partial return can legitimately
    differ from the original.
    """
    if not (payload.get('is_return') and payload.get('return_against')):
        return {}

    rows = frappe.get_all(
        'Sales Invoice Item',
        filters={'parent': payload['return_against']},
        fields=['item_code', 'item_tax_template'],
    )
    return {row['item_code']: row['item_tax_template'] for row in rows if row['item_tax_template']}


def _append_items(doc, payload: dict, company: str, settings) -> None:
    inherited = _inherited_item_tax_templates(payload)

    for item in payload['items']:
        masters.ensure_item(item, company, settings)

        row = {
            'item_code': item['item_code'],
            'qty': flt(item['qty']),
            'rate': flt(item['rate']),
        }
        if item.get('item_name'):
            row['item_name'] = item['item_name']
        if item.get('description'):
            row['description'] = item['description']

        uom = masters.ensure_uom(item.get('uom'), settings)
        if uom:
            row['uom'] = uom
            # conversion_factor must accompany a UOM override or ERPNext resolves
            # it against the item's UOM conversion table and may reject it.
            row['conversion_factor'] = _conversion_factor(item['item_code'], uom)

        for field in ('income_account', 'cost_center', 'warehouse'):
            if item.get(field):
                row[field] = item[field]

        if not item.get('item_tax_template') and inherited.get(item['item_code']):
            # Carried over from the invoice being credited. It is already a valid name on
            # that document, so if it no longer resolves the template was deleted -- leave
            # the line alone rather than failing a request that never named it.
            row['item_tax_template'] = inherited[item['item_code']]

        if item.get('item_tax_template'):
            template = masters.resolve_item_tax_template(item['item_tax_template'], company)
            if not template:
                raise PayloadError(
                    _('Item Tax Template {0} not found for company {1}.').format(
                        item['item_tax_template'], company
                    ),
                    {'field': 'item_tax_template', 'item_code': item['item_code']},
                )
            row['item_tax_template'] = template

        if 'discount_amount' in item:
            row['discount_amount'] = flt(item['discount_amount'])
        if 'discount_percentage' in item:
            row['discount_percentage'] = flt(item['discount_percentage'])

        child = doc.append('items', row)
        apply_field_mappings(child, 'Sales Invoice Item', item.get('raw') or {}, settings)


def _conversion_factor(item_code: str, uom: str) -> float:
    stock_uom = frappe.db.get_value('Item', item_code, 'stock_uom')
    if stock_uom == uom:
        return 1.0

    factor = frappe.db.get_value(
        'UOM Conversion Detail', {'parent': item_code, 'uom': uom}, 'conversion_factor'
    )
    return flt(factor) or 1.0


# ZATCA BR-KSA-17: a credit or debit note (invoice type code 383 / 381) must state the
# reason it was issued. ksa_compliance renders that reason from the Sales Invoice field
# `custom_return_reason` into the UBL `InstructionNote` element; when the field is empty
# the element is omitted entirely and ZATCA rejects the document with HTTP 400. Its own
# hardcoded 'Return of goods' fallback applies only to POS Invoice
# (ksa_compliance/output_models/e_invoice_output_model.py:96-104), so a Sales Invoice
# credit note has to supply the text itself or every clearance fails.
RETURN_REASON_FIELD = 'custom_return_reason'
DEFAULT_RETURN_REASON = 'Return of goods'
RETURN_REASON_MAX_LENGTH = 140


def _apply_return_reason(doc, payload: dict) -> None:
    if not (cint(doc.get('is_return')) or cint(doc.get('is_debit_note'))):
        return

    # Absent ksa_compliance nothing consumes the field, and setting an unknown
    # fieldname would be silently dropped anyway.
    if not frappe.get_meta(doc.doctype).get_field(RETURN_REASON_FIELD):
        return

    reason = cstr(
        payload.get('return_reason')
        or doc.get(RETURN_REASON_FIELD)
        or payload.get('remarks')
        or DEFAULT_RETURN_REASON
    ).strip()
    doc.set(RETURN_REASON_FIELD, reason[:RETURN_REASON_MAX_LENGTH] or DEFAULT_RETURN_REASON)


def _apply_header(doc, payload: dict, company: str, customer: str, settings) -> None:
    doc.customer = customer
    doc.company = company

    if payload.get('posting_date'):
        doc.set_posting_time = 1
        doc.posting_date = getdate(payload['posting_date'])
        doc.posting_time = payload.get('posting_time') or nowtime()

    if payload.get('due_date'):
        doc.due_date = getdate(payload['due_date'])

    if payload.get('currency'):
        doc.currency = payload['currency']
    if payload.get('conversion_rate'):
        doc.conversion_rate = flt(payload['conversion_rate'])
    if payload.get('selling_price_list'):
        doc.selling_price_list = payload['selling_price_list']

    # debit_to is only forced when the caller names it. Left alone, ERPNext derives
    # it from the customer's own receivable account, which a blanket company
    # default would override and post the receivable to the wrong ledger.
    if payload.get('debit_to'):
        doc.debit_to = payload['debit_to']

    if payload.get('tax_id'):
        doc.tax_id = payload['tax_id']
    if payload.get('po_no'):
        doc.po_no = payload['po_no']
    if payload.get('remarks'):
        doc.remarks = payload['remarks']
    if payload.get('cost_center'):
        doc.cost_center = payload['cost_center']
    if payload.get('update_stock'):
        doc.update_stock = 1

    if payload.get('is_return'):
        doc.is_return = 1
        if payload.get('return_against'):
            doc.return_against = payload['return_against']
    if payload.get('is_debit_note'):
        doc.is_debit_note = 1

    _apply_return_reason(doc, payload)

    if payload.get('is_pos'):
        doc.is_pos = 1

    if payload.get('payment_mode'):
        if not frappe.db.exists('Mode of Payment', payload['payment_mode']):
            raise PayloadError(
                _('Mode of Payment {0} does not exist.').format(payload['payment_mode']),
                {'field': 'payment_mode'},
            )

        # The payments row is what supplies the ZATCA payment means code:
        # ksa_compliance reads payments[0].mode_of_payment and resolves
        # `Mode of Payment.custom_zatca_payment_means_code` from it.
        #
        # But ERPNext persists Sales Invoice.payments *only* on a POS invoice - the
        # field is gated on `eval:doc.is_pos===1`, so on a regular invoice the row
        # is silently discarded at insert. Rather than accept a parameter that
        # quietly does nothing, require the caller to opt into is_pos explicitly.
        if not cint(doc.is_pos):
            raise PayloadError(
                _(
                    'payment_mode requires is_pos = 1. ERPNext only stores the payments table on '
                    'a POS invoice, so on a regular invoice the mode of payment would be silently '
                    'discarded and no ZATCA payment means code would be reported. Send '
                    '"is_pos": 1 as well, or omit payment_mode.'
                ),
                {'field': 'payment_mode'},
            )

        doc.append(
            'payments',
            {
                'mode_of_payment': payload['payment_mode'],
                'amount': flt(payload.get('payment_amount')),
            },
        )

    project = masters.ensure_project(payload, company, settings)
    if project:
        doc.project = project

    doc.set(EXTERNAL_ID_FIELD, payload.get('external_id'))
    doc.set(SYNCED_ON_FIELD, now())
    if payload.get('source_name'):
        doc.set(SOURCE_FIELD, payload['source_name'])

    apply_field_mappings(doc, 'Sales Invoice', payload.get('raw') or {}, settings)


def build_invoice(payload: dict, settings) -> tuple:
    """Create or update the Sales Invoice. Returns ``(InvoiceResult, warnings)``."""
    validate_invoice(payload)

    company = masters.ensure_company(payload, settings)
    customer = masters.ensure_customer(payload, settings)
    address_result = masters.ensure_address(payload, customer, settings)
    warnings = list(address_result['warnings'])
    payload['_is_b2b'] = address_result.get('is_b2b')

    existing = find_by_external_id(payload['external_id'], lock=True)

    if existing:
        if cint(existing['docstatus']) == 1:
            return (
                InvoiceResult(existing['name'], 'duplicate', 1, warnings),
                warnings,
            )
        if cint(existing['docstatus']) == 2:
            raise PayloadError(
                _('Invoice {0} for external id {1} is cancelled. Send a new external id.').format(
                    existing['name'], payload['external_id']
                ),
                {'field': 'external_id', 'invoice': existing['name']},
                code=ERR_IMMUTABLE,
            )
        if not cint(settings.update_existing_drafts):
            return (
                InvoiceResult(existing['name'], 'duplicate', 0, warnings),
                warnings,
            )

        doc = frappe.get_doc('Sales Invoice', existing['name'])
        doc.set('items', [])
        doc.set('taxes', [])
        doc.set('payments', [])
        action = 'updated'
    else:
        doc = frappe.new_doc('Sales Invoice')
        action = 'created'

    _apply_header(doc, payload, company, customer, settings)
    _append_items(doc, payload, company, settings)

    for row in _resolve_taxes(payload, company, doc):
        doc.append('taxes', row)

    doc.flags.ignore_permissions = True
    if action == 'created':
        doc.insert(ignore_permissions=True)
    else:
        doc.save(ignore_permissions=True)

    return InvoiceResult(doc.name, action, cint(doc.docstatus), warnings), warnings


def submit_invoice(name: str) -> int:
    """Submit a draft invoice, which is what triggers the ZATCA hook.

    `ksa_compliance` hangs its Sales Invoice Additional Fields creation off
    ``Sales Invoice.on_submit``, so no QR, UUID or hash exists until this runs.
    """
    doc = frappe.get_doc('Sales Invoice', name)
    if cint(doc.docstatus) == 1:
        return 1
    if cint(doc.docstatus) == 2:
        frappe.throw(_('Invoice {0} is cancelled and cannot be submitted.').format(name))

    doc.flags.ignore_permissions = True
    doc.submit()
    return cint(doc.docstatus)


def enqueue_submission(name: str) -> None:
    """Queue submission for Submit Mode = Queued.

    ``job_id`` deduplicates: a retried request for the same invoice does not stack
    a second submission job behind the first.
    """
    frappe.enqueue(
        'zatca_api.services.invoice.submit_invoice',
        queue='default',
        timeout=600,
        job_id=f'zatca_api::submit::{name}',
        deduplicate=True,
        enqueue_after_commit=True,
        name=name,
    )


def invoice_summary(name: str) -> dict:
    """The invoice fields returned alongside the ZATCA block."""
    doc = frappe.get_doc('Sales Invoice', name)

    return {
        'invoice': doc.name,
        'external_id': doc.get(EXTERNAL_ID_FIELD),
        'docstatus': cint(doc.docstatus),
        'status': doc.status,
        'is_return': cint(doc.is_return),
        'return_against': doc.return_against,
        # Echoed so the caller can see the BR-KSA-17 reason that went into the XML.
        'return_reason': cstr(doc.get(RETURN_REASON_FIELD) or ''),
        'company': doc.company,
        'customer': doc.customer,
        'customer_name': doc.customer_name,
        'tax_id': doc.tax_id,
        'posting_date': cstr(doc.posting_date),
        'posting_time': cstr(doc.posting_time),
        'due_date': cstr(doc.due_date),
        'currency': doc.currency,
        'conversion_rate': flt(doc.conversion_rate),
        'net_total': flt(doc.net_total),
        'total_taxes_and_charges': flt(doc.total_taxes_and_charges),
        'grand_total': flt(doc.grand_total),
        'rounded_total': flt(doc.rounded_total),
        'outstanding_amount': flt(doc.outstanding_amount),
        'project': doc.project,
        'items': [
            {
                'idx': row.idx,
                'item_code': row.item_code,
                'item_name': row.item_name,
                'qty': flt(row.qty),
                'uom': row.uom,
                'rate': flt(row.rate),
                'amount': flt(row.amount),
                'net_amount': flt(row.net_amount),
                'item_tax_template': row.item_tax_template,
            }
            for row in doc.items
        ],
        'taxes': [
            {
                'account_head': row.account_head,
                'description': row.description,
                'rate': flt(row.rate),
                'tax_amount': flt(row.tax_amount),
                'total': flt(row.total),
            }
            for row in doc.taxes
        ],
    }
