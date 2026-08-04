# zatca_api/www/integration-guide.py
# Copyright (c) 2026, Enfono Technologies and contributors
"""Controller for the provider integration guide at /integration-guide.

Public: the audience is an external development team that does not have desk access.
Nothing on the page is read from the database.
"""

import frappe

no_cache = 1


def get_context(context):
    context.no_cache = 1
    context.no_sidebar = 1
    context.title = frappe._('ZATCA API - Integration Guide for Providers')
    return context
