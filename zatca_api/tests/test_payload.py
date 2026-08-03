# zatca_api/tests/test_payload.py
# Copyright (c) 2026, Enfono Technologies and contributors
"""Tests for payload normalisation and validation.

These exercise the layer that makes the app client-agnostic: whatever key names
an upstream system uses, they must fold into one canonical shape before any
document is written.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from zatca_api.services.payload import (
    PayloadError,
    as_dict,
    as_list,
    coerce,
    normalise_invoice,
    validate_invoice,
)


class TestKeyAliasing(FrappeTestCase):
    def test_resolves_spaced_title_case_keys(self):
        """The legacy feed sends 'Naming Series' / 'Customer Name' / 'Posting Date'."""
        payload = normalise_invoice(
            {
                'Naming Series': 'INV-001',
                'Customer Name': 'Acme',
                'Company Name': 'Test Co',
                'Posting Date': '2026-08-04',
                'Tax ID': '300000000000003',
                'Items': [{'Item_Code': 'X', 'Qty': 2, 'Rate': 10}],
            }
        )
        self.assertEqual(payload['external_id'], 'INV-001')
        self.assertEqual(payload['customer'], 'Acme')
        self.assertEqual(payload['company'], 'Test Co')
        self.assertEqual(payload['tax_id'], '300000000000003')
        self.assertEqual(len(payload['items']), 1)
        self.assertEqual(payload['items'][0]['item_code'], 'X')

    def test_resolves_camel_case_and_snake_case_equally(self):
        for key in ('externalId', 'external_id', 'EXTERNAL-ID', 'External Id'):
            with self.subTest(key=key):
                payload = normalise_invoice({key: 'E1', 'customer': 'C', 'items': [{'item_code': 'I'}]})
                self.assertEqual(payload['external_id'], 'E1')

    def test_first_non_blank_alias_wins(self):
        """A present-but-empty alias must not shadow a populated one."""
        payload = normalise_invoice(
            {'external_id': '', 'invoice_no': 'FALLBACK', 'customer': 'C', 'items': [{'item_code': 'I'}]}
        )
        self.assertEqual(payload['external_id'], 'FALLBACK')

    def test_alternative_item_key_names(self):
        payload = normalise_invoice(
            {
                'external_id': 'E',
                'customer': 'C',
                'lines': [{'sku': 'SKU-1', 'quantity': '3', 'unit_price': '25.50', 'unit': 'Box'}],
            }
        )
        item = payload['items'][0]
        self.assertEqual(item['item_code'], 'SKU-1')
        self.assertEqual(item['qty'], 3.0)
        self.assertEqual(item['rate'], 25.50)
        self.assertEqual(item['uom'], 'Box')


class TestCoercion(FrappeTestCase):
    def test_locale_formatted_number_does_not_raise(self):
        """A bare float() on '1,250.00' raises; flt() must absorb it."""
        self.assertEqual(coerce('1,250.00', 'Float'), 1250.0)

    def test_none_and_blank_are_safe(self):
        self.assertIsNone(coerce(None, 'Int'))
        self.assertEqual(coerce('', 'Int'), 0)
        self.assertIsNone(coerce('', 'Date'))

    def test_date_and_check(self):
        self.assertEqual(str(coerce('2026-08-04', 'Date')), '2026-08-04')
        self.assertEqual(coerce('1', 'Check'), 1)
        self.assertEqual(coerce('yes', 'Check'), 0)

    def test_string_numbers_stay_strings_for_data(self):
        self.assertEqual(coerce(300000000000003, 'Data'), '300000000000003')


class TestQuantityHandling(FrappeTestCase):
    def test_absent_qty_defaults_to_one(self):
        payload = normalise_invoice({'external_id': 'E', 'customer': 'C', 'items': [{'item_code': 'I'}]})
        self.assertEqual(payload['items'][0]['qty'], 1.0)

    def test_explicit_zero_qty_is_preserved_not_defaulted(self):
        """Silently turning 0 into 1 would bill the customer for a line they did not buy."""
        payload = normalise_invoice(
            {'external_id': 'E', 'customer': 'C', 'items': [{'item_code': 'I', 'qty': 0}]}
        )
        self.assertEqual(payload['items'][0]['qty'], 0.0)

        with self.assertRaises(PayloadError) as ctx:
            validate_invoice(payload)
        self.assertIn('qty', str(ctx.exception))

    def test_return_normalises_positive_qty_to_negative(self):
        """ERPNext requires negative qty on a return; the caller may send either sign."""
        payload = normalise_invoice(
            {'external_id': 'CN-1', 'customer': 'C', 'items': [{'item_code': 'I', 'qty': 5}]},
            is_return=True,
        )
        self.assertEqual(payload['items'][0]['qty'], -5.0)
        self.assertEqual(payload['is_return'], 1)

    def test_return_keeps_already_negative_qty_negative(self):
        payload = normalise_invoice(
            {'external_id': 'CN-2', 'customer': 'C', 'items': [{'item_code': 'I', 'qty': -5}]},
            is_return=True,
        )
        self.assertEqual(payload['items'][0]['qty'], -5.0)

    def test_is_return_flag_in_payload_also_flips_sign(self):
        payload = normalise_invoice(
            {'external_id': 'CN-3', 'customer': 'C', 'is_return': 1, 'items': [{'item_code': 'I', 'qty': 4}]}
        )
        self.assertEqual(payload['items'][0]['qty'], -4.0)


class TestJsonStringDecoding(FrappeTestCase):
    def test_items_sent_as_json_string(self):
        """Form-encoded requests deliver nested structures as strings."""
        payload = normalise_invoice(
            {'external_id': 'E', 'customer': 'C', 'items': '[{"item_code": "I", "qty": 1, "rate": 5}]'}
        )
        self.assertEqual(payload['items'][0]['item_code'], 'I')

    def test_single_object_becomes_a_one_element_list(self):
        self.assertEqual(as_list({'a': 1}), [{'a': 1}])

    def test_unparseable_string_raises_payload_error(self):
        with self.assertRaises(PayloadError):
            as_list('{not json')
        with self.assertRaises(PayloadError):
            as_dict('{not json')

    def test_none_and_empty(self):
        self.assertEqual(as_list(None), [])
        self.assertEqual(as_list(''), [])
        self.assertEqual(as_dict(None), {})


class TestValidation(FrappeTestCase):
    def _base(self, **overrides):
        payload = {'external_id': 'E', 'customer': 'C', 'items': [{'item_code': 'I', 'qty': 1, 'rate': 1}]}
        payload.update(overrides)
        return normalise_invoice(payload)

    def test_accepts_a_minimal_valid_payload(self):
        validate_invoice(self._base())

    def test_rejects_missing_required_fields(self):
        with self.assertRaises(PayloadError) as ctx:
            validate_invoice(normalise_invoice({}))
        details = ctx.exception.details['missing']
        self.assertCountEqual(details, ['external_id', 'customer', 'items'])

    def test_rejects_item_without_code(self):
        with self.assertRaises(PayloadError):
            validate_invoice(self._base(items=[{'qty': 1, 'rate': 1}]))

    def test_rejects_negative_rate(self):
        """A negative rate is how a caller tries to fake a credit note. Use is_return."""
        with self.assertRaises(PayloadError) as ctx:
            validate_invoice(self._base(items=[{'item_code': 'I', 'qty': 1, 'rate': -5}]))
        self.assertIn('is_return', str(ctx.exception))

    def test_rejects_invalid_posting_date(self):
        with self.assertRaises(PayloadError):
            validate_invoice(self._base(posting_date='not-a-date'))

    def test_rejects_return_against_that_does_not_exist(self):
        with self.assertRaises(PayloadError) as ctx:
            validate_invoice(self._base(is_return=1, return_against='SINV-DOES-NOT-EXIST'))
        self.assertIn('does not exist', str(ctx.exception))

    def test_zero_rate_line_is_allowed(self):
        """A free-of-charge line is legitimate."""
        validate_invoice(self._base(items=[{'item_code': 'I', 'qty': 1, 'rate': 0}]))


class TestAddressNormalisation(FrappeTestCase):
    def test_address_part_aliases(self):
        payload = normalise_invoice(
            {
                'external_id': 'E',
                'customer': 'C',
                'items': [{'item_code': 'I'}],
                'address_parts': {
                    'street': 'Olaya Street',
                    'building_number': '4521',
                    'district': 'Al Murabba',
                    'postal_code': '12613',
                    'city': 'Riyadh',
                    'country': 'Saudi Arabia',
                },
            }
        )
        address = payload['address']
        self.assertEqual(address['address_line1'], 'Olaya Street')
        self.assertEqual(address['custom_building_number'], '4521')
        self.assertEqual(address['custom_area'], 'Al Murabba')
        self.assertEqual(address['pincode'], '12613')
        self.assertEqual(address['country'], 'Saudi Arabia')


class TestSubmitFlag(FrappeTestCase):
    def test_absent_submit_is_none_so_settings_decide(self):
        payload = normalise_invoice({'external_id': 'E', 'customer': 'C', 'items': [{'item_code': 'I'}]})
        self.assertIsNone(payload['submit'])

    def test_explicit_false_is_respected(self):
        payload = normalise_invoice(
            {'external_id': 'E', 'customer': 'C', 'submit': False, 'items': [{'item_code': 'I'}]}
        )
        self.assertIs(payload['submit'], False)

    def test_explicit_true(self):
        payload = normalise_invoice(
            {'external_id': 'E', 'customer': 'C', 'submit': 1, 'items': [{'item_code': 'I'}]}
        )
        self.assertIs(payload['submit'], True)


class TestFieldMappings(FrappeTestCase):
    def test_mapping_writes_custom_field_and_coerces_type(self):
        from zatca_api.services.payload import apply_field_mappings

        settings = frappe.get_single('ZATCA API Settings')
        original = list(settings.field_mappings)
        settings.field_mappings = []
        settings.append(
            'field_mappings',
            {
                'target_doctype': 'Sales Invoice',
                'source_key': 'PO_REFERENCE',
                'target_field': 'po_no',
                'value_type': 'Data',
            },
        )
        try:
            target = {}
            applied = apply_field_mappings(target, 'Sales Invoice', {'PO_REFERENCE': 'PO-77'}, settings)
            self.assertEqual(applied, ['po_no'])
            self.assertEqual(target['po_no'], 'PO-77')
        finally:
            settings.field_mappings = original

    def test_mandatory_mapping_missing_from_payload_raises(self):
        from zatca_api.services.payload import apply_field_mappings

        settings = frappe.get_single('ZATCA API Settings')
        original = list(settings.field_mappings)
        settings.field_mappings = []
        settings.append(
            'field_mappings',
            {
                'target_doctype': 'Sales Invoice',
                'source_key': 'REQUIRED_KEY',
                'target_field': 'po_no',
                'value_type': 'Data',
                'is_mandatory': 1,
            },
        )
        try:
            with self.assertRaises(PayloadError):
                apply_field_mappings({}, 'Sales Invoice', {'something_else': 1}, settings)
        finally:
            settings.field_mappings = original
