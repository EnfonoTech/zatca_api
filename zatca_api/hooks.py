# zatca_api/hooks.py
# Copyright (c) 2026, Enfono Technologies and contributors

app_name = 'zatca_api'
app_title = 'ZATCA API'
app_publisher = 'Enfono Technologies'
app_description = (
    'Generic REST bridge that creates ERPNext Sales Invoices from any external '
    'system and returns the ZATCA QR code (Phase 1 and Phase 2) in the response.'
)
app_email = 'info@enfono.com'
app_license = 'mit'

# Optional runtime dependency. ZATCA cryptography lives entirely in
# `ksa_compliance`; without it this app still creates invoices and simply reports
# `zatca.available = false`. Declaring it as a hard `required_apps` entry would
# block installation on non-KSA sites that only want the invoice REST bridge.
# required_apps = ['ksa_compliance']

# ---------------------------------------------------------------- fixtures
# Shipped so `bench migrate` recreates them on every site. The external id field
# is what makes imports idempotent, so it must exist before the first request.
fixtures = [
    {
        'dt': 'Custom Field',
        'filters': [
            [
                'name',
                'in',
                [
                    'Sales Invoice-zatca_api_section',
                    'Sales Invoice-zatca_api_external_id',
                    'Sales Invoice-zatca_api_source',
                    'Sales Invoice-zatca_api_column_break',
                    'Sales Invoice-zatca_api_synced_on',
                ],
            ]
        ],
    },
    {'dt': 'Workspace', 'filters': [['module', '=', 'ZATCA API']]},
]

# ---------------------------------------------------------------- scheduler
scheduler_events = {
    'cron': {
        # Pull mode is inert unless both ZATCA API Settings.enabled and
        # .pull_enabled are on, so this tick is a cheap no-op by default.
        '*/15 * * * *': ['zatca_api.services.puller.pull_all_sources'],
    },
    'daily': [
        'zatca_api.zatca_api.doctype.zatca_api_request_log.zatca_api_request_log.delete_old_logs',
    ],
}

# ---------------------------------------------------------------- api
# Deliberately empty: this app reacts to nothing and overrides nothing. Invoices
# are submitted through the normal ERPNext path so `ksa_compliance`'s own
# `Sales Invoice.on_submit` hook fires unchanged.
doc_events = {}

# ---------------------------------------------------------------- website
website_route_rules = []

# The hosted user guide is served from www/user-guide.html at /user-guide.
