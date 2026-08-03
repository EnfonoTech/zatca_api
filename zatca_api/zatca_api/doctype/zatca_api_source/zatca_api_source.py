# zatca_api/zatca_api/doctype/zatca_api_source/zatca_api_source.py
# Copyright (c) 2026, Enfono Technologies and contributors

import json

import frappe
from frappe.model.document import Document
from frappe.utils import add_days, cint, cstr, get_datetime, now_datetime


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
