# zatca_api/services/masters.py
# Copyright (c) 2026, Enfono Technologies and contributors
"""Idempotent master-data resolution: Customer, Address, Item, UOM, Project.

Every function here is "ensure" shaped - it returns the name of an existing
record or creates one, and calling it twice with the same input is a no-op the
second time.

Two rules that the previous implementation broke and that matter here:

* **Auto-creation is opt-in per master type.** A typo in an inbound UOM or item
  group otherwise silently pollutes the master tables forever.
* **Defaults come from settings, not from literals.** No ``"Riyadh"``,
  ``"Services"`` or ``"Saudi Arabia"`` baked into the code.

The ZATCA-specific part is :func:`ensure_customer`. `ksa_compliance` decides
whether an invoice is *standard* (B2B, full buyer details, cleared) or
*simplified* (B2C, reported) using ``is_b2b_customer()``, which reads
``Customer.custom_vat_registration_number`` and ``Customer.custom_additional_ids``
- **not** the core ``tax_id`` field. Writing only ``tax_id`` therefore causes B2B
invoices to be filed as simplified, which is a compliance defect. This module
writes both.
"""

import frappe
from frappe import _
from frappe.utils import cint, cstr

from zatca_api.services.payload import PayloadError, apply_field_mappings
from zatca_api.utils.addressing import (
    address_warnings,
    normalise_address_parts,
    parse_address_text,
)

# Customer.custom_additional_ids child rows use these ZATCA identification codes.
# Source: ksa_compliance Additional Seller IDs / _get_buyer_other_id.
ZATCA_BUYER_ID_TYPES = ('TIN', 'CRN', 'MOM', 'MLS', 'SAG', 'NAT', 'GCC', 'IQA', 'PAS', 'OTH')


def ensure_company(payload: dict, settings) -> str:
    company = (
        payload.get('company') or settings.default_company or frappe.defaults.get_user_default('Company')
    )
    if not company:
        raise PayloadError(
            _('No company in the payload and no Default Company set in ZATCA API Settings.'),
            {'field': 'company'},
        )
    if not frappe.db.exists('Company', company):
        raise PayloadError(_('Company {0} does not exist.').format(company), {'field': 'company'})
    return company


def ensure_uom(uom: str, settings) -> str | None:
    """Return a valid UOM name, or None to let ERPNext use the item's stock UOM."""
    uom = cstr(uom).strip()
    if not uom:
        return None

    if frappe.db.exists('UOM', uom):
        return uom

    if not cint(settings.create_missing_uoms):
        raise PayloadError(
            _('UOM {0} does not exist and Create Missing UOMs is disabled.').format(uom),
            {'field': 'uom', 'value': uom},
        )

    doc = frappe.new_doc('UOM')
    doc.uom_name = uom
    doc.insert(ignore_permissions=True)
    return doc.name


def ensure_item(item: dict, company: str, settings) -> str:
    item_code = cstr(item.get('item_code')).strip()
    if frappe.db.exists('Item', item_code):
        return item_code

    if not cint(settings.create_missing_items):
        raise PayloadError(
            _('Item {0} does not exist and Create Missing Items is disabled.').format(item_code),
            {'field': 'item_code', 'value': item_code},
        )

    item_group = cstr(item.get('item_group')).strip() or cstr(settings.default_item_group).strip()
    if not item_group:
        item_group = frappe.db.get_value('Item Group', {'is_group': 0}, 'name', order_by='name')
    if not item_group or not frappe.db.exists('Item Group', item_group):
        raise PayloadError(
            _('Cannot create Item {0}: set a Default Item Group in ZATCA API Settings.').format(item_code),
            {'field': 'item_group'},
        )

    stock_uom = ensure_uom(item.get('uom'), settings) or cstr(settings.default_uom).strip() or 'Nos'

    doc = frappe.new_doc('Item')
    doc.item_code = item_code
    doc.item_name = cstr(item.get('item_name')).strip() or item_code
    doc.description = cstr(item.get('description')).strip() or doc.item_name
    doc.item_group = item_group
    doc.stock_uom = stock_uom
    doc.is_sales_item = 1
    # Default to a non-stock item: an invoice-only integration has no inventory
    # to draw from, and a stock item without a warehouse fails validation.
    doc.is_stock_item = cint(item.get('is_stock_item', 0))

    if item.get('item_tax_template'):
        template = resolve_item_tax_template(item['item_tax_template'], company)
        if template:
            doc.append('taxes', {'item_tax_template': template})

    apply_field_mappings(doc, 'Item', item.get('raw') or {}, settings)
    doc.insert(ignore_permissions=True)
    return doc.name


def resolve_item_tax_template(template: str, company: str) -> str | None:
    """Resolve an Item Tax Template for this company.

    Item Tax Templates are company-scoped and usually suffixed with the company
    abbreviation, so an upstream system sending the bare label still resolves.
    """
    template = cstr(template).strip()
    if not template:
        return None

    exact = frappe.db.get_value('Item Tax Template', {'name': template, 'company': company}, 'name')
    if exact:
        return exact

    return frappe.db.get_value(
        'Item Tax Template', {'title': template, 'company': company, 'disabled': 0}, 'name'
    )


def ensure_customer(payload: dict, settings) -> str:
    """Create or update the Customer and keep its ZATCA identifiers in sync."""
    customer_id = cstr(payload.get('customer')).strip()
    customer_name = cstr(payload.get('customer_name')).strip() or customer_id
    tax_id = cstr(payload.get('tax_id')).strip()

    existing = frappe.db.exists('Customer', customer_id)
    if not existing:
        # A customer may exist under a different primary key but the same name.
        existing = frappe.db.get_value('Customer', {'customer_name': customer_id}, 'name')

    if existing:
        _sync_customer_identifiers(existing, payload, settings)
        return existing

    if not cint(settings.create_missing_customers):
        raise PayloadError(
            _('Customer {0} does not exist and Create Missing Customers is disabled.').format(customer_id),
            {'field': 'customer', 'value': customer_id},
        )

    doc = frappe.new_doc('Customer')
    doc.customer_name = customer_name
    doc.customer_type = cstr(settings.default_customer_type).strip() or 'Company'
    if settings.default_customer_group:
        doc.customer_group = settings.default_customer_group
    if settings.default_territory:
        doc.territory = settings.default_territory
    if tax_id:
        doc.tax_id = tax_id

    _set_zatca_customer_ids(doc, payload)
    apply_field_mappings(doc, 'Customer', payload.get('raw') or {}, settings)
    doc.insert(ignore_permissions=True)
    return doc.name


def _sync_customer_identifiers(customer: str, payload: dict, settings) -> None:
    """Update tax/ZATCA identifiers on an existing Customer, only when they changed."""
    tax_id = cstr(payload.get('tax_id')).strip()
    buyer_id_value = cstr(payload.get('buyer_id_value')).strip()
    if not tax_id and not buyer_id_value:
        return

    doc = frappe.get_doc('Customer', customer)
    dirty = False

    if tax_id and cstr(doc.tax_id).strip() != tax_id:
        doc.tax_id = tax_id
        dirty = True

    if _set_zatca_customer_ids(doc, payload):
        dirty = True

    if apply_field_mappings(doc, 'Customer', payload.get('raw') or {}, settings):
        dirty = True

    if dirty:
        doc.save(ignore_permissions=True)


def _set_zatca_customer_ids(doc, payload: dict) -> bool:
    """Mirror the VAT number into the field `ksa_compliance` actually reads.

    Returns True when something changed.

    `ksa_compliance.is_b2b_customer()` checks
    ``custom_vat_registration_number`` or a non-empty ``custom_additional_ids``
    row. A customer with only the core ``tax_id`` populated is classified B2C, so
    a genuine B2B sale would be *reported* as simplified instead of *cleared* as
    standard. Both fields are custom fields installed by `ksa_compliance`; when
    that app is absent the fields do not exist and this is a no-op.
    """
    changed = False
    meta = frappe.get_meta('Customer')
    tax_id = cstr(payload.get('tax_id')).strip()

    if tax_id and meta.get_field('custom_vat_registration_number'):
        if cstr(doc.get('custom_vat_registration_number')).strip() != tax_id:
            doc.set('custom_vat_registration_number', tax_id)
            changed = True

    id_type = cstr(payload.get('buyer_id_type')).strip().upper()
    id_value = cstr(payload.get('buyer_id_value')).strip()
    if id_value and meta.get_field('custom_additional_ids'):
        if id_type and id_type not in ZATCA_BUYER_ID_TYPES:
            raise PayloadError(
                _('buyer_id_type {0} is not a ZATCA identification code. Expected one of: {1}.').format(
                    id_type, ', '.join(ZATCA_BUYER_ID_TYPES)
                ),
                {'field': 'buyer_id_type'},
            )
        id_type = id_type or 'CRN'

        existing_row = None
        for row in doc.get('custom_additional_ids') or []:
            if cstr(row.get('type_code')).upper() == id_type:
                existing_row = row
                break

        if existing_row is None:
            doc.append(
                'custom_additional_ids', {'type_name': id_type, 'type_code': id_type, 'value': id_value}
            )
            changed = True
        elif cstr(existing_row.get('value')).strip() != id_value:
            existing_row.value = id_value
            changed = True

    return changed


def customer_is_b2b(payload: dict, customer: str | None = None) -> bool:
    """Is this a B2B buyer, i.e. will the invoice be a ZATCA *standard* invoice?

    Checks the incoming payload first, then the saved Customer. Both matter: on the
    first invoice for a new customer the identifier only exists in the payload, and on
    a repeat invoice the payload may omit it because the Customer already carries it.

    The saved-customer check delegates to `ksa_compliance.is_b2b_customer`, which reads
    ``custom_vat_registration_number`` or a non-empty ``custom_additional_ids`` row --
    NOT the core ``tax_id`` field. Delegating means this cannot drift from the rule
    that actually decides standard vs simplified at submission.
    """
    if cstr(payload.get('tax_id')).strip() or cstr(payload.get('buyer_id_value')).strip():
        return True

    if not customer or not frappe.db.exists('Customer', customer):
        return False

    try:
        from ksa_compliance.ksa_compliance.doctype.sales_invoice_additional_fields.sales_invoice_additional_fields import (
            is_b2b_customer,
        )
    except ImportError:
        # Without ksa_compliance there is no standard/simplified distinction, so fall
        # back to reading the same fields it would.
        meta = frappe.get_meta('Customer')
        if not meta.get_field('custom_vat_registration_number'):
            return False
        doc = frappe.get_doc('Customer', customer)
        return bool(
            cstr(doc.get('custom_vat_registration_number')).strip()
            or any(cstr(row.get('value')).strip() for row in doc.get('custom_additional_ids') or [])
        )

    return bool(is_b2b_customer(frappe.get_doc('Customer', customer)))


def ensure_address(payload: dict, customer: str, settings) -> dict:
    """Create or update the buyer Address and link it to the Customer.

    Returns ``{"address": name|None, "warnings": [...], "is_b2b": bool}``.

    **A complete address is mandatory for a B2B buyer** and is enforced here, as an
    error, before anything is written. That mirrors `ksa_compliance`, which passes
    ``validate=True`` to ``_set_buyer_address`` whenever the invoice type is
    *Standard* and throws outright when a B2B customer has no address at all.

    Enforcing it here rather than letting that fire at submission buys three things:
    the caller gets a precise per-field list instead of a rendered HTML message, the
    failure happens before any document exists, and the same verdict is available in
    the dry run on a site where ZATCA settings are not configured yet.

    For a B2C buyer the same gaps stay warnings -- ZATCA does not require a buyer
    address on a simplified invoice.
    """
    is_b2b = customer_is_b2b(payload, customer)
    enforce = is_b2b and cint(settings.enforce_b2b_address)

    parts = dict(payload.get('address') or {})

    # Free-text parsing fills only the gaps; explicit payload values always win.
    if cint(settings.parse_address_display) and payload.get('address_display'):
        parsed = parse_address_text(payload['address_display'], settings.get_address_patterns())
        for key, value in parsed.items():
            parts.setdefault(key, value)

    country = cstr(parts.pop('country', '')).strip() or cstr(settings.default_country).strip()
    email_id = cstr(parts.pop('email_id', '')).strip()
    phone = cstr(parts.pop('phone', '')).strip()

    parts = normalise_address_parts(parts)
    warnings = address_warnings(parts, country)

    if enforce and warnings:
        raise PayloadError(
            _(
                'A complete buyer address is mandatory for a B2B customer, because ZATCA '
                'rejects a standard invoice without one. Problems: {0}'
            ).format(' '.join(warnings)),
            {
                'field': 'address_parts',
                'customer': customer,
                'is_b2b': True,
                'problems': warnings,
                'required': [
                    'street',
                    'building_number (4 digits)',
                    'district',
                    'city',
                    'postal_code (5 digits)',
                ],
            },
        )

    if not parts and not payload.get('address_title'):
        return {'address': None, 'warnings': warnings, 'is_b2b': is_b2b}

    # Address.address_line1, city and country are all reqd in core frappe. Creating a
    # record from a title alone therefore fails ERPNext validation with a bare
    # "[Address, X]: city", which tells the caller nothing. When the payload does not
    # carry enough to build a valid address, skip it and report the gaps instead --
    # a B2C invoice does not need a buyer address at all. (A B2B invoice has already
    # been rejected above when enforcement is on.)
    if not parts.get('city'):
        warnings.append(
            'No buyer Address was created: city is mandatory on an Address record and was ' 'not supplied.'
        )
        return {'address': None, 'warnings': warnings, 'is_b2b': is_b2b}

    title = cstr(payload.get('address_title')).strip() or customer
    existing = _find_linked_address(title, customer)

    if existing:
        _update_address(existing, parts, country, email_id, phone, settings, payload)
        _set_primary_address(customer, existing)
        return {'address': existing, 'warnings': warnings, 'is_b2b': is_b2b}

    doc = frappe.new_doc('Address')
    doc.address_title = title
    doc.address_type = 'Billing'
    doc.address_line1 = parts.get('address_line1') or title
    for field in ('address_line2', 'city', 'pincode', 'state'):
        if parts.get(field):
            doc.set(field, parts[field])
    for field in ('custom_building_number', 'custom_area'):
        if parts.get(field) and doc.meta.get_field(field):
            doc.set(field, parts[field])
    if country and frappe.db.exists('Country', country):
        doc.country = country
    if email_id:
        doc.email_id = email_id
    if phone:
        doc.phone = phone

    doc.append('links', {'link_doctype': 'Customer', 'link_name': customer})
    apply_field_mappings(doc, 'Address', payload.get('raw') or {}, settings)
    doc.insert(ignore_permissions=True)
    _set_primary_address(customer, doc.name)

    return {'address': doc.name, 'warnings': warnings, 'is_b2b': is_b2b}


def _find_linked_address(title: str, customer: str) -> str | None:
    """Find an Address with this title that is linked to this customer.

    A single query with a join, not "fetch all addresses with this title, then
    loop issuing one Dynamic Link query each".
    """
    address = frappe.qb.DocType('Address')
    link = frappe.qb.DocType('Dynamic Link')

    rows = (
        frappe.qb.from_(address)
        .inner_join(link)
        .on(link.parent == address.name)
        .select(address.name)
        .where(
            (address.address_title == title)
            & (link.parenttype == 'Address')
            & (link.link_doctype == 'Customer')
            & (link.link_name == customer)
        )
        .limit(1)
        .run(as_dict=True)
    )
    return rows[0]['name'] if rows else None


def _update_address(name, parts, country, email_id, phone, settings, payload) -> None:
    doc = frappe.get_doc('Address', name)
    dirty = False

    for field, value in parts.items():
        if not doc.meta.get_field(field):
            continue
        if cstr(doc.get(field)).strip() != cstr(value).strip():
            doc.set(field, value)
            dirty = True

    if country and frappe.db.exists('Country', country) and doc.country != country:
        doc.country = country
        dirty = True
    if email_id and doc.email_id != email_id:
        doc.email_id = email_id
        dirty = True
    if phone and doc.phone != phone:
        doc.phone = phone
        dirty = True

    if apply_field_mappings(doc, 'Address', payload.get('raw') or {}, settings):
        dirty = True

    if dirty:
        doc.save(ignore_permissions=True)


def _set_primary_address(customer: str, address: str) -> None:
    """Point Customer.customer_primary_address at this address when unset.

    `ksa_compliance` reads the buyer address from the customer's primary address,
    so a linked-but-not-primary address produces an invoice with no buyer address.
    An existing primary address is left alone.
    """
    if not frappe.get_meta('Customer').get_field('customer_primary_address'):
        return

    current = frappe.db.get_value('Customer', customer, 'customer_primary_address')
    if current:
        return

    # db_set on the child-free Customer parent avoids a full save (and its
    # validations) for what is a denormalised convenience field.
    frappe.db.set_value('Customer', customer, 'customer_primary_address', address)


def ensure_project(payload: dict, company: str, settings) -> str | None:
    project = cstr(payload.get('project')).strip()
    if not project:
        return None

    if frappe.db.exists('Project', project):
        return project

    existing = frappe.db.get_value('Project', {'project_name': project}, 'name')
    if existing:
        return existing

    if not cint(settings.create_missing_projects):
        raise PayloadError(
            _('Project {0} does not exist and Create Missing Projects is disabled.').format(project),
            {'field': 'project', 'value': project},
        )

    doc = frappe.new_doc('Project')
    doc.project_name = project
    doc.company = company
    doc.status = 'Open'
    apply_field_mappings(doc, 'Project', payload.get('raw') or {}, settings)
    doc.insert(ignore_permissions=True)
    return doc.name
