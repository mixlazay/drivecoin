#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web_server.py — public web server for drivecoinproject.online (+ scan subdomain)
================================================================================
Serves by Host header:
    drivecoinproject.online / www  -> ./web        (landing page + /download/)
    scan.drivecoinproject.online   -> ./web/scan   (block explorer)

Proxies a READ-ONLY subset of the node API (127.0.0.1:8000):
    GET /api/info              chain summary
    GET /api/chain             block summaries (trimmed to the last 100)
    GET /api/mempool           pending transactions
    GET /api/block/<height>    COMPACT block detail (proofs stripped)
    GET /api/tx/<txid>         COMPACT transaction detail (proofs stripped)
    GET /api/scan              compact wallet-scan view (for the public miner)

PLUS the PUBLIC MINING endpoints (mining is public by design, like every
real blockchain — anyone may grind PoW and collect the subsidy):
    POST /api/template      {"address": "<payout address>"}       (rate-limited)
    POST /api/submitblock   {"template_id": "...", "nonce": N}    (PoW-validated)

Still private on 127.0.0.1 only: transaction submission (spending),
Lightning relay, wallet management — those need MCP or an SSH tunnel.
Query strings are stripped before proxying; request bodies are capped;
template requests are token-bucketed per client IP.

TLS: HTTPS on :443 with the Let's Encrypt certificate in ./tls/ (DNS-01);
     port 80 redirects to HTTPS — matches Cloudflare SSL mode "Full".

Deployment: systemd unit drivecoin-web.service
================================================================================
"""
import argparse
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NODE_URL = 'http://127.0.0.1:8000'
ALLOWED_API = ('/api/info', '/api/chain', '/api/mempool', '/api/scan', '/api/outputs')
MAX_BLOCKS = 100                      # /api/chain responses are trimmed to this
MAX_BODY = 4096                       # small public POST bodies (JSON options)
MAX_BODY_BLOCK = 2 << 20              # /api/p2p/block — full block w/ proofs
MAX_BODY_TX = 1 << 20                 # /api/p2p/tx — full transaction w/ proofs
TEMPLATE_RATE = (200, 3.0)            # (burst, refill/sec) per IP for /api/template
P2P_BLOCK_RATE = (30, 0.5)            # blocks are rare; PoW is checked first
P2P_TX_RATE = (120, 2.0)

CONTENT_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.ico': 'image/x-icon',
    '.json': 'application/json',
    '.txt': 'text/plain; charset=utf-8',
    '.zip': 'application/zip',
    '.woff2': 'font/woff2',
}

WEB_ROOT = '.'
TLS_DIR = '.'


class TokenBucket:
    """Tiny thread-safe token bucket (per-IP rate limiting)."""

    def __init__(self, burst: int, refill: float):
        self.burst = float(burst)
        self.refill = refill          # tokens per second
        self.lock = threading.Lock()
        self.state = {}               # ip -> [tokens, last_ts]

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            if key not in self.state:
                self.state[key] = [self.burst, now]
                if len(self.state) > 10000:      # keep the map bounded
                    self.state.clear()
                    self.state[key] = [self.burst, now]
                return True
            st = self.state[key]
            st[0] = min(self.burst, st[0] + (now - st[1]) * self.refill)
            st[1] = now
            if st[0] >= 1.0:
                st[0] -= 1.0
                return True
            return False


TEMPLATE_BUCKET = TokenBucket(*TEMPLATE_RATE)
P2P_BLOCK_BUCKET = TokenBucket(*P2P_BLOCK_RATE)
P2P_TX_BUCKET = TokenBucket(*P2P_TX_RATE)


def client_ip(handler) -> str:
    """Best-effort client IP (Cloudflare passes the original in CF-Connecting-IP)."""
    return (handler.headers.get('CF-Connecting-IP')
            or handler.headers.get('X-Forwarded-For', '').split(',')[0].strip()
            or handler.client_address[0])


def web_dir_for_host(host: str) -> str:
    host = (host or '').lower().split(':')[0]
    if host.startswith('scan.'):
        return os.path.join(WEB_ROOT, 'scan')
    return WEB_ROOT


def compact_tx(txid: str, t: dict) -> dict:
    """Explorer view of a transaction: keep structure, DROP the heavy
    cryptographic material (range proofs, ring signatures, encrypted
    payloads) — those are verifiable on-chain, the explorer shows shape."""
    return {
        'txid': txid,
        'coinbase': bool(t.get('is_coinbase')),
        'fee': t.get('fee', 0),
        'coinbase_amount': t.get('coinbase_amount', 0),
        'channel': t.get('channel'),
        'inputs': [{
            'ring_size': len(i.get('ring', [])),
            'key_image': i.get('key_image'),
            'pseudo_commitment': i.get('pseudo_commitment'),
        } for i in t.get('inputs', [])],
        'outputs': [{
            'dest': o.get('dest'),
            'commitment': o.get('commitment'),
            'range_proof': bool(o.get('range_proof')),
            'encrypted_amount': bool(o.get('payload')),
        } for o in t.get('outputs', [])],
    }


def compact_block(b: dict, txids: list) -> dict:
    blk = b.get('block', b)
    txs = blk.get('transactions', [])
    return {
        'height': blk.get('height'),
        'prev_hash': blk.get('prev_hash'),
        'merkle': blk.get('merkle'),
        'timestamp': blk.get('timestamp'),
        'difficulty': blk.get('difficulty'),
        'nonce': blk.get('nonce'),
        'txs': [compact_tx(txids[j] if j < len(txids) else '?', t)
                for j, t in enumerate(txs)],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = 'DriveCoinWeb/1.0'
    protocol_version = 'HTTP/1.1'

    def _send(self, code, ctype, body, cache):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', cache)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    # ---- public mining (POST) ------------------------------------------
    def do_POST(self):
        path = self.path.split('?')[0]
        if path == '/api/template':
            self._public_template()
        elif path == '/api/submitblock':
            self._public_submitblock()
        elif path == '/api/tx':
            # wallet broadcasts a signed confidential tx (rate-limited like p2p).
            # The wallet sends the tx JSON directly (not wrapped), so we wrap
            # it into the node's /api/p2p/tx format here.
            self._wallet_tx()
        elif path == '/api/p2p/block':
            self._p2p_block()
        elif path == '/api/p2p/tx':
            self._p2p_tx()
        else:
            self._send(404, 'application/json',
                       b'{"error": "unknown endpoint"}', 'no-cache')

    def _read_json(self, max_len=MAX_BODY):
        """Read a JSON body ({} if empty). Returns (data, error)."""
        try:
            length = int(self.headers.get('Content-Length', 0))
        except ValueError:
            return None, 'bad Content-Length'
        if length < 0 or length > max_len:
            return None, 'body too large'
        raw = self.rfile.read(length) if length else b'{}'
        try:
            data = json.loads(raw.decode())
        except Exception:                                  # noqa: BLE001
            return None, 'body must be JSON'
        return data, None

    # ---- public P2P seed endpoints (peer sync) --------------------------
    def _p2p_blocks(self, rest):
        """GET /api/p2p/blocks/<from>/<count> -> node /api/blocks/..."""
        parts = rest.split('/')
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            self._send(400, 'application/json',
                       b'{"error": "expected /api/p2p/blocks/<from>/<count>"}',
                       'no-cache')
            return
        start, count = int(parts[0]), int(parts[1])
        if not (0 <= start < 10 ** 9) or not (1 <= count <= 200):
            self._send(400, 'application/json',
                       b'{"error": "count must be 1..200"}', 'no-cache')
            return
        self._proxy(f'/api/blocks/{start}/{count}')

    def _p2p_block(self):
        """A peer pushes a solved block (rate-limited; the node checks PoW
        BEFORE any expensive transaction validation)."""
        if not P2P_BLOCK_BUCKET.allow(client_ip(self)):
            self._send(429, 'application/json',
                       b'{"error": "too many blocks, slow down"}', 'no-cache')
            return
        data, err = self._read_json(MAX_BODY_BLOCK)
        if err:
            self._send(400, 'application/json',
                       json.dumps({'error': err}).encode(), 'no-cache')
            return
        if not isinstance(data.get('block'), dict):
            self._send(400, 'application/json',
                       b'{"error": "expected {\'block\': {...}}"}', 'no-cache')
            return
        self._node_post('/api/p2p/block', {'block': data['block']})

    def _p2p_tx(self):
        """A peer relays a mempool transaction (rate-limited)."""
        if not P2P_TX_BUCKET.allow(client_ip(self)):
            self._send(429, 'application/json',
                       b'{"error": "too many transactions, slow down"}',
                       'no-cache')
            return
        data, err = self._read_json(MAX_BODY_TX)
        if err:
            self._send(400, 'application/json',
                       json.dumps({'error': err}).encode(), 'no-cache')
            return
        if not isinstance(data.get('tx'), dict):
            self._send(400, 'application/json',
                       b'{"error": "expected {\'tx\': {...}}"}', 'no-cache')
            return
        self._node_post('/api/p2p/tx', {'tx': data['tx']})

    def _wallet_tx(self):
        """Wallet tx broadcast: the wallet POSTs the tx JSON directly (same
        shape the node's /api/tx expects). Rate-limited, then forwarded to the
        node's /api/tx (full validation happens there)."""
        if not P2P_TX_BUCKET.allow(client_ip(self)):
            self._send(429, 'application/json',
                       b'{"error": "too many transactions, slow down"}',
                       'no-cache')
            return
        data, err = self._read_json(MAX_BODY_TX)
        if err:
            self._send(400, 'application/json',
                       json.dumps({'error': err}).encode(), 'no-cache')
            return
        # sanity: a tx dict must have these keys (node validates fully anyway)
        for k in ('is_coinbase', 'fee', 'inputs', 'outputs'):
            if k not in data:
                self._send(400, 'application/json',
                           json.dumps({'error': f'missing tx field: {k}'}).encode(),
                           'no-cache')
                return
        self._node_post('/api/tx', data)

    def _public_template(self):
        if not TEMPLATE_BUCKET.allow(client_ip(self)):
            self._send(429, 'application/json',
                       b'{"error": "too many template requests, slow down"}',
                       'no-cache')
            return
        data, err = self._read_json()
        if err:
            self._send(400, 'application/json',
                       json.dumps({'error': err}).encode(), 'no-cache')
            return
        address = data.get('address')
        if not isinstance(address, str) or not (50 <= len(address) <= 200):
            self._send(400, 'application/json',
                       b'{"error": "address must be a wallet address string"}',
                       'no-cache')
            return
        self._node_post('/api/template', {'address': address})

    def _public_submitblock(self):
        data, err = self._read_json()
        if err:
            self._send(400, 'application/json',
                       json.dumps({'error': err}).encode(), 'no-cache')
            return
        tid = data.get('template_id')
        nonce = data.get('nonce')
        if not isinstance(tid, str) or len(tid) > 32 or any(
                c not in '0123456789abcdef' for c in tid):
            self._send(400, 'application/json',
                       b'{"error": "bad template_id"}', 'no-cache')
            return
        if not isinstance(nonce, int) or isinstance(nonce, bool) \
                or not (0 <= nonce < 2 ** 64):
            self._send(400, 'application/json',
                       b'{"error": "nonce must be an integer 0..2^64"}',
                       'no-cache')
            return
        self._node_post('/api/submitblock',
                        {'template_id': tid, 'nonce': nonce})

    def _node_post(self, path, body):
        """Forward a validated JSON POST to the internal node, passing the
        node's status code and JSON answer through."""
        req = urllib.request.Request(
            NODE_URL + path, data=json.dumps(body).encode(), method='POST',
            headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                self._send(r.status, 'application/json', r.read(), 'no-cache')
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode()).get('error', '')
            except Exception:                              # noqa: BLE001
                detail = 'node returned HTTP %d' % e.code
            self._send(e.code, 'application/json',
                       json.dumps({'error': detail}).encode(), 'no-cache')
        except Exception as e:                              # noqa: BLE001
            self._send(502, 'application/json',
                       json.dumps({'error': 'node unreachable: %s'
                                   % e}).encode(), 'no-cache')

    def do_GET(self):
        path = self.path.split('?')[0]

        # ---- public API (identical on both hosts) --------------------------
        if path in ALLOWED_API:
            self._proxy(path)
            return
        if path.startswith('/api/p2p/blocks/'):
            self._p2p_blocks(path[len('/api/p2p/blocks/'):])
            return
        if path.startswith('/api/blocks/'):
            # alias: peers fetch /api/blocks/<from>/<count> directly
            self._p2p_blocks(path[len('/api/blocks/'):])
            return
        if path == '/api/p2p/mempool':
            self._proxy('/api/p2p/mempool')
            return
        if path.startswith('/api/block/') or path.startswith('/api/tx/'):
            self._explorer_proxy(path)
            return

        # ---- static files by Host ------------------------------------------
        web_dir = web_dir_for_host(self.headers.get('Host'))
        if path == '/':
            path = '/index.html'
        rel = os.path.normpath(path).lstrip('/')
        if rel.startswith('..') or rel == '':
            self._not_found()
            return
        fpath = os.path.normpath(os.path.join(web_dir, rel))
        # must stay inside the web root (prevents ../ escapes across hosts)
        if not fpath.startswith(os.path.abspath(WEB_ROOT) + os.sep):
            self._not_found()
            return
        if os.path.isdir(fpath):          # directory index (e.g. /download/)
            fpath = os.path.join(fpath, 'index.html')
        if not os.path.isfile(fpath):
            self._not_found()
            return
        ext = os.path.splitext(fpath)[1].lower()
        try:
            with open(fpath, 'rb') as f:
                body = f.read()
        except OSError:
            self._not_found()
            return
        self._send(200, CONTENT_TYPES.get(ext, 'application/octet-stream'),
                   body, 'public, max-age=300')

    def _not_found(self):
        self._send(404, 'text/plain; charset=utf-8', b'not found', 'no-cache')

    # ---- node proxying ------------------------------------------------------
    def _node_get(self, path):
        with urllib.request.urlopen(NODE_URL + path, timeout=15) as r:
            return r.status, r.read()

    def _proxy(self, path):
        """GET the internal node (query strings are dropped on purpose)."""
        try:
            code, body = self._node_get(path)
        except urllib.error.HTTPError as e:
            body = json.dumps({'error': 'node returned HTTP %d' % e.code}).encode()
            self._send(502, 'application/json', body, 'no-cache')
            return
        except Exception as e:                               # noqa: BLE001
            body = json.dumps({'error': 'node unreachable: %s' % e}).encode()
            self._send(502, 'application/json', body, 'no-cache')
            return
        if path == '/api/chain':       # trim: only the tail is shown publicly
            try:
                data = json.loads(body)
                data['blocks'] = data.get('blocks', [])[-MAX_BLOCKS:]
                body = json.dumps(data, separators=(',', ':')).encode()
            except Exception:                               # noqa: BLE001
                pass
        self._send(code, 'application/json', body, 'no-cache')

    def _explorer_proxy(self, path):
        """Compact block/tx views for the explorer (proofs stripped)."""
        arg = path.rsplit('/', 1)[-1]
        if path.startswith('/api/block/'):
            if not arg.isdigit():
                self._send(400, 'application/json',
                           b'{"error": "height must be an integer"}', 'no-cache')
                return
            node_path = '/api/block/' + str(int(arg))
        else:
            if len(arg) != 64 or any(c not in '0123456789abcdef' for c in arg):
                self._send(400, 'application/json',
                           b'{"error": "txid must be 64 hex characters"}',
                           'no-cache')
                return
            node_path = '/api/tx/' + arg
        try:
            code, body = self._node_get(node_path)
        except urllib.error.HTTPError as e:
            # pass the node's own status through (404 stays 404, etc.)
            try:
                detail = json.loads(e.read().decode()).get('error', '')
            except Exception:                               # noqa: BLE001
                detail = 'node returned HTTP %d' % e.code
            self._send(e.code, 'application/json',
                       json.dumps({'error': detail}).encode(), 'no-cache')
            return
        except Exception as e:                               # noqa: BLE001
            self._send(502, 'application/json',
                       json.dumps({'error': 'node unreachable: %s'
                                   % e}).encode(), 'no-cache')
            return
        try:
            data = json.loads(body)
            if 'block' in data:
                out = compact_block(data, data.get('txids', []))
                # hash is needed by P2P peers (fork walk-back) and the explorer UI
                out['hash'] = data.get('hash')
                out['height'] = data.get('height', out.get('height'))
            else:
                out = {'height': data.get('height'), 'txid': data.get('txid'),
                       'tx': compact_tx(data.get('txid', arg),
                                        data.get('tx', {}))}
            body = json.dumps(out, separators=(',', ':')).encode()
            self._send(200, 'application/json', body, 'no-cache')
        except Exception as e:                               # noqa: BLE001
            self._send(500, 'application/json',
                       json.dumps({'error': 'compact transform failed: %s'
                                   % e}).encode(), 'no-cache')

    def log_message(self, fmt, *args):
        pass                      # quiet: keep the journal clean under traffic


class RedirectHandler(BaseHTTPRequestHandler):
    """Port 80: redirect everything to HTTPS (for direct visitors;
    Cloudflare talks to :443 in Full mode)."""
    protocol_version = 'HTTP/1.1'

    def _redirect(self):
        host = (self.headers.get('Host') or 'drivecoinproject.online'
                ).split(':')[0]
        self.send_response(301)
        self.send_header('Location', f'https://{host}{self.path}')
        self.send_header('Content-Length', '0')
        self.end_headers()

    do_GET = do_HEAD = _redirect

    def log_message(self, fmt, *args):
        pass


def main():
    global WEB_ROOT, NODE_URL, TLS_DIR
    ap = argparse.ArgumentParser(description='DriveCoin web server '
                                              '(landing + scan explorer)')
    ap.add_argument('--bind', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=443)
    ap.add_argument('--http-port', type=int, default=80,
                    help='redirect port (0 disables the HTTP listener)')
    ap.add_argument('--web', default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'web'))
    ap.add_argument('--tls', default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'tls'))
    ap.add_argument('--node', default=NODE_URL)
    args = ap.parse_args()
    WEB_ROOT = os.path.abspath(args.web)
    TLS_DIR = os.path.abspath(args.tls)
    NODE_URL = args.node

    https = ThreadingHTTPServer((args.bind, args.port), Handler)
    https.daemon_threads = True
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(os.path.join(TLS_DIR, 'fullchain.pem'),
                        os.path.join(TLS_DIR, 'privkey.pem'))
    https.socket = ctx.wrap_socket(https.socket, server_side=True)
    print('HTTPS serving %s (+ scan/) on https://%s:%d (read-only API proxy '
          '-> %s)' % (WEB_ROOT, args.bind, args.port, NODE_URL), flush=True)

    if args.http_port:
        http = ThreadingHTTPServer((args.bind, args.http_port), RedirectHandler)
        http.daemon_threads = True
        threading.Thread(target=http.serve_forever, daemon=True).start()
        print('HTTP  redirect on http://%s:%d -> https' % (args.bind,
                                                           args.http_port),
              flush=True)

    try:
        https.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
