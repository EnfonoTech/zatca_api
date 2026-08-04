# zatca_api/setup_test_site.py
# Copyright (c) 2026, Enfono Technologies and contributors
"""Seed a site for ZATCA API testing.

Run it with::

    bench --site <site> execute zatca_api.setup_test_site.run

Idempotent -- safe to re-run. It fills only what is missing, so it can also repair a
half-provisioned site.

It configures **ZATCA Phase 1**, not Phase 2, on purpose: Phase 1 computes its QR
locally with no Java CLI and no network call to ZATCA, so a freshly seeded site
produces a genuine, verifiable QR immediately. Phase 2 additionally needs the ZATCA
CLI, a provisioned CSID and sandbox credentials; layer that on afterwards when you
want to exercise clearance.

**The fresh-site trap this works around.** ERPNext creates UOM, Item Group, Customer
Group, Territory, Warehouse Type, Gender, Mode of Payment and Supplier Group from its
**setup wizard**, not from ``bench install-app erpnext``. A site created by
``bench new-site`` + ``install-app`` and never taken through the wizard therefore has
all of those tables empty, and the failures look unrelated to each other:

* ``LinkValidationError: Could not find Warehouse Type: Transit`` when creating a
  Company, which builds default warehouses.
* ``LinkValidationError: Could not find Default Unit of Measure: Nos`` when creating
  an Item.
* No Item Group / Customer Group / Territory for anything to default to.
* No ``Fiscal Year`` covers today, so submitting an invoice fails with
  ``Date ... is not in any active Fiscal Year``.
* ``Global Defaults.default_currency`` is still frappe's factory ``INR`` while the
  company is ``SAR``, and no Price List exists at all, so every invoice dies with
  ``Exchange Rate is mandatory. Maybe Currency Exchange record is not created for
  INR to SAR.``

Rather than hand-creating each master as it trips, this calls ERPNext's own
``setup_wizard.operations.install_fixtures.install(country)`` -- the same code the
wizard runs -- so the site ends up with the standard master set instead of a
hand-rolled subset.
"""

import frappe
from frappe.utils import cint, cstr

COMPANY = 'ZATCA Test Co'
ABBR = 'ZTC'
# 15 digits, starts and ends with 3, as ZATCA requires.
SELLER_VAT = '311111111111113'
BUYER_VAT = '300000000000003'
CUSTOMER_B2C = 'Walk-in Customer'
ITEM_STANDARD = 'SVC-IMPL'
ITEM_ZERO = 'SVC-EXPORT'
API_USER = 'zatca-api@enfono.com'

# erpnext ships these as fixtures; a site whose after_install did not complete has none.
WAREHOUSE_TYPES = ('Transit',)
GENDERS = ('Male', 'Female', 'Other', 'Prefer not to say')

# ZATCA payment means codes are UN/EDIFACT 4461 values, which the ZATCA e-invoicing
# spec references. ksa_compliance stores them in a free-text mandatory Data field
# (Mode of Payment.custom_zatca_payment_means_code) with no option list, so the value
# is the integrator's responsibility -- confirm these against current ZATCA guidance
# before relying on them for a production filing.
PAYMENT_MODES = (
    ('Cash', 'Cash', '10'),  # 10 = in cash
    ('Bank Transfer', 'Bank', '30'),  # 30 = credit transfer
    ('Bank Draft', 'Bank', '42'),  # 42 = payment to bank account
    ('Credit Card', 'Bank', '48'),  # 48 = bank card
)


def run(with_api_keys: int = 1):
    """Seed everything. Returns a summary dict, also printed for `bench execute`."""
    result = {}

    result['prerequisites'] = _ensure_erpnext_masters()
    result['company'] = _ensure_company()
    result['company_repair'] = _repair_company()
    result['currency_defaults'] = _ensure_currency_defaults()
    result['fiscal_year'] = _ensure_fiscal_year()
    result['vat_account'] = _ensure_vat_account()
    result['tax_template'] = _ensure_tax_template(result['vat_account'])
    result['item_tax_templates'] = _ensure_item_tax_templates(result['vat_account'])
    result['items'] = _ensure_items()
    result['company_address'] = _ensure_company_address()
    result['zatca_phase_1'] = _ensure_phase_1(result['company_address'])
    result['payment_modes'] = _ensure_payment_modes()
    result['customer_b2c'] = _ensure_b2c_customer()
    result['settings'] = _configure_settings()

    if cint(with_api_keys):
        result['api'] = _ensure_api_user()

    frappe.db.commit()

    print('=' * 70)
    print('ZATCA API test site seeded')
    print('=' * 70)
    for key, value in result.items():
        print(f'  {key:22s} {value}')
    print('=' * 70)
    return result


# --------------------------------------------------------------------------- steps


def _ensure_erpnext_masters() -> dict:
    """Run ERPNext's setup-wizard fixtures if the standard masters are missing.

    These are NOT created by ``bench install-app erpnext`` -- they come from the setup
    wizard. A site that never went through the wizard has empty UOM, Item Group,
    Customer Group, Territory, Warehouse Type, Gender, Mode of Payment and Supplier
    Group tables, and every downstream insert fails with a different
    LinkValidationError.

    Calling erpnext's own installer keeps the site's master data standard. Falls back
    to creating just the records this seeder needs if that import is unavailable.
    """
    probes = ('UOM', 'Item Group', 'Customer Group', 'Territory')
    missing = [dt for dt in probes if not frappe.db.count(dt)]
    if not missing:
        return {'status': 'masters already present'}

    country = frappe.db.get_value('Company', COMPANY, 'country') or 'Saudi Arabia'

    try:
        from erpnext.setup.setup_wizard.operations import install_fixtures
    except ImportError:
        return _minimal_masters_fallback(f'erpnext install_fixtures unavailable; missing={missing}')

    # frappe.flags.in_setup_wizard keeps the fixtures' own validations lenient, which
    # is what the wizard itself does when it calls this.
    previous = frappe.flags.in_setup_wizard
    frappe.flags.in_setup_wizard = True
    try:
        install_fixtures.install(country)
        frappe.db.commit()
    except Exception as exc:
        frappe.db.rollback()
        return _minimal_masters_fallback(f'install_fixtures.install failed: {exc}')
    finally:
        frappe.flags.in_setup_wizard = previous

    return {
        'status': 'ran erpnext install_fixtures',
        'country': country,
        'was_missing': missing,
        'uom_count': frappe.db.count('UOM'),
        'item_group_count': frappe.db.count('Item Group'),
    }


def _minimal_masters_fallback(reason: str) -> dict:
    """Create only the masters this seeder cannot proceed without."""
    created = {
        'reason': reason,
        'uoms': [],
        'warehouse_types': [],
        'genders': [],
        'item_groups': [],
        'customer_groups': [],
        'territories': [],
    }

    for name in ('Nos', 'Unit'):
        if not frappe.db.exists('UOM', name):
            doc = frappe.new_doc('UOM')
            doc.uom_name = name
            doc.must_be_whole_number = 1 if name == 'Nos' else 0
            doc.insert(ignore_permissions=True)
            created['uoms'].append(name)

    for name in WAREHOUSE_TYPES:
        if not frappe.db.exists('Warehouse Type', name):
            doc = frappe.new_doc('Warehouse Type')
            doc.name = name
            doc.insert(ignore_permissions=True)
            created['warehouse_types'].append(name)

    if frappe.db.exists('DocType', 'Gender'):
        for name in GENDERS:
            if not frappe.db.exists('Gender', name):
                doc = frappe.new_doc('Gender')
                doc.gender = name
                doc.insert(ignore_permissions=True)
                created['genders'].append(name)

    for doctype, field, root, children in (
        ('Item Group', 'item_group_name', 'All Item Groups', ('Services', 'Products')),
        ('Customer Group', 'customer_group_name', 'All Customer Groups', ('Commercial',)),
        ('Territory', 'territory_name', 'All Territories', ('Saudi Arabia',)),
    ):
        parent_field = {
            'Item Group': 'parent_item_group',
            'Customer Group': 'parent_customer_group',
            'Territory': 'parent_territory',
        }[doctype]
        if not frappe.db.exists(doctype, root):
            doc = frappe.new_doc(doctype)
            doc.set(field, root)
            doc.is_group = 1
            doc.insert(ignore_permissions=True)
        for child in children:
            if not frappe.db.exists(doctype, child):
                doc = frappe.new_doc(doctype)
                doc.set(field, child)
                doc.set(parent_field, root)
                doc.is_group = 0
                doc.insert(ignore_permissions=True)
                created[doctype.lower().replace(' ', '_') + 's'].append(child)

    frappe.db.commit()
    return created


def _ensure_company() -> str:
    if frappe.db.exists('Company', COMPANY):
        return COMPANY

    doc = frappe.new_doc('Company')
    doc.company_name = COMPANY
    doc.abbr = ABBR
    doc.default_currency = 'SAR'
    doc.country = 'Saudi Arabia'
    doc.insert(ignore_permissions=True)
    return doc.name


def _repair_company() -> dict:
    """Finish a Company whose insert aborted part-way.

    ``Company.on_update`` builds the chart of accounts, cost centers, warehouses and
    default accounts in sequence. When an early step throws -- missing Warehouse Type,
    in the case that started this -- the Company row still exists but the later steps
    never ran, so there is no Cost Center and no default income account. Every invoice
    then fails with "Row #1: Cost Center None does not belong to company", which points
    at the invoice rather than at the half-built company.

    Each step is independent and wrapped: erpnext's own creators are preferred, but a
    failure in one must not roll back the others. ``set_default_accounts`` in particular
    raises ``AttributeError: 'Company' object has no attribute 'update_default_account'``
    on some 15.x builds, so the one account this seeder actually needs is set directly.
    """
    abbr = frappe.db.get_value('Company', COMPANY, 'abbr') or ABBR
    repaired = []

    # --- warehouses -------------------------------------------------------
    if not frappe.db.count('Warehouse', {'company': COMPANY}):
        try:
            doc = frappe.get_doc('Company', COMPANY)
            doc.create_default_warehouses()
            frappe.db.commit()
            repaired.append('warehouses')
        except Exception as exc:
            frappe.db.rollback()
            repaired.append(f'warehouses failed: {exc}')

    # --- cost centers -----------------------------------------------------
    if not frappe.db.count('Cost Center', {'company': COMPANY}):
        try:
            doc = frappe.get_doc('Company', COMPANY)
            doc.create_default_cost_center()
            frappe.db.commit()
            repaired.append('cost_centers (erpnext)')
        except Exception as exc:
            frappe.db.rollback()
            repaired.append(f'cost_centers via erpnext failed ({exc}); creating directly')
            _create_cost_centers(abbr)
            repaired.append('cost_centers (direct)')

    # --- company pointers -------------------------------------------------
    # create_default_cost_center writes the records but leaves these unset when it runs
    # outside the normal insert flow.
    updates = {}
    if not frappe.db.get_value('Company', COMPANY, 'cost_center'):
        main = frappe.db.get_value(
            'Cost Center', {'company': COMPANY, 'is_group': 0, 'cost_center_name': 'Main'}, 'name'
        ) or frappe.db.get_value('Cost Center', {'company': COMPANY, 'is_group': 0}, 'name')
        if main:
            updates['cost_center'] = main
            updates['round_off_cost_center'] = main

    if not frappe.db.get_value('Company', COMPANY, 'default_income_account'):
        income = frappe.db.get_value('Account', f'Sales - {abbr}', 'name') or frappe.db.get_value(
            'Account', {'company': COMPANY, 'root_type': 'Income', 'is_group': 0}, 'name'
        )
        if income:
            updates['default_income_account'] = income

    for field, value in (
        ('round_off_account', f'Round Off - {abbr}'),
        ('default_expense_account', f'Cost of Goods Sold - {abbr}'),
    ):
        if not frappe.db.get_value('Company', COMPANY, field) and frappe.db.exists('Account', value):
            updates[field] = value

    if updates:
        # db.set_value, not doc.save(): saving the Company re-triggers on_update, which
        # is the code path that failed in the first place.
        frappe.db.set_value('Company', COMPANY, updates)
        frappe.db.commit()
        repaired.append(f'pointers: {", ".join(sorted(updates))}')

    return {
        'repaired': repaired or 'nothing to repair',
        'cost_center': frappe.db.get_value('Company', COMPANY, 'cost_center'),
        'default_income_account': frappe.db.get_value('Company', COMPANY, 'default_income_account'),
    }


def _create_cost_centers(abbr: str) -> None:
    """Create the standard two-level cost centre tree directly.

    erpnext's own creator is preferred; this is the fallback for builds where it raises.
    """
    root_name = f'{COMPANY} - {abbr}'
    if not frappe.db.exists('Cost Center', root_name):
        root = frappe.new_doc('Cost Center')
        root.cost_center_name = COMPANY
        root.company = COMPANY
        root.is_group = 1
        root.flags.ignore_mandatory = True
        root.insert(ignore_permissions=True)

    main_name = f'Main - {abbr}'
    if not frappe.db.exists('Cost Center', main_name):
        main = frappe.new_doc('Cost Center')
        main.cost_center_name = 'Main'
        main.company = COMPANY
        main.parent_cost_center = root_name
        main.is_group = 0
        main.flags.ignore_mandatory = True
        main.insert(ignore_permissions=True)

    frappe.db.commit()


def _ensure_currency_defaults() -> dict:
    """Align the site currency with the company, and make sure a Price List exists.

    A fresh site keeps frappe's factory ``Global Defaults.default_currency = INR``. With
    a SAR company that mismatch makes ERPNext demand an INR->SAR Currency Exchange
    record, and every invoice fails with "Exchange Rate is mandatory". The setup wizard
    normally fixes this and also creates the Standard Selling / Standard Buying price
    lists; a site that skipped the wizard has neither.
    """
    currency = frappe.db.get_value('Company', COMPANY, 'default_currency') or 'SAR'
    country = frappe.db.get_value('Company', COMPANY, 'country') or 'Saudi Arabia'
    changed = {}

    if frappe.db.exists('Currency', currency) and not frappe.db.get_value('Currency', currency, 'enabled'):
        frappe.db.set_value('Currency', currency, 'enabled', 1)
        changed['currency_enabled'] = currency

    defaults = frappe.get_single('Global Defaults')
    if defaults.default_currency != currency:
        changed['global_default_currency'] = f'{defaults.default_currency} -> {currency}'
        defaults.default_currency = currency
    if defaults.meta.get_field('country') and defaults.country != country:
        defaults.country = country
        changed['global_country'] = country
    if changed:
        defaults.flags.ignore_permissions = True
        defaults.save()
        # Global Defaults are cached in frappe.defaults; without this the running
        # process keeps handing out the old currency.
        frappe.clear_cache()

    price_lists = {}
    for name, selling, buying in (('Standard Selling', 1, 0), ('Standard Buying', 0, 1)):
        if frappe.db.exists('Price List', name):
            if frappe.db.get_value('Price List', name, 'currency') != currency:
                frappe.db.set_value('Price List', name, 'currency', currency)
                price_lists[name] = f'currency -> {currency}'
            continue
        doc = frappe.new_doc('Price List')
        doc.price_list_name = name
        doc.currency = currency
        doc.selling = selling
        doc.buying = buying
        doc.enabled = 1
        doc.insert(ignore_permissions=True)
        price_lists[name] = 'created'

    selling = frappe.get_single('Selling Settings')
    if not selling.selling_price_list:
        selling.selling_price_list = 'Standard Selling'
        selling.flags.ignore_permissions = True
        selling.save()
        changed['selling_price_list'] = 'Standard Selling'

    buying_meta = frappe.get_meta('Buying Settings')
    if buying_meta.get_field('buying_price_list'):
        buying = frappe.get_single('Buying Settings')
        if not buying.buying_price_list:
            buying.buying_price_list = 'Standard Buying'
            buying.flags.ignore_permissions = True
            buying.save()

    frappe.db.commit()
    return {'currency': currency, 'changed': changed or 'already aligned', 'price_lists': price_lists}


def _ensure_fiscal_year() -> dict:
    """Ensure a Fiscal Year covers today and is linked to the company.

    The setup wizard creates this from the fiscal-year start date it asks for. Without
    it, an invoice inserts fine as a draft but fails on **submit** with
    "Date ... is not in any active Fiscal Year", because that check runs when the GL
    entries are posted -- which is why a dry run can pass while the real call fails.

    KSA uses the calendar year.
    """
    from frappe.utils import getdate, nowdate

    today = getdate(nowdate())

    candidates = frappe.get_all(
        'Fiscal Year',
        filters={
            'disabled': 0,
            'year_start_date': ['<=', today],
            'year_end_date': ['>=', today],
        },
        pluck='name',
    )
    for candidate in candidates:
        companies = frappe.get_all('Fiscal Year Company', filters={'parent': candidate}, pluck='company')
        # An unrestricted Fiscal Year (no company rows) applies to every company.
        if not companies or COMPANY in companies:
            return {'fiscal_year': candidate, 'status': 'already covers today'}

    year = str(today.year)
    if frappe.db.exists('Fiscal Year', year):
        doc = frappe.get_doc('Fiscal Year', year)
        if not any(row.company == COMPANY for row in doc.companies):
            doc.append('companies', {'company': COMPANY})
            doc.flags.ignore_permissions = True
            doc.save()
            frappe.db.commit()
            return {'fiscal_year': year, 'status': 'company linked to existing year'}
        return {'fiscal_year': year, 'status': 'already linked'}

    doc = frappe.new_doc('Fiscal Year')
    doc.year = year
    doc.year_start_date = f'{year}-01-01'
    doc.year_end_date = f'{year}-12-31'
    doc.append('companies', {'company': COMPANY})
    doc.flags.ignore_permissions = True
    doc.insert()
    frappe.db.commit()
    return {'fiscal_year': doc.name, 'status': 'created', 'range': f'{year}-01-01..{year}-12-31'}


def _ensure_vat_account() -> str:
    name = f'VAT 15 - {ABBR}'
    if frappe.db.exists('Account', name):
        return name

    parent = frappe.db.get_value(
        'Account', {'company': COMPANY, 'account_type': 'Tax', 'is_group': 1}, 'name'
    ) or frappe.db.get_value('Account', {'company': COMPANY, 'root_type': 'Liability', 'is_group': 1}, 'name')
    if not parent:
        frappe.throw(f'No liability parent account found for {COMPANY}; is its chart of accounts built?')

    doc = frappe.new_doc('Account')
    doc.account_name = 'VAT 15'
    doc.parent_account = parent
    doc.company = COMPANY
    doc.account_type = 'Tax'
    doc.tax_rate = 15
    doc.insert(ignore_permissions=True)
    return doc.name


def _ensure_tax_template(vat_account: str) -> str:
    existing = frappe.db.get_value(
        'Sales Taxes and Charges Template', {'company': COMPANY, 'is_default': 1}, 'name'
    )
    if existing:
        return existing

    doc = frappe.new_doc('Sales Taxes and Charges Template')
    doc.title = 'KSA VAT 15'
    doc.company = COMPANY
    doc.is_default = 1
    doc.append(
        'taxes',
        {
            'charge_type': 'On Net Total',
            'account_head': vat_account,
            'description': 'VAT 15%',
            'rate': 15,
        },
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _ensure_item_tax_templates(vat_account: str) -> list:
    """Standard and zero-rated templates, so mixed-rate invoices can be exercised."""
    names = []
    for title, rate in (('KSA VAT 15', 15), ('KSA Zero Rated', 0)):
        name = f'{title} - {ABBR}'
        if not frappe.db.exists('Item Tax Template', name):
            doc = frappe.new_doc('Item Tax Template')
            doc.title = title
            doc.company = COMPANY
            doc.append('taxes', {'tax_type': vat_account, 'tax_rate': rate})
            doc.insert(ignore_permissions=True)
            name = doc.name
        names.append(name)
    return names


def _ensure_items() -> list:
    group = frappe.db.get_value('Item Group', {'is_group': 0}, 'name')
    for code, label in ((ITEM_STANDARD, 'Implementation services'), (ITEM_ZERO, 'Export services')):
        if frappe.db.exists('Item', code):
            continue
        doc = frappe.new_doc('Item')
        doc.item_code = code
        doc.item_name = label
        doc.item_group = group
        doc.stock_uom = 'Nos'
        # Non-stock: an invoice-only integration has no inventory to draw from.
        doc.is_stock_item = 0
        doc.is_sales_item = 1
        doc.insert(ignore_permissions=True)
    return [ITEM_STANDARD, ITEM_ZERO]


def _ensure_company_address() -> str:
    """ZATCA Phase 1 Business Settings requires a linked Address."""
    title = f'{COMPANY} HQ'
    existing = frappe.db.get_value('Address', {'address_title': title}, 'name')
    if existing:
        return existing

    doc = frappe.new_doc('Address')
    doc.address_title = title
    doc.address_type = 'Billing'
    doc.address_line1 = 'Olaya Street'
    doc.city = 'Riyadh'
    doc.pincode = '12613'
    doc.country = 'Saudi Arabia'
    if doc.meta.get_field('custom_building_number'):
        doc.custom_building_number = '4521'
        doc.custom_area = 'Al Murabba'
    doc.append('links', {'link_doctype': 'Company', 'link_name': COMPANY})
    doc.insert(ignore_permissions=True)
    return doc.name


def _ensure_phase_1(address: str):
    """ZATCA Phase 1 Business Settings.

    `status` options are Active / Disabled (not "Enabled"), `address` is mandatory, and
    the doctype refuses to save while Phase 2 settings exist for the same company.
    """
    if not frappe.db.exists('DocType', 'ZATCA Phase 1 Business Settings'):
        return 'ksa_compliance not installed - skipped'

    if frappe.db.exists('ZATCA Business Settings', {'company': COMPANY}):
        return 'Phase 2 settings already exist for this company - Phase 1 skipped'

    existing = frappe.db.get_value('ZATCA Phase 1 Business Settings', {'company': COMPANY}, 'name')
    if existing:
        frappe.db.set_value(
            'ZATCA Phase 1 Business Settings',
            existing,
            {'status': 'Active', 'vat_registration_number': SELLER_VAT, 'address': address},
        )
        return existing

    doc = frappe.new_doc('ZATCA Phase 1 Business Settings')
    doc.company = COMPANY
    doc.vat_registration_number = SELLER_VAT
    doc.address = address
    doc.status = 'Active'
    field = doc.meta.get_field('type_of_transaction')
    if field:
        options = [o for o in (field.options or '').split('\n') if o]
        if options:
            doc.type_of_transaction = options[-1]
    doc.insert(ignore_permissions=True)
    return doc.name


def _ensure_payment_modes() -> dict:
    """Create Modes of Payment carrying a ZATCA payment means code.

    erpnext's own fixtures cannot create these on a ksa_compliance site: that app adds
    ``custom_zatca_payment_means_code`` to Mode of Payment as **mandatory**, so the
    wizard's records fail with
    ``MandatoryError: [Mode of Payment, Bank Draft]: custom_zatca_payment_means_code``
    and the table is left empty.

    Without at least one Mode of Payment, a payload carrying ``payment_mode`` cannot be
    accepted, so POS / simplified-invoice testing is blocked.
    """
    meta = frappe.get_meta('Mode of Payment')
    has_zatca_field = bool(meta.get_field('custom_zatca_payment_means_code'))
    created = []

    for name, mode_type, code in PAYMENT_MODES:
        if frappe.db.exists('Mode of Payment', name):
            # Backfill the code on an existing mode that predates ksa_compliance.
            if has_zatca_field and not frappe.db.get_value(
                'Mode of Payment', name, 'custom_zatca_payment_means_code'
            ):
                frappe.db.set_value('Mode of Payment', name, 'custom_zatca_payment_means_code', code)
                created.append(f'{name} (code backfilled)')
            continue

        doc = frappe.new_doc('Mode of Payment')
        doc.mode_of_payment = name
        doc.type = mode_type
        doc.enabled = 1
        if has_zatca_field:
            doc.custom_zatca_payment_means_code = code
        doc.insert(ignore_permissions=True)
        created.append(f'{name}={code}')

    return {'created': created, 'total': frappe.db.count('Mode of Payment')}


def _ensure_b2c_customer() -> str:
    if not frappe.db.exists('Customer', CUSTOMER_B2C):
        doc = frappe.new_doc('Customer')
        doc.customer_name = CUSTOMER_B2C
        doc.customer_type = 'Individual'
        doc.insert(ignore_permissions=True)
    return CUSTOMER_B2C


def _configure_settings() -> dict:
    group = frappe.db.get_value('Item Group', {'is_group': 0}, 'name')
    settings = frappe.get_single('ZATCA API Settings')

    settings.enabled = 1
    settings.default_company = COMPANY
    settings.auto_submit_invoices = 1
    settings.submit_mode = 'Immediate'
    settings.update_existing_drafts = 1
    settings.allow_amend_submitted = 0
    settings.create_missing_customers = 1
    settings.create_missing_items = 1
    settings.create_missing_uoms = 1
    settings.create_missing_projects = 0
    settings.default_customer_type = 'Company'
    settings.default_customer_group = frappe.db.get_value('Customer Group', {'is_group': 0}, 'name')
    settings.default_territory = frappe.db.get_value('Territory', {'is_group': 0}, 'name')
    settings.default_item_group = group
    settings.default_uom = 'Nos'
    settings.default_country = 'Saudi Arabia'
    settings.enforce_b2b_address = 1
    settings.parse_address_display = 1
    settings.zatca_phase = 'Auto'
    settings.include_qr_png = 1
    settings.include_signed_xml = 0
    settings.wait_for_zatca_seconds = 0
    settings.require_shared_secret = 0
    settings.pull_enabled = 0
    settings.log_requests = 1
    settings.log_payloads = 1
    settings.log_retention_days = 30
    settings.flags.ignore_permissions = True
    settings.save()

    return {
        'default_company': settings.default_company,
        'auto_submit': cint(settings.auto_submit_invoices),
        'enforce_b2b_address': cint(settings.enforce_b2b_address),
        'zatca_phase': settings.zatca_phase,
    }


def _ensure_api_user() -> dict:
    """Create the integration user and issue a fresh key pair.

    The secret is only recoverable at creation time, so it is returned here and must be
    stored by whoever runs this. Re-running issues a new secret and invalidates the old.
    """
    if not frappe.db.exists('User', API_USER):
        doc = frappe.new_doc('User')
        doc.email = API_USER
        doc.first_name = 'ZATCA'
        doc.last_name = 'API'
        doc.send_welcome_email = 0
        doc.flags.no_welcome_mail = True
        for role in ('Accounts Manager', 'Accounts User', 'System Manager'):
            if frappe.db.exists('Role', role):
                doc.append('roles', {'role': role})
        doc.insert(ignore_permissions=True)

    api_key = cstr(frappe.db.get_value('User', API_USER, 'api_key'))
    if not api_key:
        api_key = frappe.generate_hash(length=15)
        frappe.db.set_value('User', API_USER, 'api_key', api_key)

    api_secret = frappe.generate_hash(length=15)
    frappe.utils.password.set_encrypted_password('User', API_USER, api_secret, 'api_secret')

    return {'user': API_USER, 'api_key': api_key, 'api_secret': api_secret}
