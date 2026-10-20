# zatca_api/tests/test_failures.py
# Copyright (c) 2026, Enfono Technologies and contributors
"""Tests for the read side of the request log.

The point of the feature: a record that never imported was refused for a reason, and that
reason has to reach the person who can fix it - through the API, not through a desk screen
they cannot open. These tests write real ``ZATCA API Request Log`` rows, so they exercise the
actual queries, scoping and grouping rather than a stub of them.
"""

from types import SimpleNamespace

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from zatca_api.api import v1
from zatca_api.services import failures
from zatca_api.tests.test_api_v1 import ZATCAAPITestCase, _payload


def _log(
    endpoint,
    status,
    external_id=None,
    error=None,
    user='Administrator',
    source=None,
    minutes_ago=0,
):
    """Insert a request-log row, back-dated so the order of attempts is under the test's control."""
    doc = frappe.get_doc(
        {
            'doctype': 'ZATCA API Request Log',
            'endpoint': endpoint,
            'direction': 'Outbound' if endpoint.startswith('pull:') else 'Inbound',
            'status': status,
            'external_id': external_id,
            'source_name': source,
            'error': error,
            'user': user,
        }
    )
    doc.insert(ignore_permissions=True)
    stamp = add_to_date(now_datetime(), minutes=-minutes_ago)
    frappe.db.set_value('ZATCA API Request Log', doc.name, 'creation', stamp, update_modified=False)
    return stamp


def _settings_with(*names, enabled=1):
    """Just enough of ZATCA API Settings for source_health: a ``get('sources')`` of named rows."""
    rows = [SimpleNamespace(source_name=name, enabled=enabled) for name in names]
    return SimpleNamespace(get=lambda key: rows if key == 'sources' else None)


class TestSummariseError(FrappeTestCase):
    def test_a_clean_message_passes_through(self):
        message = 'A complete buyer address is mandatory for a B2B customer.'
        self.assertEqual(failures.summarise_error(message), message)

    def test_a_traceback_is_reduced_to_its_exception_line(self):
        traceback = (
            'Traceback (most recent call last):\n'
            '  File "apps/x/y.py", line 1, in f\n'
            '    boom()\n'
            'frappe.exceptions.ValidationError: Exchange Rate is mandatory.\n'
        )
        self.assertEqual(failures.summarise_error(traceback), 'Exchange Rate is mandatory.')

    def test_a_message_that_merely_contains_a_colon_keeps_its_start(self):
        """Only a traceback loses its module prefix; a normal message is left exactly alone."""
        message = 'Item row 3: qty must not be zero.'
        self.assertEqual(failures.summarise_error(message), message)

    def test_html_and_whitespace_are_flattened(self):
        self.assertEqual(
            failures.summarise_error('<b>No</b>  exchange rate\n for KWD'), 'No exchange rate for KWD'
        )

    def test_long_text_is_cut_with_an_ellipsis(self):
        cut = failures.summarise_error('x' * 800, limit=50)
        self.assertEqual(len(cut), 50)
        self.assertTrue(cut.endswith('…'))

    def test_empty_values(self):
        self.assertEqual(failures.summarise_error(''), '')
        self.assertEqual(failures.summarise_error(None), '')


class TestListFailures(ZATCAAPITestCase):
    SRC = 'FLR Src'

    def _list(self, **kwargs):
        kwargs.setdefault('source', self.SRC)
        return failures.list_failures('Administrator', **kwargs)

    def test_an_id_whose_newest_attempt_failed_is_listed_with_its_reason(self):
        _log(
            'pull:FLR Src',
            'Failed',
            'FLR-1',
            'Buyer address has no building number (custom_building_number).',
            source=self.SRC,
            minutes_ago=5,
        )
        result = self._list()

        self.assertEqual(result['total'], 1)
        entry = result['errors'][0]
        self.assertEqual(entry['external_id'], 'FLR-1')
        self.assertIn('no building number', entry['message'])
        self.assertEqual(entry['channel'], 'pull')
        self.assertEqual(entry['source'], self.SRC)
        self.assertEqual(entry['attempts'], 1)

    def test_an_id_that_has_since_gone_through_is_not_listed(self):
        """The list is what still needs fixing, not a history."""
        _log('pull:FLR Src', 'Failed', 'FLR-2', 'old problem', source=self.SRC, minutes_ago=20)
        _log('pull:FLR Src', 'Success', 'FLR-2', source=self.SRC, minutes_ago=5)
        self.assertEqual(self._list()['total'], 0)

    def test_attempts_count_only_the_current_run_of_failures(self):
        _log('pull:FLR Src', 'Failed', 'FLR-3', 'before the fix', source=self.SRC, minutes_ago=40)
        _log('pull:FLR Src', 'Success', 'FLR-3', source=self.SRC, minutes_ago=30)
        first = None
        for minutes in (20, 10, 1):
            stamp = _log(
                'pull:FLR Src', 'Failed', 'FLR-3', 'since the fix', source=self.SRC, minutes_ago=minutes
            )
            first = first or stamp

        entry = self._list()['errors'][0]
        self.assertEqual(entry['attempts'], 3)
        self.assertEqual(entry['first_seen'], str(first)[:19])
        self.assertEqual(entry['message'], 'since the fix')

    def test_a_traceback_in_the_log_reads_as_its_exception_line(self):
        _log(
            'pull:FLR Src',
            'Failed',
            'FLR-4',
            'Traceback (most recent call last):\n  File "a.py", line 2, in f\nValueError: bad amount\n',
            source=self.SRC,
        )
        self.assertEqual(self._list()['errors'][0]['message'], 'bad amount')

    def test_most_recent_failure_comes_first(self):
        _log('pull:FLR Src', 'Failed', 'FLR-OLD', 'a', source=self.SRC, minutes_ago=30)
        _log('pull:FLR Src', 'Failed', 'FLR-NEW', 'b', source=self.SRC, minutes_ago=1)
        ids = [entry['external_id'] for entry in self._list()['errors']]
        self.assertEqual(ids, ['FLR-NEW', 'FLR-OLD'])

    def test_another_users_push_attempt_is_not_visible(self):
        """One integration must not be able to read another's rejected invoices."""
        _log('create_invoice', 'Failed', 'FLR-PUSH', 'private to Guest', user='Guest')

        mine = failures.list_failures('Administrator', external_id='FLR-PUSH')
        theirs = failures.list_failures('Guest', external_id='FLR-PUSH')

        self.assertEqual(mine['total'], 0)
        self.assertEqual(theirs['total'], 1)
        self.assertEqual(theirs['errors'][0]['channel'], 'api')

    def test_a_pull_attempt_is_visible_to_every_caller(self):
        _log('pull:FLR Src', 'Failed', 'FLR-PULL', 'site level', user='Guest', source=self.SRC)
        self.assertEqual(failures.list_failures('Administrator', external_id='FLR-PULL')['total'], 1)

    def test_filters_by_external_id_and_by_source(self):
        _log('pull:FLR Src', 'Failed', 'FLR-A', 'a', source=self.SRC)
        _log('pull:FLR Other', 'Failed', 'FLR-A', 'a', source='FLR Other')
        _log('pull:FLR Src', 'Failed', 'FLR-B', 'b', source=self.SRC)

        self.assertEqual(self._list(external_id='FLR-A')['total'], 1)
        self.assertEqual(self._list()['total'], 2)
        self.assertEqual(self._list(source='FLR Other')['total'], 1)

    def test_old_attempts_fall_outside_the_window(self):
        _log('pull:FLR Src', 'Failed', 'FLR-OLD2', 'ancient', source=self.SRC, minutes_ago=60 * 30)
        self.assertEqual(self._list(hours=24)['total'], 0)
        self.assertEqual(self._list(hours=48)['total'], 1)

    def test_paging_reports_the_total_and_whether_more_remain(self):
        for index in range(5):
            _log('pull:FLR Src', 'Failed', f'FLR-PG-{index}', 'x', source=self.SRC, minutes_ago=index + 1)

        first = self._list(limit=2, start=0)
        last = self._list(limit=2, start=4)

        self.assertEqual((first['count'], first['total'], first['has_more']), (2, 5, True))
        self.assertEqual((last['count'], last['total'], last['has_more']), (1, 5, False))

    def test_limits_are_clamped(self):
        result = self._list(hours=99999, limit=100000)
        self.assertEqual(result['window_hours'], failures.MAX_HOURS)
        self.assertEqual(result['limit'], failures.MAX_LIMIT)
        self.assertEqual(self._list(hours=0)['window_hours'], failures.DEFAULT_HOURS)

    def test_the_invoice_that_owns_the_id_is_named(self):
        """The clash case: the id is taken, and the caller is told by what."""
        created = v1.create_invoice(**_payload('FLR-OWNED'))
        name = created['data']['invoice']['invoice']
        _log('pull:FLR Src', 'Failed', 'FLR-OWNED', 'id already belongs to it', source=self.SRC)

        entry = self._list()['errors'][0]
        self.assertEqual(entry['existing_invoice'], name)
        self.assertEqual(entry['existing_document_type'], 'sales_invoice')


class TestSourceHealth(ZATCAAPITestCase):
    def _health(self, name):
        return failures.source_health(_settings_with(name))[0]

    def test_a_source_with_no_activity_is_idle(self):
        health = self._health('SH Idle')
        self.assertEqual(health['status'], 'idle')
        self.assertIsNone(health['last_error'])
        self.assertIsNone(health['last_success_at'])

    def test_a_source_with_a_recent_success_is_ok(self):
        _log('pull:SH Ok', 'Success', 'X-1', source='SH Ok', minutes_ago=3)
        health = self._health('SH Ok')
        self.assertEqual(health['status'], 'ok')
        self.assertTrue(health['last_success_at'])

    def test_a_refused_login_is_failing_with_its_reason_and_since_when(self):
        _log('pull:SH Down', 'Success', 'X-1', source='SH Down', minutes_ago=90)
        reason = 'Login failed for source SH Down: 403. The server said: Account has been blocked.'
        started = _log('pull:SH Down', 'Failed', error=reason, source='SH Down', minutes_ago=60)
        _log('pull:SH Down', 'Failed', error=reason, source='SH Down', minutes_ago=45)
        _log('pull:SH Down', 'Failed', error=reason, source='SH Down', minutes_ago=30)

        health = self._health('SH Down')

        self.assertEqual(health['status'], 'failing')
        self.assertIn('Account has been blocked', health['last_error']['message'])
        self.assertEqual(health['last_error']['attempts'], 3)
        self.assertEqual(health['last_error']['failing_since'], str(started)[:19])
        self.assertTrue(health['last_success_at'])

    def test_failed_records_do_not_make_a_reachable_source_failing(self):
        """The source answered; those failures are listed per external id instead."""
        _log('pull:SH Rec', 'Success', 'X-1', source='SH Rec', minutes_ago=10)
        _log('pull:SH Rec', 'Failed', 'X-2', 'bad address', source='SH Rec', minutes_ago=5)
        self.assertEqual(self._health('SH Rec')['status'], 'ok')

    def test_a_success_after_a_failure_clears_it(self):
        _log('pull:SH Back', 'Failed', error='down', source='SH Back', minutes_ago=30)
        _log('pull:SH Back', 'Success', 'X-1', source='SH Back', minutes_ago=5)
        health = self._health('SH Back')
        self.assertEqual(health['status'], 'ok')
        self.assertIsNone(health['last_error'])

    def test_a_disabled_source_is_still_reported_as_disabled(self):
        health = failures.source_health(_settings_with('SH Off', enabled=0))[0]
        self.assertFalse(health['enabled'])


class TestListImportErrorsEndpoint(ZATCAAPITestCase):
    def test_the_envelope_carries_errors_and_sources(self):
        _log('pull:EP Src', 'Failed', 'EP-1', 'Exchange Rate is mandatory.', source='EP Src')

        response = v1.list_import_errors(external_id='EP-1')

        self.assertTrue(response['success'], msg=response.get('errors'))
        data = response['data']
        self.assertEqual(data['errors'][0]['external_id'], 'EP-1')
        self.assertIn('Exchange Rate', data['errors'][0]['message'])
        self.assertIn('sources', data)

    def test_it_needs_an_authenticated_caller(self):
        frappe.set_user('Guest')
        response = v1.list_import_errors()
        self.assertFalse(response['success'])
        self.assertEqual(response['errors'][0]['code'], 'unauthorized')


class TestNotFoundExplainsItself(ZATCAAPITestCase):
    def test_plain_not_found_when_nothing_was_ever_attempted(self):
        response = v1.get_status(external_id='NF-NEVER')

        error = response['errors'][0]
        self.assertEqual(error['code'], 'not_found')
        self.assertEqual(error['message'], 'Invoice not found.')
        self.assertNotIn('last_import_error', error['details'])

    def test_the_reason_is_in_the_message_when_the_last_attempt_failed(self):
        reason = 'A complete buyer address is mandatory for a B2B customer.'
        _log('pull:NF Src', 'Failed', 'NF-1', reason, source='NF Src', minutes_ago=2)

        for endpoint in (v1.get_status, v1.get_invoice):
            with self.subTest(endpoint=endpoint.__name__):
                error = endpoint(external_id='NF-1')['errors'][0]
                self.assertEqual(error['code'], 'not_found')
                self.assertIn(reason, error['message'])
                self.assertEqual(error['details']['last_import_error']['message'], reason)
                self.assertEqual(error['details']['last_import_error']['source'], 'NF Src')

    def test_a_later_success_hides_the_old_reason(self):
        _log('pull:NF Src', 'Failed', 'NF-2', 'old problem', source='NF Src', minutes_ago=10)
        _log('pull:NF Src', 'Success', 'NF-2', source='NF Src', minutes_ago=1)

        error = v1.get_status(external_id='NF-2')['errors'][0]
        self.assertEqual(error['message'], 'Invoice not found.')
        self.assertNotIn('last_import_error', error['details'])
