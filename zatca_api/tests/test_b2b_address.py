# zatca_api/tests/test_b2b_address.py
# Copyright (c) 2026, Enfono Technologies and contributors
"""A complete buyer address is mandatory for a B2B buyer.

This mirrors `ksa_compliance`, verified against 0.58.0:

* ``_set_buyer_details`` reads ``customer.get('custom_vat_registration_number')`` into
  ``buyer_vat_registration_number`` -- that specific field, not the core ``tax_id``.
* It calls ``_set_buyer_address(address_doc, _is_b2b_customer)``, so ``validate=True``
  whenever the invoice type is *Standard*, and ``validate_buyer_address`` throws.
* A B2B customer with no address at all throws "Customer address is mandatory for B2B
  transactions".

So the app rejects it up front, with a per-field message, before anything is written.
"""

import frappe

from zatca_api.api import v1
from zatca_api.services import masters
from zatca_api.tests.test_api_v1 import (
    ZATCAAPITestCase,
    _configure_settings,
    _payload,
)

VAT = '300000000000003'

COMPLETE_ADDRESS = {
    'street': 'Olaya Street',
    'building_number': '4521',
    'district': 'Al Murabba',
    'city': 'Riyadh',
    'postal_code': '12613',
    'country': 'Saudi Arabia',
}


def _b2b(external_id, address=None, **overrides):
    payload = _payload(
        external_id,
        customer=overrides.pop('customer', f'_ZAPI B2B {external_id}'),
        tax_id=overrides.pop('tax_id', VAT),
        address_title=f'{external_id} Billing',
    )
    if address is not None:
        payload['address_parts'] = address
    payload.update(overrides)
    return payload


class TestB2BDetection(ZATCAAPITestCase):
    def test_tax_id_in_the_payload_makes_it_b2b(self):
        self.assertTrue(masters.customer_is_b2b({'tax_id': VAT}))

    def test_buyer_id_value_makes_it_b2b(self):
        self.assertTrue(masters.customer_is_b2b({'buyer_id_value': '1010101010'}))

    def test_no_identifier_and_unknown_customer_is_not_b2b(self):
        self.assertFalse(masters.customer_is_b2b({}, '_ZAPI Nonexistent Customer'))

    def test_saved_customer_vat_number_makes_a_later_invoice_b2b(self):
        """The payload may omit tax_id on repeat invoices; the Customer still carries it."""
        if not frappe.get_meta('Customer').get_field('custom_vat_registration_number'):
            self.skipTest('ksa_compliance is not installed on this site.')

        v1.create_invoice(**_b2b('B2B-DETECT-1', COMPLETE_ADDRESS))
        customer = '_ZAPI B2B B2B-DETECT-1'

        # tax_id absent this time, but the Customer record carries it.
        self.assertTrue(masters.customer_is_b2b({}, customer))

    def test_vat_number_is_written_to_the_field_ksa_compliance_reads(self):
        if not frappe.get_meta('Customer').get_field('custom_vat_registration_number'):
            self.skipTest('ksa_compliance is not installed on this site.')

        v1.create_invoice(**_b2b('B2B-DETECT-2', COMPLETE_ADDRESS))
        doc = frappe.get_doc('Customer', '_ZAPI B2B B2B-DETECT-2')

        self.assertEqual(doc.tax_id, VAT)
        self.assertEqual(doc.custom_vat_registration_number, VAT)

        from ksa_compliance.ksa_compliance.doctype.sales_invoice_additional_fields.sales_invoice_additional_fields import (
            is_b2b_customer,
        )

        self.assertTrue(is_b2b_customer(doc))

    def test_buyer_id_lands_in_custom_additional_ids(self):
        if not frappe.get_meta('Customer').get_field('custom_additional_ids'):
            self.skipTest('ksa_compliance is not installed on this site.')

        payload = _b2b('B2B-DETECT-3', COMPLETE_ADDRESS, tax_id='')
        payload['buyer_id_type'] = 'CRN'
        payload['buyer_id_value'] = '1010101010'
        response = v1.create_invoice(**payload)
        self.assertTrue(response['success'], msg=response.get('errors'))

        doc = frappe.get_doc('Customer', '_ZAPI B2B B2B-DETECT-3')
        rows = [(r.type_code, r.value) for r in doc.custom_additional_ids]
        self.assertIn(('CRN', '1010101010'), rows)

        from ksa_compliance.ksa_compliance.doctype.sales_invoice_additional_fields.sales_invoice_additional_fields import (
            is_b2b_customer,
        )

        self.assertTrue(is_b2b_customer(doc))

    def test_rejects_an_invalid_buyer_id_type(self):
        payload = _b2b('B2B-DETECT-4', COMPLETE_ADDRESS, tax_id='')
        payload['buyer_id_type'] = 'NOPE'
        payload['buyer_id_value'] = '123'
        response = v1.create_invoice(**payload)
        self.assertFalse(response['success'])
        self.assertIn('not a ZATCA identification code', response['errors'][0]['message'])


class TestB2BAddressMandatory(ZATCAAPITestCase):
    def test_complete_address_succeeds_with_no_warnings(self):
        response = v1.create_invoice(**_b2b('B2B-ADDR-OK', COMPLETE_ADDRESS))
        self.assertTrue(response['success'], msg=response.get('errors'))
        self.assertEqual(response['warnings'], [])

    def test_no_address_at_all_is_rejected(self):
        response = v1.create_invoice(**_b2b('B2B-ADDR-NONE'))
        self.assertFalse(response['success'])
        error = response['errors'][0]
        self.assertEqual(error['code'], 'validation_error')
        self.assertIn('mandatory for a B2B customer', error['message'])
        self.assertTrue(error['details']['is_b2b'])
        self.assertIn('address_parts', error['details']['field'])

    def test_nothing_is_written_when_the_address_is_rejected(self):
        """The rejection must happen before any master data exists."""
        response = v1.create_invoice(**_b2b('B2B-ADDR-CLEAN'))
        self.assertFalse(response['success'])
        self.assertFalse(frappe.db.exists('Customer', '_ZAPI B2B B2B-ADDR-CLEAN'))
        self.assertFalse(frappe.db.exists('Address', {'address_title': 'B2B-ADDR-CLEAN Billing'}))

    def test_missing_building_number_is_rejected(self):
        address = dict(COMPLETE_ADDRESS)
        address.pop('building_number')
        response = v1.create_invoice(**_b2b('B2B-ADDR-NOBLD', address))
        self.assertFalse(response['success'])
        self.assertIn('building number', response['errors'][0]['message'])

    def test_overlong_building_number_is_rejected(self):
        """A short numeric value is padded to 4; an overlong one cannot be salvaged."""
        address = dict(COMPLETE_ADDRESS, building_number='45211')
        response = v1.create_invoice(**_b2b('B2B-ADDR-LONGBLD', address))
        self.assertFalse(response['success'])
        self.assertIn('4', response['errors'][0]['message'])

    def test_non_numeric_building_number_is_rejected(self):
        address = dict(COMPLETE_ADDRESS, building_number='A1')
        response = v1.create_invoice(**_b2b('B2B-ADDR-ALPHABLD', address))
        self.assertFalse(response['success'])
        self.assertIn('building number', response['errors'][0]['message'])

    def test_short_numeric_building_number_is_padded_not_rejected(self):
        address = dict(COMPLETE_ADDRESS, building_number='521')
        response = v1.create_invoice(**_b2b('B2B-ADDR-PADBLD', address))
        self.assertTrue(response['success'], msg=response.get('errors'))
        name = frappe.db.get_value('Address', {'address_title': 'B2B-ADDR-PADBLD Billing'}, 'name')
        if frappe.get_meta('Address').get_field('custom_building_number'):
            self.assertEqual(frappe.db.get_value('Address', name, 'custom_building_number'), '0521')

    def test_wrong_length_postal_code_is_rejected(self):
        address = dict(COMPLETE_ADDRESS, postal_code='123456')
        response = v1.create_invoice(**_b2b('B2B-ADDR-BADZIP', address))
        self.assertFalse(response['success'])
        self.assertIn('postal code', response['errors'][0]['message'])

    def test_missing_district_is_rejected(self):
        address = dict(COMPLETE_ADDRESS)
        address.pop('district')
        response = v1.create_invoice(**_b2b('B2B-ADDR-NODIST', address))
        self.assertFalse(response['success'])
        self.assertIn('district', response['errors'][0]['message'])

    def test_missing_street_is_rejected(self):
        address = dict(COMPLETE_ADDRESS)
        address.pop('street')
        response = v1.create_invoice(**_b2b('B2B-ADDR-NOSTREET', address))
        self.assertFalse(response['success'])
        self.assertIn('street', response['errors'][0]['message'])

    def test_error_lists_every_problem_and_what_is_required(self):
        response = v1.create_invoice(**_b2b('B2B-ADDR-MULTI', {'city': 'Riyadh'}))
        self.assertFalse(response['success'])
        details = response['errors'][0]['details']
        self.assertGreaterEqual(len(details['problems']), 3)
        self.assertIn('building_number (4 digits)', details['required'])

    def test_free_text_address_satisfies_the_requirement(self):
        _configure_settings(parse_address_display=1, default_country='Saudi Arabia')
        try:
            payload = _b2b('B2B-ADDR-FREETEXT')
            payload['address_display'] = (
                'Building No 4521, Olaya Street, Al Murabba Dist, '
                'P.C: 12613, Riyadh, Kingdom of Saudi Arabia'
            )
            response = v1.create_invoice(**payload)
            self.assertTrue(response['success'], msg=response.get('errors'))
            self.assertEqual(response['warnings'], [])
        finally:
            _configure_settings()


class TestB2CStillLenient(ZATCAAPITestCase):
    """A simplified invoice needs no buyer address, so gaps stay warnings."""

    def test_b2c_without_an_address_succeeds(self):
        response = v1.create_invoice(**_payload('B2C-ADDR-1', customer='_ZAPI B2C Lenient'))
        self.assertTrue(response['success'], msg=response.get('errors'))

    def test_b2c_with_a_partial_address_succeeds_with_warnings(self):
        response = v1.create_invoice(
            **_payload(
                'B2C-ADDR-2',
                customer='_ZAPI B2C Lenient2',
                address_title='B2C-ADDR-2 Billing',
                address_parts={'street': 'Some Street', 'city': 'Riyadh'},
            )
        )
        self.assertTrue(response['success'], msg=response.get('errors'))
        self.assertTrue(response['warnings'])


class TestEnforcementCanBeRelaxed(ZATCAAPITestCase):
    def test_turning_the_setting_off_downgrades_it_to_a_warning(self):
        """Escape hatch for a non-KSA site or a legacy migration."""
        _configure_settings(enforce_b2b_address=0)
        try:
            response = v1.create_invoice(
                **_b2b('B2B-RELAXED-1', {'street': 'Olaya Street', 'city': 'Riyadh'})
            )
            self.assertTrue(response['success'], msg=response.get('errors'))
            self.assertTrue(response['warnings'])
        finally:
            _configure_settings()

    def test_relaxed_and_no_city_skips_the_address_instead_of_failing(self):
        """city is reqd on Address, so an unbuildable address is skipped, not attempted."""
        _configure_settings(enforce_b2b_address=0)
        try:
            response = v1.create_invoice(**_b2b('B2B-RELAXED-2'))
            self.assertTrue(response['success'], msg=response.get('errors'))
            joined = ' '.join(response['warnings'])
            self.assertIn('city is mandatory on an Address record', joined)
            self.assertFalse(frappe.db.exists('Address', {'address_title': 'B2B-RELAXED-2 Billing'}))
        finally:
            _configure_settings()

    def test_default_is_enforced(self):
        settings = frappe.get_single('ZATCA API Settings')
        self.assertTrue(
            settings.meta.get_field('enforce_b2b_address').default in ('1', 1),
            'the field default must be on',
        )


class TestDryRunReportsIt(ZATCAAPITestCase):
    def test_dry_run_rejects_an_incomplete_b2b_address(self):
        response = v1.validate_payload(**_b2b('B2B-DRY-1'))
        data = response['data']
        self.assertFalse(data['valid'])
        self.assertIn('mandatory for a B2B customer', data['errors'][0]['message'])

    def test_dry_run_accepts_a_complete_b2b_address(self):
        response = v1.validate_payload(**_b2b('B2B-DRY-2', COMPLETE_ADDRESS))
        self.assertTrue(response['data']['valid'], msg=response['data']['errors'])

    def test_dry_run_writes_nothing_when_it_rejects(self):
        before = frappe.db.count('Customer')
        v1.validate_payload(**_b2b('B2B-DRY-3'))
        self.assertEqual(before, frappe.db.count('Customer'))
