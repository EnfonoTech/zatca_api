# zatca_api/tests/test_company_autocreate.py
# Copyright (c) 2026, Enfono Technologies and contributors
"""A company named in a payload is used if it exists and created if it does not.

The failure this replaces: ``create_invoice`` with ``"company": "ZATCA Test Co 3"`` came
back as ``Company ZATCA Test Co 3 does not exist.`` and the integrator had to create the
company by hand before the first invoice could move. A created company has no ZATCA
settings, so the response also carries the steps to set ZATCA up in ERPNext.
"""

from unittest import mock

import frappe

from zatca_api.api import v1
from zatca_api.services import masters, zatca
from zatca_api.tests.test_api_v1 import TEST_COMPANY, ZATCAAPITestCase, _payload

NEW_COMPANY = '_ZAPI Auto Co 1'

_real_phase_for_company = zatca.get_phase_for_company


def _company_count():
    return frappe.db.count('Company')


def _phase_for_company(company):
    """A vanilla site: a company made for the first time has no ZATCA settings.

    Other apps on a bench can seed settings the moment a company is inserted (Enfono's own
    ``enfono_master`` does), which would make these tests depend on what else is installed.
    """
    return 'None' if company == NEW_COMPANY else _real_phase_for_company(company)


def _drop_company(name):
    """Remove a company and what other apps attached to it.

    Creating a company is not always undone by a rollback: country fixtures and other apps'
    hooks run DDL, which commits. So the tests delete it themselves, and the next test
    starts from a site that has never seen it.
    """
    if not frappe.db.exists('Company', name):
        return
    for doctype in ('ZATCA Business Settings', 'ZATCA Phase 1 Business Settings'):
        # Straight delete: ksa_compliance refuses to trash a "configured" settings row, and the
        # skeleton another app seeds counts as configured.
        if frappe.db.exists('DocType', doctype):
            frappe.db.delete(doctype, {'company': name})
    frappe.delete_doc('Company', name, force=True, ignore_permissions=True)
    frappe.db.commit()


class CompanyTestCase(ZATCAAPITestCase):
    def setUp(self):
        super().setUp()
        _drop_company(NEW_COMPANY)
        patcher = mock.patch.object(zatca, 'get_phase_for_company', side_effect=_phase_for_company)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        super().tearDown()
        _drop_company(NEW_COMPANY)


class TestCreateInvoiceCreatesCompany(CompanyTestCase):
    def test_missing_company_is_created_and_the_invoice_booked_to_it(self):
        self.assertFalse(frappe.db.exists('Company', NEW_COMPANY))

        response = v1.create_invoice(**_payload('ZAPI-CO-001', company=NEW_COMPANY))

        self.assertTrue(response['success'], msg=response['errors'])
        self.assertEqual(response['data']['invoice']['company'], NEW_COMPANY)
        company = frappe.get_doc('Company', NEW_COMPANY)
        self.assertEqual(company.country, 'Saudi Arabia')
        self.assertEqual(company.default_currency, 'SAR')
        self.assertTrue(company.abbr)

    def test_the_response_says_the_company_was_created(self):
        response = v1.create_invoice(**_payload('ZAPI-CO-002', company=NEW_COMPANY))

        joined = ' '.join(response['warnings'])
        self.assertIn(NEW_COMPANY, joined)
        self.assertIn('was created', joined)

    def test_the_response_carries_the_zatca_setup_steps(self):
        response = v1.create_invoice(**_payload('ZAPI-CO-003', company=NEW_COMPANY))

        block = response['data']['zatca']
        self.assertTrue(block['setup_required'])
        setup = block['setup']
        self.assertEqual(setup['company'], NEW_COMPANY)
        self.assertIn(NEW_COMPANY, setup['message'])
        steps = ' '.join(setup['steps'])
        for expected in ('ZATCA Business Settings', 'ZATCA Phase 1 Business Settings', 'Onboard', 'OTP'):
            self.assertIn(expected, steps)

    def test_an_existing_company_is_used_as_it_is(self):
        before = _company_count()

        response = v1.create_invoice(**_payload('ZAPI-CO-004'))

        self.assertTrue(response['success'], msg=response['errors'])
        self.assertEqual(_company_count(), before)
        self.assertEqual(response['data']['invoice']['company'], TEST_COMPANY)
        self.assertNotIn('was created', ' '.join(response['warnings']))

    def test_a_second_request_reuses_the_company_the_first_one_created(self):
        v1.create_invoice(**_payload('ZAPI-CO-005', company=NEW_COMPANY))
        count = _company_count()

        response = v1.create_invoice(**_payload('ZAPI-CO-006', company=NEW_COMPANY))

        self.assertTrue(response['success'], msg=response['errors'])
        self.assertEqual(_company_count(), count)
        self.assertNotIn('was created', ' '.join(response['warnings']))

    def test_a_company_that_cannot_be_created_is_reported_and_leaves_nothing(self):
        # ERPNext refuses a second company with the same abbreviation.
        taken = frappe.db.get_value('Company', TEST_COMPANY, 'abbr')
        with mock.patch.object(masters, '_unique_abbr', return_value=taken):
            response = v1.create_invoice(**_payload('ZAPI-CO-007', company=NEW_COMPANY))

        self.assertFalse(response['success'])
        error = response['errors'][0]
        self.assertEqual(error['code'], 'validation_error')
        self.assertEqual(error['details']['field'], 'company')
        self.assertIn('could not be created', error['message'])
        self.assertFalse(frappe.db.exists('Company', NEW_COMPANY))

    def test_no_company_anywhere_is_still_an_error(self):
        with mock.patch.object(masters.frappe.defaults, 'get_user_default', return_value=None):
            with self.assertRaises(masters.PayloadError) as ctx:
                masters.ensure_company({}, frappe._dict(default_company=''))

        self.assertEqual(ctx.exception.details['field'], 'company')


class TestCompanyDefaults(ZATCAAPITestCase):
    def test_the_abbreviation_is_the_initials(self):
        self.assertEqual(masters._unique_abbr('Moon Light Trading'), 'MLT')

    def test_a_taken_abbreviation_gets_a_number(self):
        taken = frappe.db.get_value('Company', TEST_COMPANY, 'abbr')
        initials = ' '.join(taken)  # 'ZAPI' -> 'Z A P I'

        self.assertEqual(masters._unique_abbr(initials), f'{taken}2')

    def test_abbreviation_ignores_symbols_and_is_capped(self):
        self.assertEqual(masters._unique_abbr('Al & Co Of Ten Great Words'), 'ACOTG')

    def test_a_name_with_no_letters_still_gets_an_abbreviation(self):
        self.assertEqual(masters._unique_abbr('& - &'), 'CO')

    def test_the_country_comes_from_settings_then_falls_back_to_saudi_arabia(self):
        self.assertEqual(masters._company_country(frappe._dict(default_country='India')), 'India')
        self.assertEqual(masters._company_country(frappe._dict(default_country='')), 'Saudi Arabia')

    def test_the_currency_follows_the_country(self):
        self.assertEqual(masters._company_currency('India'), 'INR')
        self.assertEqual(masters._company_currency('Saudi Arabia'), 'SAR')
        self.assertEqual(masters._company_currency('Not A Country'), 'SAR')


class TestValidatePayloadDoesNotCreateCompany(CompanyTestCase):
    def test_a_missing_company_is_reported_and_not_created(self):
        before = _company_count()

        response = v1.validate_payload(**_payload('ZAPI-CO-DRY-001', company=NEW_COMPANY))

        data = response['data']
        self.assertTrue(data['valid'], msg=data['errors'])
        self.assertEqual(data['would_create']['new_company'], NEW_COMPANY)
        self.assertIn('does not exist', ' '.join(data['warnings']))
        self.assertFalse(frappe.db.exists('Company', NEW_COMPANY))
        self.assertEqual(_company_count(), before)

    def test_a_missing_company_report_carries_the_setup_steps(self):
        data = v1.validate_payload(**_payload('ZAPI-CO-DRY-002', company=NEW_COMPANY))['data']

        self.assertEqual(data['zatca_setup']['company'], NEW_COMPANY)
        self.assertIn('ZATCA Business Settings', ' '.join(data['zatca_setup']['steps']))

    def test_an_existing_company_gets_the_full_report(self):
        data = v1.validate_payload(**_payload('ZAPI-CO-DRY-003'))['data']

        self.assertTrue(data['valid'], msg=data['errors'])
        self.assertIsNone(data['would_create']['new_company'])
        self.assertEqual(data['totals']['net_total'], 200.0)


class TestSetupGuidance(ZATCAAPITestCase):
    def test_a_phase_1_company_needs_no_steps(self):
        with mock.patch.object(zatca, 'get_phase_for_company', return_value='Phase 1'):
            self.assertIsNone(zatca.setup_guidance(TEST_COMPANY))

    def test_an_onboarded_phase_2_company_needs_no_steps(self):
        onboarded = {'name': 'S-1', 'production_security_token': 'tok', 'production_request_id': '42'}
        with (
            mock.patch.object(zatca, 'get_phase_for_company', return_value='Phase 2'),
            mock.patch.object(zatca, '_phase_2_settings', return_value=onboarded),
        ):
            self.assertIsNone(zatca.setup_guidance(TEST_COMPANY))

    def test_an_empty_phase_2_row_is_not_ready(self):
        """Another app can create the row the moment a company is inserted; it has no CSID."""
        skeleton = {'name': 'S-1', 'production_security_token': '', 'production_request_id': None}
        with (
            mock.patch.object(zatca, 'get_phase_for_company', return_value='Phase 2'),
            mock.patch.object(zatca, '_phase_2_settings', return_value=skeleton),
        ):
            guidance = zatca.setup_guidance(TEST_COMPANY)

        self.assertEqual(guidance['phase'], 'Phase 2')
        self.assertFalse(guidance['onboarded'])
        self.assertIn('S-1', guidance['message'])
        self.assertIn('onboarding is not finished', guidance['message'])
        steps = ' '.join(guidance['steps'])
        self.assertIn('Onboard', steps)
        self.assertIn('Get Production CSID', steps)
        self.assertNotIn('Phase 1 only', steps)

    def test_disabled_zatca_needs_no_steps(self):
        with mock.patch.object(zatca, 'get_phase_for_company', return_value='None'):
            self.assertIsNone(zatca.setup_guidance(TEST_COMPANY, frappe._dict(zatca_phase='Disabled')))

    def test_an_unconfigured_company_gets_steps_that_name_it(self):
        with (
            mock.patch.object(zatca, 'get_phase_for_company', return_value='None'),
            mock.patch.object(zatca, 'is_ksa_compliance_installed', return_value=True),
        ):
            guidance = zatca.setup_guidance(TEST_COMPANY)

        self.assertEqual(guidance['phase'], 'None')
        self.assertFalse(guidance['onboarded'])
        self.assertIn(TEST_COMPANY, guidance['message'])
        self.assertTrue(any(TEST_COMPANY in step for step in guidance['steps']))
        self.assertNotIn('install-app', ' '.join(guidance['steps']))

    def test_a_site_without_ksa_compliance_is_told_to_install_it_first(self):
        with (
            mock.patch.object(zatca, 'get_phase_for_company', return_value='None'),
            mock.patch.object(zatca, 'is_ksa_compliance_installed', return_value=False),
        ):
            guidance = zatca.setup_guidance(TEST_COMPANY)

        self.assertFalse(guidance['ksa_compliance_installed'])
        self.assertIn('install-app ksa_compliance', guidance['steps'][0])

    def test_the_steps_say_unreported_invoices_should_stay_drafts(self):
        with mock.patch.object(zatca, 'get_phase_for_company', return_value='None'):
            steps = ' '.join(zatca.setup_guidance(TEST_COMPANY)['steps'])

        self.assertIn('NOT reported to ZATCA', steps)
        self.assertIn('"submit": false', steps)

    def test_a_listing_leaves_the_steps_out(self):
        invoice = v1.create_invoice(**_payload('ZAPI-CO-LIST-001'))['data']['invoice']['invoice']
        with mock.patch.object(zatca, 'get_phase_for_company', return_value='None'):
            block = zatca.get_zatca_details(invoice, 'Sales Invoice', include_setup=False)

        self.assertNotIn('setup', block)
        self.assertNotIn('setup_required', block)
