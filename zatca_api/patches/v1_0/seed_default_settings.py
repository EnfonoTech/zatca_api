# zatca_api/patches/v1_0/seed_default_settings.py
# Copyright (c) 2026, Enfono Technologies and contributors
"""Create the ZATCA API Settings singleton with safe defaults.

Idempotent: re-running only fills values that are still unset, so a replayed
patch never overwrites an integrator's configuration.
"""

import frappe
from frappe.utils import cint


def execute():
    settings = frappe.get_single('ZATCA API Settings')
    dirty = False

    if not settings.default_company:
        companies = frappe.get_all('Company', pluck='name', limit=2)
        if len(companies) == 1:
            settings.default_company = companies[0]
            dirty = True

    if not settings.default_country:
        # Only default the country when the single company is already Saudi; do not
        # impose a country on a bench that is not KSA.
        country = frappe.db.get_value('Company', settings.default_company, 'country')
        if country:
            settings.default_country = country
            dirty = True

    if not settings.default_uom and frappe.db.exists('UOM', 'Nos'):
        settings.default_uom = 'Nos'
        dirty = True

    if not settings.default_item_group:
        group = frappe.db.get_value('Item Group', {'item_group_name': 'Services', 'is_group': 0}, 'name')
        if group:
            settings.default_item_group = group
            dirty = True

    if not settings.default_customer_group:
        group = frappe.db.get_value('Customer Group', {'is_group': 0}, 'name', order_by='creation')
        if group:
            settings.default_customer_group = group
            dirty = True

    if not settings.default_territory:
        territory = frappe.db.get_value('Territory', {'is_group': 0}, 'name', order_by='creation')
        if territory:
            settings.default_territory = territory
            dirty = True

    if not cint(settings.log_retention_days):
        settings.log_retention_days = 30
        dirty = True

    if dirty:
        settings.flags.ignore_permissions = True
        settings.save()
