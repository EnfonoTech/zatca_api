# zatca_api/tests/test_addressing.py
# Copyright (c) 2026, Enfono Technologies and contributors
"""Unit tests for free-text address parsing.

Pure functions, no database, so these run without a site fixture.
"""

import unittest

from zatca_api.utils.addressing import (
    DEFAULT_KSA_ADDRESS_PATTERNS,
    address_warnings,
    normalise_address_parts,
    parse_address_text,
)


class TestParseAddressText(unittest.TestCase):
    def test_parses_full_ksa_address(self):
        text = 'Building No 4521, Olaya Street, Al Murabba Dist, P.C: 12613, Riyadh, Kingdom of Saudi Arabia'
        parts = parse_address_text(text)

        self.assertEqual(parts['custom_building_number'], '4521')
        self.assertEqual(parts['address_line1'], 'Olaya Street')
        self.assertEqual(parts['custom_area'], 'Al Murabba Dist')
        self.assertEqual(parts['pincode'], '12613')
        self.assertEqual(parts['city'], 'Riyadh')

    def test_parses_variant_punctuation_and_wording(self):
        """'Building No.' with a dot, 'P.C' without a colon, 'District' spelled out."""
        text = 'Building No. 0521, King Fahd Road, Al Olaya District, P.C 11564, Jeddah, Saudi Arabia'
        parts = parse_address_text(text)

        self.assertEqual(parts['custom_building_number'], '0521')
        self.assertEqual(parts['custom_area'], 'Al Olaya District')
        self.assertEqual(parts['pincode'], '11564')
        self.assertEqual(parts['city'], 'Jeddah')

    def test_parses_ksa_abbreviation(self):
        text = 'Building No 1234, Prince Sultan St, Al Hamra Dist., P.C: 23324, Dammam, KSA'
        parts = parse_address_text(text)
        self.assertEqual(parts['city'], 'Dammam')
        self.assertEqual(parts['custom_building_number'], '1234')

    def test_non_ksa_address_yields_nothing(self):
        """A non-matching address must return {} so explicit payload values survive."""
        self.assertEqual(parse_address_text('Some Street 12, Dubai, United Arab Emirates'), {})

    def test_blank_input(self):
        self.assertEqual(parse_address_text(''), {})
        self.assertEqual(parse_address_text(None), {})
        self.assertEqual(parse_address_text('   '), {})

    def test_ignores_fields_outside_the_allowlist(self):
        """A pattern for a non-address field must not be honoured."""
        parts = parse_address_text('anything', {'tax_id': r'(\d+)', 'city': r'(any\w+)'})
        self.assertNotIn('tax_id', parts)
        self.assertEqual(parts['city'], 'anything')

    def test_bad_regex_is_skipped_not_raised(self):
        """A malformed pattern must not take the invoice down with it."""
        parts = parse_address_text('Riyadh', {'city': '([unclosed'})
        self.assertEqual(parts, {})

    def test_all_default_patterns_have_a_capture_group(self):
        import re

        for field, pattern in DEFAULT_KSA_ADDRESS_PATTERNS.items():
            with self.subTest(field=field):
                self.assertGreaterEqual(re.compile(pattern).groups, 1)


class TestNormaliseAddressParts(unittest.TestCase):
    def test_pads_short_numeric_building_number(self):
        """ZATCA wants exactly 4 characters; a 3-digit source value is fixable."""
        self.assertEqual(
            normalise_address_parts({'custom_building_number': '521'})['custom_building_number'], '0521'
        )

    def test_pads_short_numeric_pincode(self):
        self.assertEqual(normalise_address_parts({'pincode': '1234'})['pincode'], '01234')

    def test_leaves_overlong_values_alone(self):
        """Too long is not fixable; ksa_compliance must report the real problem."""
        parts = normalise_address_parts({'custom_building_number': '45211', 'pincode': '123456'})
        self.assertEqual(parts['custom_building_number'], '45211')
        self.assertEqual(parts['pincode'], '123456')

    def test_leaves_non_numeric_alone(self):
        self.assertEqual(
            normalise_address_parts({'custom_building_number': 'A12'})['custom_building_number'], 'A12'
        )

    def test_strips_and_drops_blanks(self):
        parts = normalise_address_parts({'city': '  Riyadh  ', 'state': '', 'pincode': None})
        self.assertEqual(parts, {'city': 'Riyadh'})

    def test_drops_unknown_keys(self):
        self.assertEqual(normalise_address_parts({'customer': 'X'}), {})

    def test_handles_none(self):
        self.assertEqual(normalise_address_parts(None), {})


class TestAddressWarnings(unittest.TestCase):
    def test_complete_saudi_address_has_no_warnings(self):
        parts = {
            'address_line1': 'Olaya Street',
            'custom_building_number': '4521',
            'city': 'Riyadh',
            'pincode': '12613',
            'custom_area': 'Al Murabba',
        }
        self.assertEqual(address_warnings(parts, 'Saudi Arabia'), [])

    def test_flags_every_missing_part(self):
        warnings = address_warnings({}, 'Saudi Arabia')
        self.assertEqual(len(warnings), 5)

    def test_flags_wrong_length_only_for_saudi(self):
        parts = {
            'address_line1': 'Street',
            'custom_building_number': '45',
            'city': 'Riyadh',
            'pincode': '123',
            'custom_area': 'District',
        }
        self.assertEqual(len(address_warnings(parts, 'Saudi Arabia')), 2)
        # Outside KSA the fixed widths do not apply.
        self.assertEqual(address_warnings(parts, 'United Arab Emirates'), [])

    def test_blank_country_is_treated_as_saudi(self):
        parts = {
            'address_line1': 'Street',
            'custom_building_number': '45',
            'city': 'Riyadh',
            'pincode': '12345',
            'custom_area': 'District',
        }
        self.assertEqual(len(address_warnings(parts, None)), 1)


if __name__ == '__main__':
    unittest.main()
