# zatca_api/zatca_api/doctype/zatca_api_settings/zatca_api_settings.py
# Copyright (c) 2026, Enfono Technologies and contributors
# For license information, please see license.txt

import datetime
import ipaddress
import json
import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr

# Address fields the free-text parser is allowed to write. Anything else in the
# pattern list is a typo and is rejected on save rather than silently ignored.
# `custom_building_number` and `custom_area` are the Address custom fields that
# `ksa_compliance` installs and reads for the ZATCA buyer address.
PARSEABLE_ADDRESS_FIELDS = (
    'address_line1',
    'address_line2',
    'custom_building_number',
    'custom_area',
    'city',
    'pincode',
    'state',
)

MAX_WAIT_SECONDS = 30

PROTECTED_TARGET_FIELDS = ('name', 'owner', 'docstatus', 'parent', 'parenttype', 'parentfield', 'idx')


class ZATCAAPISettings(Document):
    def validate(self):
        self.validate_wait_seconds()
        self.validate_field_mappings()
        self.validate_address_patterns()
        self.validate_allowed_ips()
        self.validate_sources()
        self.validate_shared_secret()

    def validate_wait_seconds(self):
        """Clamp the synchronous clearance wait so a caller cannot pin an HTTP worker."""
        seconds = cint(self.wait_for_zatca_seconds)
        if seconds < 0:
            self.wait_for_zatca_seconds = 0
        elif seconds > MAX_WAIT_SECONDS:
            self.wait_for_zatca_seconds = MAX_WAIT_SECONDS
            frappe.msgprint(
                _('Wait For Clearance capped at {0} seconds.').format(MAX_WAIT_SECONDS),
                indicator='orange',
            )

    def validate_field_mappings(self):
        """Reject mappings that point at a field which does not exist.

        Catching this on save turns a silent data-loss bug (payload key mapped to a
        typo'd fieldname, value quietly dropped) into an error the integrator sees
        while configuring.
        """
        seen = set()
        for row in self.field_mappings:
            row.target_field = (row.target_field or '').strip()
            row.source_key = (row.source_key or '').strip()

            key = (row.target_doctype, row.source_key)
            if key in seen:
                frappe.throw(
                    _('Row #{0}: duplicate mapping for source key {1} on {2}.').format(
                        row.idx, frappe.bold(row.source_key), row.target_doctype
                    )
                )
            seen.add(key)

            meta = frappe.get_meta(row.target_doctype)
            if not meta.get_field(row.target_field):
                frappe.throw(
                    _('Row #{0}: {1} has no field named {2}.').format(
                        row.idx, row.target_doctype, frappe.bold(row.target_field)
                    )
                )

            if row.target_field in PROTECTED_TARGET_FIELDS:
                frappe.throw(_('Row #{0}: {1} cannot be mapped.').format(row.idx, row.target_field))

    def validate_address_patterns(self):
        """Compile every configured regex now so a bad pattern cannot break an invoice later."""
        if not self.address_display_patterns:
            return

        for lineno, line in enumerate(self.address_display_patterns.splitlines(), start=1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue

            if '=' not in line:
                frappe.throw(_('Address Parse Patterns line {0}: expected field=regex.').format(lineno))

            field, _sep, pattern = line.partition('=')
            field = field.strip()
            if field not in PARSEABLE_ADDRESS_FIELDS:
                frappe.throw(
                    _('Address Parse Patterns line {0}: {1} is not a parseable field. Allowed: {2}').format(
                        lineno, frappe.bold(field), ', '.join(PARSEABLE_ADDRESS_FIELDS)
                    )
                )

            try:
                compiled = re.compile(pattern.strip(), re.IGNORECASE)
            except re.error as exc:
                frappe.throw(
                    _('Address Parse Patterns line {0}: invalid regex - {1}').format(lineno, str(exc))
                )

            if compiled.groups < 1:
                frappe.throw(
                    _(
                        'Address Parse Patterns line {0}: the regex needs at least one capture group, '
                        'which supplies the value.'
                    ).format(lineno)
                )

    def validate_allowed_ips(self):
        if not self.allowed_ip_list:
            return

        for lineno, line in enumerate(self.allowed_ip_list.splitlines(), start=1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            try:
                ipaddress.ip_network(line, strict=False)
            except ValueError:
                frappe.throw(
                    _('Allowed IPs line {0}: {1} is not a valid IP address or CIDR block.').format(
                        lineno, frappe.bold(line)
                    )
                )

    def validate_sources(self):
        names = set()
        for row in self.sources:
            row.source_name = (row.source_name or '').strip()
            if row.source_name in names:
                frappe.throw(_('Row #{0}: duplicate source name {1}.').format(row.idx, row.source_name))
            names.add(row.source_name)

            if cint(row.timeout) <= 0:
                row.timeout = 30

            row.endpoint_url = (row.endpoint_url or '').strip()
            if row.endpoint_url and not row.endpoint_url.lower().startswith(('http://', 'https://')):
                frappe.throw(_('Row #{0}: Endpoint URL must start with http:// or https://.').format(row.idx))

            if row.enabled and not cint(row.verify_ssl):
                frappe.msgprint(
                    _('Source {0} has SSL verification disabled.').format(frappe.bold(row.source_name)),
                    indicator='red',
                    title=_('Insecure Source'),
                )

            self._validate_source_request_shaping(row)

    def _validate_source_request_shaping(self, row):
        """Validate the request-shaping, incremental and pagination configuration.

        Everything is checked on save so a misconfigured source fails in front of the
        person editing it, not silently at 03:00 in a scheduled job.
        """
        for lineno, line in enumerate(cstr(row.custom_headers).splitlines(), start=1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if ':' not in line:
                frappe.throw(
                    _('Source {0}, Extra Headers line {1}: expected "Name: value".').format(
                        row.source_name, lineno
                    )
                )

        for lineno, line in enumerate(cstr(row.query_params).splitlines(), start=1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' not in line:
                frappe.throw(
                    _('Source {0}, Query Parameters line {1}: expected "key=value".').format(
                        row.source_name, lineno
                    )
                )

        body = cstr(row.request_body).strip()
        if body:
            try:
                json.loads(body)
            except (ValueError, TypeError) as exc:
                frappe.throw(
                    _('Source {0}: Request Body is not valid JSON - {1}').format(row.source_name, str(exc))
                )

        if row.incremental_mode == 'Date Window':
            if not row.from_param:
                frappe.throw(_('Source {0}: a date window needs a From Parameter.').format(row.source_name))
            date_format = cstr(row.date_format).strip() or '%Y-%m-%d'
            try:
                # Round-trip a known datetime to prove the format is usable.
                datetime.datetime(2026, 1, 2, 3, 4, 5).strftime(date_format)
            except (ValueError, TypeError) as exc:
                frappe.throw(
                    _('Source {0}: Date Format is not a valid strftime format - {1}').format(
                        row.source_name, str(exc)
                    )
                )
            if cint(row.lookback_days) < 0:
                row.lookback_days = 0

        if row.pagination_mode and row.pagination_mode != 'None':
            if cint(row.page_size) <= 0:
                row.page_size = 100
            if cint(row.max_pages) <= 0:
                row.max_pages = 20

            if row.pagination_mode == 'Cursor' and not row.next_cursor_key:
                frappe.throw(
                    _(
                        'Source {0}: cursor pagination needs a Next Cursor Key, otherwise there is '
                        'no way to know when to stop.'
                    ).format(row.source_name)
                )
            if row.pagination_mode in ('Page Number', 'Offset') and not row.page_param:
                frappe.throw(
                    _('Source {0}: {1} pagination needs a Page / Offset Parameter.').format(
                        row.source_name, row.pagination_mode
                    )
                )

    def validate_shared_secret(self):
        if not self.require_shared_secret:
            return

        self.shared_secret_header = (self.shared_secret_header or '').strip() or 'X-ZATCA-API-Secret'
        if not self.get_password('shared_secret', raise_exception=False):
            frappe.throw(_('Set a Shared Secret or turn off Require Shared Secret Header.'))

    # ------------------------------------------------------------------ helpers

    def get_address_patterns(self) -> dict:
        """Return ``{field: regex}`` from the configured pattern list.

        Empty configuration falls back to the built-in KSA defaults, which match the
        address layout ERPNext's own ``address_display`` produces for Saudi addresses.
        """
        from zatca_api.utils.addressing import DEFAULT_KSA_ADDRESS_PATTERNS

        if not self.address_display_patterns:
            return dict(DEFAULT_KSA_ADDRESS_PATTERNS)

        patterns = {}
        for line in self.address_display_patterns.splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            field, _sep, pattern = line.partition('=')
            field = field.strip()
            if field in PARSEABLE_ADDRESS_FIELDS:
                patterns[field] = pattern.strip()

        return patterns or dict(DEFAULT_KSA_ADDRESS_PATTERNS)

    def get_allowed_networks(self) -> list:
        networks = []
        for line in (self.allowed_ip_list or '').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            try:
                networks.append(ipaddress.ip_network(line, strict=False))
            except ValueError:
                continue
        return networks

    def get_source(self, source_name: str):
        for row in self.sources:
            if row.source_name == source_name:
                return row
        return None

    def mappings_for(self, target_doctype: str) -> list:
        return [row for row in self.field_mappings if row.target_doctype == target_doctype]


def get_settings() -> ZATCAAPISettings:
    """Cached settings accessor.

    ``get_cached_doc`` on a Single doctype is safe here: every write goes through
    the desk, which busts the cache. Never mutate the returned document.
    """
    return frappe.get_cached_doc('ZATCA API Settings')
