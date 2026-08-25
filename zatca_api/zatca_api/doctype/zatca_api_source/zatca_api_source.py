# zatca_api/zatca_api/doctype/zatca_api_source/zatca_api_source.py
# Copyright (c) 2026, Enfono Technologies and contributors

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, cint, cstr, get_datetime, now_datetime


# A JWT is refreshed this many seconds before it expires, so a token cannot lapse
# between build_headers() and the request it authenticates.
TOKEN_REFRESH_MARGIN = 60
TOKEN_CACHE_PREFIX = 'zatca_api::source_token::'
DEFAULT_LOGIN_BODY = '{"username": "{{username}}", "password": "{{password}}"}'


def _jwt_expiry(token: str) -> int | None:
    """Seconds until a JWT's ``exp``, or None if the token is not a readable JWT.

    The signature is deliberately not verified: this is the *client* of the token and has
    no key to verify with. The claim is read only to schedule a refresh, so a wrong value
    costs one extra login, never a security decision.
    """
    import base64
    import time

    parts = cstr(token).split('.')
    if len(parts) != 3:
        return None

    try:
        payload = parts[1] + '=' * (-len(parts[1]) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get('exp')
    except Exception:
        return None

    if not exp:
        return None
    return cint(exp) - int(time.time())


class ZATCAAPISource(Document):
    """Child row of ZATCA API Settings describing one upstream pull endpoint."""

    # ------------------------------------------------------------------ auth

    def build_headers(self) -> dict:
        """Request headers: Accept, the auth header, then any extra headers.

        The secret is read through ``get_password`` so it stays encrypted at rest and
        never appears in the doctype JSON, a fixture, or a git diff.
        """
        headers = {'Accept': 'application/json'}
        secret = self.get_password('auth_secret', raise_exception=False)

        if self.auth_type == 'Header Key' and secret:
            headers[(self.auth_header_name or 'x-api-key').strip()] = secret
        elif self.auth_type == 'Bearer Token' and secret:
            headers['Authorization'] = f'Bearer {secret}'
        elif self.auth_type == 'Login (Token)':
            headers['Authorization'] = f'Bearer {self.login_token()}'

        for line in cstr(self.custom_headers).splitlines():
            line = line.strip()
            if not line or line.startswith('#') or ':' not in line:
                continue
            name, _sep, value = line.partition(':')
            name = name.strip()
            # Never let a plaintext extra header shadow the encrypted auth header.
            if name and name.lower() not in {k.lower() for k in headers}:
                headers[name] = value.strip()

        return headers

    def login_token(self) -> str:
        """A valid bearer token, from cache or by logging in.

        The token is cached in Redis rather than on this row: it is short-lived, it is a
        credential, and writing it back would mean a DB write on every pull of a child row
        inside a Single. Redis expiry also does the invalidation for us.
        """
        key = TOKEN_CACHE_PREFIX + cstr(self.source_name)
        cached = frappe.cache().get_value(key)
        if cached:
            return cstr(cached)

        token = self._fetch_login_token()
        ttl = _jwt_expiry(token)
        if ttl is None:
            # Not a JWT, so trust the configured fallback instead of guessing.
            ttl = cint(self.token_ttl_seconds) or 3300
        ttl -= TOKEN_REFRESH_MARGIN

        if ttl > 0:
            frappe.cache().set_value(key, token, expires_in_sec=ttl)
        return token

    def _fetch_login_token(self) -> str:
        """POST the credentials to the login URL and dig the token out of the reply."""
        import requests

        if not self.token_url:
            frappe.throw(
                _('Source {0} uses Login (Token) but has no Token / Login URL.').format(
                    self.source_name
                )
            )

        secret = self.get_password('auth_secret', raise_exception=False) or ''
        template = cstr(self.token_request_body).strip() or DEFAULT_LOGIN_BODY
        # json.dumps then strip the quotes: escapes any quote or backslash in the value so
        # a password containing one cannot break out of the JSON string it sits in.
        body = template.replace('{{username}}', json.dumps(cstr(self.auth_username))[1:-1])
        body = body.replace('{{password}}', json.dumps(secret)[1:-1])

        try:
            payload = json.loads(body)
        except ValueError as exc:
            frappe.throw(
                _('Login Request Body for source {0} is not valid JSON: {1}').format(
                    self.source_name, exc
                )
            )

        try:
            response = requests.post(
                self.token_url,
                json=payload,
                headers={'Accept': 'application/json', 'Content-Type': 'application/json'},
                timeout=self.request_timeout,
                verify=bool(cint(self.verify_ssl)),
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            # Deliberately does not log the request body: it holds the password.
            frappe.throw(
                _('Login failed for source {0} at {1}: {2}').format(
                    self.source_name, self.token_url, cstr(exc)[:200]
                )
            )

        path = cstr(self.token_response_path).strip() or 'data.token'
        token = data
        for part in path.split('.'):
            if not isinstance(token, dict):
                token = None
                break
            token = token.get(part)

        if not token:
            frappe.throw(
                _('Login for source {0} succeeded but no token was found at {1!r} in the '
                  'response. Check Token Path In Response.').format(self.source_name, path)
            )

        return cstr(token)

    def build_auth(self):
        """A ``requests``-compatible auth tuple for Basic auth, else None."""
        if self.auth_type != 'Basic':
            return None
        secret = self.get_password('auth_secret', raise_exception=False)
        return (self.auth_username or '', secret or '')

    @property
    def request_timeout(self) -> int:
        return cint(self.timeout) or 30

    # ------------------------------------------------------- request shaping

    def date_window(self) -> dict:
        """The ``{from_date, to_date}`` placeholder values for this pull.

        The window starts at ``last_pulled_at`` minus ``lookback_days``. The overlap
        is deliberate: a document the upstream system back-dated after our previous
        poll would otherwise be missed forever. Dedup on the external id makes
        re-reading the overlap harmless.
        """
        if self.incremental_mode != 'Date Window':
            return {}

        date_format = cstr(self.date_format).strip() or '%Y-%m-%d'
        lookback = cint(self.lookback_days) or 7
        now = now_datetime()

        anchor = get_datetime(self.last_pulled_at) if self.last_pulled_at else now
        start = add_days(anchor, -lookback)

        return {
            'from_date': get_datetime(start).strftime(date_format),
            'to_date': now.strftime(date_format),
        }

    def substitute(self, text: str, context: dict) -> str:
        """Replace ``{placeholder}`` tokens, leaving unknown ones untouched.

        ``str.format`` is not used on purpose: an upstream URL or JSON body legitimately
        contains braces, and format() would raise KeyError or misread them.
        """
        text = cstr(text)
        for key, value in (context or {}).items():
            text = text.replace('{' + key + '}', cstr(value))
        return text

    def build_query_params(self, context: dict) -> dict:
        params = {}

        for line in cstr(self.query_params).splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _sep, value = line.partition('=')
            params[key.strip()] = self.substitute(value.strip(), context)

        window = {k: v for k, v in context.items() if k in ('from_date', 'to_date')}
        if self.incremental_mode == 'Date Window':
            if self.from_param and window.get('from_date'):
                params[cstr(self.from_param).strip()] = window['from_date']
            if self.to_param and window.get('to_date'):
                params[cstr(self.to_param).strip()] = window['to_date']

        if self.pagination_mode and self.pagination_mode != 'None':
            if self.page_size_param and cint(self.page_size):
                params[cstr(self.page_size_param).strip()] = cint(self.page_size)

            if self.pagination_mode == 'Page Number' and self.page_param:
                params[cstr(self.page_param).strip()] = context.get('page')
            elif self.pagination_mode == 'Offset' and self.page_param:
                params[cstr(self.page_param).strip()] = context.get('offset')
            elif self.pagination_mode == 'Cursor' and self.cursor_param and context.get('cursor'):
                params[cstr(self.cursor_param).strip()] = context['cursor']

        return {k: v for k, v in params.items() if v is not None and cstr(v) != ''}

    def build_body(self, context: dict):
        """The POST body, with placeholders substituted. None for GET or no body."""
        if (self.http_method or 'GET').upper() != 'POST':
            return None

        raw = cstr(self.request_body).strip()
        if not raw:
            return None

        substituted = self.substitute(raw, context)
        try:
            return json.loads(substituted)
        except (ValueError, TypeError):
            frappe.throw(
                frappe._('Source {0}: Request Body is not valid JSON after substitution.').format(
                    self.source_name
                )
            )

    def page_context(self, page_index: int, cursor: str | None = None) -> dict:
        """Placeholder values for one page of a paginated pull."""
        context = dict(self.date_window())
        size = cint(self.page_size) or 100
        context['page_size'] = size
        # `or 1` would be wrong here: an API that numbers pages from 0 stores an
        # explicit 0, which is falsy. Only a genuinely unset field falls back to 1.
        first_page = cint(self.start_page) if cstr(self.start_page).strip() != '' else 1
        context['page'] = first_page + page_index
        context['offset'] = page_index * size
        context['cursor'] = cstr(cursor or '')
        return context

    @property
    def page_limit(self) -> int:
        if not self.pagination_mode or self.pagination_mode == 'None':
            return 1
        return max(cint(self.max_pages) or 20, 1)
