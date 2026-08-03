# zatca_api/www/user-guide.py
# Copyright (c) 2026, Enfono Technologies and contributors
"""Controller for the hosted guide at /user-guide.

Frappe auto-discovers www/*.html, so no route registration is needed. This
controller only sets page metadata; the HTML is otherwise static.
"""

import frappe

no_cache = 1


def get_context(context):
    context.no_cache = 1
    # Public documentation - no login required, and no data is read from the site.
    context.no_sidebar = 1
    context.title = frappe._('ZATCA API - Integration Guide')
    return context
