# ZATCA API

A generic REST bridge for ERPNext. An external system POSTs an invoice; the app
creates the Sales Invoice, submits it, and returns the **ZATCA QR code** — Phase 1
or Phase 2 — together with the full invoice detail in the same HTTP response.

```
POST /api/method/zatca_api.api.v1.create_invoice
  ↓
Customer / Address / Item / UOM resolved (idempotently)
  ↓
Sales Invoice created + submitted
  ↓
ksa_compliance signs it and files it with ZATCA
  ↓
{ invoice: {...}, zatca: { uuid, invoice_hash, qr_content, qr_png_base64, ... } }
```

## What it does not do

**No ZATCA cryptography.** No TLV encoding, no UBL XML generation, no invoice
hashing, no XAdES signing, no certificate handling. All of that is delegated to
[`ksa_compliance`](https://github.com/lavaloon-eg/ksa_compliance), which is the
audited implementation of a spec that is unforgiving about byte-level detail. This
app reads what that app produced and shapes it for the response.

`ksa_compliance` is an **optional** runtime dependency. Without it the app still
creates invoices over REST and simply reports `zatca.available = false`.

## Install

```bash
bench get-app https://github.com/EnfonoTech/zatca_api.git
bench --site <site> install-app zatca_api
bench --site <site> migrate
```

For ZATCA, also install and configure `ksa_compliance`:

```bash
bench get-app https://github.com/lavaloon-eg/ksa_compliance.git
bench --site <site> install-app ksa_compliance
```

Then open **ZATCA API Settings** in the desk (ZATCA API workspace) and set the
default company. Confirm readiness with:

```bash
curl -H "Authorization: token <api_key>:<api_secret>" \
  "https://<site>/api/method/zatca_api.api.v1.ping"
```

## Endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `zatca_api.api.v1.validate_payload` | **Dry run** — validate, writing nothing |
| POST | `zatca_api.api.v1.create_invoice` | Create + submit an invoice, return the QR |
| POST | `zatca_api.api.v1.create_credit_note` | Create a credit note (`is_return`) |
| POST | `zatca_api.api.v1.submit_invoice` | Submit an existing draft |
| POST | `zatca_api.api.v1.resubmit_to_zatca` | Retry a Rejected / Resend filing |
| GET | `zatca_api.api.v1.get_invoice` | One invoice + its ZATCA block |
| GET | `zatca_api.api.v1.get_status` | Cheap clearance-status poll |
| GET | `zatca_api.api.v1.list_invoices` | Paginated list |
| GET | `zatca_api.api.v1.ping` | Health and capability probe |
| POST | `zatca_api.api.v1.pull_now` | Trigger a configured pull source |

Full request/response reference, error codes and worked examples:
**[docs/USER_GUIDE.md](docs/USER_GUIDE.md)**, also served from the site at
`/user-guide`.

Building the feed on the other side? Send them
**[docs/DATA_CONTRACT.md](docs/DATA_CONTRACT.md)** plus
[`docs/schema/`](docs/schema/) and [`docs/samples/`](docs/samples/), and have them
self-test against `validate_payload` — a dry run that reports every problem, the real
totals, and whether the invoice would file as standard or simplified, while writing
nothing to the database.

A Postman collection and environment are in [`postman/`](postman/).

## Configuration, not code

Nothing client-specific is hardcoded. Everything below lives in
**ZATCA API Settings**:

- **Credentials** — encrypted `Password` fields, never in source or a git diff.
- **Field mapping** — map any payload key onto any ERPNext field, including custom
  fields, with type coercion. This is what replaces per-client code branches.
- **Master-data defaults** — customer group, territory, item group, UOM, country.
  Auto-creation is opt-in per master type.
- **Address parsing** — overridable regexes that recover ZATCA's required buyer
  address parts (street, building number, district, city, postal code) from a
  single free-text address line.
- **Security** — optional shared-secret header and IP/CIDR allowlist on top of
  Frappe token auth.

Key names are matched case- and separator-insensitively, so `"Customer Name"`,
`customer_name`, `customerName` and `CUSTOMER_NAME` all resolve without
configuration.

## Idempotency

Every invoice carries the caller's `external_id` in an indexed custom field
(`Sales Invoice.zatca_api_external_id`). A repeat request with the same id updates
the draft or, if the invoice is already submitted, returns it flagged
`duplicate` — it is never re-posted.

This matters because the obvious alternative does not work:
`frappe.model.naming.set_new_name` executes `doc.name = None` for any DocType with
a naming series, so passing `{"name": "INV-001"}` to `frappe.get_doc` is silently
discarded and the invoice is named `ACC-SINV-YYYY-#####`. Existence checks against
the external number can therefore never match, and a repeating import creates a
fresh duplicate on every run.

## Safety properties

- **Submitted invoices are never modified.** Re-posting one rewrites GL entries
  behind the ledger, and once ZATCA has cleared an invoice it is legally immutable.
- **Taxes are computed by ERPNext.** The per-row `item_tax_template` is set and
  ERPNext prices each line from it. `ksa_compliance` then reads that same link field
  and the ZATCA category configured on the template to build the per-line VAT
  category for the XML. Flattening templates into one `On Net Total` row would charge
  standard VAT on zero-rated and exempt lines.
  This holds only where every template prices the same VAT account as the header tax
  row, no item master carries a conflicting template, and each template has its ZATCA
  category set — see `docs/USER_GUIDE.md` §Taxes.
- **No `frappe.set_user('Administrator')`.** Every endpoint checks the *session
  user's* DocType permissions. A test asserts no module in the app calls
  `set_user`.
- **No `allow_guest`.** Authentication is Frappe token auth; list endpoints are
  paginated with a hard ceiling.
- **VAT numbers reach the field that decides B2B.** `ksa_compliance`'s
  `is_b2b_customer()` reads `Customer.custom_vat_registration_number`, not the core
  `tax_id`. Writing only `tax_id` files a genuine B2B sale as *simplified* instead
  of clearing it as *standard*. This app writes both.

## Tests

```bash
bench --site <site> run-tests --app zatca_api
```

## License

MIT
