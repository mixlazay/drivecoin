#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chain_mcp.py — MCP server for the DriveCoin / PrivateChain node.
================================================================================
A Model Context Protocol server (stdio transport, JSON-RPC 2.0, newline-
delimited) implemented with the Python STANDARD LIBRARY ONLY — matching the
project's zero-dependency philosophy. It talks to the headless node's REST API
(see node_headless.py) and manages the server-side wallets.json, so an AI
assistant can operate the whole system:

    chain queries   get_chain_info, list_blocks, get_mempool
    wallets         create_wallet, list_wallets, get_wallet, scan_wallets
    mining          mine_blocks (template -> grind -> submit, headless)
    on-chain send   send_payment (Pedersen + stealth + ring signatures)
    lightning       open_channel, ln_pay (INSTANT, no mining), ln_close

Run (the node must be running, e.g. the systemd service drivecoin-node):
    python3 chain_mcp.py                 # speaks MCP on stdin/stdout

Client configuration lives in mcp/ (opencode + Claude Desktop examples).
================================================================================
"""
import json
import os
import sys
import traceback

# All chain/wallet state lives next to this script (the project folder).
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(PROJECT_DIR)
sys.path.insert(0, PROJECT_DIR)

import core                                                # noqa: E402

WALLET_FILE = os.environ.get('CHAIN_MCP_WALLET_FILE',
                             os.path.join(PROJECT_DIR, 'wallets.json'))
NODE_URL = os.environ.get('CHAIN_MCP_URL', 'http://127.0.0.1:8000')
SERVER_NAME = 'drivecoin-chain'
SERVER_VERSION = '1.0.0'
DEFAULT_FEE_COIN = 0.1


def _log(*a):                       # diagnostics go to stderr ONLY
    print(*a, file=sys.stderr, flush=True)


# ── wallet store (same file format as wallet_app.py) ─────────────────────────
def load_wallets():
    try:
        with open(WALLET_FILE, 'r', encoding='utf-8') as fh:
            return [core.Wallet.from_json(w) for w in json.load(fh).get('wallets', [])]
    except FileNotFoundError:
        return []
    except Exception as e:                                   # noqa: BLE001
        _log('wallet load error:', e)
        return []


def save_wallets(wallets):
    tmp = WALLET_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump({'wallets': [w.to_json() for w in wallets]}, fh, indent=1)
    os.replace(tmp, WALLET_FILE)                             # atomic


def find_wallet(wallets, name):
    for w in wallets:
        if w.name == name:
            return w
    raise ValueError(f'wallet "{name}" not found '
                     f'(existing: {", ".join(w.name for w in wallets) or "none"})')


def tick_all(wallets, client, rounds: int = 4):
    """Run Lightning housekeeping for every wallet (auto-countersigns incoming
    payments / close requests between two server-side wallets).

    The channel handshake needs SEVERAL rounds (invite -> share reply ->
    open), so we loop until a round makes no further progress (the GUI apps
    get this for free from their 1.2 s worker loop)."""
    logs = []
    for _ in range(rounds):
        round_logs = []
        for w in wallets:
            try:
                round_logs.extend(w.ln_tick(client, w.address_text))
            except core.NodeError as e:
                _log(f'ln_tick({w.name}):', e)
        if not round_logs:
            break                          # mailbox drained, nothing changed
        logs.extend(round_logs)
    return logs


def scan_all(wallets, client):
    """Refresh every wallet: confirm pending, scan blocks, note unconfirmed."""
    view = client.fetch_view()
    onchain = {core.tx_txid(t) for blk in view.blocks for t in blk.transactions}
    pending = [core.tx_load(t) for t in client.mempool_full()['txs']]
    report = []
    for w in wallets:
        w.confirm_pending(onchain)
        found = w.scan(view)
        w.pending_in = w.scan_pending_incoming(pending)
        report.append({'name': w.name, 'new_outputs': found,
                       'balance': core.fmt_coin(w.balance()),
                       'unconfirmed_in': core.fmt_coin(w.pending_in),
                       'ln_balance': core.fmt_coin(w.ln_total())})
    return report


# ── tool implementations ─────────────────────────────────────────────────────
def t_get_chain_info(args):
    c = core.NodeClient(NODE_URL)
    i = c.info()
    return (f"height {i['height']}  tip {i['tip_hash'][:16]}...\n"
            f"next difficulty {i['difficulty']:,}  mempool {i['mempool']} tx(s)\n"
            f"outputs {i['outputs']}  spent key images {i['spent_images']}\n"
            f"emission {core.fmt_coin(i['emission'])} of "
            f"{core.fmt_coin(i['max_supply'])} hard cap "
            f"({i['emission'] / i['max_supply'] * 100:.4f}%)\n"
            f"next block reward {core.fmt_coin(i['next_subsidy'])} "
            f"(halving every {core.HALVING_INTERVAL:,} blocks)")


def t_list_blocks(args):
    c = core.NodeClient(NODE_URL)
    limit = min(int(args.get('limit', 10)), 100)
    blocks = c.blocks_summary()['blocks'][-limit:][::-1]     # newest first
    if not blocks:
        return 'chain has no blocks yet (genesis only or empty)'
    lines = []
    for b in blocks:
        lines.append(f"#{b['height']:<4} {b['hash'][:16]}...  txs={b['txs']:<2} "
                     f"difficulty={b['difficulty']:,}  "
                     f"reward={core.fmt_coin(b['reward'])}")
    return '\n'.join(lines)


def t_get_mempool(args):
    c = core.NodeClient(NODE_URL)
    txs = c.mempool()
    if not txs:
        return 'mempool is empty'
    return '\n'.join(
        f"{t['txid'][:16]}...  fee={core.fmt_coin(t['fee'])}  "
        f"inputs={t['inputs']} (rings {t['rings']})  outputs={t['outputs']}"
        for t in txs)


def t_create_wallet(args):
    name = str(args.get('name', '')).strip()
    if not name:
        raise ValueError('name is required')
    wallets = load_wallets()
    if any(w.name == name for w in wallets):
        raise ValueError(f'wallet "{name}" already exists')
    w = core.Wallet(name)
    wallets.append(w)
    save_wallets(wallets)
    return (f'wallet "{name}" created\naddress: {w.address_text}\n'
            f'(keys are stored server-side in wallets.json — get_wallet with '
            f'include_keys=true to export them)')


def t_list_wallets(args):
    wallets = load_wallets()
    if not wallets:
        return ('no wallets yet — create one with create_wallet '
                '(balances appear after scan_wallets)')
    c = core.NodeClient(NODE_URL)
    report = scan_all(wallets, c)
    save_wallets(wallets)
    return '\n'.join(
        f'{r["name"]:<12} on-chain {r["balance"]:>14}  '
        f'unconfirmed-in {r["unconfirmed_in"]:>12}  LN {r["ln_balance"]:>12} '
        f'({r["new_outputs"]} new)' for r in report)


def t_get_wallet(args):
    wallets = load_wallets()
    w = find_wallet(wallets, str(args.get('name', '')))
    c = core.NodeClient(NODE_URL)
    scan_all(wallets, c)
    save_wallets(wallets)
    lines = [f'wallet "{w.name}"',
             f'address: {w.address_text}',
             f'on-chain available: {core.fmt_coin(w.balance())}',
             f'unconfirmed incoming: {core.fmt_coin(w.pending_in)}',
             f'lightning balance: {core.fmt_coin(w.ln_total())}',
             f'outputs:']
    for op, rec in sorted(w.known.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        status = ('SPENT' if op in w.spent_ops else
                  'PENDING OUT' if op in w.pending_ops else 'available')
        lines.append(f'  {op[0].hex()[:16]}...#{op[1]}  '
                     f'{core.fmt_coin(rec["v"]):>14}  {status}')
    if w.channels:
        lines.append('channels:')
        for ch_id, rec in w.channels.items():
            lines.append(f'  {ch_id.hex()[:16]}...  role={rec["role"]}  '
                         f'capacity={core.fmt_coin(rec["capacity"])}  '
                         f'mine={core.fmt_coin(rec["bal_self"])}  '
                         f'peer={core.fmt_coin(rec["bal_peer"])}  '
                         f'seq={rec["seq"]}  {rec["status"]}')
    if args.get('include_keys'):
        lines.append(f'view key (a): {w.a:064x}')
        lines.append(f'spend key (b): {w.b:064x}')
        lines.append('^^ SECRET — never share these')
    return '\n'.join(lines)


def t_scan_wallets(args):
    wallets = load_wallets()
    if not wallets:
        return 'no wallets to scan'
    c = core.NodeClient(NODE_URL)
    report = scan_all(wallets, c)
    tick_all(wallets, c)
    save_wallets(wallets)
    return 'scan complete:\n' + '\n'.join(
        f'{r["name"]:<12} {r["balance"]:>14}  (+{r["unconfirmed_in"]} unconfirmed, '
        f'LN {r["ln_balance"]})' for r in report)


def t_mine_blocks(args):
    count = max(1, min(int(args.get('count', 1)), 20))
    payout = str(args.get('payout_wallet') or args.get('payout_address') or '')
    if not payout:
        raise ValueError('payout_wallet (name of a wallet here) or '
                         'payout_address is required')
    wallets = load_wallets()
    if any(w.name == payout for w in wallets):
        address = find_wallet(wallets, payout).address_text
    else:
        core.parse_address(payout)                # validate; raises on garbage
        address = payout
    c = core.NodeClient(NODE_URL)
    results = []
    for _ in range(count):
        res = c.mine_block(address)
        results.append(f"#{res['height']} hash={res['hash'][:16]}... "
                       f"reward={core.fmt_coin(res['reward'])}")
    # auto-refresh balances so the caller sees the payout immediately
    if wallets:
        scan_all(wallets, c)
        tick_all(wallets, c)          # pending channels open right here
        save_wallets(wallets)
    return f'mined {count} block(s):\n' + '\n'.join(results)


def t_send_payment(args):
    name = str(args.get('wallet', ''))
    recipient = str(args.get('recipient_address', ''))
    amount = core.parse_coin(str(args.get('amount', '0')))
    fee = core.parse_coin(str(args.get('fee', DEFAULT_FEE_COIN)))
    if fee < core.MIN_FEE:
        raise ValueError(f'fee must be >= {core.MIN_FEE} units '
                         f'({core.MIN_FEE / core.COIN} COIN)')
    wallets = load_wallets()
    w = find_wallet(wallets, name)
    c = core.NodeClient(NODE_URL)
    view = c.fetch_view()
    w.scan(view)
    recipient = core.parse_address(recipient)          # validates the address
    tx = w.build_tx(view, [(recipient, amount)], fee, consume=False)
    rings = [len(ti.ring) for ti in tx.inputs]
    res = c.submit_tx(core.tx_json(tx))
    w.register_pending(bytes.fromhex(res['txid']), tx.used_ops)
    save_wallets(wallets)
    return (f'sent {core.fmt_coin(amount)} (fee {core.fmt_coin(fee)}) '
            f'tx {res["txid"][:16]}... in MEMPOOL\n'
            f'inputs reserved (PENDING OUT) — call mine_blocks then scan_wallets '
            f'to confirm; rings used: {rings}')


def t_open_channel(args):
    name = str(args.get('wallet', ''))
    peer = str(args.get('peer_address', ''))
    capacity = core.parse_coin(str(args.get('capacity', '0')))
    fee = core.parse_coin(str(args.get('fee', DEFAULT_FEE_COIN)))
    wallets = load_wallets()
    w = find_wallet(wallets, name)
    c = core.NodeClient(NODE_URL)
    view = c.fetch_view()
    w.scan(view)
    res = w.ln_open_channel(view, c, peer, capacity, fee)
    save_wallets(wallets)
    return (f'channel funding tx {res["txid"][:16]}... submitted '
            f'(capacity {core.fmt_coin(capacity)})\n'
            f'call mine_blocks (1 block) then scan_wallets — the channel opens '
            f'and ln_pay becomes INSTANT')


def _pick_channel(w, channel_id):
    if channel_id:
        try:
            ch = bytes.fromhex(str(channel_id))
        except ValueError:
            raise ValueError('channel_id must be hex')
        if ch not in w.channels:
            raise ValueError('unknown channel for this wallet')
        return ch
    open_chs = [cid for cid, rec in w.channels.items() if rec['status'] == 'open']
    if len(open_chs) == 1:
        return open_chs[0]
    raise ValueError('specify channel_id (this wallet has '
                     f'{len(open_chs)} open channel(s))')


def t_ln_pay(args):
    name = str(args.get('wallet', ''))
    amount = core.parse_coin(str(args.get('amount', '0')))
    wallets = load_wallets()
    w = find_wallet(wallets, name)
    ch_id = _pick_channel(w, args.get('channel_id'))
    c = core.NodeClient(NODE_URL)
    w.ln_pay(c, ch_id, amount)
    # both parties are server-side wallets -> ticking completes the countersign
    tick_all(wallets, c)
    save_wallets(wallets)
    rec = w.channels[ch_id]
    return (f'LIGHTNING INSTANT: {core.fmt_coin(amount)} sent — no mining\n'
            f'channel {ch_id.hex()[:16]}... seq={rec["seq"]}  '
            f'my balance {core.fmt_coin(rec["bal_self"])}  '
            f'peer {core.fmt_coin(rec["bal_peer"])}')


def t_ln_close(args):
    name = str(args.get('wallet', ''))
    wallets = load_wallets()
    w = find_wallet(wallets, name)
    ch_id = _pick_channel(w, args.get('channel_id'))
    c = core.NodeClient(NODE_URL)
    w.ln_close_request(c, ch_id)
    tick_all(wallets, c)                    # peer co-signs here (server-side)
    st = c.ln_close_status(ch_id.hex())
    if st.get('ready'):
        view = c.fetch_view()
        tx = w.ln_build_close_tx(view, c, ch_id)
        res = c.submit_tx(core.tx_json(tx))
        w.channels[ch_id]['closing_txid'] = bytes.fromhex(res['txid'])
        save_wallets(wallets)
        return (f'channel close co-signed, settlement tx {res["txid"][:16]}... '
                f'in mempool\ncall mine_blocks (1 block) then scan_wallets — '
                f'final balances land on-chain')
    save_wallets(wallets)
    return ('close requested and signed; waiting for the peer co-signature — '
            'call scan_wallets again in a moment')


def t_get_channel(args):
    ch_id = str(args.get('channel_id', ''))
    c = core.NodeClient(NODE_URL)
    st = c.ln_state(ch_id)
    if not st.get('open') and 'capacity' not in st:
        return 'channel not found (funding tx mined?)'
    wallets = load_wallets()
    mine = next((f'{w.name}: {core.fmt_coin(w.channels[bytes.fromhex(ch_id)]["bal_self"])}'
                 for w in wallets if bytes.fromhex(ch_id) in w.channels), '-')
    return (f"channel {ch_id[:16]}...\n"
            f"status: {'OPEN' if st['open'] else 'closed'}\n"
            f"capacity {core.fmt_coin(st['capacity'])}\n"
            f"latest state: seq={st['seq']}  bal_a={core.fmt_coin(st['bal_a'])}  "
            f"bal_b={core.fmt_coin(st['bal_b'])}\n"
            f"server-side wallets: {mine}")


# ── tool catalogue (JSON-schema inputs) ──────────────────────────────────────
def _s(properties, required):
    return {'type': 'object', 'properties': properties, 'required': required}


TOOLS = [
    {'name': 'get_chain_info', 'description':
        'Chain status: height, difficulty, mempool, emission vs 50M cap, next reward.',
     'inputSchema': _s({}, [])},
    {'name': 'list_blocks', 'description':
        'List recent blocks (newest first): hash, txs, difficulty, reward.',
     'inputSchema': _s({'limit': {'type': 'integer', 'description':
                                  'how many blocks (default 10, max 100)'}}, [])},
    {'name': 'get_mempool', 'description':
        'Transactions waiting in the mempool (not mined yet).',
     'inputSchema': _s({}, [])},
    {'name': 'create_wallet', 'description':
        'Create a new stealth wallet (server-side keys, stored in wallets.json).',
     'inputSchema': _s({'name': {'type': 'string'}}, ['name'])},
    {'name': 'list_wallets', 'description':
        'All wallets with on-chain / unconfirmed / Lightning balances (scans first).',
     'inputSchema': _s({}, [])},
    {'name': 'get_wallet', 'description':
        'One wallet in detail: address, balances, outputs, channels. '
        'include_keys=true also exports the private view/spend keys (SECRET).',
     'inputSchema': _s({'name': {'type': 'string'},
                        'include_keys': {'type': 'boolean', 'default': False}},
                       ['name'])},
    {'name': 'scan_wallets', 'description':
        'Refresh every wallet against the chain (view-key scan + Lightning tick). '
        'Run after mining or receiving payments.',
     'inputSchema': _s({}, [])},
    {'name': 'mine_blocks', 'description':
        'Mine blocks (headless: template -> grind -> submit). Reward goes to '
        'payout_wallet (a wallet name here) or a raw address. Balances auto-refresh.',
     'inputSchema': _s({'count': {'type': 'integer', 'default': 1, 'minimum': 1,
                                  'maximum': 20},
                        'payout_wallet': {'type': 'string'},
                        'payout_address': {'type': 'string'}}, [])},
    {'name': 'send_payment', 'description':
        'ON-CHAIN confidential payment (Pedersen + stealth + ring signatures). '
        'Needs mining afterwards: send -> mine_blocks -> scan_wallets.',
     'inputSchema': _s({'wallet': {'type': 'string',
                                   'description': 'sender wallet name'},
                        'recipient_address': {'type': 'string',
                                              'description': "recipient '<A>:<B>' stealth address"},
                        'amount': {'type': 'number', 'description': 'COIN to send'},
                        'fee': {'type': 'number', 'default': 0.1}}, 
                       ['wallet', 'recipient_address', 'amount'])},
    {'name': 'open_channel', 'description':
        'Open a Lightning channel (on-chain funding; mine 1 block to activate). '
        'After that ln_pay is instant.',
     'inputSchema': _s({'wallet': {'type': 'string'},
                        'peer_address': {'type': 'string'},
                        'capacity': {'type': 'number', 'description': 'COIN to lock'},
                        'fee': {'type': 'number', 'default': 0.1}},
                       ['wallet', 'peer_address', 'capacity'])},
    {'name': 'ln_pay', 'description':
        'INSTANT Lightning payment inside a channel (no mining). Both wallets '
        'must be server-side for auto-countersigning.',
     'inputSchema': _s({'wallet': {'type': 'string'},
                        'amount': {'type': 'number'},
                        'channel_id': {'type': 'string', 'description':
                                       'hex channel id (optional if exactly one '
                                       'open channel)'}},
                       ['wallet', 'amount'])},
    {'name': 'ln_close', 'description':
        'Close a Lightning channel: peer co-signs automatically, settlement tx '
        'goes on-chain (mine 1 block to finalize).',
     'inputSchema': _s({'wallet': {'type': 'string'},
                        'channel_id': {'type': 'string'}},
                       ['wallet'])},
    {'name': 'get_channel', 'description':
        'Channel state from the node: capacity, seq, both balances.',
     'inputSchema': _s({'channel_id': {'type': 'string'}}, ['channel_id'])},
]

HANDLERS = {
    'get_chain_info': t_get_chain_info,
    'list_blocks': t_list_blocks,
    'get_mempool': t_get_mempool,
    'create_wallet': t_create_wallet,
    'list_wallets': t_list_wallets,
    'get_wallet': t_get_wallet,
    'scan_wallets': t_scan_wallets,
    'mine_blocks': t_mine_blocks,
    'send_payment': t_send_payment,
    'open_channel': t_open_channel,
    'ln_pay': t_ln_pay,
    'ln_close': t_ln_close,
    'get_channel': t_get_channel,
}


# ── MCP stdio protocol (JSON-RPC 2.0, one message per line) ──────────────────
def handle_request(method: str, params: dict):
    if method == 'initialize':
        return {'protocolVersion': params.get('protocolVersion', '2024-11-05'),
                'capabilities': {'tools': {}},
                'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION}}
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': TOOLS}
    if method == 'tools/call':
        name = params.get('name', '')
        handler = HANDLERS.get(name)
        if handler is None:
            return {'content': [{'type': 'text', 'text': f'unknown tool: {name}'}],
                    'isError': True}
        try:
            text = handler(params.get('arguments') or {})
            return {'content': [{'type': 'text', 'text': text}]}
        except core.NodeError as e:
            return {'content': [{'type': 'text',
                                 'text': f'node error: {e}'}], 'isError': True}
        except Exception as e:                               # noqa: BLE001
            _log('tool', name, 'failed:\n', traceback.format_exc())
            return {'content': [{'type': 'text',
                                 'text': f'error: {e}'}], 'isError': True}
    raise KeyError(method)


def serve() -> None:
    _log(f'{SERVER_NAME} v{SERVER_VERSION} — node at {NODE_URL}, '
         f'wallets at {WALLET_FILE}')
    while True:
        line = sys.stdin.readline()
        if not line:                       # client closed the pipe
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _log('unparseable line:', line[:200])
            continue
        method = msg.get('method', '')
        if not method or method.startswith('notifications/'):
            continue                       # notifications get no response
        req_id = msg.get('id')
        try:
            result = handle_request(method, msg.get('params') or {})
            response = {'jsonrpc': '2.0', 'id': req_id, 'result': result}
        except KeyError:
            response = {'jsonrpc': '2.0', 'id': req_id,
                        'error': {'code': -32601,
                                  'message': f'method not found: {method}'}}
        except Exception as e:                               # noqa: BLE001
            response = {'jsonrpc': '2.0', 'id': req_id,
                        'error': {'code': -32603, 'message': f'internal error: {e}'}}
        sys.stdout.write(json.dumps(response, separators=(',', ':')) + '\n')
        sys.stdout.flush()


if __name__ == '__main__':
    serve()
