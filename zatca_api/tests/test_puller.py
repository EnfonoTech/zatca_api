# zatca_api/tests/test_puller.py
# Copyright (c) 2026, Enfono Technologies and contributors
"""Tests for pull mode: request shaping, incremental windows and pagination.

HTTP is faked at the ``requests.request`` boundary, so these exercise the real URL,
header, query-param, body and paging logic without a network call.
"""

import json
from unittest.mock import patch

import frappe
from frappe.utils import add_days, cint, getdate, today

from zatca_api.services import puller
from zatca_api.tests.test_api_v1 import ZATCAAPITestCase


class FakeResponse:
    def __init__(self, payload, status_code=200, text=None):
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError('not json')
        return self._payload


def _source(**overrides):
    """A detached ZATCA API Source row, so tests do not need to save the parent."""
    row = frappe.new_doc('ZATCA API Source')
    row.source_name = 'Test Source'
    row.enabled = 1
    row.document_type = 'Sales Invoice'
    row.endpoint_url = 'https://upstream.example.com/invoices'
    row.http_method = 'GET'
    row.auth_type = 'None'
    row.payload_root = 'payloads'
    row.status_key = 'status'
    row.status_ok_value = 'success'
    row.timeout = 30
    row.verify_ssl = 1
    row.auto_submit = 0
    row.incremental_mode = 'None'
    row.pagination_mode = 'None'
    for key, value in overrides.items():
        row.set(key, value)
    return row


class TestHeaders(ZATCAAPITestCase):
    def test_header_key_auth(self):
        row = _source(auth_type='Header Key', auth_header_name='x-api-key')
        row.auth_secret = 'sekret'
        headers = row.build_headers()
        self.assertEqual(headers['x-api-key'], 'sekret')
        self.assertEqual(headers['Accept'], 'application/json')

    def test_bearer_auth(self):
        row = _source(auth_type='Bearer Token')
        row.auth_secret = 'tok123'
        self.assertEqual(row.build_headers()['Authorization'], 'Bearer tok123')

    def test_basic_auth_tuple(self):
        row = _source(auth_type='Basic', auth_username='bob')
        row.auth_secret = 'pw'
        self.assertEqual(row.build_auth(), ('bob', 'pw'))

    def test_no_auth_returns_no_auth_tuple(self):
        self.assertIsNone(_source().build_auth())

    def test_extra_headers_are_added(self):
        """The gap this closes: APIs needing more than one custom header."""
        row = _source(custom_headers='X-Tenant-Id: acme\nX-Trace: on\n# a comment\n')
        headers = row.build_headers()
        self.assertEqual(headers['X-Tenant-Id'], 'acme')
        self.assertEqual(headers['X-Trace'], 'on')

    def test_extra_header_cannot_shadow_the_encrypted_auth_header(self):
        """A plaintext line must not be able to override the encrypted secret."""
        row = _source(
            auth_type='Header Key',
            auth_header_name='x-api-key',
            custom_headers='X-API-KEY: plaintext-override',
        )
        row.auth_secret = 'the-real-secret'
        self.assertEqual(row.build_headers()['x-api-key'], 'the-real-secret')


class TestSubstitution(ZATCAAPITestCase):
    def test_placeholders_replaced(self):
        row = _source()
        out = row.substitute(
            '/v1/{from_date}/to/{to_date}?p={page}',
            {'from_date': '2026-08-01', 'to_date': '2026-08-04', 'page': 3},
        )
        self.assertEqual(out, '/v1/2026-08-01/to/2026-08-04?p=3')

    def test_unknown_placeholders_are_left_alone(self):
        """Braces occur legitimately in URLs and JSON; they must not raise."""
        row = _source()
        self.assertEqual(row.substitute('keep {unknown} intact', {'page': 1}), 'keep {unknown} intact')


class TestDateWindow(ZATCAAPITestCase):
    def test_no_window_when_disabled(self):
        self.assertEqual(_source().date_window(), {})

    def test_first_pull_uses_the_lookback(self):
        row = _source(
            incremental_mode='Date Window', from_param='from_date', to_param='to_date', lookback_days=7
        )
        window = row.date_window()
        self.assertEqual(getdate(window['to_date']), getdate(today()))
        self.assertEqual(getdate(window['from_date']), getdate(add_days(today(), -7)))

    def test_window_anchors_on_last_pulled_at_with_overlap(self):
        """The overlap is deliberate: a back-dated upstream document must still arrive."""
        row = _source(
            incremental_mode='Date Window',
            from_param='from_date',
            lookback_days=2,
            last_pulled_at=add_days(today(), -10),
        )
        window = row.date_window()
        self.assertEqual(getdate(window['from_date']), getdate(add_days(today(), -12)))

    def test_custom_date_format(self):
        row = _source(incremental_mode='Date Window', from_param='f', date_format='%d/%m/%Y', lookback_days=1)
        self.assertRegex(row.date_window()['from_date'], r'^\d{2}/\d{2}/\d{4}$')


class TestQueryParams(ZATCAAPITestCase):
    def test_static_params(self):
        row = _source(query_params='branch=riyadh\ntype=tax\n')
        params = row.build_query_params({})
        self.assertEqual(params['branch'], 'riyadh')
        self.assertEqual(params['type'], 'tax')

    def test_params_accept_placeholders(self):
        row = _source(query_params='since={from_date}')
        self.assertEqual(row.build_query_params({'from_date': '2026-08-01'})['since'], '2026-08-01')

    def test_date_window_params_added(self):
        row = _source(incremental_mode='Date Window', from_param='start', to_param='end', lookback_days=3)
        params = row.build_query_params(row.date_window())
        self.assertIn('start', params)
        self.assertIn('end', params)

    def test_to_param_omitted_when_blank(self):
        row = _source(incremental_mode='Date Window', from_param='start', lookback_days=3)
        params = row.build_query_params(row.date_window())
        self.assertIn('start', params)
        self.assertEqual(len([k for k in params if k == 'end']), 0)

    def test_page_number_pagination_params(self):
        row = _source(
            pagination_mode='Page Number',
            page_param='page',
            page_size_param='limit',
            page_size=50,
            start_page=1,
        )
        params = row.build_query_params(row.page_context(0))
        self.assertEqual(params['page'], 1)
        self.assertEqual(params['limit'], 50)

        params = row.build_query_params(row.page_context(2))
        self.assertEqual(params['page'], 3)

    def test_zero_based_page_numbering(self):
        row = _source(
            pagination_mode='Page Number',
            page_param='page',
            start_page=0,
            page_size_param='limit',
            page_size=10,
        )
        self.assertEqual(row.build_query_params(row.page_context(0))['page'], 0)

    def test_offset_pagination_params(self):
        row = _source(pagination_mode='Offset', page_param='offset', page_size_param='limit', page_size=25)
        self.assertEqual(row.build_query_params(row.page_context(3))['offset'], 75)

    def test_cursor_param_only_sent_when_present(self):
        row = _source(
            pagination_mode='Cursor',
            cursor_param='cursor',
            next_cursor_key='meta.next',
            page_size_param='limit',
            page_size=10,
        )
        self.assertNotIn('cursor', row.build_query_params(row.page_context(0)))
        self.assertEqual(row.build_query_params(row.page_context(1, 'abc'))['cursor'], 'abc')


class TestRequestBody(ZATCAAPITestCase):
    def test_no_body_for_get(self):
        self.assertIsNone(_source(request_body='{"a": 1}').build_body({}))

    def test_post_body_with_substitution(self):
        row = _source(http_method='POST', request_body='{"from": "{from_date}", "size": 10}')
        body = row.build_body({'from_date': '2026-08-01'})
        self.assertEqual(body, {'from': '2026-08-01', 'size': 10})

    def test_invalid_body_after_substitution_throws(self):
        row = _source(http_method='POST', request_body='{"from": {from_date}}')
        with self.assertRaises(frappe.ValidationError):
            row.build_body({'from_date': 'not-quoted'})


class TestFetchPaging(ZATCAAPITestCase):
    def _payload(self, n, start=0):
        return {'status': 'success', 'payloads': [{'Naming Series': f'UP-{start + i}'} for i in range(n)]}

    def test_single_page_when_pagination_off(self):
        row = _source()
        with patch('requests.request', return_value=FakeResponse(self._payload(3))) as mock:
            items, error, truncated = puller.fetch_source(row)
        self.assertIsNone(error)
        self.assertEqual(len(items), 3)
        self.assertFalse(truncated)
        self.assertEqual(mock.call_count, 1)

    def test_page_number_pagination_stops_on_a_short_page(self):
        row = _source(
            pagination_mode='Page Number',
            page_param='page',
            page_size_param='limit',
            page_size=2,
            max_pages=10,
        )
        responses = [
            FakeResponse(self._payload(2, 0)),
            FakeResponse(self._payload(2, 2)),
            FakeResponse(self._payload(1, 4)),  # short page -> last
        ]
        with patch('requests.request', side_effect=responses) as mock:
            items, error, truncated = puller.fetch_source(row)
        self.assertIsNone(error)
        self.assertEqual(len(items), 5)
        self.assertFalse(truncated)
        self.assertEqual(mock.call_count, 3)

    def test_pagination_stops_on_an_empty_page(self):
        row = _source(
            pagination_mode='Page Number',
            page_param='page',
            page_size_param='limit',
            page_size=2,
            max_pages=10,
        )
        responses = [FakeResponse(self._payload(2)), FakeResponse(self._payload(0))]
        with patch('requests.request', side_effect=responses):
            items, error, truncated = puller.fetch_source(row)
        self.assertEqual(len(items), 2)
        self.assertFalse(truncated)

    def test_cursor_pagination_follows_and_stops(self):
        row = _source(
            pagination_mode='Cursor',
            cursor_param='cursor',
            next_cursor_key='meta.next',
            page_size_param='limit',
            page_size=2,
            max_pages=10,
        )
        first = self._payload(2, 0)
        first['meta'] = {'next': 'CUR2'}
        second = self._payload(2, 2)
        second['meta'] = {'next': None}
        with patch('requests.request', side_effect=[FakeResponse(first), FakeResponse(second)]) as mock:
            items, error, truncated = puller.fetch_source(row)
        self.assertIsNone(error)
        self.assertEqual(len(items), 4)
        self.assertFalse(truncated)
        # Second call must carry the cursor from the first response.
        self.assertEqual(mock.call_args_list[1].kwargs['params']['cursor'], 'CUR2')

    def test_hitting_the_page_limit_reports_truncation(self):
        """Silent truncation would look identical to a fully imported feed."""
        row = _source(
            pagination_mode='Page Number',
            page_param='page',
            page_size_param='limit',
            page_size=2,
            max_pages=2,
        )
        with patch('requests.request', return_value=FakeResponse(self._payload(2))):
            items, error, truncated = puller.fetch_source(row)
        self.assertIsNone(error)
        self.assertEqual(len(items), 4)
        self.assertTrue(truncated)


class TestFetchErrors(ZATCAAPITestCase):
    def test_non_200_is_reported_and_truncated(self):
        row = _source()
        long_html = '<html>' + ('x' * 5000) + '</html>'
        with patch('requests.request', return_value=FakeResponse(None, 500, long_html)):
            items, error, _t = puller.fetch_source(row)
        self.assertEqual(items, [])
        self.assertIn('HTTP 500', error)
        self.assertLess(len(error), 2200)

    def test_non_json_response(self):
        with patch('requests.request', return_value=FakeResponse(None, 200, 'not json')):
            items, error, _t = puller.fetch_source(_source())
        self.assertIn('not JSON', error)

    def test_envelope_status_mismatch(self):
        payload = {'status': 'error', 'payloads': []}
        with patch('requests.request', return_value=FakeResponse(payload)):
            items, error, _t = puller.fetch_source(_source())
        self.assertIn('expected', error)

    def test_payload_root_not_a_list(self):
        payload = {'status': 'success', 'payloads': 'oops'}
        with patch('requests.request', return_value=FakeResponse(payload)):
            items, error, _t = puller.fetch_source(_source())
        self.assertIn('did not resolve to a list', error)

    def test_blank_payload_root_uses_the_whole_body(self):
        with patch('requests.request', return_value=FakeResponse([{'a': 1}, {'a': 2}])):
            items, error, _t = puller.fetch_source(_source(payload_root='', status_key=''))
        self.assertIsNone(error)
        self.assertEqual(len(items), 2)

    def test_dotted_payload_root(self):
        payload = {'data': {'invoices': [{'a': 1}]}}
        with patch('requests.request', return_value=FakeResponse(payload)):
            items, error, _t = puller.fetch_source(_source(payload_root='data.invoices', status_key=''))
        self.assertIsNone(error)
        self.assertEqual(len(items), 1)

    def test_network_exception_is_caught(self):
        import requests

        with patch('requests.request', side_effect=requests.exceptions.Timeout('too slow')):
            items, error, _t = puller.fetch_source(_source())
        self.assertEqual(items, [])
        self.assertIn('Timeout', error)

    def test_a_timeout_is_always_passed_to_requests(self):
        """A request without a timeout can hang a background worker forever."""
        with patch(
            'requests.request', return_value=FakeResponse({'status': 'success', 'payloads': []})
        ) as mock:
            puller.fetch_source(_source(timeout=17))
        self.assertEqual(mock.call_args.kwargs['timeout'], 17)

    def test_timeout_falls_back_when_unset(self):
        with patch(
            'requests.request', return_value=FakeResponse({'status': 'success', 'payloads': []})
        ) as mock:
            puller.fetch_source(_source(timeout=0))
        self.assertEqual(mock.call_args.kwargs['timeout'], 30)


class TestSourceValidation(ZATCAAPITestCase):
    def _save_with_source(self, **overrides):
        settings = frappe.get_single('ZATCA API Settings')
        settings.sources = []
        row = settings.append('sources', {})
        base = {
            'source_name': 'Validated',
            'enabled': 1,
            'document_type': 'Sales Invoice',
            'endpoint_url': 'https://upstream.example.com/x',
            'http_method': 'GET',
            'auth_type': 'None',
            'timeout': 30,
            'verify_ssl': 1,
            'incremental_mode': 'None',
            'pagination_mode': 'None',
        }
        base.update(overrides)
        for key, value in base.items():
            row.set(key, value)
        settings.flags.ignore_permissions = True
        settings.save()
        return settings.sources[0]

    def test_bad_header_line_rejected(self):
        with self.assertRaises(frappe.ValidationError):
            self._save_with_source(custom_headers='no-colon-here')

    def test_bad_query_param_line_rejected(self):
        with self.assertRaises(frappe.ValidationError):
            self._save_with_source(query_params='no-equals-here')

    def test_invalid_request_body_json_rejected(self):
        with self.assertRaises(frappe.ValidationError):
            self._save_with_source(http_method='POST', request_body='{not json}')

    def test_date_window_requires_from_param(self):
        with self.assertRaises(frappe.ValidationError):
            self._save_with_source(incremental_mode='Date Window', from_param='')

    def test_cursor_pagination_requires_a_next_cursor_key(self):
        with self.assertRaises(frappe.ValidationError):
            self._save_with_source(pagination_mode='Cursor', next_cursor_key='')

    def test_page_pagination_requires_a_page_param(self):
        with self.assertRaises(frappe.ValidationError):
            self._save_with_source(pagination_mode='Page Number', page_param='')

    def test_sane_defaults_are_forced(self):
        row = self._save_with_source(
            pagination_mode='Page Number', page_param='page', page_size=0, max_pages=0
        )
        self.assertEqual(cint(row.page_size), 100)
        self.assertEqual(cint(row.max_pages), 20)

    def test_valid_full_configuration_saves(self):
        row = self._save_with_source(
            custom_headers='X-Tenant-Id: acme',
            query_params='branch=riyadh',
            http_method='POST',
            request_body='{"since": "{from_date}"}',
            incremental_mode='Date Window',
            from_param='from_date',
            to_param='to_date',
            lookback_days=5,
            pagination_mode='Cursor',
            cursor_param='cursor',
            next_cursor_key='meta.next',
            page_size=50,
            max_pages=10,
        )
        self.assertEqual(row.source_name, 'Validated')
        self.assertEqual(row.next_cursor_key, 'meta.next')
