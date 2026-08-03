# zatca_api/tests/test_api_v1.py
# Copyright (c) 2026, Enfono Technologies and contributors
"""End-to-end tests for the v1 REST surface.

These write real documents, so they need a site with erpnext installed. Each test
class sets up its own company/customer/item so it does not depend on demo data.

The behaviours asserted here are the defects this app was written to fix:
idempotency, submitted-document immutability, mixed-rate tax correctness, and
never exposing invoice data without authentication.
"""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, cint, flt, today

from zatca_api.api import v1
from zatca_api.services import invoice as invoice_service

TEST_COMPANY = '_ZAPI Test Co'
TEST_ABBR = 'ZAPI'
TEST_CUSTOMER = '_ZAPI Customer'
TEST_ITEM = '_ZAPI-SERVICE'
TEST_ITEM_ZERO = '_ZAPI-EXPORT'


def _ensure_company():
    if frappe.db.exists('Company', TEST_COMPANY):
        return TEST_COMPANY

    company = frappe.new_doc('Company')
    company.company_name = TEST_COMPANY
    company.abbr = TEST_ABBR
    company.default_currency = 'SAR'
    company.country = 'Saudi Arabia'
    company.insert(ignore_permissions=True)
    return company.name


def _ensure_item(item_code):
    if frappe.db.exists('Item', item_code):
        return item_code

    item = frappe.new_doc('Item')
    item.item_code = item_code
    item.item_name = item_code
    item.item_group = frappe.db.get_value('Item Group', {'is_group': 0}, 'name')
    item.stock_uom = 'Nos'
    item.is_stock_item = 0
    item.is_sales_item = 1
    item.insert(ignore_permissions=True)
    return item.name


def ensure_vat_account():
    """A 15% output VAT account for the test company."""
    name = f'VAT 15 - {TEST_ABBR}'
    if frappe.db.exists('Account', name):
        return name

    parent = frappe.db.get_value(
        'Account', {'company': TEST_COMPANY, 'account_type': 'Tax', 'is_group': 1}, 'name'
    ) or frappe.db.get_value(
        'Account', {'company': TEST_COMPANY, 'root_type': 'Liability', 'is_group': 1}, 'name'
    )
    account = frappe.new_doc('Account')
    account.account_name = 'VAT 15'
    account.parent_account = parent
    account.company = TEST_COMPANY
    account.account_type = 'Tax'
    account.tax_rate = 15
    account.insert(ignore_permissions=True)
    return account.name


def _ensure_default_tax_template():
    """A default Sales Taxes and Charges Template for the test company.

    Required because ksa_compliance's Sales Invoice validate hook rejects an
    invoice with an empty taxes table once ZATCA is enabled for the company, and
    this app surfaces that as an explicit error rather than letting the hook raise
    a vague one. Every real KSA company has such a template.
    """
    existing = frappe.db.get_value(
        'Sales Taxes and Charges Template', {'company': TEST_COMPANY, 'is_default': 1}, 'name'
    )
    if existing:
        return existing

    template = frappe.new_doc('Sales Taxes and Charges Template')
    template.title = 'KSA VAT 15'
    template.company = TEST_COMPANY
    template.is_default = 1
    template.append(
        'taxes',
        {
            'charge_type': 'On Net Total',
            'account_head': ensure_vat_account(),
            'description': 'VAT 15%',
            'rate': 15,
        },
    )
    template.insert(ignore_permissions=True)
    return template.name


def _ensure_customer():
    if frappe.db.exists('Customer', TEST_CUSTOMER):
        return TEST_CUSTOMER

    customer = frappe.new_doc('Customer')
    customer.customer_name = TEST_CUSTOMER
    customer.customer_type = 'Company'
    customer.insert(ignore_permissions=True)
    return customer.name


def _configure_settings(**overrides):
    settings = frappe.get_single('ZATCA API Settings')
    settings.enabled = 1
    settings.default_company = TEST_COMPANY
    settings.create_missing_customers = 1
    settings.create_missing_items = 1
    settings.create_missing_uoms = 1
    settings.auto_submit_invoices = 0  # keep tests fast and independent of ZATCA setup
    settings.submit_mode = 'Immediate'
    settings.update_existing_drafts = 1
    settings.log_requests = 0
    settings.wait_for_zatca_seconds = 0
    settings.field_mappings = []
    for key, value in overrides.items():
        setattr(settings, key, value)
    settings.flags.ignore_permissions = True
    settings.save()
    frappe.clear_cache(doctype='ZATCA API Settings')
    return settings


def _payload(external_id, **overrides):
    payload = {
        'external_id': external_id,
        'customer': TEST_CUSTOMER,
        'company': TEST_COMPANY,
        'posting_date': today(),
        'due_date': add_days(today(), 30),
        'items': [{'item_code': TEST_ITEM, 'qty': 2, 'rate': 100}],
    }
    payload.update(overrides)
    return payload


class ZATCAAPITestCase(FrappeTestCase):
    """Base class owning the shared fixtures.

    The shared company/item/customer are committed in ``setUpClass`` on purpose.
    FrappeTestCase rolls the transaction back at class teardown, so without a
    commit here the very first rollback would also erase the fixtures the
    remaining tests depend on. Per-test data is left to that rollback.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        frappe.set_user('Administrator')
        _ensure_company()
        _ensure_item(TEST_ITEM)
        _ensure_item(TEST_ITEM_ZERO)
        _ensure_customer()
        _ensure_default_tax_template()
        _configure_settings()
        frappe.db.commit()

    def setUp(self):
        frappe.set_user('Administrator')

    def tearDown(self):
        frappe.set_user('Administrator')
        # Per-test isolation. Safe because setUpClass committed the shared
        # fixtures: this rollback discards only what the test itself wrote, so
        # tests cannot leak state into one another and stay order-independent.
        frappe.db.rollback()
        frappe.clear_cache(doctype='ZATCA API Settings')


class TestCreateInvoice(ZATCAAPITestCase):
    def test_creates_invoice_and_returns_envelope(self):
        response = v1.create_invoice(**_payload('ZAPI-E2E-001'))

        self.assertTrue(response['success'], msg=response.get('errors'))
        self.assertIn('request_id', response)
        self.assertEqual(response['errors'], [])

        data = response['data']
        self.assertEqual(data['action'], 'created')
        self.assertFalse(data['duplicate'])

        invoice = data['invoice']
        self.assertEqual(invoice['external_id'], 'ZAPI-E2E-001')
        self.assertEqual(invoice['customer'], TEST_CUSTOMER)
        self.assertEqual(invoice['docstatus'], 0)
        self.assertEqual(invoice['net_total'], 200.0)
        self.assertEqual(len(invoice['items']), 1)
        self.assertEqual(invoice['items'][0]['qty'], 2.0)

        # The ZATCA block is always present, even when unavailable, so a caller can
        # branch on the flag instead of on a missing key.
        self.assertIn('zatca', data)
        self.assertIn('available', data['zatca'])

    def test_draft_invoice_reports_zatca_unavailable_with_a_reason(self):
        response = v1.create_invoice(**_payload('ZAPI-E2E-DRAFT', submit=0))
        zatca_block = response['data']['zatca']

        self.assertFalse(zatca_block['available'])
        self.assertIn('Draft', zatca_block['reason'])

    def test_external_id_is_persisted_on_the_invoice(self):
        response = v1.create_invoice(**_payload('ZAPI-E2E-002'))
        name = response['data']['invoice']['invoice']

        stored = frappe.db.get_value('Sales Invoice', name, invoice_service.EXTERNAL_ID_FIELD)
        self.assertEqual(stored, 'ZAPI-E2E-002')

    def test_invoice_name_follows_the_naming_series_not_the_external_id(self):
        """Documents the naming behaviour that makes name-based dedup impossible.

        frappe.model.naming.set_new_name assigns doc.name = None for any DocType
        with a naming series, so an external number can never become the document
        name. This is why dedup keys on the indexed custom field instead.
        """
        response = v1.create_invoice(**_payload('ZAPI-E2E-NAMING'))
        name = response['data']['invoice']['invoice']
        self.assertNotEqual(name, 'ZAPI-E2E-NAMING')
        self.assertTrue(frappe.db.exists('Sales Invoice', name))

    def test_posting_date_and_time_are_honoured(self):
        response = v1.create_invoice(
            **_payload('ZAPI-E2E-TIME', posting_date='2026-07-01', posting_time='14:30:00')
        )
        invoice = response['data']['invoice']
        self.assertEqual(invoice['posting_date'], '2026-07-01')
        self.assertTrue(invoice['posting_time'].startswith('14:30'))

    def test_locale_formatted_amounts_are_accepted(self):
        response = v1.create_invoice(
            **_payload('ZAPI-E2E-LOCALE', items=[{'item_code': TEST_ITEM, 'qty': '3', 'rate': '1,250.50'}])
        )
        self.assertTrue(response['success'], msg=response.get('errors'))
        self.assertEqual(response['data']['invoice']['net_total'], 3751.5)


class TestIdempotency(ZATCAAPITestCase):
    def test_second_request_with_same_external_id_updates_the_draft(self):
        first = v1.create_invoice(**_payload('ZAPI-IDEM-001'))
        name = first['data']['invoice']['invoice']

        second = v1.create_invoice(
            **_payload('ZAPI-IDEM-001', items=[{'item_code': TEST_ITEM, 'qty': 5, 'rate': 100}])
        )

        self.assertEqual(second['data']['action'], 'updated')
        self.assertEqual(second['data']['invoice']['invoice'], name)
        self.assertEqual(second['data']['invoice']['net_total'], 500.0)

        # Exactly one invoice, not two.
        count = frappe.db.count('Sales Invoice', {invoice_service.EXTERNAL_ID_FIELD: 'ZAPI-IDEM-001'})
        self.assertEqual(count, 1)

    def test_repeated_requests_never_duplicate(self):
        """The regression that mattered: an hourly poller re-importing the same feed."""
        for _ in range(4):
            v1.create_invoice(**_payload('ZAPI-IDEM-LOOP'))

        count = frappe.db.count('Sales Invoice', {invoice_service.EXTERNAL_ID_FIELD: 'ZAPI-IDEM-LOOP'})
        self.assertEqual(count, 1)

    def test_update_existing_drafts_off_returns_duplicate_untouched(self):
        _configure_settings(update_existing_drafts=0)
        try:
            first = v1.create_invoice(**_payload('ZAPI-IDEM-002'))
            name = first['data']['invoice']['invoice']

            second = v1.create_invoice(
                **_payload('ZAPI-IDEM-002', items=[{'item_code': TEST_ITEM, 'qty': 9, 'rate': 100}])
            )
            self.assertEqual(second['data']['action'], 'duplicate')
            self.assertTrue(second['data']['duplicate'])
            # Original amount preserved.
            self.assertEqual(frappe.db.get_value('Sales Invoice', name, 'net_total'), 200.0)
        finally:
            _configure_settings()


class TestSubmittedInvoiceImmutability(ZATCAAPITestCase):
    def test_submitted_invoice_is_never_rewritten(self):
        """Re-posting a submitted invoice would rewrite GL entries behind the ledger."""
        created = v1.create_invoice(**_payload('ZAPI-IMMUT-001'))
        name = created['data']['invoice']['invoice']
        invoice_service.submit_invoice(name)

        before = frappe.db.get_value(
            'Sales Invoice', name, ['modified', 'net_total', 'docstatus'], as_dict=True
        )

        repeat = v1.create_invoice(
            **_payload('ZAPI-IMMUT-001', items=[{'item_code': TEST_ITEM, 'qty': 99, 'rate': 100}])
        )

        self.assertTrue(repeat['success'])
        self.assertEqual(repeat['data']['action'], 'duplicate')
        self.assertTrue(repeat['data']['duplicate'])

        after = frappe.db.get_value(
            'Sales Invoice', name, ['modified', 'net_total', 'docstatus'], as_dict=True
        )
        self.assertEqual(before['net_total'], after['net_total'])
        self.assertEqual(before['modified'], after['modified'])
        self.assertEqual(after['docstatus'], 1)

    def test_cancelled_invoice_external_id_is_rejected(self):
        created = v1.create_invoice(**_payload('ZAPI-IMMUT-002'))
        name = created['data']['invoice']['invoice']
        invoice_service.submit_invoice(name)
        doc = frappe.get_doc('Sales Invoice', name)
        doc.flags.ignore_permissions = True
        doc.cancel()

        repeat = v1.create_invoice(**_payload('ZAPI-IMMUT-002'))
        self.assertFalse(repeat['success'])
        self.assertEqual(repeat['errors'][0]['code'], 'immutable_document')


class TestTaxHandling(ZATCAAPITestCase):
    def _vat_account(self):
        return ensure_vat_account()

    def test_default_company_template_is_used_when_the_payload_omits_taxes(self):
        response = v1.create_invoice(**_payload('ZAPI-TAX-DEFAULT'))
        self.assertTrue(response['success'], msg=response.get('errors'))

        invoice = response['data']['invoice']
        self.assertEqual(invoice['net_total'], 200.0)
        self.assertEqual(invoice['total_taxes_and_charges'], 30.0)
        self.assertEqual(invoice['grand_total'], 230.0)

    def test_named_tax_template_is_resolved(self):
        template = _ensure_default_tax_template()
        response = v1.create_invoice(**_payload('ZAPI-TAX-NAMED', tax_template=template))
        self.assertTrue(response['success'], msg=response.get('errors'))
        self.assertEqual(response['data']['invoice']['total_taxes_and_charges'], 30.0)

    def test_unknown_tax_template_is_rejected(self):
        response = v1.create_invoice(**_payload('ZAPI-TAX-BADTPL', tax_template='No Such Template'))
        self.assertFalse(response['success'])
        self.assertIn('not found', response['errors'][0]['message'])

    def test_explicit_tax_row_is_applied(self):
        response = v1.create_invoice(
            **_payload(
                'ZAPI-TAX-001',
                taxes=[{'account_head': self._vat_account(), 'charge_type': 'On Net Total', 'rate': 15}],
            )
        )
        invoice = response['data']['invoice']
        self.assertEqual(invoice['net_total'], 200.0)
        self.assertEqual(invoice['total_taxes_and_charges'], 30.0)
        self.assertEqual(invoice['grand_total'], 230.0)

    def test_tax_account_from_another_company_is_rejected(self):
        other = frappe.db.get_value(
            'Account', {'account_type': 'Tax', 'is_group': 0, 'company': ['!=', TEST_COMPANY]}, 'name'
        )
        if not other:
            self.skipTest('No tax account from another company available on this site.')

        response = v1.create_invoice(**_payload('ZAPI-TAX-002', taxes=[{'account_head': other, 'rate': 15}]))
        self.assertFalse(response['success'])
        self.assertEqual(response['errors'][0]['code'], 'validation_error')
        self.assertIn('belongs to company', response['errors'][0]['message'])

    def test_unknown_tax_account_is_rejected(self):
        response = v1.create_invoice(
            **_payload('ZAPI-TAX-003', taxes=[{'account_head': 'No Such Account - XX', 'rate': 15}])
        )
        self.assertFalse(response['success'])
        self.assertIn('does not exist', response['errors'][0]['message'])

    def test_mixed_rate_lines_are_taxed_per_line_not_flattened(self):
        """A zero-rated line must not be charged standard VAT.

        Collapsing Item Tax Templates into a single On Net Total row - the previous
        implementation's approach - taxes the zero-rated line at 15%. Setting the
        per-row item_tax_template lets ERPNext compute item_wise_tax_detail, which
        is also the field ksa_compliance reads for the ZATCA per-line VAT category.
        """
        vat_account = self._vat_account()

        standard = self._ensure_item_tax_template('_ZAPI Standard 15', vat_account, 15)
        zero = self._ensure_item_tax_template('_ZAPI Zero Rated', vat_account, 0)

        response = v1.create_invoice(
            **_payload(
                'ZAPI-TAX-MIXED',
                items=[
                    {'item_code': TEST_ITEM, 'qty': 1, 'rate': 100, 'item_tax_template': standard},
                    {'item_code': TEST_ITEM_ZERO, 'qty': 1, 'rate': 100, 'item_tax_template': zero},
                ],
                taxes=[{'account_head': vat_account, 'charge_type': 'On Net Total', 'rate': 15}],
            )
        )

        self.assertTrue(response['success'], msg=response.get('errors'))
        invoice = response['data']['invoice']
        self.assertEqual(invoice['net_total'], 200.0)
        # 15% on the standard line only. A flattened 15%-on-everything gives 30.
        self.assertEqual(invoice['total_taxes_and_charges'], 15.0)
        self.assertEqual(invoice['grand_total'], 215.0)

    def _ensure_item_tax_template(self, title, account, rate):
        name = f'{title} - {TEST_ABBR}'
        if frappe.db.exists('Item Tax Template', name):
            return name

        template = frappe.new_doc('Item Tax Template')
        template.title = title
        template.company = TEST_COMPANY
        template.append('taxes', {'tax_type': account, 'tax_rate': rate})
        template.insert(ignore_permissions=True)
        return template.name


class TestPaymentMeans(ZATCAAPITestCase):
    """ZATCA payment means, and the ERPNext constraint behind it.

    ksa_compliance resolves the ZATCA payment means code from
    payments[0].mode_of_payment -> Mode of Payment.custom_zatca_payment_means_code.
    But ERPNext gates Sales Invoice.payments on `eval:doc.is_pos===1`, so on a
    regular invoice the row is discarded at insert. These tests pin both halves:
    the constraint is real, and the app refuses rather than silently dropping it.
    """

    def _mode(self):
        mode = frappe.db.get_value('Mode of Payment', {'enabled': 1}, 'name')
        if not mode:
            self.skipTest('No Mode of Payment on this site.')
        return mode

    def test_erpnext_discards_payments_on_a_non_pos_invoice(self):
        """Documents the ERPNext behaviour this app has to work around."""
        mode = self._mode()

        doc = frappe.new_doc('Sales Invoice')
        doc.customer = TEST_CUSTOMER
        doc.company = TEST_COMPANY
        doc.is_pos = 0
        doc.append('items', {'item_code': TEST_ITEM, 'qty': 1, 'rate': 100})
        doc.append('payments', {'mode_of_payment': mode, 'amount': 100})
        self.assertEqual(len(doc.payments), 1)

        doc.insert(ignore_permissions=True)
        # Silently dropped - which is precisely why payment_mode demands is_pos.
        self.assertEqual(len(doc.payments), 0)

    def test_payment_mode_without_is_pos_is_refused_not_ignored(self):
        response = v1.create_invoice(**_payload('ZAPI-PAY-001', payment_mode=self._mode()))

        self.assertFalse(response['success'])
        self.assertEqual(response['errors'][0]['code'], 'validation_error')
        self.assertIn('is_pos', response['errors'][0]['message'])

    def test_payment_mode_with_is_pos_reaches_the_payments_row(self):
        mode = self._mode()
        response = v1.create_invoice(
            **_payload('ZAPI-PAY-002', payment_mode=mode, is_pos=1, payment_amount=230)
        )
        self.assertTrue(response['success'], msg=response.get('errors'))

        doc = frappe.get_doc('Sales Invoice', response['data']['invoice']['invoice'])
        self.assertEqual(cint(doc.is_pos), 1)
        self.assertEqual(len(doc.payments), 1)
        self.assertEqual(doc.payments[0].mode_of_payment, mode)
        self.assertEqual(flt(doc.paid_amount), 230.0)

    def test_payment_amount_defaults_to_zero(self):
        """Declaring the payment means must not imply the invoice was paid."""
        mode = self._mode()
        response = v1.create_invoice(**_payload('ZAPI-PAY-003', payment_mode=mode, is_pos=1))
        self.assertTrue(response['success'], msg=response.get('errors'))

        doc = frappe.get_doc('Sales Invoice', response['data']['invoice']['invoice'])
        self.assertEqual(len(doc.payments), 1)
        self.assertEqual(flt(doc.paid_amount), 0.0)
        self.assertGreater(flt(doc.outstanding_amount), 0.0)

    def test_unknown_payment_mode_is_rejected(self):
        response = v1.create_invoice(**_payload('ZAPI-PAY-004', payment_mode='No Such Mode', is_pos=1))
        self.assertFalse(response['success'])
        self.assertIn('Mode of Payment', response['errors'][0]['message'])


class TestCreditNote(ZATCAAPITestCase):
    def test_credit_note_quantities_are_negative(self):
        response = v1.create_credit_note(
            **_payload('ZAPI-CN-001', items=[{'item_code': TEST_ITEM, 'qty': 2, 'rate': 100}])
        )
        self.assertTrue(response['success'], msg=response.get('errors'))
        invoice = response['data']['invoice']
        self.assertEqual(invoice['is_return'], 1)
        self.assertEqual(invoice['items'][0]['qty'], -2.0)
        self.assertEqual(invoice['net_total'], -200.0)

    def test_credit_note_against_a_submitted_invoice(self):
        original = v1.create_invoice(**_payload('ZAPI-CN-ORIG'))
        original_name = original['data']['invoice']['invoice']
        invoice_service.submit_invoice(original_name)

        response = v1.create_credit_note(
            **_payload(
                'ZAPI-CN-002',
                return_against=original_name,
                items=[{'item_code': TEST_ITEM, 'qty': 1, 'rate': 100}],
            )
        )
        self.assertTrue(response['success'], msg=response.get('errors'))
        self.assertEqual(response['data']['invoice']['return_against'], original_name)

    def test_credit_note_against_a_draft_invoice_is_rejected(self):
        draft = v1.create_invoice(**_payload('ZAPI-CN-DRAFT-ORIG'))
        draft_name = draft['data']['invoice']['invoice']

        response = v1.create_credit_note(**_payload('ZAPI-CN-003', return_against=draft_name))
        self.assertFalse(response['success'])
        self.assertIn('not submitted', response['errors'][0]['message'])


class TestValidationErrors(ZATCAAPITestCase):
    def test_missing_customer_returns_400_with_field_details(self):
        response = v1.create_invoice(external_id='ZAPI-BAD-001', items=[{'item_code': TEST_ITEM}])
        self.assertFalse(response['success'])
        self.assertEqual(response['errors'][0]['code'], 'validation_error')
        self.assertIn('customer', response['errors'][0]['details']['missing'])

    def test_unknown_company_is_rejected(self):
        response = v1.create_invoice(**_payload('ZAPI-BAD-002', company='No Such Company Ltd'))
        self.assertFalse(response['success'])
        self.assertIn('does not exist', response['errors'][0]['message'])

    def test_no_items_is_rejected(self):
        response = v1.create_invoice(**_payload('ZAPI-BAD-003', items=[]))
        self.assertFalse(response['success'])
        self.assertIn('items', response['errors'][0]['details']['missing'])

    def test_failed_request_creates_no_partial_documents(self):
        """A rejected payload must not leave master data behind."""
        marker = '_ZAPI-GHOST-ITEM'
        response = v1.create_invoice(
            external_id='ZAPI-BAD-004',
            customer='_ZAPI Ghost Customer',
            company=TEST_COMPANY,
            items=[{'item_code': marker, 'qty': 0, 'rate': 10}],
        )
        self.assertFalse(response['success'])
        self.assertFalse(frappe.db.exists('Item', marker))
        self.assertFalse(frappe.db.exists('Customer', '_ZAPI Ghost Customer'))


class TestMasterDataCreation(ZATCAAPITestCase):
    def test_creates_customer_and_item_when_enabled(self):
        response = v1.create_invoice(
            **_payload(
                'ZAPI-MASTER-001',
                customer='_ZAPI New Customer',
                items=[{'item_code': '_ZAPI-NEW-ITEM', 'item_name': 'New Item', 'qty': 1, 'rate': 50}],
            )
        )
        self.assertTrue(response['success'], msg=response.get('errors'))
        self.assertTrue(frappe.db.exists('Customer', '_ZAPI New Customer'))
        self.assertTrue(frappe.db.exists('Item', '_ZAPI-NEW-ITEM'))
        # Invoice-only integrations have no inventory to draw from.
        self.assertEqual(frappe.db.get_value('Item', '_ZAPI-NEW-ITEM', 'is_stock_item'), 0)

    def test_unknown_item_is_rejected_when_creation_disabled(self):
        _configure_settings(create_missing_items=0)
        try:
            response = v1.create_invoice(
                **_payload('ZAPI-MASTER-002', items=[{'item_code': '_ZAPI-BLOCKED', 'qty': 1, 'rate': 1}])
            )
            self.assertFalse(response['success'])
            self.assertIn('Create Missing Items is disabled', response['errors'][0]['message'])
            self.assertFalse(frappe.db.exists('Item', '_ZAPI-BLOCKED'))
        finally:
            _configure_settings()

    def test_unknown_uom_is_rejected_when_creation_disabled(self):
        _configure_settings(create_missing_uoms=0)
        try:
            response = v1.create_invoice(
                **_payload(
                    'ZAPI-MASTER-003',
                    items=[{'item_code': TEST_ITEM, 'qty': 1, 'rate': 1, 'uom': '_ZAPI Furlong'}],
                )
            )
            self.assertFalse(response['success'])
            self.assertFalse(frappe.db.exists('UOM', '_ZAPI Furlong'))
        finally:
            _configure_settings()

    def test_vat_number_is_mirrored_into_the_field_ksa_compliance_reads(self):
        """is_b2b_customer() reads custom_vat_registration_number, not tax_id.

        Writing only tax_id classifies a genuine B2B buyer as B2C, so the invoice
        is reported as simplified instead of cleared as standard.
        """
        if not frappe.get_meta('Customer').get_field('custom_vat_registration_number'):
            self.skipTest('ksa_compliance is not installed on this site.')

        v1.create_invoice(
            **_payload(
                'ZAPI-MASTER-VAT',
                customer='_ZAPI B2B Customer',
                tax_id='300000000000003',
            )
        )
        customer = frappe.get_doc('Customer', '_ZAPI B2B Customer')
        self.assertEqual(customer.tax_id, '300000000000003')
        self.assertEqual(customer.custom_vat_registration_number, '300000000000003')

        from ksa_compliance.ksa_compliance.doctype.sales_invoice_additional_fields.sales_invoice_additional_fields import (
            is_b2b_customer,
        )

        self.assertTrue(is_b2b_customer(customer))


class TestAddressCreation(ZATCAAPITestCase):
    def test_free_text_address_is_parsed_into_zatca_parts(self):
        _configure_settings(parse_address_display=1, default_country='Saudi Arabia')
        try:
            response = v1.create_invoice(
                **_payload(
                    'ZAPI-ADDR-001',
                    customer='_ZAPI Addr Customer',
                    address_title='_ZAPI Addr Billing',
                    address_display=(
                        'Building No 4521, Olaya Street, Al Murabba Dist, '
                        'P.C: 12613, Riyadh, Kingdom of Saudi Arabia'
                    ),
                )
            )
            self.assertTrue(response['success'], msg=response.get('errors'))

            address_name = frappe.db.get_value('Address', {'address_title': '_ZAPI Addr Billing'}, 'name')
            self.assertTrue(address_name)

            address = frappe.get_doc('Address', address_name)
            self.assertEqual(address.address_line1, 'Olaya Street')
            self.assertEqual(address.city, 'Riyadh')
            self.assertEqual(address.pincode, '12613')
            if address.meta.get_field('custom_building_number'):
                self.assertEqual(address.custom_building_number, '4521')
                self.assertEqual(address.custom_area, 'Al Murabba Dist')

            # No warnings, because every ZATCA-required part was recovered.
            self.assertEqual(response['warnings'], [])
        finally:
            _configure_settings()

    def test_incomplete_address_produces_warnings_but_still_succeeds(self):
        response = v1.create_invoice(
            **_payload(
                'ZAPI-ADDR-002',
                customer='_ZAPI Addr2 Customer',
                address_title='_ZAPI Addr2 Billing',
                address_parts={'street': 'Some Street', 'city': 'Riyadh'},
            )
        )
        self.assertTrue(response['success'], msg=response.get('errors'))
        self.assertTrue(response['warnings'])
        joined = ' '.join(response['warnings'])
        self.assertIn('building number', joined)
        self.assertIn('postal code', joined)

    def test_address_is_set_as_customer_primary_address(self):
        """ksa_compliance reads the buyer address from the primary address."""
        v1.create_invoice(
            **_payload(
                'ZAPI-ADDR-003',
                customer='_ZAPI Addr3 Customer',
                address_title='_ZAPI Addr3 Billing',
                address_parts={
                    'street': 'Olaya Street',
                    'building_number': '4521',
                    'district': 'Al Murabba',
                    'postal_code': '12613',
                    'city': 'Riyadh',
                },
            )
        )
        primary = frappe.db.get_value('Customer', '_ZAPI Addr3 Customer', 'customer_primary_address')
        self.assertTrue(primary)


class TestReadEndpoints(ZATCAAPITestCase):
    def test_get_invoice_by_external_id(self):
        created = v1.create_invoice(**_payload('ZAPI-READ-001'))
        name = created['data']['invoice']['invoice']

        response = v1.get_invoice(external_id='ZAPI-READ-001')
        self.assertTrue(response['success'])
        self.assertEqual(response['data']['invoice']['invoice'], name)

    def test_get_invoice_by_name(self):
        created = v1.create_invoice(**_payload('ZAPI-READ-002'))
        name = created['data']['invoice']['invoice']

        response = v1.get_invoice(invoice=name)
        self.assertEqual(response['data']['invoice']['invoice'], name)

    def test_get_invoice_not_found(self):
        response = v1.get_invoice(external_id='ZAPI-NOPE')
        self.assertFalse(response['success'])
        self.assertEqual(response['errors'][0]['code'], 'not_found')

    def test_get_status_shape(self):
        v1.create_invoice(**_payload('ZAPI-READ-003'))
        response = v1.get_status(external_id='ZAPI-READ-003')

        self.assertTrue(response['success'])
        for key in ('available', 'phase', 'integration_status', 'is_cleared', 'is_pending'):
            self.assertIn(key, response['data']['zatca'])

    def test_list_invoices_is_paginated_and_capped(self):
        for index in range(3):
            v1.create_invoice(**_payload(f'ZAPI-LIST-{index}'))

        response = v1.list_invoices(company=TEST_COMPANY, docstatus=0, limit=2)
        data = response['data']

        self.assertTrue(response['success'])
        self.assertLessEqual(data['count'], 2)
        self.assertEqual(data['limit'], 2)
        self.assertIn('has_more', data)
        self.assertIn('total', data)

    def test_list_invoices_limit_is_clamped_to_the_ceiling(self):
        response = v1.list_invoices(company=TEST_COMPANY, docstatus=0, limit=99999)
        self.assertEqual(response['data']['limit'], v1.MAX_PAGE_LIMIT)

    def test_list_invoices_with_qr_caps_the_page_harder(self):
        response = v1.list_invoices(company=TEST_COMPANY, docstatus=0, limit=200, include_qr=1)
        self.assertLessEqual(response['data']['limit'], 50)

    def test_ping_reports_capabilities(self):
        response = v1.ping()
        self.assertTrue(response['success'])
        data = response['data']
        self.assertEqual(data['app'], 'zatca_api')
        self.assertTrue(data['custom_fields_installed'])
        self.assertIn('readiness', data)
        self.assertIn('ksa_compliance_installed', data['readiness'])


class TestSecurity(ZATCAAPITestCase):
    def test_no_endpoint_allows_guest(self):
        """An unauthenticated caller must never reach invoice data.

        Asserted against frappe's live ``guest_methods`` registry, which is the
        set ``@frappe.whitelist(allow_guest=True)`` populates. Also verifies the
        source contains no ``allow_guest`` at all, so the guarantee cannot be
        weakened by an endpoint that has not been imported yet.
        """
        import ast
        import pathlib

        guest_methods = frappe.guest_methods

        checked = 0
        for name in dir(v1):
            attribute = getattr(v1, name)
            if callable(attribute) and getattr(attribute, '__module__', None) == v1.__name__:
                unwrapped = getattr(attribute, '__wrapped__', attribute)
                with self.subTest(endpoint=name):
                    self.assertNotIn(attribute, guest_methods)
                    self.assertNotIn(unwrapped, guest_methods)
                checked += 1

        self.assertGreater(checked, 5, 'endpoint discovery found almost nothing')

        # No module in the app may pass allow_guest to frappe.whitelist.
        root = pathlib.Path(v1.__file__).parent.parent
        offenders = []
        for path in root.rglob('*.py'):
            if 'tests' in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                fname = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', None)
                if fname != 'whitelist':
                    continue
                for keyword in node.keywords:
                    if keyword.arg == 'allow_guest':
                        offenders.append(f'{path.relative_to(root)}:{node.lineno}')

        self.assertEqual(offenders, [])

    def test_guest_session_is_refused(self):
        frappe.set_user('Guest')
        try:
            response = v1.create_invoice(**_payload('ZAPI-SEC-001'))
            self.assertFalse(response['success'])
            self.assertEqual(response['errors'][0]['code'], 'unauthorized')
        finally:
            frappe.set_user('Administrator')

    def test_disabled_app_refuses_every_write(self):
        _configure_settings(enabled=0)
        try:
            response = v1.create_invoice(**_payload('ZAPI-SEC-002'))
            self.assertFalse(response['success'])
            self.assertEqual(response['errors'][0]['code'], 'app_disabled')
        finally:
            _configure_settings()

    def test_user_without_invoice_permission_is_refused(self):
        email = '_zapi_noperm@example.com'
        if not frappe.db.exists('User', email):
            user = frappe.new_doc('User')
            user.email = email
            user.first_name = 'No'
            user.last_name = 'Perm'
            user.send_welcome_email = 0
            user.append('roles', {'role': 'Blogger'})
            # The welcome mail renders an email template against built assets,
            # which is unrelated to what this test asserts.
            user.flags.no_welcome_mail = True
            user.insert(ignore_permissions=True)

        frappe.set_user(email)
        try:
            response = v1.create_invoice(**_payload('ZAPI-SEC-003'))
            self.assertFalse(response['success'])
            self.assertEqual(response['errors'][0]['code'], 'forbidden')
        finally:
            frappe.set_user('Administrator')

    def test_no_module_calls_set_user(self):
        """Guard against a privilege-escalation regression.

        frappe.set_user('Administrator') inside a whitelisted method grants full
        system privileges to any authenticated caller.

        The scan parses each module's AST rather than grepping for the text, so
        comments and docstrings that merely *mention* set_user do not trip it.
        """
        import ast
        import pathlib

        import zatca_api

        root = pathlib.Path(zatca_api.__file__).parent
        offenders = []

        for path in root.rglob('*.py'):
            if 'tests' in path.parts:
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', None)
                if name == 'set_user':
                    offenders.append(f'{path.relative_to(root)}:{node.lineno}')

        self.assertEqual(offenders, [])
