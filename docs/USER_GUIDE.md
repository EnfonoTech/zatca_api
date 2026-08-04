# ZATCA API — Integration Guide

Version 1.0.0 · for Frappe/ERPNext v15 · verified against `frappe 15.68.1`, `ksa_compliance 0.58.0`

This is the connecting guide for an external system that needs to create ERPNext
Sales Invoices over REST and get the **ZATCA QR code** back in the response.

---

## Contents

1. [Install](#1-install)
2. [Authentication](#2-authentication)
3. [Configure](#3-configure)
4. [Response envelope](#4-response-envelope)
5. [The `zatca` block](#5-the-zatca-block)
6. [Endpoints](#6-endpoints)
7. [Invoice payload reference](#7-invoice-payload-reference)
8. [Error codes](#8-error-codes)
9. [Idempotency](#9-idempotency)
10. [ZATCA Phase 2 checklist](#10-zatca-phase-2-checklist)
11. [Pull mode](#11-pull-mode-optional)
12. [Custom field mapping](#12-custom-field-mapping)
13. [Troubleshooting](#13-troubleshooting)
14. [Postman](#14-postman)
15. [Handing the spec to a vendor](#15-handing-the-spec-to-a-vendor)

---

## 1. Install

```bash
bench get-app https://github.com/EnfonoTech/zatca_api.git
bench --site <site> install-app zatca_api
bench --site <site> migrate
```

For ZATCA e-invoicing you also need `ksa_compliance`, which does all the
cryptography. `zatca_api` performs **no** TLV encoding, XML generation, hashing or
signing itself:

```bash
bench get-app https://github.com/lavaloon-eg/ksa_compliance.git
bench --site <site> install-app ksa_compliance
```

`ksa_compliance` is optional. Without it, invoice creation over REST still works
and every response reports `zatca.available = false`.

Verify the install:

```bash
curl -s -H "Authorization: token <api_key>:<api_secret>" \
  "https://<site>/api/method/zatca_api.api.v1.ping" | python3 -m json.tool
```

`custom_fields_installed` must be `true`. If it is `false`, run `bench migrate`.

---

## 2. Authentication

Standard Frappe token authentication. No endpoint allows anonymous access.

**Generate a key pair:** ERPNext desk → **User** → your integration user →
**Settings** → **API Access** → **Generate Keys**. The secret is displayed once.

Send it on every request:

```
Authorization: token <api_key>:<api_secret>
```

The user needs `create` and `submit` permission on **Sales Invoice**, plus `read`
on **ZATCA API Settings** for `pull_now`. `Accounts Manager` covers all of it.

Permissions are checked against **this user**, not escalated. A user without
Sales Invoice permission gets `403 forbidden` even with valid keys.

### Optional extra guards

Configured in **ZATCA API Settings → Security**:

| Guard | Effect |
|---|---|
| Require Shared Secret Header | Every request must also carry `X-ZATCA-API-Secret: <secret>` (header name configurable). Compared in constant time. |
| Allowed IPs / CIDRs | One IP or CIDR per line. Blank allows any source. `X-Forwarded-For` is honoured left-most only. |

---

## 3. Configure

Desk → **ZATCA API** workspace → **ZATCA API Settings**.

The settings you will actually care about:

| Setting | Default | Notes |
|---|---|---|
| **Enabled** | on | Master switch. Off ⇒ every endpoint returns `503 app_disabled`. |
| **Default Company** | — | Used when a payload omits `company`. |
| **Auto Submit Invoices** | on | **A ZATCA QR only exists for a submitted invoice.** Off ⇒ drafts, no QR. |
| **Submit Mode** | Immediate | `Immediate` puts the QR in the create response. `Queued` returns the draft and submits in a background job — poll `get_status`. |
| **Update Existing Drafts** | on | A repeat `external_id` overwrites the draft. Submitted invoices are never touched. |
| **Allow Modifying Submitted Invoices** | off | Leave off. Re-posting a submitted invoice rewrites GL entries, and a cleared invoice is legally immutable. |
| **Create Missing Customers / Items** | on | Auto-create master data from the payload. |
| **Create Missing UOMs / Projects** | off | Off by default: a typo would otherwise pollute the master tables permanently. |
| **Require Complete Address For B2B** | **on** | Reject an invoice for a buyer with a VAT number or other ZATCA identifier whose address is missing street, building number (4 digits), district, city or postal code (5 digits). Mirrors `ksa_compliance`, which validates the buyer address whenever the invoice type is *Standard*. Turn off only on a non-KSA site or during a legacy migration. |
| **Parse Free-Text Address** | off | Turn on when the source system sends one concatenated address line — see §10. |
| **Phase Resolution** | Auto | `Auto` returns Phase 2 when the company has active ZATCA Business Settings, else falls back to Phase 1. |
| **Include QR PNG** | on | Off keeps responses small when the caller renders the QR itself. |
| **Include Signed XML** | off | Adds ~30–60 KB per invoice. Also available per-request via `include_xml=1`. |
| **Wait For Clearance (seconds)** | 0 | **Keep at 0.** The QR/UUID/hash are already in the response; only `integration_status` is asynchronous. Capped at 30. |
| **Log Requests / Log Payload Bodies** | on | Writes a **ZATCA API Request Log** row per call. Base64 images and XML are recorded by size only. Secret-shaped keys are redacted. |
| **Log Retention (days)** | 30 | Nightly cleanup. `0` keeps forever. |

---

## 4. Response envelope

Frappe wraps every whitelisted return value in `message`, so the envelope sits one
level down:

```json
{
  "message": {
    "success": true,
    "request_id": "a1b2c3d4e5f6",
    "timestamp": "2026-08-04 11:30:00",
    "data": { "...": "..." },
    "warnings": [],
    "errors": []
  }
}
```

The shape is **identical for success and failure**, so one parser handles both.

- `success` — branch on this, plus the HTTP status.
- `request_id` — correlation id. It is also stored on the request log; quote it
  when reporting a problem.
- `warnings` — non-fatal. The invoice was created. Most commonly buyer address
  parts that ZATCA will reject for a *standard* invoice. Log these.
- `errors[].code` — **branch on the code, never on the message text.** Messages are
  translated.

---

## 5. The `zatca` block

`data.zatca` is **always present**. Check `available` first.

```json
"zatca": {
  "available": true,
  "phase": "Phase 2",
  "additional_fields_doc": "ACC-SINV-2026-00051-AdditionalFields-42",
  "uuid": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
  "invoice_counter": 42,
  "invoice_hash": "NWZlY2ViNjZmZmM4NmYzOGQ5NTI3ODZjNmQ2OTZjNzk=",
  "previous_invoice_hash": "...",
  "qr_content": "AQ5aQVRDQSBTZWxsZXI...",
  "qr_format": "base64-tlv",
  "qr_png_base64": "iVBORw0KGgoAAAANSUhEUg...",
  "qr_png_data_uri": "data:image/png;base64,iVBORw0KGgo...",
  "integration_status": "Accepted",
  "invoice_type_transaction": "0100000",
  "invoice_type_code": "388",
  "is_cleared": true,
  "is_pending": false
}
```

### Phase 1 vs Phase 2

| Field | Phase 1 | Phase 2 |
|---|---|---|
| `phase` | `Phase 1` | `Phase 2` |
| `qr_content` | `null` — the `ksa_compliance` helper returns a rendered image only | base64 **TLV** string |
| `qr_format` | `png-only` | `base64-tlv` |
| `qr_png_base64` / `qr_png_data_uri` | ✅ | ✅ |
| `uuid`, `invoice_hash`, `previous_invoice_hash`, `invoice_counter` | `null` — there is no filing | ✅ |
| `integration_status` | `Not Applicable` | ZATCA outcome, **asynchronous** |
| `additional_fields_doc` | absent | SIAF document name |

### Timing — the one thing to get right

On submit, `ksa_compliance` signs the invoice **locally** and creates a
*Sales Invoice Additional Fields* document. That means:

- **`uuid`, `invoice_hash`, `previous_invoice_hash`, `qr_content` and the QR image
  are available immediately** in the `create_invoice` response.
- **Only `integration_status` is asynchronous** — the clearance (standard) or
  reporting (simplified) call to ZATCA runs in a background job.

So: take the QR from the create response, and poll `get_status` for the filing
outcome. Do **not** set *Wait For Clearance* to hold the HTTP connection open.

### `integration_status` values

| Value | `is_pending` | `is_cleared` | Meaning |
|---|---|---|---|
| `Ready For Batch` | ✅ | — | Signed, not yet filed. |
| `Accepted` | — | ✅ | ZATCA accepted it. |
| `Accepted with warnings` | — | ✅ | Accepted; ZATCA raised warnings. |
| `Clearance switched off` | — | ✅ | Filing intentionally disabled. |
| `Rejected` | — | — | ZATCA refused it. Fix and `resubmit_to_zatca`. |
| `Resend` | — | — | Transient failure (timeout, 5xx, rate limit). Retry. |
| `Corrected` | — | — | Superseded by a corrected filing. |
| `Duplicate` | — | — | ZATCA already had this invoice. |

### When `available` is false

`reason` says why. The usual causes:

- `"Invoice is in Draft. A ZATCA QR only exists for a submitted invoice."`
- `"No ZATCA settings resolved for this company..."` — configure ZATCA Business
  Settings (Phase 2) or ZATCA Phase 1 Business Settings.
- `"The ksa_compliance app is not installed on this site..."`
- `"ZATCA reporting disabled in settings."` — *Phase Resolution* is `Disabled`.

Creating the invoice still **succeeded** in all of these. Branch on the flag.

---

## 6. Endpoints

Base path: `https://<site>/api/method/zatca_api.api.v1.`

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `validate_payload` | **Dry run** — validate and report, writing nothing |
| POST | `create_invoice` | Create (+submit) an invoice, return the QR |
| POST | `create_credit_note` | Create a return / credit note |
| POST | `submit_invoice` | Submit an existing draft |
| POST | `resubmit_to_zatca` | Retry a Rejected / Resend filing |
| GET | `get_invoice` | One invoice + ZATCA block |
| GET | `get_status` | Cheap clearance-status poll |
| GET | `list_invoices` | Paginated list |
| GET | `ping` | Health / capability probe |
| POST | `pull_now` | Trigger a configured pull source |

A wrong HTTP verb returns **403**, not 405 — that is Frappe's behaviour.

### `validate_payload` — dry run

The endpoint to hand to whoever builds the feed. **Nothing is written to the
database**, so they can hammer it against the live site while iterating.

```bash
curl -X POST "https://<site>/api/method/zatca_api.api.v1.validate_payload" \
  -H "Authorization: token $API_KEY:$API_SECRET" \
  -H "Content-Type: application/json" \
  -d @invoice.json
```

Add `"document_type": "Credit Note"` to the body, or `?document_type=Credit%20Note`
on the URL — both work. (Frappe discards the query string when a JSON body is
present, so this endpoint reads it off the request directly.)

Response `data`:

| Key | Meaning |
|---|---|
| `valid` | The verdict. **A 200 only means the check ran** — always read this. |
| `errors[]` | Every problem, each naming the exact field. |
| `warnings[]` | Non-fatal, mostly incomplete buyer address. |
| `totals` | The **real** ERPNext totals — net, tax, grand, plus each tax row. |
| `would_create` | Which Customer / Items / UOMs would be new. A large unexpected `new_items` means unstable item codes. |
| `resolved` | What the payload parsed to, and any existing invoice with that `external_id`. |
| `zatca` | `invoice_type` (`Standard`/`Simplified`), `buyer_is_b2b`, and the `reason`. |
| `zatca_readiness` | `would_be_rejected_by_zatca` plus `blocking` and `advisory` lists. |
| `dry_run` | Always `true`. |

**How it can promise "writes nothing" and still give real totals.** The payload runs
through the *same* code path as a real request, inside a database savepoint that is
always rolled back — on every path, including an unexpected exception. So the totals
come from ERPNext's own `calculate_taxes_and_totals` and the errors from ERPNext's and
`ksa_compliance`'s own `validate` hooks, rather than a parallel reimplementation that
could drift and pass payloads that later fail. The invoice is inserted as a **draft
only**, never submitted, so no GL entry is written and nothing is filed with ZATCA.

It requires the same `create` permission on Sales Invoice as the real endpoint, so it
cannot be used to probe the site anonymously.
> **What the dry run does not cover.** It validates *creation*, not *submission* — the
> invoice is inserted as a draft and rolled back, never submitted. Checks that only run
> when GL entries are posted therefore pass here and can still fail on the real call.
> The one that bit during test-site setup was a missing Fiscal Year:
> `valid: true` from the dry run, then
> `Date ... is not in any active Fiscal Year` from `create_invoice`. Treat a clean dry
> run as "the payload is right", not "the site is configured".


### `create_invoice`

```bash
curl -X POST "https://erp.example.com/api/method/zatca_api.api.v1.create_invoice" \
  -H "Authorization: token $API_KEY:$API_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "external_id": "INV-2026-0001",
    "customer": "Al Rajhi Trading",
    "company": "Enfono KSA",
    "posting_date": "2026-08-04",
    "items": [
      {"item_code": "SVC-01", "qty": 2, "rate": 500}
    ]
  }'
```

Response `data`:

| Key | Meaning |
|---|---|
| `action` | `created` · `updated` · `duplicate` |
| `duplicate` | `true` when the external id already mapped to a submitted invoice |
| `submitted` | `docstatus == 1` |
| `submission_queued` | `true` in Queued submit mode |
| `invoice` | Full invoice detail — see below |
| `zatca` | §5 |
| `clearance_wait` | Only when *Wait For Clearance* > 0 |

`data.invoice` carries `invoice` (ERPNext name), `external_id`, `docstatus`,
`status`, `is_return`, `return_against`, `company`, `customer`, `customer_name`,
`tax_id`, `posting_date`, `posting_time`, `due_date`, `currency`,
`conversion_rate`, `net_total`, `total_taxes_and_charges`, `grand_total`,
`rounded_total`, `outstanding_amount`, `project`, plus `items[]` and `taxes[]`.

### `create_credit_note`

Same payload with `is_return` implied. Quantities are **sign-normalised** — send
positive or negative, either works. `return_against` is optional but, when
supplied, must reference a **submitted** Sales Invoice (ZATCA requires a credit
note to identify its original).

### `get_invoice` / `get_status`

```
GET .get_invoice?external_id=INV-2026-0001
GET .get_invoice?invoice=ACC-SINV-2026-00051&include_xml=1
GET .get_status?external_id=INV-2026-0001
```

`get_status` returns only `docstatus`, `status` and a trimmed `zatca` block —
use it for polling.

### `list_invoices`

```
GET .list_invoices?company=Enfono%20KSA&from_date=2026-08-01&to_date=2026-08-31&limit=50&start=0
GET .list_invoices?integration_status=Rejected&limit=50
```

| Param | Default | Notes |
|---|---|---|
| `limit` | 20 | Hard cap **200**; **50** when `include_qr=1` |
| `start` | 0 | Offset |
| `docstatus` | 1 | `0` draft, `1` submitted, `2` cancelled |
| `include_qr` | 0 | Renders a QR per row — expensive |
| `integration_status` | — | Filtered **before** pagination, so `total`/`has_more` stay truthful |

Results respect the calling user's permissions, including User Permissions that
restrict them to one company.

### `resubmit_to_zatca`

```json
{ "invoice": "ACC-SINV-2026-00051" }
```

Delegates to `ksa_compliance`'s own `fix_rejection`, which creates a fresh SIAF
document. Re-signing outside that routine would break the previous-invoice-hash
chain for every later invoice. An already-accepted invoice is skipped, not refiled.

---

## 7. Invoice payload reference

Key names are matched **case- and separator-insensitively**, so `"Customer Name"`,
`customer_name`, `customerName` and `CUSTOMER_NAME` all resolve. Aliases in
parentheses.

### Required

| Field | Notes |
|---|---|
| `external_id` (`naming_series`, `invoice_no`, `invoice_number`, `reference`, `document_no`, `source_id`) | Your unique reference. Drives idempotency — see §9. |
| `customer` (`customer_name`, `customer_code`, `buyer`) | Created if missing and auto-creation is on. |
| `items` (`lines`, `invoice_items`, `item_list`) | At least one row. |

### Header — optional

| Field | Aliases | Notes |
|---|---|---|
| `company` | `company_name` | Falls back to Default Company. |
| `tax_id` | `vat_number`, `vat_registration_number`, `buyer_vat` | Buyer VAT. Also written to `Customer.custom_vat_registration_number` — see §10. |
| `buyer_id_type` / `buyer_id_value` | `crn`, `commercial_registration` | ZATCA codes: `TIN CRN MOM MLS SAG NAT GCC IQA PAS OTH`. Use when the buyer has no VAT number. |
| `posting_date` | `invoice_date`, `date` | Defaults to today. |
| `posting_time` | `invoice_time`, `time` | Feeds the ZATCA QR timestamp. |
| `due_date` | `payment_due_date` | |
| `currency` / `conversion_rate` | `currency_code`, `exchange_rate` | |
| `taxes` / `tax_template` | `taxes_and_charges` | See tax precedence below. |
| `address_title` | `customer_address` | Address record title. |
| `address_display` | `address_text`, `full_address`, `billing_address` | Free-text address; parsed when enabled. |
| `address_parts` | `billing_address_parts` | Structured address — preferred. |
| `project` | `project_name` | |
| `po_no` | `purchase_order_no`, `customer_po` | |
| `remarks` | `notes`, `comments` | |
| `cost_center`, `debit_to`, `selling_price_list`, `update_stock` | | Pass-through. |
| `payment_mode` + `is_pos` | `mode_of_payment` | Supplies the ZATCA **payment means code**. **Requires `is_pos: 1`** — see below. |
| `payment_amount` | `paid_amount` | Amount on the payments row. Defaults to `0`, so declaring a payment means does not mark the invoice paid. |
| `is_return` | `is_credit_note` | |
| `return_against` | `against_invoice`, `original_invoice` | Must be submitted. |
| `is_debit_note` | | |
| `submit` | `auto_submit`, `do_submit` | Overrides the Auto Submit setting for this request. |

### Item rows

| Field | Aliases | Notes |
|---|---|---|
| `item_code` | `code`, `sku`, `item` | **Required.** |
| `qty` | `quantity` | Defaults to 1 when absent. An explicit `0` is **rejected**, not silently changed. |
| `rate` | `price`, `unit_price` | Must not be negative — use `is_return` for credit notes. |
| `item_name`, `description` | | |
| `uom` | `unit`, `unit_of_measure` | Conversion factor resolved automatically. |
| `item_tax_template` | `tax_template`, `item_tax` | **Set this on mixed-rate invoices.** |
| `discount_amount`, `discount_percentage` | | |
| `income_account`, `cost_center`, `warehouse`, `item_group`, `is_stock_item` | | |

### Address parts

| Field | Aliases | ZATCA requirement (standard invoice) |
|---|---|---|
| `street` | `address_line1`, `street_name` | Required |
| `building_number` | `custom_building_number` | Required, **exactly 4 digits** |
| `district` | `custom_area`, `area`, `neighbourhood` | Required |
| `city` | `town` | Required |
| `postal_code` | `pincode`, `zip`, `postcode` | Required, **exactly 5 digits** |
| `additional_street` | `address_line2` | Optional |
| `state`, `country`, `email`, `phone` | | Optional |

Short numeric building numbers and postal codes are zero-padded automatically
(`521` → `0521`). Values that are too long are left alone so `ksa_compliance`
reports the real problem against the Address record.

### Payment means (ZATCA) — requires `is_pos`

`ksa_compliance` resolves the ZATCA payment means code from
`payments[0].mode_of_payment` → `Mode of Payment.custom_zatca_payment_means_code`.

ERPNext, however, gates the `Sales Invoice.payments` table on
`eval:doc.is_pos===1`, so on a regular invoice the row is **silently discarded at
insert** and no payment means code is ever reported.

This app refuses rather than dropping it. Sending `payment_mode` without
`is_pos` returns:

```json
{ "code": "validation_error",
  "message": "payment_mode requires is_pos = 1. ERPNext only stores the payments table on a POS invoice..." }
```

Send both when you genuinely want a POS invoice:

```json
{ "payment_mode": "Cash", "is_pos": 1, "payment_amount": 230 }
```

Be aware that `is_pos: 1` makes it a POS invoice: ERPNext posts the paid amount
against the mode of payment's account, so `payment_amount` changes the GL. Leave
`payment_amount` off (defaults to `0`) to declare the means without recording a
receipt.

### Tax precedence

1. **`taxes` array in the payload** — used verbatim. Each `account_head` is
   validated to exist *and belong to the invoice's company*.
2. **`tax_template`** — a named Sales Taxes and Charges Template for the company.
3. **The company's default template.**

In all three cases the per-row `item_tax_template` still governs which lines are
taxed and at what rate. ERPNext computes `item_wise_tax_detail`, which is exactly
the field `ksa_compliance` reads to build per-line VAT categories for the ZATCA
XML — so **mixed standard / zero-rated / exempt invoices come out correct**.

If no tax rows can be resolved and ZATCA is enabled for the company, the request
is rejected with a specific message, because `ksa_compliance` requires a non-empty
taxes table.

```json
"taxes": [
  {"account_head": "VAT 15% - EK", "charge_type": "On Net Total", "rate": 15, "description": "VAT 15%"}
]
```

`charge_type` `Actual` takes `tax_amount`; every other type takes `rate`.

---

## 8. Error codes

| HTTP | `code` | Meaning |
|---|---|---|
| 400 | `validation_error` | Bad payload, or ERPNext/ksa_compliance validation. `details` names the field. |
| 401 | `unauthorized` | No authenticated session. |
| 403 | `forbidden` | Missing DocType permission, bad shared secret, IP not allowed — or a wrong HTTP verb. |
| 404 | `not_found` | Unknown invoice / external id. |
| 409 | `duplicate` | External id conflict. |
| 409 | `immutable_document` | External id maps to a **cancelled** invoice. Send a new id. |
| 424 | `zatca_unavailable` | `ksa_compliance` not installed. |
| 502 | `upstream_error` | Pull-mode source failed. |
| 503 | `app_disabled` | *Enabled* is off in settings. |
| 500 | `internal_error` | Unexpected. The `request_id` matches an Error Log entry. |

```json
{
  "message": {
    "success": false,
    "request_id": "9f8e7d6c5b4a",
    "data": {},
    "warnings": [],
    "errors": [
      {
        "code": "validation_error",
        "message": "Missing required field(s): customer, items.",
        "details": { "missing": ["customer", "items"] }
      }
    ]
  }
}
```

A failed request is fully rolled back — it never leaves half-created Customers,
Items or Addresses behind.

---

## 9. Idempotency

Every invoice stores your `external_id` in the indexed custom field
`Sales Invoice.zatca_api_external_id`. On a repeat request:

| Existing invoice state | Result |
|---|---|
| none | `action: "created"` |
| Draft, *Update Existing Drafts* on | `action: "updated"` — rewritten in place |
| Draft, *Update Existing Drafts* off | `action: "duplicate"` — left untouched |
| **Submitted** | `action: "duplicate"`, `duplicate: true` — **never re-posted** |
| Cancelled | `409 immutable_document` — send a new id |

The lookup takes a `FOR UPDATE` lock on the indexed column, so two concurrent
requests carrying the same id serialise instead of racing to insert two invoices.

**Retrying is always safe.** A network timeout where the server actually committed
resolves cleanly: retry and you get the existing invoice with its QR.

> **Why not just use the invoice name?** Because it cannot work.
> `frappe.model.naming.set_new_name` executes `doc.name = None` for any DocType
> with a naming series, which Sales Invoice has. Passing `{"name": "INV-001"}` is
> silently discarded and the invoice is named `ACC-SINV-YYYY-#####`. An existence
> check against the external number therefore never matches, and a repeating
> import creates a fresh duplicate on every run.

---

## 10. ZATCA Phase 2 checklist

Work through this before going live. `ping` reports items 1–3.

1. **`ksa_compliance` installed and configured.** `ZATCA Business Settings` for
   the company with `status = Active` and `enable_zatca_integration` on. CSR
   generated, production CSID obtained, `zatca_cli_path` and `java_home` set.
2. **Company address complete.** `ZATCA Business Settings` pulls street, building
   number, city, district and postal code from the company address.
3. **Company VAT registration number set** (15 digits, starts and ends with `3`).
4. **A default Sales Taxes and Charges Template** for the company, or send `taxes`
   / `tax_template` on every request. `ksa_compliance` rejects an invoice with an
   empty taxes table.
5. **Buyer identification for B2B.** Send `tax_id`, or `buyer_id_type` +
   `buyer_id_value`.

   > This one bites. `ksa_compliance.is_b2b_customer()` reads
   > `Customer.custom_vat_registration_number` and `Customer.custom_additional_ids`
   > — **not** the core `tax_id` field. A customer with only `tax_id` populated is
   > classified B2C, so a genuine B2B sale is *reported* as **simplified** instead
   > of *cleared* as **standard**. This app writes both fields, so sending `tax_id`
   > is enough — but if you create customers by any other route, set
   > `custom_vat_registration_number` yourself.

6. **Buyer address complete for standard invoices.** Street, building number
   (4 digits), district, city, postal code (5 digits). Send `address_parts`, or
   enable *Parse Free-Text Address* and send `address_display`.

   **This is enforced as an error for a B2B buyer**, before any document is created:

   ```json
   {
     "code": "validation_error",
     "message": "A complete buyer address is mandatory for a B2B customer, because ZATCA rejects a standard invoice without one. Problems: ...",
     "details": { "field": "address_parts", "is_b2b": true, "problems": ["..."],
                  "required": ["street", "building_number (4 digits)", "district", "city", "postal_code (5 digits)"] }
   }
   ```

   The rule comes from `ksa_compliance`: `_set_buyer_details` passes `validate=True`
   to `_set_buyer_address` whenever the invoice type is *Standard*, and throws
   outright when a B2B customer has no address at all. Enforcing it here means a
   precise per-field error instead of a rendered message at submission.

   For a **B2C** buyer the same gaps stay warnings — a simplified invoice needs no
   buyer address:

   ```json
   "warnings": [
     "Buyer address has no building number (custom_building_number).",
     "Buyer postal code \"123\" is 3 characters; ZATCA requires exactly 5."
   ]
   ```

7. **Auto Submit on.** No submission ⇒ no QR.
8. **Reconcile.** Poll `get_status`, or sweep
   `list_invoices?integration_status=Rejected` and refile with
   `resubmit_to_zatca`.

### Free-text address parsing

When the source system only exports one address line, enable *Parse Free-Text
Address*. The built-in patterns handle ERPNext's own KSA `address_display` layout:

```
Building No 4521, Olaya Street, Al Murabba Dist, P.C: 12613, Riyadh, Kingdom of Saudi Arabia
  → street = Olaya Street, building_number = 4521, district = Al Murabba Dist,
    postal_code = 12613, city = Riyadh
```

Variants are handled: `Building No.` with a dot, `P.C` without a colon,
`District` spelled out, `KSA` / `Saudi Arabia` / `Kingdom of Saudi Arabia`.

Override them in **Address Parse Patterns**, one `field=regex` per line — the
first capture group supplies the value:

```
address_line1=Street:\s*([^,]+)
custom_building_number=Bldg\s*#?\s*(\d+)
pincode=ZIP\s*(\d{5})
custom_area=Area:\s*([^,]+)
city=City:\s*([^,]+)
```

Patterns are compiled when you save, so a bad regex is rejected then rather than
breaking an invoice later. Explicit `address_parts` values always win over parsed
ones.

---

## 11. Pull mode (optional)

Use this only when the external system **cannot** POST into ERPNext. Push mode is
preferred because it returns the QR synchronously.

**ZATCA API Settings → Pull Sources**, one row per upstream endpoint:

| Field | Notes |
|---|---|
| Endpoint URL | Must be `http://` or `https://`. |
| HTTP Method | `GET` or `POST`. |
| Auth Type | `None` · `Header Key` · `Bearer Token` · `Basic`. |
| Secret / API Key | **Encrypted at rest.** Never echoed by any response, never in a git diff. |
| Payload Root Key | Key holding the invoice array, e.g. `payloads`. Dotted paths work. Blank ⇒ the body is the array. |
| Status Key / Status OK Value | Optional envelope assertion, e.g. `status` must equal `success`. |
| External ID Key | Which key uniquely identifies the source document. |
| Timeout | Seconds. Defaults to 30 — never unlimited. |
| Auto Submit | Submit pulled invoices. Required for a QR. |
| **Extra Headers** | One `Name: value` per line, for APIs needing more than one custom header. Not encrypted — keep secrets in *Secret / API Key*, which cannot be shadowed from here. |
| **Query Parameters** | One `key=value` per line. Placeholders substituted. |
| **Request Body (POST)** | JSON body for a POST-style feed. Placeholders substituted. |
| **Incremental Mode** | `Date Window` sends a from/to range so you stop refetching the whole history every 15 minutes. |
| **From / To Parameter**, **Lookback (days)**, **Date Format** | The window. Lookback is re-requested every poll on purpose, so a document the upstream system back-dates after a poll is still picked up; dedup makes the overlap free. |
| **Last Pulled At** | Read-only watermark. Only advances after a pull with **no** failures and no truncation — otherwise the failed documents would be skipped forever. |
| **Pagination Mode** | `Page Number`, `Offset` or `Cursor`. Without it only page one is ever imported. |
| **Page / Offset Parameter**, **Page Size**, **First Page Number** | Zero-based paging is supported — an explicit `0` is honoured. |
| **Cursor Parameter**, **Next Cursor Key** | Dotted path to the next cursor, e.g. `meta.next_cursor`. Paging stops when it is empty. |
| **Max Pages** | Hard stop so a bad cursor cannot loop forever. Hitting it sets `truncated` on the result and writes an Error Log entry — a partially imported feed is never reported as complete. |

Then tick **Enable Scheduled Pull**. A cron runs every 15 minutes and is a cheap
no-op while disabled. Trigger one immediately with `pull_now`.

Each invoice is committed in **its own transaction**, so a failure on invoice 40
leaves the first 39 durably imported and rolls back only the failed one. Dedup is
by external id, so re-polling the same feed does not duplicate anything.

---

## 12. Custom field mapping

Nothing client-specific is hardcoded. To land an arbitrary payload key on an
ERPNext field — including a custom field — add a row under
**ZATCA API Settings → Field Mapping**:

| Target DocType | Source Key | Target Field | Value Type | Mandatory |
|---|---|---|---|---|
| Sales Invoice | `MEASUREMENTDATE` | `custom_measurement_date` | Date | ☐ |
| Sales Invoice | `ANNEXUREREFID` | `custom_annexure_ref_id` | Data | ☐ |
| Sales Invoice Item | `LINE_REF` | `custom_line_ref` | Data | ☐ |
| Project | `CONTROLVALUEINSAR` | `custom_control_value` | Currency | ☐ |

- **Source Key** is matched with the same case/separator-insensitive rules; dotted
  paths reach into nested objects.
- **Target Field** is validated against the DocType meta **when you save**, so a
  typo is an error you see immediately rather than a value silently dropped.
- **Value Type** coercion uses `cint`/`flt`/`getdate`, which absorb `null`, `""`
  and locale-formatted numbers like `"1,250.00"`.
- **Mandatory** rejects the whole request when the key is absent.

Supported targets: Sales Invoice, Sales Invoice Item, Customer, Item, Address,
Project.

---

## 13. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `403` on every call | Bad or missing `Authorization` header; wrong header format (`token key:secret`); user lacks Sales Invoice permission; shared secret required but not sent. Also returned for a wrong HTTP verb. |
| `zatca.available = false`, reason mentions Draft | *Auto Submit* is off, or you sent `"submit": false`. |
| `zatca.available = false`, reason mentions settings | No `ZATCA Business Settings` (Active) or `ZATCA Phase 1 Business Settings` for the company. |
| `available` true but `integration_status` stuck at `Ready For Batch` | The background job has not run. Check `bench worker` / the scheduler is up, and `sync_with_zatca` on the business settings. |
| `custom_fields_installed = false` | Run `bench --site <site> migrate`. |
| Invoice filed as *simplified* when it should be *standard* | Buyer not recognised as B2B. Send `tax_id` (or `buyer_id_type`/`buyer_id_value`) — see §10 item 5. |
| `Please include tax rate in Sales Taxes and Charges Table` | No tax rows resolved. Set a default template for the company or send `taxes`/`tax_template`. |
| Duplicate invoices appearing | You are not sending a stable `external_id`, or sending a different one for the same source document. |
| `warnings` about the buyer address | Missing ZATCA-required address parts. Send `address_parts`, or enable free-text parsing. |
| Response too large | Turn off *Include QR PNG* and/or *Include Signed XML*; drop `include_qr` from `list_invoices`. |
| Where do I see what happened? | **ZATCA API Request Log** (endpoint, status, HTTP code, duration, payloads, ZATCA phase/UUID/status) and **Error Log** for tracebacks. Match on `request_id`. |

---

## 14. Postman

`postman/zatca_api.postman_collection.json` and
`postman/zatca_api.postman_environment.json`.

1. Import both in Postman.
2. Select the **ZATCA API** environment and fill in `base_url`, `api_key`,
   `api_secret`, `company`, `customer`, `item_code`.
3. Run **01 Ping** first.
4. Run the rest in order — later requests reuse `last_invoice` and `external_id`
   captured by earlier ones.

16 requests, covering: ping, minimal create, full B2B create with address, legacy
key names, idempotency, credit note, draft-then-submit, get, status poll, list,
list-rejected, resubmit, pull, and two deliberate error cases. Each has test
scripts asserting the envelope and logging the ZATCA phase, UUID and hash to the
Postman console.

---

## Appendix — running the tests

```bash
bench --site <site> run-tests --app zatca_api
```

94 tests: address parsing, payload normalisation and validation, end-to-end
invoice creation, idempotency, submitted-document immutability, mixed-rate tax
correctness, credit notes, master-data creation, security guards, and the ZATCA
bridge for both phases.

---

## 15. Handing the spec to a vendor

When the external team is building *to our requirements*, send them
**[`DATA_CONTRACT.md`](DATA_CONTRACT.md)** rather than this guide. It is written for
them: what to send, the ZATCA rules behind each field, the mistakes that cause silent
compliance defects, and a checklist to sign off against.

Also point them at:

| Artifact | Purpose |
|---|---|
| [`schema/invoice.schema.json`](schema/invoice.schema.json) | JSON Schema (2020-12) for offline validation in their own CI |
| [`schema/feed.schema.json`](schema/feed.schema.json) | Pull-mode response envelope |
| [`samples/`](samples/) | Worked payloads: standard B2B, B2B without a VAT number, simplified B2C, credit note, mixed VAT rates, minimal |
| `validate_payload` | Their self-test loop — see §6 |

```bash
pip install check-jsonschema
check-jsonschema --schemafile docs/schema/invoice.schema.json their-invoice.json
```

The schema catches shape and format problems offline. `validate_payload` catches
everything that needs our data — unknown accounts, unknown tax templates, and the
standard-vs-simplified classification.

Tests in `zatca_api/tests/test_contract.py` validate every shipped sample against the
schema **and** against the app's own normaliser, so the published contract cannot
drift from the code without a test failing.

### Placeholder tokens for pull sources

Usable in the endpoint URL, query parameters and request body:

| Token | Value |
|---|---|
| `{from_date}` / `{to_date}` | The incremental window, in the configured `Date Format` |
| `{page}` | Page number, offset by `First Page Number` |
| `{offset}` | `page_index × page_size` |
| `{cursor}` | Cursor from the previous response |
| `{page_size}` | Configured page size |

Unknown tokens are left untouched, so braces that occur naturally in a URL or JSON
body are safe.
