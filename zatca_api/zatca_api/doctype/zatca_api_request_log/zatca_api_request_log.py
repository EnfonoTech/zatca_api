# zatca_api/zatca_api/doctype/zatca_api_request_log/zatca_api_request_log.py
# Copyright (c) 2026, Enfono Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import add_days, cint, today


class ZATCAAPIRequestLog(Document):
    pass


def delete_old_logs():
    """Nightly cleanup driven by ZATCA API Settings.log_retention_days.

    Registered under ``scheduler_events.daily``. A retention of 0 keeps everything.
    """
    retention = cint(frappe.db.get_single_value('ZATCA API Settings', 'log_retention_days'))
    if retention <= 0:
        return

    cutoff = add_days(today(), -retention)
    log = frappe.qb.DocType('ZATCA API Request Log')
    frappe.qb.from_(log).delete().where(log.creation < cutoff).run()
