# Invoice data contract

**For the team building the invoice feed.** This is the complete specification of the
data we need from your system to raise a ZATCA-compliant e-invoice in ERPNext.

You can validate your output against this contract yourself, without us — see
[§8 Test it yourself](#8-test-it-yourself). Please do that before we schedule an
integration call.

- Machine-readable schema: [`schema/invoice.schema.json`](schema/invoice.schema.json)
- Worked examples: [`samples/`](samples/)

---

## 1. Two ways to connect

Pick one. **Push is strongly preferred.**

| | **Push** (you call us) | **Pull** (we poll you) |
|---|---|---|
| Who initiates | You, per invoice | Us, every 15 minutes |
| You get the ZATCA QR back | **Yes, immediately, in the response** | No |
| You learn about a rejected invoice | **Yes, in the response** | No — failures land in our logs |
| Effort for you | One HTTPS POST | Expose an authenticated JSON endpoint |

Push means you find out instantly that invoice `INV-1234` was rejected because the
buyer's postal code is 4 digits. In pull mode you find out when someone asks why
last month's numbers are short. Choose push unless your system genuinely cannot make
outbound calls.

---

## 2. The one field that matters most

```json
"external_id": "INV-2026-0001"
```

Your unique, **stable** identifier for the document.

- Send the same value again and you get the existing invoice back. Nothing is
  duplicated. **Retrying after a timeout is safe.**
- If this value changes between retries, you create duplicates. Do not use a
  timestamp, a random id, or a row number that can shift.
- Use whatever your system already calls the document — its invoice number.

---

## 3. Minimum viable invoice

```json
{
  "external_id": "INV-2026-0004",
  "customer": "Cash Customer",
  "items": [
    { "item_code": "SVC-IMPL", "qty": 1, "rate": 100 }
  ]
}
```

That is genuinely all that is required. Everything else has a default or is derived.

But a minimum invoice is a **simplified (B2C)** invoice. For B2B you need §4.

---

## 4. ZATCA: standard vs simplified

ZATCA treats the two differently, and they have different mandatory fields.

| | **Simplified** (B2C) | **Standard** (B2B) |
|---|---|---|
| Who | Consumer, walk-in | A registered business |
| Filed with ZATCA as | *Reported* after the fact | *Cleared* before issue |
| Buyer VAT number | Not needed | **Required** |
| Buyer address | Not needed | **Required, in full** |

**How we decide which one:** if you send a buyer VAT number (`tax_id`) or another
buyer identifier (`buyer_id_type` + `buyer_id_value`), it is standard. Otherwise
simplified.

> **This is the single most common integration mistake.** If you sell to businesses
> and omit `tax_id`, every invoice is filed as *simplified*. It will not error. It
> will look fine. It is a compliance defect, and it is only discovered in an audit.
>
> If the buyer is a business, send the VAT number.

### Standard invoice — required buyer fields

```json
"tax_id": "300000000000003",
"address_parts": {
  "street": "Olaya Street",
  "building_number": "4521",
  "district": "Al Murabba",
  "city": "Riyadh",
  "postal_code": "12613",
  "country": "Saudi Arabia"
}
```

ZATCA's exact rules, which it enforces and will reject on:

| Field | Rule |
|---|---|
| `tax_id` | Exactly 15 digits, **starting and ending with `3`** |
| `building_number` | Exactly **4** digits — `521` is invalid, send `"0521"` |
| `postal_code` | Exactly **5** digits |
| `street`, `district`, `city` | Must not be blank |

Send `building_number` and `postal_code` as **strings**, not numbers, so leading
zeros survive.

### Business buyer with no VAT number

Some buyers (government bodies, non-registered entities) have no VAT number. Send an
alternative identifier instead:

```json
"buyer_id_type": "CRN",
"buyer_id_value": "1010101010"
```

`buyer_id_type` must be one of: `TIN` `CRN` `MOM` `MLS` `SAG` `NAT` `GCC` `IQA`
`PAS` `OTH`. Most common: `CRN` (commercial registration), `SAG` (government),
`NAT` (national id), `IQA` (iqama), `PAS` (passport).

---

## 5. Line items

```json
"items": [
  {
    "item_code": "SVC-IMPL",
    "item_name": "Implementation services",
    "qty": 10,
    "rate": 450,
    "uom": "Nos",
    "item_tax_template": "KSA VAT 15% - EK"
  }
]
```

| Field | Required | Notes |
|---|---|---|
| `item_code` | **Yes** | Your product/service code. Must be **stable** — it becomes the ERPNext item. |
| `qty` | **Yes** | Must not be `0`. A zero-quantity line is rejected, not silently changed. |
| `rate` | **Yes** | Unit price **excluding VAT**. Never negative. |
| `item_name` | No | Used when we have to create the item. Send it on first sight of a code. |
| `uom` | No | Unit of measure. Tell us your list up front so we can create them. |
| `item_tax_template` | See below | Per-line VAT treatment. |
| `discount_percentage` / `discount_amount` | No | |

### Mixed VAT rates on one invoice

If **any** invoice can contain lines with different VAT treatment — standard 15%,
zero-rated exports, exempt items — you **must** send `item_tax_template` on every
line. Otherwise all lines get the same rate, and ZATCA receives the wrong per-line
VAT category.

We will give you the exact template names for your setup. See
[`samples/mixed-vat-rates.json`](samples/mixed-vat-rates.json).

If every line on every invoice is always standard 15%, you can omit it entirely.

---

## 6. Tax

Easiest option: **send nothing.** We apply the company's default VAT template.

If you need to be explicit:

```json
"taxes": [
  { "account_head": "VAT 15% - EK", "charge_type": "On Net Total",
    "rate": 15, "description": "VAT 15%" }
]
```

We will give you the correct `account_head`. It must belong to the selling company —
we reject accounts from another company rather than posting to the wrong ledger.

⚠️ ZATCA requires at least one tax row. An invoice with no tax at all is rejected.

---

## 7. Credit notes

```json
{
  "external_id": "CN-2026-0007",
  "customer": "Al Rajhi Trading Est",
  "tax_id": "300000000000003",
  "is_return": 1,
  "return_against": "ACC-SINV-2026-00051",
  "items": [{ "item_code": "SVC-ONSITE", "qty": 2, "rate": 1200 }]
}
```

- `is_return: 1`.
- Send `qty` as the quantity being returned. Positive or negative both work — we
  normalise the sign.
- `return_against` is the **ERPNext invoice name** of the original, which we returned
  to you when we created it (`data.invoice.invoice`). Store it. ZATCA requires a
  credit note to identify its original.
- The original must already be submitted.

---

## 8. Test it yourself

Do not wait for us. Point your output at the dry-run endpoint:

```
POST /api/method/zatca_api.api.v1.validate_payload
Authorization: token <api_key>:<api_secret>
Content-Type: application/json
```

Send exactly the invoice JSON you intend to send for real. To validate a credit note,
add `"document_type": "Credit Note"` to the body (or `?document_type=Credit Note` on
the URL — both work).

You get back:

```json
{
  "message": {
    "data": {
      "valid": true,
      "errors": [],
      "warnings": ["Buyer address has no district (custom_area)."],
      "totals": { "net_total": 4500.0, "total_taxes_and_charges": 675.0,
                  "grand_total": 5175.0, "currency": "SAR" },
      "would_create": { "customer_exists": false, "new_items": ["SVC-IMPL"] },
      "zatca": { "invoice_type": "Standard", "buyer_is_b2b": true,
                 "reason": "Buyer has a VAT registration number..." },
      "zatca_readiness": { "would_be_rejected_by_zatca": false,
                           "blocking": [], "advisory": [] }
    }
  }
}
```

**Nothing is written.** No invoice, no customer, no item. Run it as often as you like
against the live site.

What to check:

| Field | What to look for |
|---|---|
| `valid` | Must be `true`. |
| `errors` | Must be empty. Each names the exact field. |
| `zatca.invoice_type` | Is it what you expect — `Standard` for a business buyer? |
| `zatca_readiness.would_be_rejected_by_zatca` | Must be `false`. |
| `zatca_readiness.blocking` | Must be empty. These *are* the ZATCA rejections. |
| `totals.grand_total` | Compare against your own figure. A mismatch means a tax or discount misunderstanding. |
| `would_create.new_items` | If this is large and unexpected, your item codes are unstable. |

You can also validate offline against
[`schema/invoice.schema.json`](schema/invoice.schema.json) in your own CI:

```bash
pip install check-jsonschema
check-jsonschema --schemafile docs/schema/invoice.schema.json your-invoice.json
```

The schema catches shape and format problems. The dry run additionally catches
anything requiring our data — unknown accounts, unknown tax templates, ZATCA
classification.

---

## 9. Pull mode specifics

Only if you cannot push.

Expose one authenticated endpoint returning JSON:

```json
{
  "status": "success",
  "payloads": [ { "external_id": "...", "customer": "...", "items": [] } ],
  "meta": { "next_cursor": null }
}
```

Every key name here is **configurable on our side** — these are only defaults. Tell
us yours and we match them. See
[`schema/feed.schema.json`](schema/feed.schema.json) and
[`samples/feed-response.json`](samples/feed-response.json).

Tell us:

| Question | Why |
|---|---|
| Endpoint URL | |
| Auth: header key, bearer token or basic? Header name? | We store the secret encrypted |
| Any additional headers (tenant id etc.)? | We support multiple |
| Which key holds the invoice array? | Dotted paths fine, e.g. `data.invoices` |
| Which key is the unique document id? | Idempotency depends on it |
| Can we request a date range? Parameter names and date format? | Otherwise we refetch your whole history every 15 minutes |
| Is the feed paginated? Page number, offset or cursor? Parameter names? | Otherwise we only ever read page one |
| Expected volume per day | Sizing |

An empty `payloads` array is a valid response and means nothing new.

---

## 10. Things that will bite

| Mistake | Consequence |
|---|---|
| `external_id` not stable across retries | Duplicate invoices |
| B2B buyer sent without `tax_id` | Filed as *simplified* — compliance defect, no error shown |
| `building_number` as `521` not `"0521"` | ZATCA rejects the invoice |
| `postal_code` as a number, leading zero lost | ZATCA rejects the invoice |
| `rate` sent **including** VAT | Totals overstated by 15% |
| Negative `rate` to represent a credit | Rejected — use `is_return` |
| Mixed VAT rates without `item_tax_template` | Zero-rated lines charged 15% |
| Item codes that change between runs | Item master fills with duplicates |
| Sending the invoice before it is final | Submitted invoices cannot be edited — ZATCA-cleared ones are legally immutable |

Last point deserves emphasis: **only send an invoice once it is final in your
system.** Once submitted and cleared with ZATCA it cannot be changed — a correction
requires a credit note.

---

## 11. What we send you back

On success, for every invoice:

| Field | Use |
|---|---|
| `data.invoice.invoice` | The ERPNext invoice name. **Store it** — needed for `return_against`. |
| `data.invoice.grand_total` | Reconcile against your figure. |
| `data.zatca.qr_png_base64` | The ZATCA QR image. Put it on your printed/PDF invoice. |
| `data.zatca.uuid`, `data.zatca.invoice_hash` | ZATCA identifiers, for your records. |
| `data.zatca.integration_status` | ZATCA's verdict. Arrives asynchronously — poll `get_status`. |
| `warnings` | Non-fatal, but log them. Usually incomplete buyer address. |

Full API reference: [`USER_GUIDE.md`](USER_GUIDE.md).

---

## 12. Checklist before you tell us you are ready

- [ ] `external_id` is our document number and never changes on retry
- [ ] Every business buyer sends `tax_id` (or `buyer_id_type` + `buyer_id_value`)
- [ ] Buyer address sends street, building number (4 digits), district, city, postal code (5 digits)
- [ ] `building_number` and `postal_code` are strings
- [ ] `rate` excludes VAT
- [ ] Mixed VAT rates send `item_tax_template` per line
- [ ] Credit notes send `is_return: 1` and `return_against`
- [ ] Item codes and UOM list shared with us so masters can be pre-created
- [ ] `validate_payload` returns `valid: true` with empty `errors` **and** empty `zatca_readiness.blocking`, for at least: one B2B invoice, one B2C invoice, one credit note, one mixed-VAT invoice
- [ ] Retry logic re-sends the same `external_id` rather than generating a new one
