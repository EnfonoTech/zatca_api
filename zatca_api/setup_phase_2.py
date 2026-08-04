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

# ZATCA's sandbox hands back a FIXED certificate issued to its own test taxpayer, then
# refuses any invoice whose seller VAT differs from it:
#   errorMessages: certificate-permissions
#   'User only allowed to use the vat number that exists in the authentication certificate'
# Decoded from the certificate ZATCA actually returned:
#   subject = C=SA, O=Maximum Speed Tech Supply LTD, OU=Riyadh Branch,
#             CN=TST-886431145-399999999900003
#   UID     = 399999999900003
# So on sandbox the seller identity is ZATCA's, not the client's. Simulation and
# Production use the real taxpayer's own VAT.
SANDBOX_VAT = '399999999900003'
SANDBOX_CRN = '886431145'


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
    Arabic text. The per-template mapping lives in
    :data:`zatca_api.setup_test_site.ITEM_TAX_TEMPLATES`, which is the single place it is
    declared; anything not in there is reported for manual mapping rather than guessed.

    Tax Category records are setup-wizard fixtures, so a fresh site has none.
    """

    standard = 'Standard rate'
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

    # Item Tax Templates carry their own per-line category. Backfill from the seeder's
    # explicit table rather than guessing from the name: an unrecognised template stamped
    # 'Standard rate' would file zero-rated or exempt lines as standard -- right arithmetic,
    # wrong VAT category, and ZATCA accepts it, so nothing would ever surface the mistake.
    item_meta = frappe.get_meta('Item Tax Template')
    if item_meta.get_field('custom_zatca_item_tax_category'):
        from zatca_api.setup_test_site import ITEM_TAX_TEMPLATES

        known = {f'{title} - {ABBR}': category for title, (_, category) in ITEM_TAX_TEMPLATES.items()}
        for name in frappe.get_all('Item Tax Template', filters={'company': COMPANY}, pluck='name'):
            if frappe.db.get_value('Item Tax Template', name, 'custom_zatca_item_tax_category'):
                continue
            category = known.get(name)
            if not category:
                created.setdefault('needs_manual_category', []).append(name)
                continue
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
    # On sandbox the VAT must match ZATCA's fixed certificate, or every invoice comes
    # back rejected with certificate-permissions.
    is_sandbox = fatoora_server == SANDBOX
    seller_vat = SANDBOX_VAT if is_sandbox else SELLER_VAT
    doc.vat_registration_number = seller_vat

    # These identify the EGS unit to ZATCA and both have enforced formats.
    #
    # company_unit becomes the CSR's Organization Unit (OU). The ZATCA CLI rejects a
    # free-text name with: "Organization Unit Name must be the 10-digit TIN number of the
    # individual group member whose device is being onboarded". For a group VAT
    # registration that is the member's TIN, which is the first 10 digits of the 15-digit
    # VAT number.
    doc.company_unit = seller_vat[:10]
    # ZATCA's expected serial format: 1-<solution>|2-<model/version>|3-<serial>
    doc.company_unit_serial = f'1-Enfono|2-zatca_api|3-{ABBR}-TEST-001'
    doc.company_category = 'Services'

    doc.fatoora_server = fatoora_server
    doc.type_of_business_transactions = 'Let the system decide (both)'
    doc.enable_zatca_integration = 1
    doc.sync_with_zatca = 'Live'
    doc.status = 'Active'

    # ZATCA warns BR-KSA-08 when the seller carries no additional identifier: 'The seller
    # identification (BT-29) must exist only once with one of the scheme ID (CRN, MOM,
    # MLS, SAG, OTH, 700)'. A CRN satisfies it.
    if not doc.other_ids:
        doc.append('other_ids', {'type_name': 'CRN', 'type_code': 'CRN', 'value': SANDBOX_CRN})

    doc.cli_setup = 'Manual'
    doc.zatca_cli_path = cli.get('cli_path')
    doc.java_home = cli.get('jre_path')

    # Validate every generated XML against the UBL schema on a test site: it turns a
    # silent ZATCA rejection into a local error naming the failing rule.
    doc.validate_generated_xml = 1
    doc.block_invoice_on_invalid_xml = 0

    # A changed seller VAT invalidates the CSR its CSID was issued against, so clear the
    # onboarding and let it redo rather than signing with a mismatched identity.
    previous_vat = (
        cstr(frappe.db.get_value('ZATCA Business Settings', doc.name, 'vat_registration_number'))
        if existing
        else ''
    )
    if previous_vat and previous_vat != seller_vat:
        doc.compliance_request_id = None
        doc.production_request_id = None
        doc.production_security_token = None

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


# --------------------------------------------------------------------------- simulation

SIMULATION = 'Simulation'


def simulation_preflight(vat: str | None = None, company: str = COMPANY) -> dict:
    """Report whether a Simulation onboarding would be able to proceed. Calls nothing.

    Simulation is the stage before Production and behaves like it in every way that
    matters, which is exactly why it cannot be faked:

    * it issues a certificate against a **real taxpayer VAT** in ZATCA's registry, so a
      placeholder number is rejected;
    * it needs an **OTP generated in the ZATCA Fatoora portal** by someone signed in to
      that taxpayer's account, and the OTP is short-lived;
    * unlike sandbox it generates its **own private key** rather than using the one
      bundled with ksa_compliance, and the CSR is built with the CLI's simulation flag,
      which selects a different certificate template.

    Run this first; it tells you what is missing without touching ZATCA.
    """
    checks = {}
    settings_name = frappe.db.get_value('ZATCA Business Settings', {'company': company}, 'name')
    checks['business_settings'] = settings_name or 'MISSING - run run() first'

    if settings_name:
        doc = frappe.get_doc('ZATCA Business Settings', settings_name)
        checks['cli_path_ok'] = _exists(doc.zatca_cli_path)
        checks['java_home_ok'] = _exists(doc.java_home)
        checks['current_server'] = doc.fatoora_server
        checks['current_vat'] = doc.vat_registration_number
        checks['company_address'] = doc.company_address
        checks['seller_other_ids'] = [(r.type_code, r.value) for r in doc.other_ids]
        checks['tax_category_set'] = bool(
            frappe.db.get_value(
                'Sales Taxes and Charges Template', {'company': company, 'is_default': 1}, 'tax_category'
            )
        )

    target_vat = cstr(vat or '').strip()
    if not target_vat:
        checks['vat'] = 'NOT SUPPLIED - Simulation needs the real taxpayer VAT'
    else:
        checks['vat'] = _validate_vat(target_vat, raise_on_error=False)

    checks['otp'] = 'must be generated in the ZATCA Fatoora portal at run time'
    checks['simulation_url'] = 'https://gw-fatoora.zatca.gov.sa/e-invoicing/simulation/'
    checks['ready'] = bool(
        settings_name
        and checks.get('cli_path_ok')
        and checks.get('java_home_ok')
        and target_vat
        and checks.get('vat') == 'ok'
    )

    print('=' * 70)
    print('Simulation preflight')
    print('=' * 70)
    for key, value in checks.items():
        print(f'  {key:22s} {value}')
    print('=' * 70)
    if not checks['ready']:
        print('NOT READY. Fix the above, then run:')
        print('  from zatca_api.setup_phase_2 import simulation')
        print("  simulation(vat='<real 15-digit VAT>', otp='<OTP from Fatoora portal>')")
    return checks


def _validate_vat(vat: str, raise_on_error: bool = True) -> str:
    """ZATCA VAT numbers are 15 digits starting and ending with 3."""
    problems = []
    if not vat.isdigit() or len(vat) != 15:
        problems.append('must be exactly 15 digits')
    elif not (vat.startswith('3') and vat.endswith('3')):
        problems.append('must start and end with 3')
    if vat in (SANDBOX_VAT, SELLER_VAT):
        problems.append(
            'this is a test/placeholder VAT; Simulation issues a certificate against a '
            'real taxpayer and will reject it'
        )

    if not problems:
        return 'ok'
    message = f'Invalid VAT {vat!r}: ' + '; '.join(problems)
    if raise_on_error:
        frappe.throw(message)
    return message


def simulation(vat: str, otp: str, company: str = COMPANY) -> dict:
    """Onboard the company against ZATCA's **Simulation** environment.

    ``vat`` is the real taxpayer VAT registration number. ``otp`` is generated in the
    ZATCA Fatoora portal, under the taxpayer's own account, and expires quickly -- so it
    has to be passed in at the moment of running, never stored.

    Switching a company from Sandbox to Simulation invalidates the sandbox CSID, because
    the certificate is issued for a different environment and a different identity. The
    onboarding fields are therefore cleared and reissued.
    """
    _validate_vat(cstr(vat).strip())
    if not cstr(otp).strip():
        frappe.throw(
            'An OTP is required. Generate it in the ZATCA Fatoora portal for this '
            'taxpayer, then pass it in immediately -- it is short-lived.'
        )

    settings_name = frappe.db.get_value('ZATCA Business Settings', {'company': company}, 'name')
    if not settings_name:
        frappe.throw(f'No ZATCA Business Settings for {company}; run zatca_api.setup_phase_2.run first.')

    doc = frappe.get_doc('ZATCA Business Settings', settings_name)
    previous = {'server': doc.fatoora_server, 'vat': doc.vat_registration_number}

    doc.fatoora_server = SIMULATION
    doc.vat_registration_number = cstr(vat).strip()
    doc.company_unit = cstr(vat).strip()[:10]
    # The sandbox CSID was issued for a different environment and identity.
    doc.compliance_request_id = None
    doc.production_request_id = None
    doc.production_security_token = None
    doc.flags.ignore_permissions = True
    doc.save()
    frappe.db.commit()

    result = {'previous': previous, 'now': {'server': SIMULATION, 'vat': doc.vat_registration_number}}
    result['onboarding'] = _onboard(settings_name, otp)
    frappe.db.commit()

    print('=' * 70)
    print('Simulation onboarding')
    print('=' * 70)
    for key, value in result.items():
        print(f'  {key:14s} {value}')
    print('=' * 70)
    return result
