# zatca_api/setup_phase_2.py
# Copyright (c) 2026, Enfono Technologies and contributors
"""Provision ZATCA **Phase 2** on a test site, against ZATCA's sandbox.

    bench --site <site> execute zatca_api.setup_phase_2.run
    # or, since bench execute is unreliable for app modules:
    echo "from zatca_api.setup_phase_2 import run; run()" | bench --site <site> console

Companion to :mod:`zatca_api.setup_test_site`, which sets up Phase 1. Run that first.

**All cryptography and every ZATCA API call belongs to `ksa_compliance`.** This module
only fills in its configuration and calls its own onboarding methods in the right order,
so nothing here reimplements the compliance flow.

What Phase 2 needs that Phase 1 does not:

* the **ZATCA CLI** (Java) for signing -- install it with
  ``ksa_compliance.zatca_cli.setup(None, None)``, which downloads Temurin JRE 11 and
  lavaloon's zatca-cli into the site's ``zatca-tools/``;
* a **CSID** obtained from ZATCA by submitting a CSR;
* ``ZATCA Business Settings`` for the company, which **cannot coexist with
  ZATCA Phase 1 Business Settings** for that same company -- Phase 1 settings are
  removed here, deliberately.

On ``fatoora_server = Sandbox`` ZATCA returns a *fixed* certificate that pairs with a
private key bundled in `ksa_compliance`, so signing works without real credentials. Per
the doctype's own description, only *Simulation* needs an OTP from the Fatoora portal;
sandbox does not, so the OTP below is a placeholder.

Nothing here is for production. A real go-live needs Simulation then Production, each
with its own portal-issued OTP, performed by whoever owns the taxpayer account.
"""

import frappe
from frappe.utils import cint, cstr

from zatca_api.setup_test_site import ABBR, COMPANY, SELLER_VAT

# Sandbox ignores the OTP; a value is still required by the API signature.
SANDBOX_OTP = '123456'
SANDBOX = 'Sandbox'


def run(otp: str = SANDBOX_OTP, fatoora_server: str = SANDBOX):
    """Install the CLI if needed, create Business Settings, then onboard against sandbox."""
    if fatoora_server not in ('Sandbox', 'Simulation'):
        frappe.throw(
            f'Refusing to target {fatoora_server!r}. This module is for test environments '
            f'only; Production onboarding must be done by the taxpayer account owner.'
        )

    result = {'fatoora_server': fatoora_server}
    result['cli'] = _ensure_cli()
    result['phase_1_removed'] = _remove_phase_1()
    result['tax_categories'] = _ensure_tax_categories()
    result['settings'] = _ensure_business_settings(result['cli'], fatoora_server)
    result['onboarding'] = _onboard(result['settings'], otp)

    frappe.db.commit()

    print('=' * 70)
    print('ZATCA Phase 2 provisioning')
    print('=' * 70)
    for key, value in result.items():
        print(f'  {key:18s} {value}')
    print('=' * 70)
    return result


def _ensure_cli() -> dict:
    """Install the ZATCA CLI + JRE if absent, and return **absolute** paths to both.

    Two things this has to get right:

    * Always return ``jre_path`` as well as ``cli_path``. Returning only the CLI leaves
      ``java_home`` unset and signing dies with
      ``ERROR: JAVA_HOME is not set and no 'java' command could be found in your PATH``.
    * Resolve both to absolute paths. ``get_zatca_tool_path()`` is site-relative
      (``./<site>/zatca-tools/...``), which resolves differently depending on the working
      directory of whatever process runs the CLI -- a background worker is not the bench
      root.

    Paths are per-site, so every site needs its own copy.
    """
    import glob
    import os

    from ksa_compliance import zatca_cli

    tool_path = os.path.abspath(zatca_cli.get_zatca_tool_path())

    def discover() -> tuple:
        cli = sorted(glob.glob(os.path.join(tool_path, 'zatca-cli-*', 'bin', 'zatca-cli')))
        jre = sorted(glob.glob(os.path.join(tool_path, 'jdk-*jre')))
        return (os.path.abspath(cli[-1]) if cli else None, os.path.abspath(jre[-1]) if jre else None)

    cli_path, jre_path = discover()
    if cli_path and jre_path:
        return {'status': 'already installed', 'cli_path': cli_path, 'jre_path': jre_path}

    zatca_cli.setup(None, None)
    cli_path, jre_path = discover()
    if not (cli_path and jre_path):
        frappe.throw(
            f'ZATCA CLI setup did not produce both a CLI and a JRE under {tool_path} '
            f'(cli={cli_path}, jre={jre_path})'
        )
    return {'status': 'downloaded', 'cli_path': cli_path, 'jre_path': jre_path}


def _exists(path: str) -> bool:
    import os

    return bool(path) and os.path.exists(path)


def _remove_phase_1() -> str:
    """Drop Phase 1 settings for the company.

    ZATCAPhase1BusinessSettings.validate refuses to save while Phase 2 settings exist for
    the same company, and the reverse pairing is equally unsupported. A company is one
    phase or the other.
    """
    names = frappe.get_all('ZATCA Phase 1 Business Settings', filters={'company': COMPANY}, pluck='name')
    if not names:
        return 'none present'
    for name in names:
        frappe.delete_doc('ZATCA Phase 1 Business Settings', name, force=True, ignore_permissions=True)
    frappe.db.commit()
    return f'removed {names}'


def _ensure_tax_categories() -> dict:
    """Give every tax template a ZATCA VAT category, which Phase 2 requires.

    Phase 1 never needs this. On Phase 2, submitting an invoice fails with
    ``Please set Tax Category on Sales Taxes and Charges Template <name>`` because
    ksa_compliance has to put a VAT category code on every XML line.

    ``map_tax_category`` reads ``Tax Category.custom_zatca_category`` (or
    ``Item Tax Template.custom_zatca_item_tax_category``) and maps it to the ZATCA
    code: S standard, E exempt, Z zero-rated, O outside scope. Anything other than
    'Standard rate' is stored as ``"<category> || <reason>"``, and the reason half must
    match ksa_compliance's own list exactly -- it resolves to a VATEX-SA-* code and its
    Arabic text. 'Export of goods' is used for the zero-rated template here.

    Tax Category records are setup-wizard fixtures, so a fresh site has none.
    """

    standard = 'Standard rate'
    zero_rated = 'Zero rated goods || Export of goods'
    created = {}

    meta = frappe.get_meta('Tax Category')
    has_field = bool(meta.get_field('custom_zatca_category'))

    if not frappe.db.exists('Tax Category', 'Standard'):
        doc = frappe.new_doc('Tax Category')
        doc.title = 'Standard'
        if has_field:
            doc.custom_zatca_category = standard
        doc.insert(ignore_permissions=True)
        created['tax_category'] = doc.name
    elif has_field and not frappe.db.get_value('Tax Category', 'Standard', 'custom_zatca_category'):
        frappe.db.set_value('Tax Category', 'Standard', 'custom_zatca_category', standard)
        created['tax_category'] = 'Standard (backfilled)'

    # Sales Taxes and Charges Template needs the Tax Category link.
    for name in frappe.get_all(
        'Sales Taxes and Charges Template', filters={'company': COMPANY}, pluck='name'
    ):
        if not frappe.db.get_value('Sales Taxes and Charges Template', name, 'tax_category'):
            frappe.db.set_value('Sales Taxes and Charges Template', name, 'tax_category', 'Standard')
            created.setdefault('templates', []).append(name)

    # Item Tax Templates carry their own per-line category.
    item_meta = frappe.get_meta('Item Tax Template')
    if item_meta.get_field('custom_zatca_item_tax_category'):
        for name in frappe.get_all('Item Tax Template', filters={'company': COMPANY}, pluck='name'):
            if frappe.db.get_value('Item Tax Template', name, 'custom_zatca_item_tax_category'):
                continue
            category = zero_rated if 'Zero' in name else standard
            frappe.db.set_value('Item Tax Template', name, 'custom_zatca_item_tax_category', category)
            created.setdefault('item_tax_templates', []).append(f'{name} -> {category}')

    frappe.db.commit()
    return created or {'status': 'already configured'}


def _ensure_business_settings(cli: dict, fatoora_server: str) -> str:
    address = frappe.db.get_value('Address', {'address_title': f'{COMPANY} HQ'}, 'name')
    if not address:
        frappe.throw(f'No company address for {COMPANY}; run zatca_api.setup_test_site first.')

    existing = frappe.db.get_value('ZATCA Business Settings', {'company': COMPANY}, 'name')
    doc = (
        frappe.get_doc('ZATCA Business Settings', existing)
        if existing
        else frappe.new_doc('ZATCA Business Settings')
    )

    doc.company = COMPANY
    doc.company_address = address
    doc.currency = frappe.db.get_value('Company', COMPANY, 'default_currency') or 'SAR'
    doc.country = frappe.db.get_value('Company', COMPANY, 'country') or 'Saudi Arabia'
    doc.seller_name = COMPANY
    doc.vat_registration_number = SELLER_VAT

    # These identify the EGS unit to ZATCA and both have enforced formats.
    #
    # company_unit becomes the CSR's Organization Unit (OU). The ZATCA CLI rejects a
    # free-text name with: "Organization Unit Name must be the 10-digit TIN number of the
    # individual group member whose device is being onboarded". For a group VAT
    # registration that is the member's TIN, which is the first 10 digits of the 15-digit
    # VAT number.
    doc.company_unit = SELLER_VAT[:10]
    # ZATCA's expected serial format: 1-<solution>|2-<model/version>|3-<serial>
    doc.company_unit_serial = f'1-Enfono|2-zatca_api|3-{ABBR}-TEST-001'
    doc.company_category = 'Services'

    doc.fatoora_server = fatoora_server
    doc.type_of_business_transactions = 'Let the system decide (both)'
    doc.enable_zatca_integration = 1
    doc.sync_with_zatca = 'Live'
    doc.status = 'Active'

    doc.cli_setup = 'Manual'
    doc.zatca_cli_path = cli.get('cli_path')
    doc.java_home = cli.get('jre_path')

    # Validate every generated XML against the UBL schema on a test site: it turns a
    # silent ZATCA rejection into a local error naming the failing rule.
    doc.validate_generated_xml = 1
    doc.block_invoice_on_invalid_xml = 0

    doc.flags.ignore_permissions = True
    doc.save()
    frappe.db.commit()
    return doc.name


def _onboard(settings_name: str, otp: str) -> dict:
    """Generate a CSR and obtain compliance + production CSIDs from ZATCA.

    Both calls go to ZATCA's sandbox host
    (``https://gw-fatoora.zatca.gov.sa/e-invoicing/developer-portal/``). Failures are
    returned rather than raised so the caller sees how far it got.
    """
    doc = frappe.get_doc('ZATCA Business Settings', settings_name)
    out = {'server_url': doc.fatoora_server_url}

    if doc.compliance_request_id:
        out['compliance'] = f'already onboarded ({doc.compliance_request_id})'
    else:
        try:
            doc.onboard(otp)
            doc.reload()
            out['compliance'] = f'compliance_request_id={doc.compliance_request_id}'
        except Exception as exc:
            out['compliance'] = f'FAILED: {cstr(exc)[:400]}'
            return out

    if doc.production_request_id:
        out['production'] = f'already issued ({doc.production_request_id})'
    else:
        try:
            doc.get_production_csid(otp)
            doc.reload()
            out['production'] = f'production_request_id={doc.production_request_id}'
        except Exception as exc:
            out['production'] = f'FAILED: {cstr(exc)[:400]}'

    out['has_production_token'] = bool(doc.production_security_token)
    out['counting_settings'] = frappe.db.get_value(
        'ZATCA Invoice Counting Settings',
        {'business_settings_reference': settings_name},
        ['name', 'invoice_counter'],
        as_dict=True,
    )
    return out


def status() -> dict:
    """Report where Phase 2 provisioning currently stands, without changing anything."""
    name = frappe.db.get_value('ZATCA Business Settings', {'company': COMPANY}, 'name')
    if not name:
        return {'business_settings': None}

    doc = frappe.get_doc('ZATCA Business Settings', name)
    return {
        'business_settings': name,
        'fatoora_server': doc.fatoora_server,
        'status': doc.status,
        'enabled': cint(doc.enable_zatca_integration),
        'sync_with_zatca': doc.sync_with_zatca,
        'cli_path_exists': _exists(doc.zatca_cli_path),
        'java_home_exists': _exists(doc.java_home),
        'has_csr': bool(doc.csr),
        'compliance_request_id': doc.compliance_request_id,
        'production_request_id': doc.production_request_id,
        'has_production_token': bool(doc.production_security_token),
        'phase_1_still_present': frappe.db.count('ZATCA Phase 1 Business Settings', {'company': COMPANY}),
    }
