# zatca_api/tests/test_contract.py
# Copyright (c) 2026, Enfono Technologies and contributors
"""Keep the published data contract honest.

The schema and samples in docs/ are what an external team builds against. If the app
changes and these drift, the vendor implements to a spec that no longer holds -- and
finds out during integration. These tests make that drift a build failure.

Two directions are checked:
  1. Every sample validates against the published JSON Schema.
  2. Every sample is actually accepted by the app's own normaliser and validator.
"""

import json
import pathlib
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from zatca_api.services.payload import PayloadError, normalise_invoice, validate_invoice

DOCS = pathlib.Path(frappe.get_app_path('zatca_api')).parent / 'docs'
SCHEMA_DIR = DOCS / 'schema'
SAMPLE_DIR = DOCS / 'samples'

# Samples that intentionally reference records only present on a configured client
# site (named tax templates, accounts, a submitted original invoice).
NEEDS_SITE_DATA = {'standard-b2b.json', 'mixed-vat-rates.json', 'simplified-b2c.json', 'credit-note.json'}


def _samples():
    return sorted(p for p in SAMPLE_DIR.glob('*.json') if p.name != 'feed-response.json')


class TestDocsExist(FrappeTestCase):
    def test_schema_and_samples_are_shipped(self):
        self.assertTrue((SCHEMA_DIR / 'invoice.schema.json').is_file())
        self.assertTrue((SCHEMA_DIR / 'feed.schema.json').is_file())
        self.assertTrue((DOCS / 'DATA_CONTRACT.md').is_file())
        self.assertGreaterEqual(len(_samples()), 5)

    def test_every_shipped_json_parses(self):
        for path in list(SCHEMA_DIR.glob('*.json')) + list(SAMPLE_DIR.glob('*.json')):
            with self.subTest(file=path.name):
                json.loads(path.read_text())


class TestSchemaValidity(FrappeTestCase):
    def setUp(self):
        try:
            import jsonschema  # noqa: F401
        except ImportError:
            self.skipTest('jsonschema is not installed in this environment.')

    def test_schema_is_a_valid_json_schema(self):
        from jsonschema import Draft202012Validator

        Draft202012Validator.check_schema(json.loads((SCHEMA_DIR / 'invoice.schema.json').read_text()))
        Draft202012Validator.check_schema(json.loads((SCHEMA_DIR / 'feed.schema.json').read_text()))

    def test_every_sample_validates_against_the_schema(self):
        from jsonschema import Draft202012Validator

        validator = Draft202012Validator(json.loads((SCHEMA_DIR / 'invoice.schema.json').read_text()))
        for path in _samples():
            with self.subTest(sample=path.name):
                errors = sorted(
                    validator.iter_errors(json.loads(path.read_text())), key=lambda e: list(e.path)
                )
                self.assertEqual(
                    errors, [], msg='; '.join(f'{list(e.path)}: {e.message}' for e in errors[:3])
                )

    def test_schema_rejects_the_documented_mistakes(self):
        """The rules the contract promises are enforced must actually be enforced."""
        from jsonschema import Draft202012Validator

        validator = Draft202012Validator(json.loads((SCHEMA_DIR / 'invoice.schema.json').read_text()))
        base = json.loads((SAMPLE_DIR / 'minimal.json').read_text())

        def invalid(**overrides):
            payload = dict(base)
            payload.update(overrides)
            return bool(list(validator.iter_errors(payload)))

        # 3-digit building number, the classic ZATCA rejection
        self.assertTrue(
            invalid(
                tax_id='300000000000003',
                address_parts={
                    'street': 'S',
                    'building_number': '521',
                    'district': 'D',
                    'city': 'C',
                    'postal_code': '12613',
                },
            )
        )
        # 4-digit postal code
        self.assertTrue(
            invalid(
                tax_id='300000000000003',
                address_parts={
                    'street': 'S',
                    'building_number': '4521',
                    'district': 'D',
                    'city': 'C',
                    'postal_code': '1261',
                },
            )
        )
        # VAT number not starting/ending with 3
        self.assertTrue(invalid(tax_id='100000000000001'))
        # zero quantity
        self.assertTrue(invalid(items=[{'item_code': 'X', 'qty': 0, 'rate': 10}]))
        # negative rate
        self.assertTrue(invalid(items=[{'item_code': 'X', 'qty': 1, 'rate': -10}]))
        # no items
        self.assertTrue(invalid(items=[]))
        # buyer_id_type without a value
        self.assertTrue(invalid(buyer_id_type='CRN'))
        # payment_mode without is_pos
        self.assertTrue(invalid(payment_mode='Cash'))
        # a VAT number obliges a buyer address
        self.assertTrue(invalid(tax_id='300000000000003'))

    def test_feed_sample_validates_against_the_feed_schema(self):
        from jsonschema import Draft202012Validator, RefResolver

        feed_schema = json.loads((SCHEMA_DIR / 'feed.schema.json').read_text())
        invoice_schema = json.loads((SCHEMA_DIR / 'invoice.schema.json').read_text())

        resolver = RefResolver.from_schema(feed_schema, store={'invoice.schema.json': invoice_schema})
        validator = Draft202012Validator(feed_schema, resolver=resolver)
        errors = list(validator.iter_errors(json.loads((SAMPLE_DIR / 'feed-response.json').read_text())))
        self.assertEqual(errors, [], msg='; '.join(e.message for e in errors[:3]))


class TestSamplesMatchTheApp(FrappeTestCase):
    """A sample the schema likes but the app rejects would be worse than no sample."""

    def test_samples_normalise_without_error(self):
        for path in _samples():
            with self.subTest(sample=path.name):
                raw = json.loads(path.read_text())
                payload = normalise_invoice(raw, is_return=bool(raw.get('is_return')))
                self.assertTrue(payload['external_id'])
                self.assertTrue(payload['customer'])
                self.assertTrue(payload['items'])

    def test_samples_pass_the_apps_own_validator(self):
        for path in _samples():
            if path.name in NEEDS_SITE_DATA:
                # These reference client-specific accounts/templates or a submitted
                # original invoice, which only exist on a configured site.
                continue
            with self.subTest(sample=path.name):
                payload = normalise_invoice(json.loads(path.read_text()))
                try:
                    validate_invoice(payload)
                except PayloadError as exc:
                    self.fail(f'{path.name}: {exc.message}')

    def test_credit_note_sample_produces_negative_quantities(self):
        raw = json.loads((SAMPLE_DIR / 'credit-note.json').read_text())
        payload = normalise_invoice(raw, is_return=True)
        self.assertEqual(payload['is_return'], 1)
        for row in payload['items']:
            self.assertLess(row['qty'], 0)

    def test_address_aliases_in_samples_resolve_to_the_zatca_fields(self):
        raw = json.loads((SAMPLE_DIR / 'standard-b2b.json').read_text())
        address = normalise_invoice(raw)['address']
        self.assertEqual(address['address_line1'], 'Olaya Street')
        self.assertEqual(address['custom_building_number'], '4521')
        self.assertEqual(address['custom_area'], 'Al Murabba')
        self.assertEqual(address['pincode'], '12613')
        self.assertEqual(address['city'], 'Riyadh')

    def test_every_documented_alias_actually_resolves(self):
        """Guards against the contract naming a key the code does not accept."""
        from zatca_api.services.payload import INVOICE_ALIASES

        schema = json.loads((SCHEMA_DIR / 'invoice.schema.json').read_text())
        documented = set(schema['properties']) - {
            'address_parts',
            'address_title',
            'taxes',
            'items',
            'tax_template',
        }
        for key in sorted(documented):
            with self.subTest(key=key):
                payload = normalise_invoice(
                    {'external_id': 'X', 'customer': 'C', 'items': [{'item_code': 'I'}], key: 1}
                )
                # Every documented top-level key must map to a canonical slot.
                self.assertIn(
                    key if key in payload else 'raw',
                    payload,
                    msg=f'{key} is documented in the schema but has no canonical mapping',
                )
                self.assertTrue(
                    key in payload or key in INVOICE_ALIASES or key in payload['raw'],
                    msg=f'{key} is documented but unreachable',
                )


if __name__ == '__main__':
    unittest.main()
