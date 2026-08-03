# zatca_api/zatca_api/doctype/zatca_api_field_mapping/zatca_api_field_mapping.py
# Copyright (c) 2026, Enfono Technologies and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ZATCAAPIFieldMapping(Document):
    """Child row of ZATCA API Settings.

    Validation lives on the parent (ZATCA API Settings.validate_field_mappings)
    because it needs cross-row duplicate detection.
    """

    pass
