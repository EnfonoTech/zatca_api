# zatca_api/patches/v1_1/enable_b2b_address_enforcement.py
# Copyright (c) 2026, Enfono Technologies and contributors
"""Turn on Require Complete Address For B2B on sites that predate the field.

A DocType field ``default`` is only applied when a document is *created*. The
ZATCA API Settings singleton already exists on an upgraded site, so a newly added
Check field lands as NULL there and reads as off -- silently disabling a compliance
guard on exactly the installations that already have live traffic.

Idempotent: only fills the value when it has never been set, so an integrator who
deliberately turned it off keeps their choice.
"""

import frappe


def execute():
    if not frappe.db.exists('DocType', 'ZATCA API Settings'):
        return

    current = frappe.db.get_value('ZATCA API Settings', 'ZATCA API Settings', 'enforce_b2b_address')
    if current is not None and str(current).strip() != '':
        return

    frappe.db.set_value(
        'ZATCA API Settings', 'ZATCA API Settings', 'enforce_b2b_address', 1, update_modified=False
    )
    frappe.clear_cache(doctype='ZATCA API Settings')
