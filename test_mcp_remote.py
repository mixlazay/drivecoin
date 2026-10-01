#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_mcp_remote.py — verify both MCP servers over real SSH (exactly how
OpenCode/Claude will launch them):
  1. drivecoin-fs      -> node  .../server-filesystem/dist/index.js /drivecoinproject
  2. drivecoin-chain   -> python3 /drivecoinproject/chain_mcp.py
Drives the full MCP handshake, lists tools, and exercises calls.
"""
import json
import subprocess
import sys

HOST = 'dritestudio@82.26.104.210'
FS_CMD = ['ssh', HOST, 'node',
          '/drivecoinproject/mcp/filesystem/node_modules/'
          '@modelcontextprotocol/server-filesystem/dist/index.js',
          '/drivecoinproject']
CHAIN_CMD = ['ssh', HOST, 'python3', '/drivecoinproject/chain_mcp.py']

passed = 0


class McpClient:
    def __init__(self, cmd):
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL,
                                     text=True, bufsize=1)
        self._id = 0

    def request(self, method, params=None):
        self._id += 1
        rid = self._id
        self.proc.stdin.write(json.dumps(
            {'jsonrpc': '2.0', 'id': rid, 'method': method,
             'params': params or {}}) + '\n')
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError('server exited')
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if msg.get('id') == rid:
                return msg

    def notify(self, method):
        self.proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': method}) + '\n')
        self.proc.stdin.flush()

    def call(self, name, args=None):
        return self.request('tools/call', {'name': name, 'arguments': args or {}})

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def text(resp):
    r = resp.get('result', {})
    t = ''.join(c.get('text', '') for c in r.get('content', []))
    if r.get('isError') or 'error' in resp:
        raise AssertionError(f'tool error: {t}')
    return t


def main():
    global passed

    # ---- 1. filesystem MCP over SSH -------------------------------------------
    fs = McpClient(FS_CMD)
    try:
        init = fs.request('initialize', {'protocolVersion': '2024-11-05',
                                         'capabilities': {},
                                         'clientInfo': {'name': 't', 'version': '0'}})
        assert 'serverInfo' in init['result'], init
        fs.notify('notifications/initialized')
        tools = fs.request('tools/list')['result']['tools']
        names = {t['name'] for t in tools}
        assert 'read_file' in names and 'write_file' in names and 'list_directory' in names, names
        print(f'[OK] drivecoin-fs over SSH: {len(tools)} filesystem tools '
              f'({", ".join(sorted(names)[:5])}...)')
        passed += 1

        r = text(fs.call('list_directory', {'path': '/drivecoinproject'}))
        assert 'core.py' in r and 'chain_mcp.py' in r and 'mcp' in r
        print('[OK] list_directory /drivecoinproject sees the project files')
        passed += 1

        r = text(fs.call('read_file', {'path': '/drivecoinproject/drivecoin-node.service'}))
        assert 'node_headless.py' in r
        print('[OK] read_file works through the MCP filesystem server')
        passed += 1
    finally:
        fs.close()

    # ---- 2. chain MCP over SSH -------------------------------------------------
    ch = McpClient(CHAIN_CMD)
    try:
        init = ch.request('initialize', {'protocolVersion': '2024-11-05',
                                         'capabilities': {},
                                         'clientInfo': {'name': 't', 'version': '0'}})
        si = init['result']['serverInfo']
        assert si['name'] == 'drivecoin-chain'
        ch.notify('notifications/initialized')
        tools = ch.request('tools/list')['result']['tools']
        print(f'[OK] drivecoin-chain over SSH: {si["name"]} v{si["version"]}, '
              f'{len(tools)} tools')
        passed += 1

        r = text(ch.call('get_chain_info'))
        assert 'height' in r and 'emission' in r and '50000000' in r
        print(f'[OK] get_chain_info (against the systemd node):\n      '
              + r.replace('\n', '\n      '))
        passed += 1

        # full live round: wallet -> mine -> balance (via SSH MCP)
        text(ch.call('create_wallet', {'name': 'founder'}))
        r = text(ch.call('mine_blocks', {'count': 1, 'payout_wallet': 'founder'}))
        assert 'mined 1 block(s)' in r and '120 COIN' in r
        print('[OK] live through MCP over SSH: wallet created, block mined '
              '(reward 120 COIN)')
        passed += 1

        r = text(ch.call('list_wallets'))
        assert 'founder' in r and '120' in r
        print(f'[OK] list_wallets: {r.splitlines()[0]}')
        passed += 1
    finally:
        ch.close()

    print(f'\nALL {passed}/{passed} REMOTE MCP CHECKS PASSED '
          '(over SSH, exactly as OpenCode will launch them)')


if __name__ == '__main__':
    main()
