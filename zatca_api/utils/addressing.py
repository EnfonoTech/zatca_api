# zatca_api/utils/addressing.py
# Copyright (c) 2026, Enfono Technologies and contributors
"""Turn a single free-text address string into the structured parts ZATCA needs.

ZATCA Phase 2 rejects a *standard* (B2B) invoice whose buyer address is missing
street, building number, city, postal code or district. `ksa_compliance` reads
those from the Customer's primary Address:

    address_line1            -> buyer_street_name
    address_line2            -> buyer_additional_street_name
    custom_building_number   -> buyer_building_number   (exactly 4 characters)
    city                     -> buyer_city
    pincode                  -> buyer_postal_code       (exactly 5 characters)
    custom_area              -> buyer_district
    state                    -> buyer_province_state
    country                  -> buyer_country_code (resolved via Country.code)

Source: apps/ksa_compliance/ksa_compliance/ksa_compliance/doctype/
        sales_invoice_additional_fields/sales_invoice_additional_fields.py
        (_set_buyer_address / validate_buyer_address, ksa_compliance 0.58.0)

Many legacy systems only export one concatenated address line, so the patterns
below pull the parts back out. Every pattern is overridable in ZATCA API Settings
so this stays generic instead of hardcoding one client's address format.
"""

import re

import frappe

# The first capture group of each regex supplies the value.
# Tuned for the layout ERPNext's own `address_display` renders for KSA addresses,
# e.g. "Building No 4521, Olaya Street, Al Murabba Dist, P.C: 12613, Riyadh,
#       Kingdom of Saudi Arabia".
DEFAULT_KSA_ADDRESS_PATTERNS = {
    'address_line1': r'Building\s+No\.?\s*\d{3,4}\s*,\s*([^,]+)',
    'custom_building_number': r'Building\s+No\.?\s*(\d{3,4})',
    'pincode': r'P\.?\s?C\.?\s*:?\s*(\d{5})',
    'custom_area': r',\s*([^,]*?Dist[a-z]*\.?)\s*,',
    'city': r',\s*([^,]+?)\s*,\s*(?:Kingdom of Saudi Arabia|Saudi Arabia|KSA)\s*$',
}

# ZATCA fixed widths. Enforced as a warning, not a hard failure: `ksa_compliance`
# raises the authoritative error at invoice submission, and this app should not
# invent a second, subtly different rule.
BUILDING_NUMBER_LENGTH = 4
POSTAL_CODE_LENGTH = 5

ADDRESS_PART_FIELDS = (
    'address_line1',
    'address_line2',
    'custom_building_number',
    'custom_area',
    'city',
    'pincode',
    'state',
)


def parse_address_text(text: str, patterns: dict | None = None) -> dict:
    """Extract address parts from ``text``.

    Returns only the keys that actually matched, so callers can merge the result
    over explicit payload values without blanking them.
    """
    if not text or not str(text).strip():
        return {}

    patterns = patterns or DEFAULT_KSA_ADDRESS_PATTERNS
    text = str(text)
    parsed = {}

    for field, pattern in patterns.items():
        if field not in ADDRESS_PART_FIELDS:
            continue
        try:
            match = re.search(pattern, text, re.IGNORECASE)
        except re.error:
            # A bad regex is rejected when the settings are saved. Reaching here
            # means the pattern was edited in the database directly; skip it
            # rather than failing the whole invoice.
            frappe.log_error(
                title='ZATCA API: invalid address pattern',
                message=f'field={field} pattern={pattern}',
            )
            continue

        if match and match.lastindex:
            value = (match.group(1) or '').strip(' ,.-')
            if value:
                parsed[field] = value

    return parsed


def normalise_address_parts(parts: dict) -> dict:
    """Trim, drop blanks, and left-pad the numeric fields to their ZATCA widths.

    A source system that stores building number ``521`` is fixable (pad to
    ``0521``); one that stores ``52100`` is not, and is left untouched so
    `ksa_compliance` reports the real problem against the Address.
    """
    cleaned = {}
    for key, value in (parts or {}).items():
        if key not in ADDRESS_PART_FIELDS:
            continue
        value = ('' if value is None else str(value)).strip()
        if not value:
            continue
        cleaned[key] = value

    building = cleaned.get('custom_building_number')
    if building and building.isdigit() and len(building) < BUILDING_NUMBER_LENGTH:
        cleaned['custom_building_number'] = building.zfill(BUILDING_NUMBER_LENGTH)

    pincode = cleaned.get('pincode')
    if pincode and pincode.isdigit() and len(pincode) < POSTAL_CODE_LENGTH:
        cleaned['pincode'] = pincode.zfill(POSTAL_CODE_LENGTH)

    return cleaned


def address_warnings(parts: dict, country: str | None = None) -> list:
    """Non-fatal notes about parts that will fail a ZATCA standard invoice.

    Surfacing these in the API response tells the integrator which field to fix
    before ZATCA rejects the invoice, rather than after.
    """
    warnings = []
    is_saudi = (country or '').strip().lower() in ('', 'saudi arabia', 'ksa')

    if not parts.get('address_line1'):
        warnings.append('Buyer address has no street name (address_line1).')

    if not parts.get('city'):
        warnings.append('Buyer address has no city.')

    building = parts.get('custom_building_number') or ''
    if not building:
        warnings.append('Buyer address has no building number (custom_building_number).')
    elif is_saudi and len(building) != BUILDING_NUMBER_LENGTH:
        warnings.append(
            f'Buyer building number "{building}" is {len(building)} characters; '
            f'ZATCA requires exactly {BUILDING_NUMBER_LENGTH}.'
        )

    pincode = parts.get('pincode') or ''
    if not pincode:
        warnings.append('Buyer address has no postal code (pincode).')
    elif is_saudi and len(pincode) != POSTAL_CODE_LENGTH:
        warnings.append(
            f'Buyer postal code "{pincode}" is {len(pincode)} characters; '
            f'ZATCA requires exactly {POSTAL_CODE_LENGTH}.'
        )

    if not parts.get('custom_area'):
        warnings.append('Buyer address has no district (custom_area).')

    return warnings
