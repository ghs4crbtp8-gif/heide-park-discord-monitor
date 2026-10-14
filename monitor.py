#!/usr/bin/env python3
"""Heide-Park event-date change monitor (standard library only).

The API response shape has not been validated. Only dates explicitly marked
as available are announced; unknown schemas are recorded, not misreported.
"""
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

API = 'https://ecomm.api.meg-eu.accessoticketing.com/api/request/getmerchantpackageeventdates'
STATE_PATH = Path('state.json')
DAYS_AHEAD = max(1, min(365, int(os.getenv('DAYS_AHEAD', '90'))))
CHUNK_DAYS = max(1, min(31, int(os.getenv('CHUNK_DAYS', '30'))))
HEADERS = {
    'Origin': 'https://me-hpgermany.tickets.heide-park.de',
    'Content-Type': 'application/json',
    'com-accessopassport-app-id': '1500',
    'com-accessopassport-language': 'de-de',
    'com-accessopassport-merchant-id': '6500',
    'com-accessopassport-client': 'accesso26',
}
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
# Availability is only inferred from explicit truthy availability or positive
# capacity; no generic date appearing in a response is treated as bookable.
CAPACITY_KEYS = {'capacity', 'availablecapacity', 'remainingcapacity', 'remaining', 'availability', 'availableqty', 'availablequantity', 'spacesavailable', 'quantityavailable'}
FLAG_KEYS = {'isavailable', 'bookable', 'isbookable', 'available', 'on_sale', 'onsale'}
DATE_KEYS = {'date', 'eventdate', 'event_date', 'performancedate', 'startdate', 'start_date', 'eventdatetime'}


def request_json(url, payload=None, headers=None):
    body = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = Request(url, data=body, headers=headers or {}, method='POST' if body else 'GET')
    try:
        with urlopen(req, timeout=25) as r:
            return json.load(r)
    except HTTPError as err:
        print(f'HTTP {err.code} bei {url}', file=sys.stderr)
        print(f'Server: {err.headers.get("Server", "unbekannt")}', file=sys.stderr)
        print(f'CF-Ray: {err.headers.get("CF-Ray", "nicht vorhanden")}', file=sys.stderr)
        raise


def fetch_dates(start, end):
    payload = {
        'request_type': 'GetMerchantPackageEventDates',
        'application_id': '1500', 'merchant_id': '6500',
        'machine_id': '500', 'agent_id': '5', 'user_id': '5',
        'request_token': 'probe',
        'P': [{'id': '84326', 'event_id': '2579', 'CT': [{'id': '1283', 'qty': '1'}]}],
        'extra_movie': 'SnapWizardDE', 'identify_customer_types': '1',
        'min_capacity': '1', 'version': '2', 'display_zero_capacity': '1',
        'start_date': start.isoformat(), 'end_date': end.isoformat(),
    }
    return request_json(API, payload, HEADERS)


def as_positive(value):
    if isinstance(value, bool):
        return value
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def extract_bookable_dates(data):
    """Conservatively recognize only dates alongside explicit positive availability."""
    found = set()
    observed_dates = set()

    def scan(node):
        if isinstance(node, list):
            for item in node:
                scan(item)
        elif isinstance(node, dict):
            lower = {str(k).lower(): v for k, v in node.items()}
            dates = set()
            for key in DATE_KEYS:
                value = lower.get(key)
                if isinstance(value, str) and DATE_RE.fullmatch(value[:10]):
                    dates.add(value[:10])
            observed_dates.update(dates)
            positive = any(as_positive(lower[k]) for k in CAPACITY_KEYS if k in lower)
            positive |= any((lower[k] is True or str(lower[k]).lower() in ('true', '1', 'yes')) for k in FLAG_KEYS if k in lower)
            if positive:
                found.update(dates)
            for v in node.values():
                scan(v)
    scan(data)
    return found, observed_dates


def send_discord(webhook, message):
    req = Request(webhook, data=json.dumps({'content': message, 'allowed_mentions': {'parse': []}}).encode('utf-8'), headers={'Content-Type': 'application/json', 'User-Agent': 'HeideParkAvailabilityMonitor/1.0'}, method='POST')
    with urlopen(req, timeout=20) as response:
        if response.status not in (200, 204):
            raise RuntimeError(f'Discord HTTP {response.status}')


def main():
    webhook = os.getenv('DISCORD_WEBHOOK_URL', '').strip()
    if not webhook.startswith('https://discord.com/api/webhooks/') and not webhook.startswith('https://discordapp.com/api/webhooks/'):
        print('Fehler: GitHub Secret DISCORD_WEBHOOK_URL fehlt oder ist ungültig.', file=sys.stderr)
        return 1

    today = datetime.now(ZoneInfo('Europe/Berlin')).date()
    last = today + timedelta(days=DAYS_AHEAD - 1)
    print(f'Prüfe {today} bis {last} in Teilfenstern von maximal {CHUNK_DAYS} Tagen.')
    bookable = set()
    observed = set()
    response_digests = []
    cursor = today
    try:
        while cursor <= last:
            end = min(cursor + timedelta(days=CHUNK_DAYS - 1), last)
            response = fetch_dates(cursor, end)
            dates, seen = extract_bookable_dates(response)
            bookable.update(dates)
            observed.update(seen)
            response_digests.append(hashlib.sha256(json.dumps(response, sort_keys=True, ensure_ascii=False).encode()).hexdigest())
            print(f'{cursor} – {end}: {len(dates)} ausdrücklich verfügbare Datumswerte; {len(seen)} Datumswerte insgesamt.')
            cursor = end + timedelta(days=1)
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        print(f'API-Abfrage fehlgeschlagen: {exc}', file=sys.stderr)
        print('Keine Zustandsänderung und keine Discord-Meldung.', file=sys.stderr)
        return 1

    # Unknown response schema: don't claim every shown date is bookable.
    if not observed:
        print('WARNUNG: Keine erkennbaren Datumsfelder. Antwortschema muss geprüft werden.')
    previous = json.loads(STATE_PATH.read_text(encoding='utf-8')) if STATE_PATH.exists() else None
    current = sorted(bookable)
    if previous is None:
        print(f'Erster Lauf: {len(current)} ausdrücklich verfügbare Termine gespeichert; keine Benachrichtigung (Baseline).')
    else:
        old = set(previous.get('bookable_dates', []))
        new = sorted(bookable - old)
        if new:
            msg = '🎢 **Heide-Park: Neue möglicherweise buchbare Termine**\n' + '\n'.join(f'• {day}' for day in new[:30])
            if len(new) > 30:
                msg += f'\n… und {len(new)-30} weitere.'
            msg += '\nBitte Buchbarkeit auf der offiziellen Ticketseite prüfen.'
            send_discord(webhook, msg)
            print(f'Discord-Nachricht über {len(new)} neue Datumswerte verschickt.')
        else:
            print('Keine neu erkannten buchbaren Termine. Keine Discord-Nachricht.')
    state = {'checked_at': datetime.now(ZoneInfo('Europe/Berlin')).isoformat(), 'window_start': str(today), 'window_end': str(last), 'bookable_dates': current, 'observed_dates_count': len(observed), 'response_hashes': response_digests}
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0

if __name__ == '__main__':
    sys.exit(main())
