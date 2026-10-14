#!/usr/bin/env python3
"""Heide-Park API monitor; posts only changed API data to a Discord webhook."""
import datetime as dt
import hashlib
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request
import uuid

API = 'https://ecomm.api.meg-eu.accessoticketing.com/api/request/getmerchantpackageeventdates'
STATE = pathlib.Path('state.json')
HEADERS = {
    'Origin': 'https://me-hpgermany.tickets.heide-park.de',
    'Content-Type': 'application/json',
    'com-accessopassport-app-id': '1500',
    'com-accessopassport-language': 'de-de',
    'com-accessopassport-merchant-id': '6500',
    'com-accessopassport-client': 'accesso26',
}


def request_json(url, payload, headers):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=25) as response:
        return json.load(response)


def normalize(value):
    # Exclude common volatile metadata. This is not a validated capacity parser.
    ignored = {'request_token', 'timestamp', 'request_time', 'response_time', 'server_time', 'session_id'}
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in sorted(value.items()) if k.lower() not in ignored}
    if isinstance(value, list):
        return [normalize(x) for x in value]
    return value


def main():
    webhook = os.environ.get('DISCORD_WEBHOOK_URL', '')
    if not webhook.startswith('https://discord.com/api/webhooks/') and not webhook.startswith('https://discordapp.com/api/webhooks/'):
        raise RuntimeError('DISCORD_WEBHOOK_URL fehlt oder ist ungültig')
    from zoneinfo import ZoneInfo
    day = dt.datetime.now(ZoneInfo('Europe/Berlin')).date().isoformat()
    payload = {
        'request_type': 'GetMerchantPackageEventDates', 'application_id': '1500',
        'merchant_id': '6500', 'machine_id': '500', 'agent_id': '5', 'user_id': '5',
        'request_token': str(uuid.uuid4()),
        'P': [{'id': '84326', 'event_id': '2579', 'CT': [{'id': '1283', 'qty': '1'}]}],
        'extra_movie': 'SnapWizardDE', 'identify_customer_types': '1',
        'min_capacity': '1', 'version': '2', 'display_zero_capacity': '1',
        'start_date': day, 'end_date': day,
    }
    data = request_json(API, payload, HEADERS)
    # Fail safe on common error shapes rather than treating errors as availability changes.
    if isinstance(data, dict) and (data.get('error') or data.get('errors')):
        raise RuntimeError('API meldet einen Fehler: ' + str(data.get('error') or data.get('errors'))[:200])
    digest = hashlib.sha256(json.dumps(normalize(data), sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    previous = json.loads(STATE.read_text()) if STATE.exists() else {}
    changed = previous.get('digest') != digest
    if changed and previous.get('digest'):
        message = ('🎢 **Heide-Park API-Änderung erkannt**\n'
                   f'Datum: **{day}**\n'
                   'Die API-Antwort hat sich seit der letzten erfolgreichen Prüfung verändert. '
                   '**Das ist keine bestätigte neue Ticketverfügbarkeit.**')
        request_json(webhook, {'content': message, 'allowed_mentions': {'parse': []}}, {'Content-Type': 'application/json', 'User-Agent': 'HeideParkMonitor/1.0'})
        print('Änderung an Discord gesendet')
    else:
        print('Erstprüfung (Baseline)' if not previous else 'Keine Änderung')
    # Persist state only when changed. Github Actions commits this file back to repo.
    if changed:
        STATE.write_text(json.dumps({'date': day, 'digest': digest}, indent=2) + '\n')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Fehler: {exc}', file=sys.stderr)
        sys.exit(1)
