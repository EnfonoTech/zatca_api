# zatca_api/tests/test_credit_note_rules.py
"""The two ZATCA rules a live Phase 2 clearance rejected us on.

Both were found against ZATCA's real sandbox, not in review:

* BR-KSA-17 — a credit or debit note must state the reason it was issued.
  ksa_compliance reads Sales Invoice `custom_return_reason` into the UBL
  `InstructionNote`; we never set it, so every Phase 2 credit note came back
  HTTP 400 Rejected.
* BR-KSA-04 — the issue date must not be in the future. ZATCA only says so at
  clearance, by which point the invoice is submitted and in the ledger, so we
  refuse the payload up front instead.
"""

from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, today

from zatca_api.services.invoice import (
    DEFAULT_RETURN_REASON,
    RETURN_REASON_FIELD,
    RETURN_REASON_MAX_LENGTH,
    _apply_return_reason,
    _inherited_item_tax_templates,
)
from zatca_api.services.payload import PayloadError, normalise_invoice, validate_invoice


class _Doc(dict):
    """Stands in for a Sales Invoice: attribute access, .get and .set."""

    doctype = 'Sales Invoice'

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)

    def set(self, key, value):
        self[key] = value


def _meta(has_field=True):
    meta = MagicMock()
    meta.get_field.return_value = MagicMock() if has_field else None
    return meta


class TestReturnReason(FrappeTestCase):
    """BR-KSA-17: the reason has to reach `custom_return_reason`."""

    def _apply(self, doc, payload, has_field=True):
        with patch('zatca_api.services.invoice.frappe.get_meta', return_value=_meta(has_field)):
            _apply_return_reason(doc, payload)
        return doc

    def test_payload_reason_is_used(self):
        doc = self._apply(_Doc(is_return=1), {'return_reason': 'Goods returned damaged'})
        self.assertEqual(doc[RETURN_REASON_FIELD], 'Goods returned damaged')

    def test_falls_back_to_remarks(self):
        doc = self._apply(_Doc(is_return=1), {'remarks': 'Two onsite days cancelled'})
        self.assertEqual(doc[RETURN_REASON_FIELD], 'Two onsite days cancelled')

    def test_payload_reason_wins_over_remarks(self):
        doc = self._apply(_Doc(is_return=1), {'return_reason': 'Wrong item', 'remarks': 'chatter'})
        self.assertEqual(doc[RETURN_REASON_FIELD], 'Wrong item')

    def test_defaults_rather_than_leaving_it_empty(self):
        """An empty field is what ZATCA rejected — never leave it unset."""
        doc = self._apply(_Doc(is_return=1), {})
        self.assertEqual(doc[RETURN_REASON_FIELD], DEFAULT_RETURN_REASON)

    def test_whitespace_only_reason_still_defaults(self):
        doc = self._apply(_Doc(is_return=1), {'return_reason': '   '})
        self.assertEqual(doc[RETURN_REASON_FIELD], DEFAULT_RETURN_REASON)

    def test_debit_note_also_needs_a_reason(self):
        """BR-KSA-17 covers 381 (debit) as well as 383 (credit)."""
        doc = self._apply(_Doc(is_debit_note=1), {'return_reason': 'Undercharged freight'})
        self.assertEqual(doc[RETURN_REASON_FIELD], 'Undercharged freight')

    def test_plain_invoice_is_untouched(self):
        doc = self._apply(_Doc(), {'return_reason': 'irrelevant here'})
        self.assertNotIn(RETURN_REASON_FIELD, doc)

    def test_noop_when_ksa_compliance_is_absent(self):
        """Nothing consumes the field, so don't invent it."""
        doc = self._apply(_Doc(is_return=1), {'return_reason': 'x'}, has_field=False)
        self.assertNotIn(RETURN_REASON_FIELD, doc)

    def test_long_reason_is_truncated_to_the_data_field_limit(self):
        doc = self._apply(_Doc(is_return=1), {'return_reason': 'R' * 400})
        self.assertEqual(len(doc[RETURN_REASON_FIELD]), RETURN_REASON_MAX_LENGTH)

    def test_existing_value_on_the_doc_is_preserved(self):
        doc = _Doc(is_return=1)
        doc[RETURN_REASON_FIELD] = 'set by a mapping rule'
        self._apply(doc, {'remarks': 'chatter'})
        self.assertEqual(doc[RETURN_REASON_FIELD], 'set by a mapping rule')

    def test_reason_aliases_normalise(self):
        for alias in ('return_reason', 'reason', 'creditNoteReason', 'custom_return_reason'):
            payload = normalise_invoice(
                {alias: 'Damaged in transit', 'external_id': 'E1', 'customer': 'C', 'items': []}
            )
            self.assertEqual(payload['return_reason'], 'Damaged in transit', alias)


class TestFutureIssueDate(FrappeTestCase):
    """BR-KSA-04: refuse a future issue date before it reaches the ledger."""

    def _payload(self, posting_date):
        return {
            'external_id': 'EXT-1',
            'customer': 'Acme',
            'posting_date': posting_date,
            'items': [{'item_code': 'SVC', 'qty': 1, 'rate': 100}],
        }

    def test_future_date_is_rejected(self):
        with self.assertRaises(PayloadError) as caught:
            validate_invoice(self._payload(add_days(today(), 6)))

        self.assertIn('BR-KSA-04', str(caught.exception))
        self.assertEqual(caught.exception.details.get('zatca_rule'), 'BR-KSA-04')
        self.assertEqual(caught.exception.details.get('field'), 'posting_date')

    def test_today_is_allowed(self):
        validate_invoice(self._payload(today()))

    def test_backdated_is_allowed(self):
        validate_invoice(self._payload(add_days(today(), -30)))

    def test_unparseable_date_still_reports_as_invalid(self):
        with self.assertRaises(PayloadError) as caught:
            validate_invoice(self._payload('not-a-date'))
        self.assertIn('not a valid date', str(caught.exception))


class TestInheritedItemTaxTemplate(FrappeTestCase):
    """A credit note must not reclaim VAT the original sale never charged.

    ERPNext does not carry a return line's tax treatment over from the invoice being
    credited. Measured on a live site before this was fixed: a zero-rated invoice of
    1,000 charged 0 VAT, and crediting it without ``item_tax_template`` produced VAT
    of -150 -- a refund of VAT that was never collected.
    """

    def _inherited(self, payload, rows):
        with patch('zatca_api.services.invoice.frappe.get_all', return_value=rows) as get_all:
            result = _inherited_item_tax_templates(payload)
        return result, get_all

    def test_return_inherits_the_originals_templates(self):
        payload = {'is_return': 1, 'return_against': 'ACC-SINV-2026-00034'}
        rows = [{'item_code': 'SVC-EXPORT', 'item_tax_template': 'KSA Zero Rated - ZTC'}]
        result, get_all = self._inherited(payload, rows)

        self.assertEqual(result, {'SVC-EXPORT': 'KSA Zero Rated - ZTC'})
        self.assertEqual(get_all.call_args.kwargs['filters'], {'parent': 'ACC-SINV-2026-00034'})

    def test_lines_without_a_template_on_the_original_are_skipped(self):
        rows = [
            {'item_code': 'SVC-IMPL', 'item_tax_template': None},
            {'item_code': 'SVC-EXPORT', 'item_tax_template': 'KSA Zero Rated - ZTC'},
        ]
        result, _ = self._inherited({'is_return': 1, 'return_against': 'X'}, rows)
        self.assertEqual(result, {'SVC-EXPORT': 'KSA Zero Rated - ZTC'})

    def test_a_plain_invoice_inherits_nothing(self):
        result, get_all = self._inherited({'items': []}, [])
        self.assertEqual(result, {})
        get_all.assert_not_called()

    def test_a_return_with_no_original_inherits_nothing(self):
        """Nothing to read, and no query worth making."""
        result, get_all = self._inherited({'is_return': 1}, [])
        self.assertEqual(result, {})
        get_all.assert_not_called()

    def test_all_three_categories_are_carried_over(self):
        rows = [
            {'item_code': 'SVC-IMPL', 'item_tax_template': 'KSA VAT 15 - ZTC'},
            {'item_code': 'SVC-EXPORT', 'item_tax_template': 'KSA Zero Rated - ZTC'},
            {'item_code': 'SVC-EXEMPT', 'item_tax_template': 'KSA Exempt - ZTC'},
        ]
        result, _ = self._inherited({'is_return': 1, 'return_against': 'X'}, rows)
        self.assertEqual(len(result), 3)
        self.assertEqual(result['SVC-EXEMPT'], 'KSA Exempt - ZTC')
