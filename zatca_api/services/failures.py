# zatca_api/services/failures.py
# Copyright (c) 2026, Enfono Technologies and contributors
"""The read side of the request log: why a record did not import, and whether a pull source is healthy.

Until now an integrator whose record never appeared had one place to look - the Error Log in the
desk, which they cannot open. Pull mode made it worse: the caller never calls us, we call them, so
nobody is ever handed the error in a response. The reasons were recorded; nothing could read them.

Everything here is derived from ``ZATCA API Request Log`` and writes nothing, so it needs no
schema change and no migrate to ship.

**Scope.** A pull attempt belongs to the site, so every caller sees it. A push attempt belongs to
the user who made the call, so a caller sees only their own: one integration must not be able to
read another's rejected invoices.
"""

import re

import frappe
from frappe import _
from frappe.query_builder import DocType, Order
from frappe.query_builder.functions import Count, Max, Min
from frappe.utils import add_to_date, cint, cstr, now_datetime, strip_html

from zatca_api.services.invoice import EXTERNAL_ID_FIELD

LOG = 'ZATCA API Request Log'
PULL_PREFIX = 'pull:'

DEFAULT_HOURS = 72
MAX_HOURS = 24 * 30
MAX_LIMIT = 200
SOURCE_LOOKBACK_DAYS = 30
# How many attempts are counted when working out how long an id has been failing.
STREAK_CAP = 500
SUMMARY_LENGTH = 500

_TRACEBACK = 'Traceback (most recent call last)'


def summarise_error(text, limit: int = SUMMARY_LENGTH) -> str:
    """The one line of a stored error that a caller should read.

    The log keeps whatever its writer had: a clean message for an expected failure, a whole
    traceback for an unexpected one. A caller wants the message either way, so a traceback is cut
    down to its final line - the exception itself - with the module path in front of it removed.
    """
    text = cstr(text).strip()
    if not text:
        return ''

    if _TRACEBACK in text:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        text = re.sub(r'^[A-Za-z_][\w.]*:\s+', '', lines[-1])

    text = ' '.join(strip_html(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + '…'


def _stamp(value) -> str:
    return cstr(value)[:19] if value else ''


def _visible(log, user: str):
    """Pull attempts are the site's own business; a push attempt belongs to its caller."""
    return log.endpoint.like(PULL_PREFIX + '%') | (log.user == user)


def _entry(row: dict, streak: dict | None = None, existing: dict | None = None) -> dict:
    endpoint = cstr(row.get('endpoint'))
    pull = endpoint.startswith(PULL_PREFIX)
    streak = streak or {}
    entry = {
        'external_id': row.get('external_id'),
        'message': summarise_error(row.get('error')) or _('The attempt failed without a recorded reason.'),
        'channel': 'pull' if pull else 'api',
        'source': row.get('source_name') or (endpoint[len(PULL_PREFIX) :] if pull else None),
        'attempts': streak.get('attempts', 1),
        'first_seen': _stamp(streak.get('first_seen') or row.get('creation')),
        'last_seen': _stamp(row.get('creation')),
        'existing_invoice': None,
        'existing_document_type': None,
    }
    if existing:
        entry['existing_invoice'] = existing['name']
        entry['existing_document_type'] = (
            'credit_note' if cint(existing.get('is_return')) else 'sales_invoice'
        )
    return entry


def latest_failure(external_id: str, user: str | None = None) -> dict | None:
    """Why the newest attempt for this external id failed.

    ``None`` when the newest attempt succeeded (the id is fine, or has been fixed since) or when
    nothing was ever attempted - so a caller is never shown a stale reason for a record that has
    since gone through.
    """
    external_id = cstr(external_id).strip()
    if not external_id:
        return None

    log = DocType(LOG)
    rows = (
        frappe.qb.from_(log)
        .select(log.creation, log.endpoint, log.source_name, log.status, log.error, log.external_id)
        .where(log.external_id == external_id)
        .where(_visible(log, user or frappe.session.user))
        .orderby(log.creation, order=Order.desc)
        .limit(STREAK_CAP)
    ).run(as_dict=True)

    if not rows or rows[0]['status'] != 'Failed':
        return None

    attempts = 0
    for row in rows:
        if row['status'] != 'Failed':
            break
        attempts += 1

    return _entry(rows[0], {'attempts': attempts, 'first_seen': rows[attempts - 1]['creation']})


def _streaks(ids: list, since, user: str) -> dict:
    """``external_id -> {attempts, first_seen}`` for the current run of failures of each id."""
    if not ids:
        return {}

    log = DocType(LOG)
    last_ok = {
        row['external_id']: row['ok_at']
        for row in (
            frappe.qb.from_(log)
            .select(log.external_id, Max(log.creation).as_('ok_at'))
            .where(log.external_id.isin(ids))
            .where(log.status != 'Failed')
            .where(log.creation >= since)
            .where(_visible(log, user))
            .groupby(log.external_id)
        ).run(as_dict=True)
    }

    failed = (
        frappe.qb.from_(log)
        .select(log.external_id, log.creation)
        .where(log.external_id.isin(ids))
        .where(log.status == 'Failed')
        .where(log.creation >= since)
        .where(_visible(log, user))
    ).run(as_dict=True)

    streaks = {}
    for row in failed:
        cutoff = last_ok.get(row['external_id'])
        if cutoff is not None and row['creation'] <= cutoff:
            continue
        streak = streaks.setdefault(row['external_id'], {'attempts': 0, 'first_seen': row['creation']})
        streak['attempts'] += 1
        streak['first_seen'] = min(streak['first_seen'], row['creation'])
    return streaks


def list_failures(
    user: str,
    external_id: str | None = None,
    source: str | None = None,
    hours: int = DEFAULT_HOURS,
    limit: int = 50,
    start: int = 0,
) -> dict:
    """One entry per external id whose NEWEST attempt in the window failed, most recent first.

    An id that failed and has since gone through is not listed: this is the list of what still
    needs fixing, not a history.
    """
    hours = min(max(cint(hours) or DEFAULT_HOURS, 1), MAX_HOURS)
    limit = min(max(cint(limit) or 50, 1), MAX_LIMIT)
    start = max(cint(start), 0)
    since = add_to_date(now_datetime(), hours=-hours)

    def conditions(table):
        rules = [
            table.external_id.isnotnull(),
            table.external_id != '',
            table.creation >= since,
            _visible(table, user),
        ]
        if external_id:
            rules.append(table.external_id == external_id)
        if source:
            rules.append(table.source_name == source)
        return rules

    log = DocType(LOG)
    newest_at = frappe.qb.from_(log).select(log.external_id, Max(log.creation).as_('last_at'))
    for rule in conditions(log):
        newest_at = newest_at.where(rule)
    newest_at = newest_at.groupby(log.external_id).as_('newest_at')

    newest = DocType(LOG, alias='newest')
    failed = (
        frappe.qb.from_(newest)
        .join(newest_at)
        .on((newest.external_id == newest_at.external_id) & (newest.creation == newest_at.last_at))
        .where(newest.status == 'Failed')
    )

    total = cint(
        frappe.qb.from_(failed.select(newest.external_id).as_('failed')).select(Count('*')).run()[0][0]
    )
    rows = (
        failed.select(newest.external_id, newest.creation, newest.endpoint, newest.source_name, newest.error)
        .orderby(newest.creation, order=Order.desc)
        .limit(limit)
        .offset(start)
    ).run(as_dict=True)

    ids = [row['external_id'] for row in rows]
    streaks = _streaks(ids, since, user)

    existing = {}
    if ids:
        # get_list, not get_all: an id belonging to an invoice the caller may not read must not be
        # named to them.
        for invoice in frappe.get_list(
            'Sales Invoice',
            filters={EXTERNAL_ID_FIELD: ['in', ids]},
            fields=['name', EXTERNAL_ID_FIELD, 'is_return'],
            limit_page_length=0,
        ):
            existing[invoice[EXTERNAL_ID_FIELD]] = invoice

    return {
        'window_hours': hours,
        'count': len(rows),
        'total': total,
        'start': start,
        'limit': limit,
        'has_more': start + len(rows) < total,
        'errors': [
            _entry(row, streaks.get(row['external_id']), existing.get(row['external_id'])) for row in rows
        ],
    }


def source_health(settings) -> list:
    """For every configured pull source: is it working, and if not, why and since when.

    A source is *failing* when its newest source-level failure (a refused login, an unreachable
    host, a crash - an attempt that never got as far as a single record) is newer than its newest
    success. Failures of individual records do not count here: the source is reachable, and those
    are listed per external id instead.
    """
    since = add_to_date(now_datetime(), days=-SOURCE_LOOKBACK_DAYS)
    log = DocType(LOG)
    report = []

    for source in settings.get('sources') or []:
        name = cstr(source.source_name)
        endpoint = PULL_PREFIX + name
        source_level = log.external_id.isnull() | (log.external_id == '')

        def newest(*rules, _endpoint=endpoint):
            query = (
                frappe.qb.from_(log)
                .select(log.creation, log.error)
                .where(log.endpoint == _endpoint)
                .where(log.creation >= since)
            )
            for rule in rules:
                query = query.where(rule)
            rows = query.orderby(log.creation, order=Order.desc).limit(1).run(as_dict=True)
            return rows[0] if rows else None

        last_ok = newest(log.status != 'Failed')
        last_fail = newest(log.status == 'Failed', source_level)
        failing = bool(last_fail) and (not last_ok or last_fail['creation'] > last_ok['creation'])

        item = {
            'source': name,
            'enabled': bool(cint(source.enabled)),
            'status': 'failing' if failing else ('ok' if last_ok else 'idle'),
            'last_success_at': _stamp(last_ok['creation']) if last_ok else None,
            'last_error': None,
        }

        if failing:
            run = (
                frappe.qb.from_(log)
                .select(Count('*').as_('attempts'), Min(log.creation).as_('failing_since'))
                .where(log.endpoint == endpoint)
                .where(log.status == 'Failed')
                .where(source_level)
                .where(log.creation >= since)
            )
            if last_ok:
                run = run.where(log.creation > last_ok['creation'])
            counted = run.run(as_dict=True)[0]
            item['last_error'] = {
                'message': summarise_error(last_fail['error'])
                or _('The attempt failed without a recorded reason.'),
                'at': _stamp(last_fail['creation']),
                'failing_since': _stamp(counted['failing_since']),
                'attempts': cint(counted['attempts']),
            }

        report.append(item)

    return report
