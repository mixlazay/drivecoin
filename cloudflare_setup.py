#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cloudflare_setup.py — connect drivecoinproject.online to the DriveCoin origin
================================================================================
Does everything through the Cloudflare API (token from the CF_TOKEN env var):

  1. verify the token
  2. find (or create) the zone drivecoinproject.online
  3. set SSL mode  -> flexible   (origin is HTTP :80 behind the proxy)
  4. always_use_https -> on
  5. DNS  A     drivecoinproject.online -> 82.26.104.210  (proxied, orange cloud)
  6. DNS  CNAME www                     -> drivecoinproject.online (proxied)
  7. print the nameservers you must set at your registrar + a checklist

Run:   set CF_TOKEN=...   then   python cloudflare_setup.py
The token is never written to disk — revoke it in the dashboard when done.
================================================================================
"""
import json
import os
import sys
import urllib.error
import urllib.request

API = 'https://api.cloudflare.com/client/v4'
ZONE_NAME = 'drivecoinproject.online'
ORIGIN_IP = '82.26.104.210'
TOKEN = os.environ.get('CF_TOKEN', '')

if not TOKEN:
    print('CF_TOKEN env var not set', file=sys.stderr)
    sys.exit(2)


def call(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={'Authorization': 'Bearer ' + TOKEN,
                                          'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode())
        except Exception:                                # noqa: BLE001
            err = {'errors': [{'message': 'HTTP %d' % e.code}]}
        return {'success': False, 'errors': err.get('errors', [])}


def ok(result, what):
    if result.get('success'):
        print(f'[OK] {what}')
        return True
    msgs = '; '.join(e.get('message', '?') for e in result.get('errors', []))
    print(f'[!!] {what} FAILED: {msgs}')
    return False


def main():
    # 1. verify token -----------------------------------------------------------
    v = call('GET', '/user/tokens/verify')
    if not ok(v, 'token verified'):
        sys.exit(1)

    # 2. find or create the zone -------------------------------------------------
    z = call('GET', f'/zones?name={ZONE_NAME}')
    zone = (z.get('result') or [None])[0]
    if zone is None:
        print(f'[*] zone {ZONE_NAME} not found — creating it')
        acc = call('GET', '/accounts')
        if not ok(acc, 'list accounts'):
            sys.exit(1)
        accounts = acc.get('result') or []
        if not accounts:
            print('[!!] no accounts visible to this token '
                  '(needs Zone:Edit permission)')
            sys.exit(1)
        acct = accounts[0]
        z = call('POST', '/zones', {'name': ZONE_NAME, 'type': 'full',
                                    'account': {'id': acct['id']}})
        if not ok(z, f'create zone {ZONE_NAME}'):
            sys.exit(1)
        zone = z['result']
        print(f"[*] zone created (status: {zone.get('status')})")
    else:
        print(f"[*] zone found (status: {zone.get('status')})")
    zid = zone['id']

    # 3. SSL mode flexible (origin serves plain HTTP on :80) ---------------------
    call('PATCH', f'/zones/{zid}/settings/ssl', {'value': 'flexible'})
    print('[OK] SSL mode -> flexible (visitors get HTTPS at the edge)')

    # 4. always use https ---------------------------------------------------------
    call('PATCH', f'/zones/{zid}/settings/always_use_https', {'value': 'on'})
    print('[OK] Always Use HTTPS -> on')

    # 5. A record @ -> origin (proxied) --------------------------------------------
    existing = call('GET', f'/zones/{zid}/dns_records?type=A&name={ZONE_NAME}')
    rec = (existing.get('result') or [None])[0]
    if rec is not None:
        r = call('PUT', f"/zones/{zid}/dns_records/{rec['id']}",
                 {'type': 'A', 'name': ZONE_NAME, 'content': ORIGIN_IP,
                  'proxied': True, 'ttl': 1})
        ok(r, f'A record {ZONE_NAME} -> {ORIGIN_IP} updated (proxied)')
    else:
        r = call('POST', f'/zones/{zid}/dns_records',
                 {'type': 'A', 'name': ZONE_NAME, 'content': ORIGIN_IP,
                  'proxied': True, 'ttl': 1})
        ok(r, f'A record {ZONE_NAME} -> {ORIGIN_IP} created (proxied)')

    # 6. CNAME www -> domain (proxied) ----------------------------------------------
    existing = call('GET', f'/zones/{zid}/dns_records?type=CNAME&name=www')
    rec = (existing.get('result') or [None])[0]
    body = {'type': 'CNAME', 'name': 'www', 'content': ZONE_NAME,
            'proxied': True, 'ttl': 1}
    if rec is not None:
        r = call('PUT', f"/zones/{zid}/dns_records/{rec['id']}", body)
        ok(r, 'CNAME www -> domain updated (proxied)')
    else:
        r = call('POST', f'/zones/{zid}/dns_records', body)
        ok(r, 'CNAME www -> domain created (proxied)')

    # 7. nameservers to set at the registrar ------------------------------------------
    z2 = call('GET', f'/zones/{zid}')
    nss = (z2.get('result') or {}).get('name_servers') or zone.get('name_servers')
    print()
    print('=' * 64)
    if z2.get('result', {}).get('status') == 'active':
        print(f'ZONE IS ACTIVE — https://{ZONE_NAME} should work within minutes!')
    else:
        print('LAST STEP (one-time, at your domain registrar):')
        print(f'  change the nameservers of {ZONE_NAME} to:')
        for ns in (nss or []):
            print(f'    - {ns}')
        print('  (registrar: manage domain -> nameservers -> custom/Cloudflare)')
        print('  propagation usually takes minutes, at most a few hours.')
    print('=' * 64)
    print('checklist when active:')
    print(f'  https://{ZONE_NAME}          -> landing page (HTTPS via Cloudflare)')
    print(f'  https://{ZONE_NAME}/api/info -> live chain info')
    print('  remember to REVOKE the API token in the Cloudflare dashboard.')


if __name__ == '__main__':
    main()
