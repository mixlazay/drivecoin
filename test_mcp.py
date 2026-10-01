#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_mcp.py — end-to-end test of the MCP system (chain_mcp.py over stdio).

Spawns a real headless node (node_headless.py) and a real chain_mcp.py
subprocess, drives the full MCP handshake, then exercises every tool the way
an AI client would: create wallets, mine, send on-chain, open/pay/close a
Lightning channel. Verifies the JSON-RPC responses.

Run:  python test_mcp.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8931
TMP = tempfile.mkdtemp(prefix='mcp_e2e_')

# tests mine a fast block target on an ephemeral chain (public consensus is
# 60s); the node is spawned as a subprocess, so use the env override
os.environ['DCC_BLOCK_TIME'] = '0.15'

passed = 0


def wait_node(url, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url + '/api/info', timeout=3) as r:
                if r.status == 200:
                    return json.loads(r.read().decode())
        except Exception:
            time.sleep(0.3)
    raise TimeoutError('node did not come up')


class McpClient:
    """Minimal MCP stdio client for testing."""

    def __init__(self, cmd, env):
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL,
                                     env=env, text=True, bufsize=1)
        self._id = 0

    def send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + '\n')
        self.proc.stdin.flush()

    def request(self, method, params=None):
        self._id += 1
        rid = self._id
        self.send({'jsonrpc': '2.0', 'id': rid, 'method': method,
                   'params': params or {}})
        return self.wait(rid)

    def notify(self, method, params=None):
        self.send({'jsonrpc': '2.0', 'method': method, 'params': params or {}})

    def wait(self, rid, timeout=180):
        t0 = time.time()
        while time.time() - t0 < timeout:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError('MCP server exited')
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if msg.get('id') == rid:
                return msg
        raise TimeoutError(f'no response for id {rid}')

    def call(self, name, args=None):
        return self.request('tools/call', {'name': name, 'arguments': args or {}})

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def text(resp):
    """Tool result text (raises with the error text if isError)."""
    r = resp.get('result', {})
    t = ''.join(c.get('text', '') for c in r.get('content', []))
    if r.get('isError') or 'error' in resp:
        raise AssertionError(f'tool error: {t}')
    return t


def main():
    global passed
    # ---- 1. headless node on a scratch datafile -----------------------------
    chain_file = os.path.join(TMP, 'chain.json')
    node = subprocess.Popen(
        [sys.executable, os.path.join(HERE, 'node_headless.py'),
         '--port', str(PORT), '--datafile', chain_file],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        info = wait_node(f'http://127.0.0.1:{PORT}')
        print(f"[OK] headless node up (height {info['height']})")
        passed += 1

        # ---- 2. MCP server subprocess ---------------------------------------
        env = dict(os.environ)
        env['CHAIN_MCP_URL'] = f'http://127.0.0.1:{PORT}'
        env['CHAIN_MCP_WALLET_FILE'] = os.path.join(TMP, 'wallets.json')
        mcp = McpClient([sys.executable, os.path.join(HERE, 'chain_mcp.py')], env)
        try:
            init = mcp.request('initialize', {
                'protocolVersion': '2024-11-05',
                'capabilities': {},
                'clientInfo': {'name': 'test', 'version': '0'}})
            si = init['result']['serverInfo']
            assert si['name'] == 'drivecoin-chain', si
            mcp.notify('notifications/initialized')
            tools = mcp.request('tools/list')['result']['tools']
            names = {t['name'] for t in tools}
            expected = {'get_chain_info', 'list_blocks', 'get_mempool',
                        'create_wallet', 'list_wallets', 'get_wallet',
                        'scan_wallets', 'mine_blocks', 'send_payment',
                        'open_channel', 'ln_pay', 'ln_close', 'get_channel'}
            assert expected <= names, expected - names
            print(f'[OK] MCP handshake: {si["name"]} v{si["version"]}, '
                  f'{len(tools)} tools listed')
            passed += 1

            # ---- 3. wallets + mining --------------------------------------
            text(mcp.call('create_wallet', {'name': 'alice'}))
            text(mcp.call('create_wallet', {'name': 'bob'}))
            print('[OK] create_wallet x2 (alice, bob)')
            passed += 1

            r = text(mcp.call('mine_blocks', {'count': 2, 'payout_wallet': 'alice'}))
            assert 'mined 2 block(s)' in r
            print(f'[OK] mine_blocks x2 -> {r.splitlines()[1]}')
            passed += 1

            r = text(mcp.call('list_wallets'))
            assert 'alice' in r and 'bob' in r and '240' in r, r
            print(f'[OK] list_wallets shows alice 240 COIN:\n      '
                  + r.replace('\n', '\n      '))
            passed += 1

            # ---- 4. on-chain payment --------------------------------------
            w = json.load(open(env['CHAIN_MCP_WALLET_FILE']))
            bob_rec = next(x for x in w['wallets'] if x['name'] == 'bob')
            import core
            bob_wallet = core.Wallet.from_json(bob_rec)
            r = text(mcp.call('send_payment', {
                'wallet': 'alice', 'recipient_address': bob_wallet.address_text,
                'amount': 5, 'fee': 0.1}))
            assert 'MEMPOOL' in r
            print('[OK] send_payment (on-chain, confidential) accepted into mempool')
            passed += 1

            text(mcp.call('mine_blocks', {'count': 1, 'payout_wallet': 'alice'}))
            r = text(mcp.call('scan_wallets'))
            assert 'bob' in r and '5' in r, r
            print('[OK] mined + scanned: bob received 5 COIN on-chain')
            passed += 1

            # ---- 5. Lightning: open -> instant pay -> close ---------------
            r = text(mcp.call('open_channel', {
                'wallet': 'alice', 'peer_address': bob_wallet.address_text,
                'capacity': 30}))
            assert 'funding tx' in r
            text(mcp.call('mine_blocks', {'count': 1, 'payout_wallet': 'alice'}))
            text(mcp.call('scan_wallets'))
            print('[OK] open_channel + mined 1 block (channel should be open)')
            passed += 1

            r = text(mcp.call('ln_pay', {'wallet': 'alice', 'amount': 5}))
            assert 'INSTANT' in r, r
            print(f'[OK] ln_pay INSTANT: {r.splitlines()[0]}')
            passed += 1

            r = text(mcp.call('ln_close', {'wallet': 'alice'}))
            assert 'settlement tx' in r or 'co-signed' in r, r
            text(mcp.call('mine_blocks', {'count': 1, 'payout_wallet': 'alice'}))
            r = text(mcp.call('scan_wallets'))
            print(f'[OK] ln_close settled on-chain\n      ' + r.replace('\n', '\n      '))
            passed += 1

            # ---- 6. error handling -----------------------------------------
            err = mcp.call('send_payment', {'wallet': 'ghost',
                                            'recipient_address': bob_wallet.address_text,
                                            'amount': 1})
            assert err['result'].get('isError'), err
            print('[OK] unknown wallet -> isError true (no crash)')
            passed += 1

            r = text(mcp.call('get_chain_info'))
            assert 'emission' in r
            print(f'[OK] get_chain_info:\n      ' + r.replace('\n', '\n      '))
            passed += 1

        finally:
            mcp.close()
    finally:
        node.terminate()
        node.wait(timeout=10)
        import shutil
        shutil.rmtree(TMP, ignore_errors=True)

    print(f'\nALL {passed}/{passed} MCP E2E CHECKS PASSED')


if __name__ == '__main__':
    main()
