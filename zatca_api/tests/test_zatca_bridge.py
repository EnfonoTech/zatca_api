# zatca_api/tests/test_zatca_bridge.py
# Copyright (c) 2026, Enfono Technologies and contributors
"""Tests for the ZATCA block returned by the API.

Phase 1 is exercised end to end: its QR is computed locally by `ksa_compliance`
from the invoice totals with no Java CLI and no network call, so a real invoice
produces a real, decodable QR here.

Phase 2 is exercised against a Sales Invoice Additional Fields row inserted
directly. The signing step it normally goes through requires the ZATCA Java CLI
and a provisioned certificate, and re-implementing or stubbing that would be
testing `ksa_compliance` rather than this app. What belongs to this app is
reading that row, rendering the TLV to a PNG, and reporting status correctly -
which is what these tests assert.

Every test skips cleanly when `ksa_compliance` is not installed.
"""

import base64
import json

import frappe
from frappe.tests.utils import FrappeTestCase

from zatca_api.api import v1
from zatca_api.services import zatca
from zatca_api.tests.test_api_v1 import (
    TEST_COMPANY,
    TEST_ITEM,
    ZATCAAPITestCase,
    _configure_settings,
    _payload,
    ensure_vat_account,
)


def _taxed_payload(external_id, **overrides):
    """A payload carrying a VAT row.

    ksa_compliance's Sales Invoice validate hook rejects an invoice with an empty
    taxes table once ZATCA is enabled for the company, and the test company has no
    default Sales Taxes and Charges Template.
    """
    overrides.setdefault(
        'taxes', [{'account_head': ensure_vat_account(), 'charge_type': 'On Net Total', 'rate': 15}]
    )
    return _payload(external_id, **overrides)


PHASE_1_VAT = '300000000000003'

# A representative Phase 2 QR payload: base64 of the TLV structure ZATCA defines.
SAMPLE_TLV_QR = base64.b64encode(bytes.fromhex('0104' + '54657374')).decode()


def _skip_without_ksa(test):
    if not zatca.is_ksa_compliance_installed():
        test.skipTest('ksa_compliance is not installed on this site.')


def _disable_phase_2_signing():
    """Turn off Phase 2 integration for the test company.

    ksa_compliance skips creating a Sales Invoice Additional Fields document when
    enable_zatca_integration is off, which keeps these tests away from the Java
    signing CLI and from any outbound ZATCA call.
    """
    name = frappe.db.get_value('ZATCA Business Settings', {'company': TEST_COMPANY, 'status': 'Active'})
    if name:
        frappe.db.set_value('ZATCA Business Settings', name, 'enable_zatca_integration', 0)
    return name


def _remove_phase_2_settings():
    """Drop Phase 2 settings so Phase 1 settings can be created.

    ZATCAPhase1BusinessSettings.validate refuses to save while Phase 2 settings
    exist for the same company. A raw delete keeps this inside the test
    transaction, which FrappeTestCase rolls back at class teardown.
    """
    frappe.db.delete('ZATCA Business Settings', {'company': TEST_COMPANY})
    frappe.clear_cache(doctype='ZATCA Business Settings')


def _ensure_company_address():
    """ZATCA Phase 1 Business Settings requires a linked Address."""
    title = f'{TEST_COMPANY} HQ'
    name = frappe.db.get_value('Address', {'address_title': title})
    if name:
        return name

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
    doc.append('links', {'link_doctype': 'Company', 'link_name': TEST_COMPANY})
    doc.insert(ignore_permissions=True)
    return doc.name


def _ensure_phase_1_settings():
    """Create/enable Phase 1 settings for the test company.

    status options are 'Active' / 'Disabled' (verified against
    zatca_phase_1_business_settings.json) and `address` is mandatory.
    """
    address = _ensure_company_address()

    name = frappe.db.get_value('ZATCA Phase 1 Business Settings', {'company': TEST_COMPANY})
    if name:
        frappe.db.set_value(
            'ZATCA Phase 1 Business Settings',
            name,
            {'status': 'Active', 'vat_registration_number': PHASE_1_VAT, 'address': address},
        )
        return name

    doc = frappe.new_doc('ZATCA Phase 1 Business Settings')
    doc.company = TEST_COMPANY
    doc.vat_registration_number = PHASE_1_VAT
    doc.address = address
    doc.status = 'Active'
    if doc.meta.get_field('type_of_transaction'):
        options = [o for o in (doc.meta.get_field('type_of_transaction').options or '').split('\n') if o]
        if options:
            doc.type_of_transaction = options[-1]  # 'Both'
    doc.insert(ignore_permissions=True)
    return doc.name


class TestPhase1QR(ZATCAAPITestCase):
    """End-to-end: a submitted invoice must come back with a usable Phase 1 QR."""

    def setUp(self):
        super().setUp()
        _skip_without_ksa(self)
        _remove_phase_2_settings()
        _ensure_phase_1_settings()
        _configure_settings(auto_submit_invoices=1, submit_mode='Immediate', include_qr_png=1)

    def test_submitted_invoice_returns_a_phase_1_qr_png(self):
        response = v1.create_invoice(**_taxed_payload('ZAPI-P1-001'))
        self.assertTrue(response['success'], msg=response.get('errors'))

        data = response['data']
        self.assertTrue(data['submitted'])

        block = data['zatca']
        self.assertTrue(block['available'], msg=block.get('reason'))
        self.assertEqual(block['phase'], 'Phase 1')
        self.assertEqual(block['integration_status'], 'Not Applicable')
        self.assertTrue(block['is_cleared'])
        self.assertFalse(block['is_pending'])

        # Phase 1 has no UUID or hash - there is no filing, only a local QR.
        self.assertIsNone(block['uuid'])
        self.assertIsNone(block['invoice_hash'])

        # The helper returns a rendered image, not the underlying TLV string.
        self.assertEqual(block['qr_format'], 'png-only')
        self.assertIsNone(block['qr_content'])

        self.assertTrue(block['qr_png_base64'])
        self.assertTrue(block['qr_png_data_uri'].startswith('data:image/png;base64,'))

    def test_phase_1_qr_is_a_genuine_decodable_png(self):
        response = v1.create_invoice(**_taxed_payload('ZAPI-P1-002'))
        raw = base64.b64decode(response['data']['zatca']['qr_png_base64'])

        # PNG magic number. Proves an image was actually rendered, not a stub.
        self.assertEqual(raw[:8], b'\x89PNG\r\n\x1a\n')
        self.assertGreater(len(raw), 200)

    def test_phase_1_qr_encodes_this_invoice_totals(self):
        """Recompute the TLV independently and confirm the API's QR matches.

        This is what makes the test meaningful: it proves the QR belongs to *this*
        invoice rather than merely being a well-formed image.
        """
        from ksa_compliance.jinja import _format_date, _generate_decoded_string, _generate_qrcode

        response = v1.create_invoice(
            **_taxed_payload(
                'ZAPI-P1-003',
                items=[
                    {'item_code': TEST_ITEM, 'qty': 3, 'rate': 70},
                ],
            )
        )
        invoice_name = response['data']['invoice']['invoice']
        doc = frappe.get_doc('Sales Invoice', invoice_name)

        expected_tlv = _generate_decoded_string(
            [
                doc.company,
                PHASE_1_VAT,
                _format_date(doc.posting_date, doc.posting_time),
                doc.grand_total,
                doc.total_taxes_and_charges,
            ]
        )
        expected_png = _generate_qrcode(expected_tlv)

        self.assertEqual(response['data']['zatca']['qr_png_base64'], expected_png)

        # And the TLV really does carry the seller VAT number and the total.
        decoded = base64.b64decode(expected_tlv)
        self.assertIn(PHASE_1_VAT.encode(), decoded)
        self.assertIn(str(doc.grand_total).encode(), decoded)

    def test_include_qr_png_off_omits_the_image(self):
        _configure_settings(auto_submit_invoices=1, include_qr_png=0)
        try:
            response = v1.create_invoice(**_taxed_payload('ZAPI-P1-004'))
            block = response['data']['zatca']
            self.assertTrue(block['available'])
            self.assertNotIn('qr_png_base64', block)
        finally:
            _configure_settings(auto_submit_invoices=1, include_qr_png=1)

    def test_phase_1_only_setting_skips_the_phase_2_lookup(self):
        _configure_settings(auto_submit_invoices=1, zatca_phase='Phase 1 Only')
        try:
            response = v1.create_invoice(**_taxed_payload('ZAPI-P1-005'))
            self.assertEqual(response['data']['zatca']['phase'], 'Phase 1')
        finally:
            _configure_settings(auto_submit_invoices=1)

    def test_disabled_phase_setting_reports_disabled(self):
        _configure_settings(auto_submit_invoices=1, zatca_phase='Disabled')
        try:
            response = v1.create_invoice(**_taxed_payload('ZAPI-P1-006'))
            block = response['data']['zatca']
            self.assertFalse(block['available'])
            self.assertEqual(block['phase'], 'Disabled')
        finally:
            _configure_settings(auto_submit_invoices=1)

    def test_get_status_reflects_phase_1(self):
        v1.create_invoice(**_taxed_payload('ZAPI-P1-007'))
        response = v1.get_status(external_id='ZAPI-P1-007')
        self.assertEqual(response['data']['zatca']['phase'], 'Phase 1')
        self.assertTrue(response['data']['zatca']['is_cleared'])


_SIAF_SEQ = [0]


class TestPhase2Block(ZATCAAPITestCase):
    """Reading, rendering and reporting a Phase 2 record.

    The SIAF row is inserted directly. Producing one for real requires the ZATCA
    Java CLI and a provisioned certificate; signing is ksa_compliance's job, and
    what is under test here is this app's handling of the result.
    """

    def setUp(self):
        super().setUp()
        _skip_without_ksa(self)
        _disable_phase_2_signing()
        _configure_settings(auto_submit_invoices=1, submit_mode='Immediate', include_qr_png=1)

    def _invoice_with_siaf(self, external_id, **siaf_fields):
        response = v1.create_invoice(**_taxed_payload(external_id))
        self.assertTrue(response['success'], msg=response.get('errors'))
        invoice_name = response['data']['invoice']['invoice']

        row = frappe.new_doc('Sales Invoice Additional Fields')
        row.flags.ignore_permissions = True
        row.flags.ignore_mandatory = True
        row.invoice_doctype = 'Sales Invoice'
        row.sales_invoice = invoice_name
        row.precomputed = 1  # short-circuits before_insert's signing path
        # SIAF.uuid carries a unique index, and the document name is derived from
        # invoice_counter, so both have to be distinct per row.
        _SIAF_SEQ[0] += 1
        seq = _SIAF_SEQ[0]
        row.uuid = f'11111111-2222-3333-4444-{seq:012d}'
        row.invoice_counter = 1000 + seq
        row.invoice_hash = 'NWZlY2ViNjZmZmM4NmYzOGQ5NTI3ODZjNmQ2OTZjNzk='
        row.previous_invoice_hash = 'cHJldmlvdXNoYXNo'
        row.qr_code = SAMPLE_TLV_QR
        row.is_latest = 1
        for key, value in siaf_fields.items():
            if key != 'integration_status':
                row.set(key, value)
        row.insert(ignore_permissions=True)

        # ksa_compliance's before_insert forces integration_status to
        # 'Ready For Batch' unconditionally, so the desired status has to be
        # written after the insert.
        status = siaf_fields.get('integration_status', 'Accepted')
        frappe.db.set_value('Sales Invoice Additional Fields', row.name, 'integration_status', status)

        return invoice_name, row.name

    def test_phase_2_fields_are_returned(self):
        invoice_name, siaf_name = self._invoice_with_siaf('ZAPI-P2-001')

        block = zatca.get_zatca_details(invoice_name, 'Sales Invoice')

        self.assertTrue(block['available'], msg=block.get('reason'))
        self.assertEqual(block['phase'], 'Phase 2')
        self.assertEqual(block['additional_fields_doc'], siaf_name)
        self.assertTrue(block['uuid'].startswith('11111111-2222-3333-4444-'))
        self.assertGreater(block['invoice_counter'], 1000)
        self.assertEqual(block['invoice_hash'], 'NWZlY2ViNjZmZmM4NmYzOGQ5NTI3ODZjNmQ2OTZjNzk=')
        self.assertEqual(block['previous_invoice_hash'], 'cHJldmlvdXNoYXNo')
        self.assertEqual(block['qr_content'], SAMPLE_TLV_QR)
        self.assertEqual(block['qr_format'], 'base64-tlv')
        self.assertEqual(block['integration_status'], 'Accepted')
        self.assertTrue(block['is_cleared'])
        self.assertFalse(block['is_pending'])

    def test_phase_2_tlv_is_rendered_to_a_png(self):
        """Unlike Phase 1, the Phase 2 field is a TLV string; this app renders it."""
        invoice_name, _ = self._invoice_with_siaf('ZAPI-P2-002')
        block = zatca.get_zatca_details(invoice_name, 'Sales Invoice')

        raw = base64.b64decode(block['qr_png_base64'])
        self.assertEqual(raw[:8], b'\x89PNG\r\n\x1a\n')
        self.assertTrue(block['qr_png_data_uri'].startswith('data:image/png;base64,'))

    def test_pending_status_is_flagged_as_pending(self):
        """'Ready For Batch' means the filing has not happened yet.

        The QR and hash are still present, because signing happens locally at SIAF
        insert; only the ZATCA network outcome is outstanding.
        """
        invoice_name, _ = self._invoice_with_siaf('ZAPI-P2-003', integration_status='Ready For Batch')
        block = zatca.get_zatca_details(invoice_name, 'Sales Invoice')

        self.assertTrue(block['available'])
        self.assertTrue(block['is_pending'])
        self.assertFalse(block['is_cleared'])
        self.assertTrue(block['qr_content'])
        self.assertTrue(block['invoice_hash'])

    def test_rejected_status_is_not_cleared(self):
        invoice_name, _ = self._invoice_with_siaf('ZAPI-P2-004', integration_status='Rejected')
        block = zatca.get_zatca_details(invoice_name, 'Sales Invoice')

        self.assertFalse(block['is_cleared'])
        self.assertFalse(block['is_pending'])
        self.assertEqual(block['integration_status'], 'Rejected')

    def test_accepted_with_warnings_counts_as_cleared(self):
        invoice_name, _ = self._invoice_with_siaf('ZAPI-P2-005', integration_status='Accepted with warnings')
        block = zatca.get_zatca_details(invoice_name, 'Sales Invoice')
        self.assertTrue(block['is_cleared'])

    def test_every_status_option_is_classified(self):
        """No integration_status value may fall through as both pending and cleared."""
        options = (
            frappe.get_meta('Sales Invoice Additional Fields').get_field('integration_status').options
        ).split('\n')

        for status in options:
            with self.subTest(status=status):
                pending = status in zatca.PENDING_STATUSES
                cleared = status in zatca.TERMINAL_OK_STATUSES
                self.assertFalse(pending and cleared, f'{status!r} is both pending and cleared')

    def test_signed_xml_is_excluded_by_default_and_included_on_request(self):
        invoice_name, _ = self._invoice_with_siaf('ZAPI-P2-006', invoice_xml='<Invoice>test</Invoice>')

        default_block = zatca.get_zatca_details(invoice_name, 'Sales Invoice')
        self.assertNotIn('signed_xml', default_block)

        with_xml = zatca.get_zatca_details(invoice_name, 'Sales Invoice', include_xml=True)
        self.assertEqual(with_xml['signed_xml'], '<Invoice>test</Invoice>')

    def test_get_status_returns_the_integration_status(self):
        self._invoice_with_siaf('ZAPI-P2-007', integration_status='Rejected')
        response = v1.get_status(external_id='ZAPI-P2-007')

        self.assertEqual(response['data']['zatca']['integration_status'], 'Rejected')
        self.assertFalse(response['data']['zatca']['is_cleared'])

    def test_list_invoices_reports_integration_status_per_row(self):
        invoice_name, _ = self._invoice_with_siaf('ZAPI-P2-008', integration_status='Accepted')

        response = v1.list_invoices(company=TEST_COMPANY, docstatus=1, limit=50)
        row = next((r for r in response['data']['invoices'] if r['name'] == invoice_name), None)

        self.assertIsNotNone(row, 'invoice missing from the list response')
        self.assertEqual(row['integration_status'], 'Accepted')

    def test_list_invoices_can_filter_by_integration_status(self):
        accepted, _ = self._invoice_with_siaf('ZAPI-P2-009', integration_status='Accepted')
        rejected, _ = self._invoice_with_siaf('ZAPI-P2-010', integration_status='Rejected')

        response = v1.list_invoices(
            company=TEST_COMPANY, docstatus=1, integration_status='Rejected', limit=50
        )
        names = [r['name'] for r in response['data']['invoices']]

        self.assertIn(rejected, names)
        self.assertNotIn(accepted, names)

    def test_resubmit_skips_an_already_accepted_invoice(self):
        """Re-filing an accepted invoice would burn an invoice counter for nothing."""
        invoice_name, _ = self._invoice_with_siaf('ZAPI-P2-011', integration_status='Accepted')

        response = v1.resubmit_to_zatca(invoice=invoice_name)

        self.assertTrue(response['success'], msg=response.get('errors'))
        self.assertFalse(response['data']['resubmission']['queued'])
        self.assertEqual(response['data']['resubmission']['integration_status'], 'Accepted')

    def test_resubmit_without_a_zatca_record_reports_not_found(self):
        response = v1.create_invoice(**_taxed_payload('ZAPI-P2-012'))
        invoice_name = response['data']['invoice']['invoice']

        result = v1.resubmit_to_zatca(invoice=invoice_name)
        self.assertFalse(result['success'])
        self.assertEqual(result['errors'][0]['code'], 'not_found')


class TestZatcaUnavailable(ZATCAAPITestCase):
    def test_draft_invoice_reports_why_no_qr_exists(self):
        _configure_settings(auto_submit_invoices=0)
        try:
            response = v1.create_invoice(**_taxed_payload('ZAPI-NA-001'))
            block = response['data']['zatca']
            self.assertFalse(block['available'])
            self.assertIn('Draft', block['reason'])
        finally:
            _configure_settings()

    def test_missing_invoice_reports_not_found(self):
        block = zatca.get_zatca_details('SINV-NOT-A-REAL-INVOICE', 'Sales Invoice')
        self.assertFalse(block['available'])
        self.assertIn('not found', block['reason'])

    def test_phase_2_only_explains_a_missing_siaf(self):
        _skip_without_ksa(self)
        _disable_phase_2_signing()
        _configure_settings(auto_submit_invoices=1, zatca_phase='Phase 2 Only')
        try:
            response = v1.create_invoice(**_taxed_payload('ZAPI-NA-002'))
            block = response['data']['zatca']
            self.assertFalse(block['available'])
            self.assertEqual(block['phase'], 'Phase 2')
            self.assertIn('ZATCA Business Settings', block['reason'])
        finally:
            _configure_settings()

    def test_qr_render_failure_degrades_instead_of_raising(self):
        """A rendering problem must not fail the invoice that was already created."""
        self.assertEqual(zatca._render_qr_png(''), {})
        self.assertEqual(zatca._render_qr_png(None), {})


class TestSubmitAndQueue(ZATCAAPITestCase):
    def setUp(self):
        super().setUp()
        _skip_without_ksa(self)
        _remove_phase_2_settings()
        _ensure_phase_1_settings()

    def test_queued_mode_returns_a_draft_then_submit_endpoint_yields_the_qr(self):
        _configure_settings(auto_submit_invoices=1, submit_mode='Queued')
        try:
            created = v1.create_invoice(**_taxed_payload('ZAPI-Q-001'))
            self.assertTrue(created['success'], msg=created.get('errors'))
            self.assertTrue(created['data']['submission_queued'])
            self.assertFalse(created['data']['submitted'])
            self.assertFalse(created['data']['zatca']['available'])
        finally:
            _configure_settings()

        # No worker runs in a test, so drive the submission explicitly - which is
        # also exactly what a caller does when it polls and then submits.
        submitted = v1.submit_invoice(external_id='ZAPI-Q-001')
        self.assertTrue(submitted['success'], msg=submitted.get('errors'))
        self.assertTrue(submitted['data']['submitted'])
        self.assertTrue(submitted['data']['zatca']['available'], msg=submitted['data']['zatca'])
        self.assertEqual(submitted['data']['zatca']['phase'], 'Phase 1')

    def test_submit_is_idempotent(self):
        v1.create_invoice(**_taxed_payload('ZAPI-Q-002', submit=1))
        again = v1.submit_invoice(external_id='ZAPI-Q-002')
        self.assertTrue(again['success'], msg=again.get('errors'))
        self.assertEqual(again['data']['invoice']['docstatus'], 1)

    def test_payload_submit_flag_overrides_the_setting(self):
        _configure_settings(auto_submit_invoices=1)
        try:
            response = v1.create_invoice(**_taxed_payload('ZAPI-Q-003', submit=0))
            self.assertFalse(response['data']['submitted'])
            self.assertEqual(response['data']['invoice']['docstatus'], 0)
        finally:
            _configure_settings()


class TestZatcaMessageParsing(FrappeTestCase):
    """ZATCA's reply body is upstream JSON, so parsing it must never raise.

    A status the caller can trust is worth more than a parse error: ZATCA has changed
    this envelope before, and an exception here would fail a request whose invoice was
    filed perfectly well.
    """

    def test_parses_the_three_message_lists(self):
        body = json.dumps(
            {
                'validationResults': {
                    'infoMessages': [
                        {'code': 'XSD_ZATCA_VALID', 'category': 'XSD validation', 'message': 'ok'}
                    ],
                    'warningMessages': [{'code': 'BR-KSA-F-08', 'category': 'KSA', 'message': 'crn'}],
                    'errorMessages': [{'code': 'BR-CO-14', 'category': 'EN', 'message': 'vat total'}],
                }
            }
        )
        parsed = zatca._parse_zatca_message(body)

        self.assertEqual([m['code'] for m in parsed['info']], ['XSD_ZATCA_VALID'])
        self.assertEqual([m['code'] for m in parsed['warnings']], ['BR-KSA-F-08'])
        self.assertEqual(parsed['errors'][0]['category'], 'EN')
        self.assertEqual(parsed['errors'][0]['message'], 'vat total')

    def test_missing_lists_become_empty(self):
        parsed = zatca._parse_zatca_message('{"validationResults": {}}')
        self.assertEqual(parsed, {'info': [], 'warnings': [], 'errors': []})

    def test_no_validation_results_key(self):
        parsed = zatca._parse_zatca_message('{"something": "else"}')
        self.assertEqual(parsed, {'info': [], 'warnings': [], 'errors': []})

    def test_none_and_empty_are_safe(self):
        for value in (None, '', '   '):
            self.assertEqual(
                zatca._parse_zatca_message(value), {'info': [], 'warnings': [], 'errors': []}
            )

    def test_plain_text_body_is_preserved_not_dropped(self):
        """A non-JSON body still carries the reason; surface it rather than discard it."""
        parsed = zatca._parse_zatca_message('502 Bad Gateway from the gateway')
        self.assertEqual(parsed['raw_text'], '502 Bad Gateway from the gateway')
        self.assertEqual(parsed['errors'], [])

    def test_malformed_json_does_not_raise(self):
        parsed = zatca._parse_zatca_message('{"validationResults": {oops')
        self.assertEqual(parsed, {'info': [], 'warnings': [], 'errors': []})

    def test_non_dict_entries_are_skipped(self):
        """Defensive: a list of strings where objects were expected must not crash."""
        body = json.dumps({'validationResults': {'errorMessages': ['just a string', None]}})
        self.assertEqual(zatca._parse_zatca_message(body)['errors'], [])
