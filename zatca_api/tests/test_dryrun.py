# zatca_api/tests/test_dryrun.py
# Copyright (c) 2026, Enfono Technologies and contributors
"""Tests for the validate_payload dry run.

The contract these pin down: it must report the same verdict a real request would
reach, and it must leave the database byte-for-byte unchanged on every path,
including when the payload explodes.
"""

import frappe

from zatca_api.api import v1
from zatca_api.services import invoice as invoice_service
from zatca_api.tests.test_api_v1 import (
    TEST_COMPANY,
    TEST_CUSTOMER,
    TEST_ITEM,
    ZATCAAPITestCase,
    _configure_settings,
    _payload,
    ensure_vat_account,
)


def _counts():
    return {
        dt: frappe.db.count(dt)
        for dt in ('Sales Invoice', 'Sales Invoice Item', 'Customer', 'Item', 'Address', 'UOM')
    }


class TestDryRunLeavesNothingBehind(ZATCAAPITestCase):
    def test_valid_payload_writes_nothing(self):
        before = _counts()
        response = v1.validate_payload(**_payload('DRY-CLEAN-001'))

        self.assertTrue(response['success'])
        self.assertTrue(response['data']['valid'], msg=response['data']['errors'])
        self.assertEqual(before, _counts())

    def test_new_master_data_is_not_created(self):
        """The most important guarantee: probing must not pollute the masters."""
        response = v1.validate_payload(
            **_payload(
                'DRY-CLEAN-002',
                customer='_ZAPI Phantom Customer',
                items=[{'item_code': '_ZAPI-PHANTOM-ITEM', 'qty': 1, 'rate': 10}],
            )
        )
        self.assertTrue(response['data']['valid'], msg=response['data']['errors'])
        self.assertFalse(frappe.db.exists('Customer', '_ZAPI Phantom Customer'))
        self.assertFalse(frappe.db.exists('Item', '_ZAPI-PHANTOM-ITEM'))

    def test_no_invoice_is_created(self):
        v1.validate_payload(**_payload('DRY-CLEAN-003'))
        self.assertIsNone(invoice_service.find_by_external_id('DRY-CLEAN-003'))

    def test_invalid_payload_also_writes_nothing(self):
        before = _counts()
        response = v1.validate_payload(
            **_payload(
                'DRY-CLEAN-004',
                customer='_ZAPI Phantom Two',
                items=[{'item_code': '_ZAPI-PHANTOM-TWO', 'qty': 0, 'rate': 10}],
            )
        )
        self.assertFalse(response['data']['valid'])
        self.assertEqual(before, _counts())
        self.assertFalse(frappe.db.exists('Item', '_ZAPI-PHANTOM-TWO'))

    def test_repeated_dry_runs_stay_clean(self):
        before = _counts()
        for index in range(3):
            v1.validate_payload(**_payload(f'DRY-CLEAN-LOOP-{index}'))
        self.assertEqual(before, _counts())

    def test_a_real_create_still_works_after_a_dry_run(self):
        """The savepoint rollback must not poison the surrounding transaction."""
        v1.validate_payload(**_payload('DRY-THEN-REAL'))
        response = v1.create_invoice(**_payload('DRY-THEN-REAL'))
        self.assertTrue(response['success'], msg=response.get('errors'))
        self.assertEqual(response['data']['action'], 'created')


class TestDryRunReport(ZATCAAPITestCase):
    def test_reports_real_erpnext_totals(self):
        """Totals come from ERPNext's own calculation, not a reimplementation."""
        response = v1.validate_payload(
            **_payload(
                'DRY-TOTALS-001',
                items=[{'item_code': TEST_ITEM, 'qty': 3, 'rate': 100}],
                taxes=[{'account_head': ensure_vat_account(), 'charge_type': 'On Net Total', 'rate': 15}],
            )
        )
        totals = response['data']['totals']
        self.assertEqual(totals['net_total'], 300.0)
        self.assertEqual(totals['total_taxes_and_charges'], 45.0)
        self.assertEqual(totals['grand_total'], 345.0)
        self.assertEqual(totals['currency'], 'SAR')
        self.assertEqual(len(totals['tax_rows']), 1)

    def test_reports_which_masters_would_be_created(self):
        response = v1.validate_payload(
            **_payload(
                'DRY-MASTERS-001',
                customer='_ZAPI Brand New Co',
                items=[
                    {'item_code': TEST_ITEM, 'qty': 1, 'rate': 10},
                    {'item_code': '_ZAPI-UNSEEN-1', 'qty': 1, 'rate': 10},
                    {'item_code': '_ZAPI-UNSEEN-2', 'qty': 1, 'rate': 10},
                ],
            )
        )
        would = response['data']['would_create']
        self.assertFalse(would['customer_exists'])
        self.assertIn(TEST_ITEM, would['existing_items'])
        self.assertCountEqual(would['new_items'], ['_ZAPI-UNSEEN-1', '_ZAPI-UNSEEN-2'])

    def test_flags_an_external_id_already_in_use(self):
        v1.create_invoice(**_payload('DRY-DUP-001'))
        response = v1.validate_payload(**_payload('DRY-DUP-001'))

        joined = ' '.join(response['data']['warnings'])
        self.assertIn('already maps to invoice', joined)
        self.assertIn('existing_invoice', response['data']['resolved'])

    def test_missing_fields_are_named(self):
        response = v1.validate_payload(customer=TEST_CUSTOMER, company=TEST_COMPANY)
        self.assertFalse(response['data']['valid'])
        error = response['data']['errors'][0]
        self.assertEqual(error['code'], 'validation_error')
        self.assertIn('external_id', error['details']['missing'])

    def test_surfaces_erpnext_validation_errors(self):
        """A tax account from another company is an ERPNext-level problem."""
        response = v1.validate_payload(
            **_payload('DRY-BADTAX-001', taxes=[{'account_head': 'Nope - XX', 'rate': 15}])
        )
        self.assertFalse(response['data']['valid'])
        self.assertIn('does not exist', response['data']['errors'][0]['message'])

    def test_buyer_address_gaps_are_reported(self):
        response = v1.validate_payload(**_payload('DRY-ADDR-001'))
        joined = ' '.join(response['data']['warnings'])
        self.assertIn('building number', joined)
        self.assertIn('postal code', joined)

    def test_complete_address_produces_no_address_warnings(self):
        response = v1.validate_payload(
            **_payload(
                'DRY-ADDR-002',
                customer='_ZAPI Dry Addr Co',
                address_title='_ZAPI Dry Addr Billing',
                address_parts={
                    'street': 'Olaya Street',
                    'building_number': '4521',
                    'district': 'Al Murabba',
                    'postal_code': '12613',
                    'city': 'Riyadh',
                    'country': 'Saudi Arabia',
                },
            )
        )
        self.assertTrue(response['data']['valid'], msg=response['data']['errors'])
        address_warnings = [w for w in response['data']['warnings'] if 'Buyer address' in w]
        self.assertEqual(address_warnings, [])

    def test_credit_note_via_named_argument(self):
        response = v1.validate_payload(
            document_type='Credit Note',
            **_payload('DRY-CN-001', items=[{'item_code': TEST_ITEM, 'qty': 2, 'rate': 100}]),
        )
        self.assertEqual(response['data']['document_type'], 'Credit Note')
        self.assertTrue(response['data']['valid'], msg=response['data']['errors'])
        self.assertTrue(response['data']['resolved']['is_return'])
        self.assertEqual(response['data']['totals']['net_total'], -200.0)

    def test_credit_note_via_the_json_body(self):
        """document_type must work from inside the body too.

        Over HTTP with a JSON body, frappe replaces form_dict with the parsed body and
        discards the query string, so an integrator putting document_type in the body
        is the reliable path -- and it has to work.
        """
        payload = _payload('DRY-CN-002', items=[{'item_code': TEST_ITEM, 'qty': 3, 'rate': 100}])
        payload['document_type'] = 'Credit Note'
        response = v1.validate_payload(**payload)

        self.assertEqual(response['data']['document_type'], 'Credit Note')
        self.assertTrue(response['data']['resolved']['is_return'])
        self.assertEqual(response['data']['totals']['net_total'], -300.0)

    def test_document_type_does_not_leak_into_the_invoice(self):
        payload = _payload('DRY-CN-003')
        payload['document_type'] = 'Sales Invoice'
        response = v1.validate_payload(**payload)
        self.assertTrue(response['data']['valid'], msg=response['data']['errors'])
        self.assertFalse(response['data']['resolved']['is_return'])

    def test_dry_run_flag_is_set(self):
        response = v1.validate_payload(**_payload('DRY-FLAG-001'))
        self.assertTrue(response['data']['dry_run'])


class TestDryRunZatcaClassification(ZATCAAPITestCase):
    """Whether the payload would file as standard (B2B) or simplified (B2C)."""

    def setUp(self):
        super().setUp()
        if not frappe.db.exists('DocType', 'ZATCA Business Settings'):
            self.skipTest('ksa_compliance is not installed on this site.')
        _configure_settings()

    def _with_phase_2(self, mode='Let the system decide (both)'):
        """Ensure active Phase 2 settings for the test company, rolled back per test."""
        name = frappe.db.get_value('ZATCA Business Settings', {'company': TEST_COMPANY})
        if name:
            frappe.db.set_value(
                'ZATCA Business Settings',
                name,
                {'status': 'Active', 'type_of_business_transactions': mode, 'enable_zatca_integration': 0},
            )
            return name

        doc = frappe.new_doc('ZATCA Business Settings')
        doc.flags.ignore_permissions = True
        doc.flags.ignore_mandatory = True
        doc.flags.ignore_validate = True
        doc.company = TEST_COMPANY
        doc.country = 'Saudi Arabia'
        doc.currency = 'SAR'
        doc.status = 'Active'
        doc.type_of_business_transactions = mode
        doc.enable_zatca_integration = 0
        doc.insert(ignore_permissions=True)
        return doc.name

    def test_buyer_with_vat_number_is_standard(self):
        self._with_phase_2()
        response = v1.validate_payload(
            **_payload(
                'DRY-Z-001',
                customer='_ZAPI B2B Dry',
                tax_id='300000000000003',
                address_title='_ZAPI B2B Dry Billing',
                address_parts={
                    'street': 'Olaya Street',
                    'building_number': '4521',
                    'district': 'Al Murabba',
                    'city': 'Riyadh',
                    'postal_code': '12613',
                    'country': 'Saudi Arabia',
                },
            )
        )
        self.assertTrue(response['data']['valid'], msg=response['data']['errors'])
        block = response['data']['zatca']
        self.assertEqual(block['invoice_type'], 'Standard')
        self.assertTrue(block['buyer_is_b2b'])

    def test_buyer_without_identifier_is_simplified_and_says_why(self):
        self._with_phase_2()
        response = v1.validate_payload(**_payload('DRY-Z-002', customer='_ZAPI B2C Dry'))
        block = response['data']['zatca']
        self.assertEqual(block['invoice_type'], 'Simplified')
        self.assertFalse(block['buyer_is_b2b'])
        # The reason must tell the integrator what to send to change the outcome.
        self.assertIn('tax_id', block['reason'])

    def test_company_forced_to_standard_overrides_the_buyer(self):
        self._with_phase_2(mode='Standard Tax Invoices')
        response = v1.validate_payload(**_payload('DRY-Z-003', customer='_ZAPI B2C Dry2'))
        block = response['data']['zatca']
        self.assertEqual(block['invoice_type'], 'Standard')
        self.assertIn('Standard Tax Invoices', block['reason'])

    def test_b2b_with_incomplete_address_is_a_hard_error_not_just_a_flag(self):
        """Incomplete B2B address is now rejected outright, before anything is written.

        It used to surface only as zatca_readiness.blocking. ksa_compliance validates
        the buyer address whenever the invoice type is Standard and throws, so there is
        no value in letting the invoice get as far as submission.
        """
        self._with_phase_2(mode='Standard Tax Invoices')
        response = v1.validate_payload(
            **_payload('DRY-Z-004', customer='_ZAPI B2B Dry3', tax_id='300000000000003')
        )
        data = response['data']
        self.assertFalse(data['valid'])
        self.assertIn('mandatory for a B2B customer', data['errors'][0]['message'])

    def test_standard_mode_with_a_buyer_that_has_no_identifier_is_blocking(self):
        """Company forced to Standard, buyer has nothing to identify them by."""
        self._with_phase_2(mode='Standard Tax Invoices')
        response = v1.validate_payload(**_payload('DRY-Z-004B', customer='_ZAPI B2C Forced'))
        readiness = response['data']['zatca_readiness']
        self.assertEqual(readiness['invoice_type'], 'Standard')
        self.assertFalse(readiness['buyer_is_b2b'])
        self.assertTrue(readiness['would_be_rejected_by_zatca'])
        self.assertTrue(any('identifier' in b for b in readiness['blocking']))

    def test_simplified_invoice_treats_address_gaps_as_advisory(self):
        self._with_phase_2(mode='Simplified Tax Invoices')
        response = v1.validate_payload(**_payload('DRY-Z-005', customer='_ZAPI B2C Dry3'))
        readiness = response['data']['zatca_readiness']
        self.assertEqual(readiness['invoice_type'], 'Simplified')
        self.assertFalse(readiness['would_be_rejected_by_zatca'])
        self.assertTrue(readiness['advisory'])

    def test_complete_standard_payload_is_not_blocked(self):
        self._with_phase_2(mode='Standard Tax Invoices')
        response = v1.validate_payload(
            **_payload(
                'DRY-Z-006',
                customer='_ZAPI B2B Complete',
                tax_id='300000000000003',
                address_title='_ZAPI B2B Complete Billing',
                address_parts={
                    'street': 'Olaya Street',
                    'building_number': '4521',
                    'district': 'Al Murabba',
                    'postal_code': '12613',
                    'city': 'Riyadh',
                    'country': 'Saudi Arabia',
                },
            )
        )
        readiness = response['data']['zatca_readiness']
        self.assertFalse(readiness['would_be_rejected_by_zatca'], msg=readiness['blocking'])


class TestDryRunSecurity(ZATCAAPITestCase):
    def test_guest_cannot_probe(self):
        frappe.set_user('Guest')
        try:
            response = v1.validate_payload(**_payload('DRY-SEC-001'))
            self.assertFalse(response['success'])
            self.assertEqual(response['errors'][0]['code'], 'unauthorized')
        finally:
            frappe.set_user('Administrator')

    def test_requires_the_same_permission_as_a_real_create(self):
        email = '_zapi_noperm@example.com'
        if not frappe.db.exists('User', email):
            user = frappe.new_doc('User')
            user.email = email
            user.first_name = 'No'
            user.last_name = 'Perm'
            user.send_welcome_email = 0
            user.flags.no_welcome_mail = True
            user.append('roles', {'role': 'Blogger'})
            user.insert(ignore_permissions=True)

        frappe.set_user(email)
        try:
            response = v1.validate_payload(**_payload('DRY-SEC-002'))
            self.assertFalse(response['success'])
            self.assertEqual(response['errors'][0]['code'], 'forbidden')
        finally:
            frappe.set_user('Administrator')
