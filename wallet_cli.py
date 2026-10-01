#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wallet_cli.py — standalone DriveCoin wallet, command-line (Windows / Linux)
================================================================================
Pure Python 3.8+ stdlib + core.py. Talks to any node over HTTP (local node,
SSH tunnel, or the public node). Same wallet file format as wallet_app.py.

Quick start:
    python wallet_cli.py new alice              # create wallet 'alice'
    python wallet_cli.py list                   # list wallets + balances
    python wallet_cli.py address alice          # show receiving address
    python wallet_cli.py scan                   # refresh all balances (view key)
    python wallet_cli.py send <from> <to_addr> <amount> [--fee N]
    python wallet_cli.py ln <from> open <peer_addr> <capacity>   # open channel
    python wallet_cli.py ln <from> pay <ch_id> <amount>          # instant pay
    python wallet_cli.py ln <from> close <ch_id>                 # close/settle

Options (all commands):
    --node URL     node base URL (default http://127.0.0.1:8000)
    --wallet FILE  wallet file  (default: wallets.json)

File format = wallet_app.py's wallets.json (round-trip compatible).
================================================================================
"""
import argparse
import sys

import core
import wallet_lib
from wallet_lib import WalletError


def log(msg: str):
    print(msg, flush=True)


def die(msg: str):
    print(f'error: {msg}', file=sys.stderr, flush=True)
    raise SystemExit(1)


def pick(wallets, name: str):
    for w in wallets:
        if w.name == name:
            return w
    die(f'wallet "{name}" not found (see: list)')


def fmt_bal(w) -> str:
    pend = f' (+{core.fmt_coin(w.pending_in)} pending)' if w.pending_in else ''
    return f'{core.fmt_coin(w.balance())}{pend}'


# ── commands ─────────────────────────────────────────────────────────────────
def cmd_new(args):
    wallets = wallet_lib.load_wallets(args.wallet)
    if any(w.name == args.name for w in wallets):
        die(f'wallet "{args.name}" already exists')
    w = wallet_lib.create_wallet(args.name)
    wallets.append(w)
    wallet_lib.save_wallets(args.wallet, wallets)
    log(f'created wallet "{w.name}"')
    log(f'address: {w.address_text}')
    log(f'saved to {args.wallet} — keep this file safe (holds your spend key)')


def cmd_list(args):
    wallets = wallet_lib.load_wallets(args.wallet)
    if not wallets:
        log(f'no wallets in {args.wallet} — create one:  new <name>')
        return
    # refresh balances (view-key scan over the full chain)
    try:
        stats = wallet_lib.scan_wallets(wallets, args.node)
    except core.NodeError as e:
        die(f'node unreachable: {e}')
    wallet_lib.save_wallets(args.wallet, wallets)
    total = sum(w.balance() for w in wallets)
    log(f'{len(wallets)} wallet(s) in {args.wallet}:')
    for w in wallets:
        log(f'  {w.name:16} {fmt_bal(w)}')
    log(f'  {"TOTAL":16} {core.fmt_coin(total)}')


def cmd_address(args):
    wallets = wallet_lib.load_wallets(args.wallet)
    w = pick(wallets, args.name)
    log(w.address_text)


def cmd_scan(args):
    wallets = wallet_lib.load_wallets(args.wallet)
    if not wallets:
        die(f'no wallets in {args.wallet}')
    try:
        stats = wallet_lib.scan_wallets(wallets, args.node)
    except core.NodeError as e:
        die(f'node unreachable: {e}')
    wallet_lib.save_wallets(args.wallet, wallets)
    for name, s in stats.items():
        pend = f' (+{core.fmt_coin(s["pending_in"])} pending)' if s['pending_in'] else ''
        rel = f'  [released {s["released"]} dead pending tx(s)]' if s.get('released') else ''
        log(f'  {name:16} {core.fmt_coin(s["balance"])}{pend}  '
            f'({s["blocks"]} blocks scanned){rel}')


def cmd_send(args):
    wallets = wallet_lib.load_wallets(args.wallet)
    w = pick(wallets, args.from_)
    try:
        amount = core.parse_coin(args.amount)
        fee = core.parse_coin(args.fee)
    except ValueError as e:
        die(str(e))
    try:
        res = wallet_lib.send(w, args.node, args.to, amount, fee)
    except (WalletError, core.NodeError) as e:
        die(str(e))
    wallet_lib.save_wallets(args.wallet, wallets)
    log(f'tx {res["txid"][:24]}… accepted into mempool')
    log(f'  {res["inputs"]} input(s), ring sizes {res["ring_sizes"]}, '
        f'{res["outputs"]} output(s), fee {core.fmt_coin(res["fee"])}')
    log('  unconfirmed — a miner must include it in a block '
        '(refresh to confirm)')


def cmd_ln(args):
    """ln <wallet> open/pay/close/status — thin wrappers over core LN."""
    wallets = wallet_lib.load_wallets(args.wallet)
    w = pick(wallets, args.name)
    client = core.NodeClient(args.node, timeout=15)
    action = args.action
    try:
        if action == 'status':
            total = w.ln_total()
            log(f'Lightning balance: {core.fmt_coin(total)} in {len(w.channels)} channel(s)')
            for ch, rec in w.channels.items():
                log(f'  {ch.hex()[:16]}… status={rec["status"]} '
                    f'self={core.fmt_coin(rec["bal_self"])} '
                    f'peer={core.fmt_coin(rec["bal_peer"])}')
            return
        # process inbound messages first
        for line in w.ln_process_inbox(client, w.address_text):
            log(f'  inbox: {line}')
        if action == 'open':
            view = wallet_lib.fetch_view(client)
            w.scan(view)
            capacity = core.parse_coin(args.capacity)
            res = w.ln_open_channel(view, client, args.peer, capacity, core.CLOSE_FEE)
            log(f'channel opened: {res.get("ch_id", "?")[:16]}… '
                f'(funding tx in mempool — needs 1 block to confirm)')
        elif action == 'pay':
            ch_id = bytes.fromhex(args.ch_id)
            amount = core.parse_coin(args.amount)
            res = w.ln_pay(client, ch_id, amount)
            log(f'paid {core.fmt_coin(amount)} — {res.get("status", "ok")} '
                f'(instant, no block)')
        elif action == 'close':
            ch_id = bytes.fromhex(args.ch_id)
            res = w.ln_close_request(client, ch_id)
            log(f'close requested: {res.get("status", "ok")} '
                f'(settle on-chain after counterparty signs)')
        else:
            die(f'unknown ln action: {action}')
        # drain ticks so handshakes complete
        for _ in range(10):
            lines = w.ln_tick(client, w.address_text)
            for line in lines:
                log(f'  tick: {line}')
    except (WalletError, core.NodeError, ValueError, KeyError) as e:
        die(f'ln error: {e}')
    wallet_lib.save_wallets(args.wallet, wallets)


def main() -> int:
    ap = argparse.ArgumentParser(
        prog='drivecoin-wallet',
        description='Standalone DriveCoin wallet (CLI) — scan, send, Lightning.')
    ap.add_argument('--node', default=wallet_lib.DEFAULT_NODE)
    ap.add_argument('--wallet', default='wallets.json')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('new', help='create a new wallet')
    p.add_argument('name')
    p.set_defaults(func=cmd_new)

    p = sub.add_parser('list', help='list wallets + balances (scans chain)')
    p.set_defaults(func=cmd_list)

    p = sub.add_parser('address', help='print a wallet receiving address')
    p.add_argument('name')
    p.set_defaults(func=cmd_address)

    p = sub.add_parser('scan', help='refresh balances for all wallets')
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser('send', help='send coins on-chain (confidential tx)')
    p.add_argument('from_', metavar='from')
    p.add_argument('to', help='recipient address (66hex:66hex)')
    p.add_argument('amount', help='amount in COIN (e.g. 1.5)')
    p.add_argument('--fee', default='0.001')
    p.set_defaults(func=cmd_send)

    p = sub.add_parser('ln', help='Lightning channel operations')
    p.add_argument('name', help='wallet name')
    p.add_argument('action', choices=['open', 'pay', 'close', 'status'])
    p.add_argument('args', nargs='*',
                   help='open: <peer_addr> <capacity> | pay: <ch_id> <amount> | '
                        'close: <ch_id>')
    p.set_defaults(func=cmd_ln)

    args = ap.parse_args()
    # ln positional args are in args.args — unpack based on action
    if args.cmd == 'ln':
        a = args.args
        if args.action == 'open':
            if len(a) != 2:
                die('ln open usage: ln <wallet> open <peer_address> <capacity>')
            args.peer, args.capacity = a[0], a[1]
        elif args.action == 'pay':
            if len(a) != 2:
                die('ln pay usage: ln <wallet> pay <ch_id> <amount>')
            args.ch_id, args.amount = a[0], a[1]
        elif args.action == 'close':
            if len(a) != 1:
                die('ln close usage: ln <wallet> close <ch_id>')
            args.ch_id = a[0]
    try:
        args.func(args)
    except WalletError as e:
        die(str(e))
    return 0


if __name__ == '__main__':
    sys.exit(main())
