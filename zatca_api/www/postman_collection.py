# zatca_api/www/postman-collection.py
"""Serve the Postman collection as a downloadable JSON file.

Route: ``/postman-collection.json``

Why a template rather than a static file. Frappe's ``StaticPage`` renderer refuses
``.json`` outright -- the extension is in ``UNSUPPORTED_STATIC_PAGE_TYPES`` and the
renderer additionally only serves *binary* files -- so the request falls through to
``TemplatePage``, which renders the file as **Jinja**. A Postman collection is full of
``{{base_url}}``-style placeholders, which is Jinja's own syntax, so serving the file
directly would silently blank every variable in it and hand the vendor a broken
collection.

The sibling ``postman-collection.json`` is therefore a one-line passthrough, ``{{ payload }}``,
and the real bytes are injected here. Frappe's Jinja environment has ``autoescape`` off and
does not re-render an interpolated value, so the collection round-trips byte for byte.

Serving it from ``www`` rather than ``public/`` is deliberate: ``/assets/<app>/`` depends on a
``sites/assets`` symlink that only ``bench build`` creates, and this app's deploy path is a
plain ``git pull`` plus ``bench clear-cache``.
"""

import json
import os

import frappe

no_cache = 1

# Relative to the app package directory, i.e. apps/zatca_api/postman/.
COLLECTION = os.path.join('..', 'postman', 'zatca_api.postman_collection.json')


def get_context(context):
    path = os.path.normpath(os.path.join(frappe.get_app_path('zatca_api'), COLLECTION))

    try:
        with open(path, encoding='utf-8') as handle:
            payload = handle.read()
    except OSError:
        frappe.log_error(f'Postman collection missing at {path}', 'zatca_api')
        # A JSON body, because the caller asked for .json and will try to parse it.
        payload = json.dumps(
            {'error': 'The Postman collection is not available on this site.'}, indent=2
        )

    context.payload = payload
    context.no_cache = 1
    return context
