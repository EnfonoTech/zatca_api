# zatca_api/services/payload.py
# Copyright (c) 2026, Enfono Technologies and contributors
"""Normalise and validate an inbound invoice payload.

Two jobs:

1. **Normalise.** The same request may arrive as JSON, as form-encoded fields, or
   from the scheduled puller with an upstream system's own key names. Everything
   is folded into one canonical dict before any document is touched.
2. **Validate.** Fail with a precise, machine-readable error *before* creating
   master data, so a bad request cannot leave half-written Customers and Items
   behind.

Key lookup is alias-aware and case/separator-insensitive, so ``"Customer Name"``,
``customer_name``, ``customerName`` and ``CUSTOMER_NAME`` all resolve. That is
what lets one generic app serve clients whose upstream systems disagree about
naming, without a per-client code branch.
"""

import json

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, get_datetime, getdate, today

from zatca_api.services.envelope import ERR_VALIDATION


class PayloadError(Exception):
    """Raised for a payload that cannot produce a valid invoice."""

    def __init__(self, message: str, details: dict | None = None, code: str = ERR_VALIDATION):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.code = code


# Canonical key -> accepted aliases (compared after normalisation).
INVOICE_ALIASES = {
    'external_id': (
        'external_id',
        'externalid',
        'naming_series',
        'reference',
        'invoice_no',
        'invoice_number',
        'source_id',
        'document_no',
        'doc_no',
    ),
    'customer': ('customer', 'customer_name', 'customer_code', 'buyer', 'buyer_name'),
    'customer_name_override': ('customer_full_name', 'customer_display_name'),
    'company': ('company', 'company_name'),
    'tax_id': ('tax_id', 'vat_number', 'vat_registration_number', 'buyer_vat', 'customer_tax_id'),
    'buyer_id_type': ('buyer_id_type', 'other_buyer_id_type', 'buyer_identification_type'),
    'buyer_id_value': ('buyer_id', 'buyer_id_value', 'other_buyer_id', 'crn', 'commercial_registration'),
    'posting_date': ('posting_date', 'invoice_date', 'date', 'document_date'),
    'posting_time': ('posting_time', 'invoice_time', 'time'),
    'due_date': ('due_date', 'payment_due_date'),
    'currency': ('currency', 'currency_code'),
    'conversion_rate': ('conversion_rate', 'exchange_rate'),
    'items': ('items', 'lines', 'invoice_items', 'item_list'),
    'taxes': ('taxes', 'tax_lines', 'taxes_and_charges_rows'),
    'tax_template': ('tax_template', 'taxes_and_charges', 'sales_taxes_and_charges_template'),
    'address_title': ('address_title', 'customer_address', 'billing_address_title'),
    'address_display': ('address_display', 'address_text', 'full_address', 'billing_address'),
    'address': ('address_parts', 'billing_address_parts'),
    'project': ('project', 'project_name'),
    'cost_center': ('cost_center',),
    'po_no': ('po_no', 'purchase_order_no', 'customer_po'),
    'remarks': ('remarks', 'notes', 'comments'),
    'is_return': ('is_return', 'is_credit_note'),
    'return_against': ('return_against', 'against_invoice', 'original_invoice'),
    # ZATCA BR-KSA-17 requires a stated reason on a credit or debit note.
    'return_reason': ('return_reason', 'reason', 'credit_note_reason', 'custom_return_reason'),
    'is_debit_note': ('is_debit_note',),
    'update_stock': ('update_stock',),
    'is_pos': ('is_pos', 'is_point_of_sale'),
    'payment_amount': ('payment_amount', 'paid_amount'),
    'submit': ('submit', 'auto_submit', 'do_submit'),
    'debit_to': ('debit_to', 'receivable_account'),
    'selling_price_list': ('selling_price_list', 'price_list'),
    'payment_mode': ('payment_mode', 'mode_of_payment'),
}

ITEM_ALIASES = {
    'item_code': ('item_code', 'code', 'sku', 'item'),
    'item_name': ('item_name', 'name', 'description_short'),
    'description': ('description', 'desc', 'item_description'),
    'qty': ('qty', 'quantity', 'qnty'),
    'rate': ('rate', 'price', 'unit_price', 'unit_rate'),
    'uom': ('uom', 'unit', 'unit_of_measure'),
    'discount_amount': ('discount_amount',),
    'discount_percentage': ('discount_percentage', 'discount_percent'),
    'item_tax_template': ('item_tax_template', 'tax_template', 'item_tax'),
    'income_account': ('income_account', 'revenue_account'),
    'cost_center': ('cost_center',),
    'warehouse': ('warehouse',),
    'item_group': ('item_group',),
    'is_stock_item': ('is_stock_item',),
}

TAX_ALIASES = {
    'account_head': ('account_head', 'account', 'tax_account'),
    'charge_type': ('charge_type',),
    'rate': ('rate', 'tax_rate', 'percentage'),
    'tax_amount': ('tax_amount', 'amount'),
    'description': ('description', 'label'),
    'included_in_print_rate': ('included_in_print_rate', 'tax_inclusive', 'is_inclusive'),
}

ADDRESS_ALIASES = {
    'address_line1': ('address_line1', 'street', 'street_name', 'line1'),
    'address_line2': ('address_line2', 'additional_street', 'line2'),
    'custom_building_number': ('building_number', 'custom_building_number', 'building'),
    'custom_area': ('district', 'custom_area', 'area', 'neighbourhood', 'neighborhood'),
    'city': ('city', 'town'),
    'pincode': ('pincode', 'postal_code', 'zip', 'zipcode', 'postcode'),
    'state': ('state', 'province', 'region'),
    'country': ('country',),
    'email_id': ('email', 'email_id'),
    'phone': ('phone', 'mobile', 'telephone'),
}


def _norm(key) -> str:
    """Fold a key to a comparable form: lowercase, no spaces/underscores/dashes."""
    return cstr(key).strip().lower().replace(' ', '').replace('_', '').replace('-', '')


def _build_index(raw: dict) -> dict:
    index = {}
    for key, value in (raw or {}).items():
        index.setdefault(_norm(key), value)
    return index


def pick(raw: dict, canonical: str, aliases: dict, default=None):
    """Resolve one canonical key from ``raw`` using the alias table."""
    index = raw if raw.get('__indexed__') else _build_index(raw)
    for alias in aliases.get(canonical, (canonical,)):
        normalised = _norm(alias)
        if normalised in index:
            value = index[normalised]
            if value is not None and cstr(value).strip() != '':
                return value
    return default


def as_list(value) -> list:
    """Coerce a payload value to a list of dicts.

    Form-encoded requests deliver nested structures as JSON strings, so decode
    those. A single dict is treated as a one-element list.
    """
    if value is None:
        return []
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            raise PayloadError(_('Expected a JSON array but received an unparseable string.'))
    if isinstance(value, dict):
        return [value]
    if isinstance(value, (list, tuple)):
        return [row for row in value if row is not None]
    raise PayloadError(_('Expected a JSON array, got {0}.').format(type(value).__name__))


def as_dict(value) -> dict:
    if value is None:
        return {}
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return {}
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            raise PayloadError(_('Expected a JSON object but received an unparseable string.'))
    if isinstance(value, dict):
        return value
    raise PayloadError(_('Expected a JSON object, got {0}.').format(type(value).__name__))


def coerce(value, value_type: str):
    """Coerce a mapped value using frappe.utils, never bare int()/float()/str().

    Bare casts raise on ``None``, on ``''``, and on locale-formatted numbers such
    as ``"1,250.00"``; the frappe helpers absorb all three.
    """
    if value is None:
        return None

    value_type = (value_type or 'Data').lower()
    if value_type in ('int', 'check'):
        return cint(value)
    if value_type in ('float', 'currency'):
        return flt(value)
    if value_type == 'date':
        return getdate(value) if cstr(value).strip() else None
    if value_type in ('datetime', 'time'):
        return get_datetime(value) if cstr(value).strip() else None
    if value_type == 'json':
        return frappe.as_json(value) if not isinstance(value, str) else value
    return cstr(value)


def normalise_invoice(raw: dict, is_return: bool = False) -> dict:
    """Fold an arbitrary inbound payload into the canonical invoice shape."""
    raw = as_dict(raw)
    index = _build_index(raw)
    index['__indexed__'] = True

    def get(canonical, default=None):
        return pick(index, canonical, INVOICE_ALIASES, default)

    payload = {
        'external_id': cstr(get('external_id') or '').strip(),
        'customer': cstr(get('customer') or '').strip(),
        'customer_name': cstr(get('customer_name_override') or get('customer') or '').strip(),
        'company': cstr(get('company') or '').strip(),
        'tax_id': cstr(get('tax_id') or '').strip(),
        'buyer_id_type': cstr(get('buyer_id_type') or '').strip(),
        'buyer_id_value': cstr(get('buyer_id_value') or '').strip(),
        'posting_date': get('posting_date'),
        'posting_time': cstr(get('posting_time') or '').strip(),
        'due_date': get('due_date'),
        'currency': cstr(get('currency') or '').strip(),
        'conversion_rate': flt(get('conversion_rate')) or None,
        'tax_template': cstr(get('tax_template') or '').strip(),
        'address_title': cstr(get('address_title') or '').strip(),
        'address_display': cstr(get('address_display') or '').strip(),
        'project': cstr(get('project') or '').strip(),
        'cost_center': cstr(get('cost_center') or '').strip(),
        'po_no': cstr(get('po_no') or '').strip(),
        'remarks': cstr(get('remarks') or '').strip(),
        'return_against': cstr(get('return_against') or '').strip(),
        'return_reason': cstr(get('return_reason') or '').strip(),
        'debit_to': cstr(get('debit_to') or '').strip(),
        'selling_price_list': cstr(get('selling_price_list') or '').strip(),
        'payment_mode': cstr(get('payment_mode') or '').strip(),
        'is_return': cint(get('is_return')) or cint(is_return),
        'is_debit_note': cint(get('is_debit_note')),
        'update_stock': cint(get('update_stock')),
        'is_pos': cint(get('is_pos')),
        'payment_amount': flt(get('payment_amount')),
        'items': [
            _normalise_item(row, is_return=bool(cint(get('is_return')) or cint(is_return)))
            for row in as_list(get('items'))
        ],
        'taxes': [_normalise_tax(row) for row in as_list(get('taxes'))],
        'address': _normalise_address(as_dict(get('address'))),
        'raw': raw,
    }

    submit_flag = get('submit')
    payload['submit'] = None if submit_flag is None else bool(cint(submit_flag))

    return payload


def _normalise_item(row, is_return: bool) -> dict:
    row = as_dict(row)
    index = _build_index(row)
    index['__indexed__'] = True

    def get(canonical, default=None):
        return pick(index, canonical, ITEM_ALIASES, default)

    # Default to 1 only when qty is absent. An explicit 0 is preserved so
    # validate_invoice can reject it, rather than being silently coerced to a
    # non-zero quantity and billed.
    raw_qty = get('qty')
    qty = flt(raw_qty) if raw_qty is not None else 1.0
    # ERPNext requires negative quantities on a return. Normalising the sign here
    # means the caller can send either convention and still get a valid document.
    if is_return and qty:
        qty = -abs(qty)

    item = {
        'item_code': cstr(get('item_code') or '').strip(),
        'item_name': cstr(get('item_name') or '').strip(),
        'description': cstr(get('description') or '').strip(),
        'qty': qty,
        'rate': flt(get('rate')),
        'uom': cstr(get('uom') or '').strip(),
        'item_tax_template': cstr(get('item_tax_template') or '').strip(),
        'income_account': cstr(get('income_account') or '').strip(),
        'cost_center': cstr(get('cost_center') or '').strip(),
        'warehouse': cstr(get('warehouse') or '').strip(),
        'item_group': cstr(get('item_group') or '').strip(),
        'raw': row,
    }

    discount_amount = get('discount_amount')
    if discount_amount is not None:
        item['discount_amount'] = flt(discount_amount)

    discount_percentage = get('discount_percentage')
    if discount_percentage is not None:
        item['discount_percentage'] = flt(discount_percentage)

    is_stock_item = get('is_stock_item')
    if is_stock_item is not None:
        item['is_stock_item'] = cint(is_stock_item)

    return item


def _normalise_tax(row) -> dict:
    row = as_dict(row)
    index = _build_index(row)
    index['__indexed__'] = True

    def get(canonical, default=None):
        return pick(index, canonical, TAX_ALIASES, default)

    return {
        'account_head': cstr(get('account_head') or '').strip(),
        'charge_type': cstr(get('charge_type') or 'On Net Total').strip(),
        'rate': flt(get('rate')),
        'tax_amount': flt(get('tax_amount')) if get('tax_amount') is not None else None,
        'description': cstr(get('description') or '').strip(),
        'included_in_print_rate': cint(get('included_in_print_rate')),
    }


def _normalise_address(row) -> dict:
    row = as_dict(row)
    if not row:
        return {}

    index = _build_index(row)
    index['__indexed__'] = True

    parts = {}
    for canonical in ADDRESS_ALIASES:
        value = pick(index, canonical, ADDRESS_ALIASES)
        if value is not None and cstr(value).strip():
            parts[canonical] = cstr(value).strip()
    return parts


def validate_invoice(payload: dict) -> None:
    """Reject a payload that cannot produce a valid invoice. Raises PayloadError."""
    missing = []
    if not payload.get('external_id'):
        missing.append('external_id')
    if not payload.get('customer'):
        missing.append('customer')
    if not payload.get('items'):
        missing.append('items')

    if missing:
        raise PayloadError(
            _('Missing required field(s): {0}.').format(', '.join(missing)),
            {'missing': missing},
        )

    for idx, item in enumerate(payload['items'], start=1):
        if not item.get('item_code'):
            raise PayloadError(
                _('Item row {0}: item_code is required.').format(idx),
                {'row': idx, 'field': 'item_code'},
            )
        if flt(item.get('qty')) == 0:
            raise PayloadError(
                _('Item row {0} ({1}): qty must not be zero.').format(idx, item['item_code']),
                {'row': idx, 'field': 'qty'},
            )
        if flt(item.get('rate')) < 0:
            raise PayloadError(
                _('Item row {0} ({1}): rate must not be negative. Use is_return for credit notes.').format(
                    idx, item['item_code']
                ),
                {'row': idx, 'field': 'rate'},
            )

    if payload.get('posting_date'):
        try:
            posting_date = getdate(payload['posting_date'])
        except Exception:
            raise PayloadError(
                _('posting_date {0} is not a valid date.').format(payload['posting_date']),
                {'field': 'posting_date'},
            )

        # ZATCA BR-KSA-04: the issue date (BT-2) must be less than or equal to the
        # current date. Caught here rather than at clearance, because by the time
        # ZATCA rejects it the invoice is already submitted and posted to the ledger.
        if posting_date > getdate(today()):
            message = _('posting_date {0} is in the future. ZATCA rejects a future issue date (BR-KSA-04); send today or an earlier date.')
            raise PayloadError(
                message.format(posting_date),
                {'field': 'posting_date', 'zatca_rule': 'BR-KSA-04'},
            )

    if payload.get('is_return') and payload.get('return_against'):
        against = payload['return_against']
        docstatus = frappe.db.get_value('Sales Invoice', against, 'docstatus')
        if docstatus is None:
            raise PayloadError(
                _('return_against invoice {0} does not exist.').format(against),
                {'field': 'return_against'},
            )
        if cint(docstatus) != 1:
            raise PayloadError(
                _('return_against invoice {0} is not submitted.').format(against),
                {'field': 'return_against'},
            )


def apply_field_mappings(doc_or_dict, target_doctype: str, raw: dict, settings) -> list:
    """Write configured custom-field mappings onto a document or dict.

    Returns the list of keys that were applied, for the response and the log.
    This is the mechanism that replaces hardcoded client-specific fieldnames.
    """
    applied = []
    index = _build_index(raw)

    for row in settings.mappings_for(target_doctype):
        source_key = _norm(row.source_key)
        if source_key not in index:
            if row.is_mandatory:
                raise PayloadError(
                    _('Mapped field {0} is mandatory but missing from the payload.').format(row.source_key),
                    {'field': row.source_key, 'target_doctype': target_doctype},
                )
            continue

        value = coerce(index[source_key], row.value_type)
        if isinstance(doc_or_dict, dict):
            doc_or_dict[row.target_field] = value
        else:
            doc_or_dict.set(row.target_field, value)
        applied.append(row.target_field)

    return applied
