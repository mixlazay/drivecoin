#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
node_headless.py — PrivateChain REST API node WITHOUT the Tkinter UI.
================================================================================
Runs the same NodeService as node_app.py (same chain file format, same API,
same Lightning relay) for servers/containers with no display.

    python3 node_headless.py [--bind 127.0.0.1] [--port 8000]
                             [--datafile node_chain.json]

Recommended deployment: systemd unit (see drivecoin-node.service) so the node
restarts automatically. The API deliberately defaults to 127.0.0.1 — expose it
only through an SSH tunnel or a reverse proxy with authentication.

Explorer endpoints (read-only, used by scan.drivecoinproject.online):
    GET /api/block/<height>   full block JSON + per-tx txids
    GET /api/tx/<txid>        transaction JSON + containing block height
================================================================================
"""
import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import core

SERVICE: core.NodeService = None          # set in main(); Handler reads this


class Handler(BaseHTTPRequestHandler):
    """REST API (identical to node_app.Handler — kept in sync)."""

    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get('Content-Length', 0))
        if length > (8 << 20):
            raise ValueError('body too large')
        return json.loads(self.rfile.read(length).decode()) if length else {}

    # -- explorer / P2P helpers ----------------------------------------------
    def _explorer_block(self, arg: str) -> None:
        try:
            h = int(arg)
        except ValueError:
            self._send(400, {'error': 'height must be an integer'})
            return
        try:
            self._send(200, SERVICE.block_detail(h))
        except core.NodeError:
            self._send(404, {'error': f'block {h} not found'})

    def _explorer_tx(self, arg: str) -> None:
        try:
            self._send(200, SERVICE.tx_detail(arg))
        except core.NodeError as e:
            self._send(404 if 'not found' in str(e) else 400, {'error': str(e)})

    def do_GET(self):
        path = self.path.split('?')[0]
        try:
            if path == '/api/info':
                self._send(200, SERVICE.info())
            elif path == '/api/chain':
                full = 'full=1' in self.path
                self._send(200, SERVICE.full_chain() if full
                           else {'blocks': SERVICE.blocks_summary()})
            elif path == '/api/outputs':
                self._send(200, SERVICE.outputs_list())
            elif path == '/api/scan':
                # compact wallet-scan view (no proofs) -- used by public miners
                self._send(200, SERVICE.scan_view())
            elif path == '/api/mempool':
                full = 'full=1' in self.path
                self._send(200, SERVICE.mempool_full() if full
                           else SERVICE.mempool_summary())
            elif path == '/api/inbox':
                self._send(200, SERVICE.inbox_pop(
                    self.path.split('address=')[-1].split('&')[0]))
            elif path == '/api/ln/state':
                self._send(200, SERVICE.ln_state(
                    self.path.split('ch=')[-1].split('&')[0]))
            elif path == '/api/ln/close-status':
                self._send(200, SERVICE.ln_close_status(
                    self.path.split('ch=')[-1].split('&')[0]))
            elif path.startswith('/api/block/'):
                self._explorer_block(path.rsplit('/', 1)[-1])
            elif path.startswith('/api/tx/'):
                self._explorer_tx(path.rsplit('/', 1)[-1])
            elif path.startswith('/api/blocks/'):
                parts = path[len('/api/blocks/'):].split('/')
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    self._send(200, SERVICE.blocks_range(int(parts[0]),
                                                         int(parts[1])))
                else:
                    self._send(400, {'error': 'expected /api/blocks/<from>/<count>'})
            elif path == '/api/p2p/mempool':
                self._send(200, SERVICE.mempool_full())
            elif path == '/api/peers':
                self._send(200, SERVICE.peers_status())
            else:
                self._send(404, {'error': 'unknown endpoint'})
        except core.NodeError as e:
            self._send(400, {'error': str(e)})
        except Exception as e:                       # noqa: BLE001
            self._send(500, {'error': f'internal error: {e}'})

    def do_POST(self):
        path = self.path.split('?')[0]
        try:
            body = self._body()
            if path == '/api/tx':
                self._send(200, SERVICE.submit_tx(body))
            elif path == '/api/p2p/tx':
                self._send(200, SERVICE.receive_peer_tx(body.get('tx', {})))
            elif path == '/api/p2p/block':
                self._send(200, SERVICE.accept_external_block(
                    body.get('block', {})))
            elif path == '/api/peers':
                self._send(200, SERVICE.add_peer(body.get('url', '')))
            elif path == '/api/template':
                self._send(200, SERVICE.block_template(body.get('address', '')))
            elif path == '/api/submitblock':
                self._send(200, SERVICE.submit_block(body.get('template_id', ''),
                                                     body.get('nonce', 0)))
            elif path == '/api/inbox/send':
                self._send(200, SERVICE.inbox_send(body.get('to', ''),
                                                   body.get('msg', {})))
            elif path == '/api/ln/pay':
                self._send(200, SERVICE.ln_pay(body))
            elif path == '/api/ln/confirm':
                self._send(200, SERVICE.ln_confirm(body))
            elif path == '/api/ln/close-req':
                self._send(200, SERVICE.ln_close_req(body))
            elif path == '/api/ln/close-sig':
                self._send(200, SERVICE.ln_close_sig(body))
            else:
                self._send(404, {'error': 'unknown endpoint'})
        except core.NodeError as e:
            self._send(400, {'error': str(e)})
        except Exception as e:                       # noqa: BLE001
            self._send(500, {'error': f'internal error: {e}'})

    def log_message(self, fmt, *args):
        # one concise line per request to stdout (journalctl picks it up)
        print(f'[{time.strftime("%H:%M:%S")}] {self.address_string()} '
              f'{fmt % args}', flush=True)


def app_dir() -> str:
    """Directory for persistent data (chain file). When frozen with
    PyInstaller onefile, __file__ points into a TEMP extraction folder that
    is DELETED on exit — so we must use the executable's own directory."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def main() -> None:
    global SERVICE
    ap = argparse.ArgumentParser(description='PrivateChain headless node')
    ap.add_argument('--bind', default='127.0.0.1',
                    help='bind address (default 127.0.0.1 — do NOT expose raw)')
    ap.add_argument('--port', type=int, default=8000)
    ap.add_argument('--peer', action='append', default=[],
                    help='P2P peer URL (repeatable), e.g. '
                         'https://scan.drivecoinproject.online — joins that '
                         'network if our chain is fresh, then stays in sync')
    ap.add_argument('--datafile', default=os.environ.get(
        'NODE_DATAFILE', os.path.join(app_dir(), 'node_chain.json')))
    args = ap.parse_args()

    SERVICE = core.NodeService(args.datafile)
    print(SERVICE.start(), flush=True)
    for peer in args.peer:
        try:
            r = SERVICE.add_peer(peer)
            if r.get('added'):
                print(f'peer added: {r}', flush=True)
        except core.NodeError as e:
            print(f'bad peer {peer}: {e}', flush=True)
    if SERVICE.peers:
        print(f'P2P: {len(SERVICE.peers)} peer(s), polling every '
              f'{core.PEER_POLL_SECS:.0f}s — syncing in the background', flush=True)
    httpd = ThreadingHTTPServer((args.bind, args.port), Handler)
    httpd.daemon_threads = True
    print(f'API listening on http://{args.bind}:{args.port} '
          f'(datafile: {args.datafile})', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with SERVICE.lock:
            SERVICE._save()
        print('node stopped, chain saved', flush=True)


if __name__ == '__main__':
    main()
