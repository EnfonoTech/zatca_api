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

from zatca_api.services import failures, puller
from zatca_api.tests.test_api_v1 import ZATCAAPITestCase
from zatca_api.zatca_api.doctype.zatca_api_source.zatca_api_source import _redact, _vendor_reply


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


class _StubSettings:
    """Just enough of ZATCA API Settings for the puller, so a test needs no saved Single."""

    log_requests = 1
    enabled = 1
    pull_enabled = 1

    def __init__(self, row):
        self.sources = [row]
        self._row = row

    def get(self, key):
        return self.sources if key == 'sources' else None

    def get_source(self, name):
        return self._row if name == self._row.source_name else None


_FEED_ITEM = {'external_id': 'MSG-1', 'customer': 'C', 'items': [{'item_code': 'I', 'qty': 1, 'rate': 1}]}


class TestImportSourceMessages(ZATCAAPITestCase):
    """A record the pull cannot import must say why - in the result and in the request log."""

    def _import(self, build_effect, name='MSG Src'):
        row = _source(source_name=name)
        with (
            patch.object(puller, 'get_settings', return_value=_StubSettings(row)),
            patch.object(puller, 'fetch_source', return_value=([dict(_FEED_ITEM)], None, False)),
            patch.object(puller.invoice_service, 'build_invoice', side_effect=build_effect),
        ):
            return puller.import_source(name)

    def _log_rows(self, name):
        return frappe.get_all(
            'ZATCA API Request Log',
            filters={'endpoint': f'pull:{name}'},
            fields=['status', 'external_id', 'error'],
        )

    def test_erpnext_validation_errors_keep_their_message(self):
        """The KWD case: ERPNext refuses, and the feed's owner needs to read why."""
        reason = 'Exchange Rate is mandatory. Maybe Currency Exchange record is not created for KWD to SAR.'
        results = self._import(frappe.ValidationError(reason))

        self.assertEqual(results['failed'], 1)
        detail = results['details'][0]
        self.assertIn('Exchange Rate is mandatory', detail['error'])
        self.assertEqual(detail['code'], 'validation_error')

        row = self._log_rows('MSG Src')[0]
        self.assertEqual(row.status, 'Failed')
        self.assertEqual(row.external_id, 'MSG-1')
        self.assertIn('Exchange Rate is mandatory', row.error)
        self.assertNotIn('Traceback', row.error)

    def test_an_unexpected_error_stays_generic_in_the_result(self):
        """Its traceback belongs in the Error Log, not in something a caller reads."""
        results = self._import(RuntimeError('db exploded'), name='MSG Boom')

        self.assertEqual(results['failed'], 1)
        self.assertEqual(results['details'][0]['error'], 'Unhandled error, see Error Log')

    def test_a_payload_error_is_still_reported_by_its_message(self):
        from zatca_api.services.payload import PayloadError

        results = self._import(PayloadError('Buyer address has no building number.'), name='MSG Payload')

        self.assertEqual(results['details'][0]['error'], 'Buyer address has no building number.')
        self.assertEqual(self._log_rows('MSG Payload')[0].error, 'Buyer address has no building number.')

    def test_a_refused_login_is_recorded_for_the_source_with_its_reason(self):
        """The outage that read 'Crashed, see Error Log' and was visible nowhere an integrator looks."""
        row = _source(source_name='MSG Down')
        stub = _StubSettings(row)
        reason = (
            'Login failed for source MSG Down at https://up.example.com/login: 403 Client Error. '
            'The server said: Account has been blocked.'
        )
        with (
            patch.object(puller, 'get_settings', return_value=stub),
            patch.object(puller, 'import_source', side_effect=frappe.ValidationError(reason)),
        ):
            summary = puller.pull_all_sources()

        self.assertIn('Account has been blocked', summary['sources'][0]['error'])

        rows = self._log_rows('MSG Down')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, 'Failed')
        self.assertFalse(rows[0].external_id, 'a source-level failure has no external id')
        self.assertIn('Account has been blocked', rows[0].error)

        health = failures.source_health(stub)[0]
        self.assertEqual(health['status'], 'failing')
        self.assertIn('Account has been blocked', health['last_error']['message'])

    def test_a_blocked_login_travels_the_whole_chain_to_the_source_health(self):
        """The real incident end to end: nothing between the cron and the login is patched out."""
        row = _source(
            source_name='MSG Chain',
            auth_type='Login (Token)',
            token_url='https://up.example.com/login',
            auth_username='api-user',
            token_request_body='',
            token_response_path='',
        )
        row.auth_secret = 'S3cretPassw0rd'
        stub = _StubSettings(row)
        body = (
            '{"isSuccess":false,"data":null,'
            '"error":"Account has been blocked. Please contact your administrator For support."}'
        )

        with (
            patch.object(puller, 'get_settings', return_value=stub),
            patch('requests.post', return_value=_LoginResponse(403, body)),
        ):
            summary = puller.pull_all_sources()

        self.assertIn('The server said: Account has been blocked.', summary['sources'][0]['error'])
        self.assertNotIn('S3cretPassw0rd', summary['sources'][0]['error'])

        health = failures.source_health(stub)[0]
        self.assertEqual(health['status'], 'failing')
        self.assertIn('Account has been blocked', health['last_error']['message'])
        self.assertNotIn('S3cretPassw0rd', health['last_error']['message'])

    def test_an_unexpected_crash_of_a_source_is_logged_without_leaking_detail(self):
        row = _source(source_name='MSG Crash')
        with (
            patch.object(puller, 'get_settings', return_value=_StubSettings(row)),
            patch.object(puller, 'import_source', side_effect=RuntimeError('secret internals')),
        ):
            summary = puller.pull_all_sources()

        self.assertEqual(summary['sources'][0]['error'], 'Crashed, see Error Log')
        error = self._log_rows('MSG Crash')[0].error
        self.assertIn('see the Error Log', error)
        self.assertNotIn('secret internals', error)


class _LoginResponse:
    """A reply to the login POST that raises on an error status, as ``requests`` does."""

    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        import requests

        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f'{self.status_code} Client Error: Forbidden for url: https://up.example.com/login'
            )

    def json(self):
        return json.loads(self.text)


class TestLoginReply(ZATCAAPITestCase):
    """'403 Forbidden' says the login failed. What the server said says why."""

    PASSWORD = 'S3cretPassw0rd'

    def _row(self):
        row = _source(
            auth_type='Login (Token)',
            token_url='https://up.example.com/login',
            auth_username='api-user',
            token_request_body='',
            token_response_path='',
        )
        row.auth_secret = self.PASSWORD
        return row

    def _login_error(self, response=None, side_effect=None):
        with patch('requests.post', return_value=response, side_effect=side_effect):
            with self.assertRaises(frappe.ValidationError) as caught:
                self._row()._fetch_login_token()
        from zatca_api.services.envelope import clean_message

        return clean_message(caught.exception)

    def test_the_servers_reason_is_in_the_error(self):
        body = '{"isSuccess":false,"data":null,"error":"Account has been blocked. Please contact your administrator."}'
        message = self._login_error(_LoginResponse(403, body))

        self.assertIn('403', message)
        self.assertIn('The server said: Account has been blocked.', message)
        # said once: requests' own text repeats the url, and the sentence has already named it
        self.assertEqual(message.count('https://up.example.com/login'), 1)

    def test_the_password_and_username_are_never_echoed(self):
        body = '{"error":"Invalid password S3cretPassw0rd for api-user"}'
        message = self._login_error(_LoginResponse(401, body))

        self.assertNotIn(self.PASSWORD, message)
        self.assertNotIn('api-user', message)
        self.assertIn('[redacted]', message)

    def test_an_html_error_page_is_reduced_to_its_text(self):
        html = '<html><body><h1>403 - Forbidden: Access is denied.</h1></body></html>'
        message = self._login_error(_LoginResponse(403, html))

        self.assertIn('Access is denied', message)
        self.assertNotIn('<h1>', message)

    def test_no_body_means_no_clause(self):
        message = self._login_error(_LoginResponse(403, ''))
        self.assertNotIn('The server said', message)

    def test_a_connection_failure_has_no_reply_to_quote(self):
        import requests

        message = self._login_error(side_effect=requests.exceptions.ConnectionError('refused'))
        self.assertIn('Login failed for source Test Source', message)
        self.assertNotIn('The server said', message)


class TestVendorReplyRedaction(ZATCAAPITestCase):
    def test_reads_the_usual_keys_whatever_their_case(self):
        for body in ('{"Message": "Nope"}', '{"detail": "Nope"}', '{"title": "Nope"}', '{"ERROR": "Nope"}'):
            with self.subTest(body=body):
                self.assertEqual(_vendor_reply(_LoginResponse(403, body)), 'Nope')

    def test_a_structured_error_is_kept_as_json_text(self):
        reply = _vendor_reply(_LoginResponse(400, '{"error": {"code": 17, "text": "locked"}}'))
        self.assertIn('locked', reply)

    def test_tokens_and_emails_are_redacted(self):
        token = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abcdefghijklmnop'
        reply = _vendor_reply(_LoginResponse(400, f'{{"error": "bad token {token} for user@example.com"}}'))

        self.assertNotIn(token, reply)
        self.assertNotIn('user@example.com', reply)
        self.assertIn('[email]', reply)

    def test_the_reply_is_capped(self):
        long_reply = _LoginResponse(400, '{"error": "' + 'x ' * 400 + '"}')
        self.assertLessEqual(len(_vendor_reply(long_reply)), 300)

    def test_no_response_is_an_empty_reason(self):
        self.assertEqual(_vendor_reply(None), '')

    def test_redact_ignores_a_secret_too_short_to_be_one(self):
        """A 2-character 'password' would otherwise blank out half of every message."""
        self.assertEqual(_redact('ab is fine', ('ab',)), 'ab is fine')
