# zatca_api/services/puller.py
# Copyright (c) 2026, Enfono Technologies and contributors
"""Optional pull mode: poll an upstream endpoint and import the invoices it returns.

Push mode (``zatca_api.api.v1.create_invoice``) is the preferred integration -
it is synchronous, so the caller receives the ZATCA QR in the HTTP response. Pull
mode exists for upstream systems that cannot make outbound calls into ERPNext.

Differences from the naive polling loop this replaces:

* **Credentials come from ZATCA API Source rows**, with the secret in an encrypted
  ``Password`` field. Nothing is hardcoded and no key ever reaches a git diff.
* **Every request has a timeout.** A ``requests.get`` without one can hang a
  background worker indefinitely, and a stuck worker silently stops the queue.
* **One transaction per invoice.** ``commit`` after each document means a failure
  on invoice 40 leaves the first 39 durably imported instead of rolling the batch
  back, and a ``rollback`` on failure prevents a half-built invoice from leaking
  into the next iteration.
* **Idempotent.** Dedup is on the external id, so re-polling the same feed does not
  duplicate invoices. The previous implementation re-saved already-submitted
  invoices on every run, silently mutating posted accounting documents.
"""

import time

import frappe
from frappe import _
from frappe.utils import cint, cstr, now

from zatca_api.services import invoice as invoice_service
from zatca_api.services.payload import PayloadError, as_dict, normalise_invoice
from zatca_api.zatca_api.doctype.zatca_api_settings.zatca_api_settings import get_settings


def _dig(data, path: str):
    """Resolve a dotted path inside a nested dict. Blank path returns ``data``."""
    if not path:
        return data
    current = data
    for part in path.split('.'):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def fetch_page(source, context: dict) -> tuple:
    """Fetch one page. Returns ``(items, next_cursor, error)``.

    ``requests`` is imported lazily so this module still imports on a bench where it
    is unavailable.
    """
    import requests

    started = time.monotonic()
    url = source.substitute(source.endpoint_url, context)

    try:
        response = requests.request(
            method=(source.http_method or 'GET').upper(),
            url=url,
            headers=source.build_headers(),
            params=source.build_query_params(context),
            json=source.build_body(context),
            auth=source.build_auth(),
            timeout=source.request_timeout,
            verify=bool(cint(source.verify_ssl)),
        )
    except requests.exceptions.RequestException as exc:
        return [], None, f'{type(exc).__name__}: {exc}'

    duration_ms = int((time.monotonic() - started) * 1000)

    if response.status_code != 200:
        # Truncate: an upstream error page can be megabytes of HTML.
        return [], None, f'HTTP {response.status_code} after {duration_ms} ms: {response.text[:2000]}'

    try:
        body = response.json()
    except ValueError:
        return [], None, f'Response is not JSON: {response.text[:2000]}'

    if source.status_key:
        actual = cstr(_dig(body, source.status_key))
        expected = cstr(source.status_ok_value)
        if expected and actual != expected:
            return [], None, f'Upstream {source.status_key}={actual!r}, expected {expected!r}'

    items = _dig(body, cstr(source.payload_root or '').strip())
    if items is None and not source.payload_root:
        items = body

    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return [], None, f'Payload root {source.payload_root!r} did not resolve to a list'

    next_cursor = None
    if source.pagination_mode == 'Cursor' and source.next_cursor_key:
        next_cursor = cstr(_dig(body, cstr(source.next_cursor_key).strip()) or '') or None

    return items, next_cursor, None


def fetch_source(source) -> tuple:
    """Fetch every page of a source. Returns ``(items, error, truncated)``.

    Pagination stops on: an empty page, a missing next cursor, or the configured page
    limit. Hitting the limit sets ``truncated`` -- reported rather than passing
    silently, because a quietly half-imported feed looks like a complete one.
    """
    all_items = []
    cursor = None
    truncated = False
    limit = source.page_limit

    for page_index in range(limit):
        context = source.page_context(page_index, cursor)
        items, next_cursor, error = fetch_page(source, context)

        if error:
            # Report a mid-pagination failure rather than silently keeping a partial
            # feed: the caller cannot tell a short page from a broken one.
            return all_items, error, truncated

        all_items.extend(items)

        if not source.pagination_mode or source.pagination_mode == 'None':
            break
        if not items:
            break
        if source.pagination_mode == 'Cursor':
            if not next_cursor:
                break
            cursor = next_cursor
        elif len(items) < (cint(source.page_size) or 100):
            # A short page means the last page, for page/offset styles.
            break

        if page_index == limit - 1:
            truncated = True

    return all_items, None, truncated


def import_source(source_name: str) -> dict:
    """Import every invoice available from one configured source."""
    settings = get_settings()
    source = settings.get_source(source_name)
    if not source:
        frappe.throw(_('No ZATCA API Source named {0}.').format(source_name))
    if not cint(source.enabled):
        return {'source': source_name, 'skipped': True, 'reason': 'Source is disabled.'}

    items, error, truncated = fetch_source(source)
    if error:
        _log(source_name, 'Failed', error=error)
        frappe.log_error(
            title=f'ZATCA API pull failed: {source_name}'[:140],
            message=error,
        )
        return {'source': source_name, 'fetched': 0, 'error': error}

    is_return = source.document_type == 'Credit Note'
    results = {
        'source': source_name,
        'fetched': len(items),
        'imported': 0,
        'skipped': 0,
        'failed': 0,
        'truncated': truncated,
        'details': [],
    }

    if truncated:
        # Never let a coverage limit look like full coverage.
        message = (
            f'Stopped at the {source.page_limit}-page limit for source {source_name}; '
            f'more pages may remain unimported. Raise Max Pages or narrow the date window.'
        )
        results['warning'] = message
        frappe.log_error(title=f'ZATCA API pull truncated: {source_name}'[:140], message=message)

    for raw in items:
        external_id = None
        try:
            raw = as_dict(raw)
            payload = normalise_invoice(raw, is_return=is_return)
            payload['source_name'] = source_name

            if source.external_id_key:
                override = _dig(raw, source.external_id_key)
                if override:
                    payload['external_id'] = cstr(override).strip()

            external_id = payload.get('external_id')

            result, warnings = invoice_service.build_invoice(payload, settings)

            if result.action == 'duplicate':
                results['skipped'] += 1
            else:
                results['imported'] += 1
                if cint(source.auto_submit) and result.docstatus == 0:
                    invoice_service.submit_invoice(result.name)

            frappe.db.commit()

            results['details'].append(
                {
                    'external_id': external_id,
                    'invoice': result.name,
                    'action': result.action,
                    'warnings': warnings,
                }
            )
            _log(source_name, 'Success', external_id=external_id, reference_name=result.name)

        except PayloadError as exc:
            frappe.db.rollback()
            results['failed'] += 1
            results['details'].append({'external_id': external_id, 'error': exc.message, 'code': exc.code})
            _log(source_name, 'Failed', external_id=external_id, error=exc.message)

        except Exception:
            # Roll back first: continuing to the next item on a dirty transaction
            # would attribute this item's partial writes to the next invoice.
            frappe.db.rollback()
            results['failed'] += 1
            traceback = frappe.get_traceback()
            results['details'].append({'external_id': external_id, 'error': 'Unhandled error, see Error Log'})
            frappe.log_error(
                title=f'ZATCA API import error: {source_name} / {cstr(external_id)[:40]}'[:140],
                message=traceback,
            )
            _log(source_name, 'Failed', external_id=external_id, error=traceback)

    _advance_watermark(source_name, results)
    return results


def _advance_watermark(source_name: str, results: dict) -> None:
    """Move ``last_pulled_at`` forward, but only after a fully clean pull.

    Advancing it after a partial failure would skip past the documents that failed,
    so they would never be retried. Leaving it put means the next poll re-reads the
    same window, and dedup makes the successful ones no-ops.
    """
    if results.get('failed') or results.get('error') or results.get('truncated'):
        return

    settings = frappe.get_doc('ZATCA API Settings')
    row = settings.get_source(source_name)
    if not row or row.incremental_mode != 'Date Window':
        return

    # Child-row field on a non-submittable Single: set it directly, no full save.
    frappe.db.set_value('ZATCA API Source', row.name, 'last_pulled_at', now(), update_modified=False)
    frappe.clear_cache(doctype='ZATCA API Settings')


def _log(source_name: str, status: str, external_id=None, reference_name=None, error=None) -> None:
    settings = get_settings()
    if not cint(settings.log_requests):
        return

    try:
        frappe.get_doc(
            {
                'doctype': 'ZATCA API Request Log',
                'endpoint': f'pull:{source_name}',
                'direction': 'Outbound',
                'status': status,
                'source_name': source_name,
                'external_id': external_id,
                'reference_doctype': 'Sales Invoice' if reference_name else None,
                'reference_name': reference_name,
                'error': (error or '')[:100000] or None,
                'user': frappe.session.user,
            }
        ).insert(ignore_permissions=True)
    except Exception:
        # A logging failure must never take down the import it is logging.
        frappe.log_error(title='ZATCA API: log write failed', message=frappe.get_traceback())


def pull_all_sources() -> dict:
    """Scheduler entry point. Registered under ``scheduler_events.cron``."""
    settings = get_settings()
    if not cint(settings.enabled) or not cint(settings.pull_enabled):
        return {'skipped': True, 'reason': 'Pull disabled.'}

    summary = {'sources': []}
    for source in settings.sources:
        if not cint(source.enabled):
            continue
        try:
            summary['sources'].append(import_source(source.source_name))
        except Exception:
            frappe.db.rollback()
            frappe.log_error(
                title=f'ZATCA API pull crashed: {source.source_name}'[:140],
                message=frappe.get_traceback(),
            )
            summary['sources'].append({'source': source.source_name, 'error': 'Crashed, see Error Log'})

    return summary
