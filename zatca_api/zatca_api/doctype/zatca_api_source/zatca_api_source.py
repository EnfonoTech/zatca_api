# zatca_api/zatca_api/doctype/zatca_api_source/zatca_api_source.py
# Copyright (c) 2026, Enfono Technologies and contributors
# For license information, please see license.txt

from frappe.model.document import Document
from frappe.utils import cint


class ZATCAAPISource(Document):
    """Child row of ZATCA API Settings describing one upstream pull endpoint."""

    def build_headers(self) -> dict:
        """Return request headers for this source.

        The secret is read through ``get_password`` so it stays encrypted at rest and
        is never present in the doctype JSON, a fixture, or a git diff.
        """
        headers = {'Accept': 'application/json'}
        secret = self.get_password('auth_secret', raise_exception=False)

        if self.auth_type == 'Header Key' and secret:
            headers[(self.auth_header_name or 'x-api-key').strip()] = secret
        elif self.auth_type == 'Bearer Token' and secret:
            headers['Authorization'] = f'Bearer {secret}'

        return headers

    def build_auth(self):
        """Return a ``requests``-compatible auth tuple for Basic auth, else None."""
        if self.auth_type != 'Basic':
            return None
        secret = self.get_password('auth_secret', raise_exception=False)
        return (self.auth_username or '', secret or '')

    @property
    def request_timeout(self) -> int:
        return cint(self.timeout) or 30
