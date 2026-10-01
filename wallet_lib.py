#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wallet_lib.py — shared engine for the standalone DriveCoin wallet (CLI + GUI)
================================================================================
Pure Python 3.8+ standard library. Talks to a node over HTTP (local or remote):

    fetch chain view  ->  scan with view key (ECDH)  ->  balance
    build tx          ->  rings + stealth + range proofs  ->  POST /api/tx

Wallet file format is IDENTICAL to wallet_app.py's (wallets.json), so a wallet
created here can be spent in wallet_app and vice-versa.

Requires core.py next to this file (the crypto engine).
================================================================================
"""
import json
import os

import core

DEFAULT_NODE = 'http://127.0.0.1:8000'


class WalletError(Exception):
    pass


# ── HTTP helpers (via NodeClient) ────────────────────────────────────────────
def client_for(url: str) -> core.NodeClient:
    return core.NodeClient(url)


def fetch_view(client: core.NodeClient) -> core.ChainView:
    """Full chain + output set (needed for rings/decoys when spending).
    Uses NodeClient.fetch_view which pulls /api/blocks/<f>/<c> in batches
    (path-based, so it works through web proxies that strip query strings)."""
    return client.fetch_view()


# ── wallet file (same format as wallet_app.py) ───────────────────────────────
def load_wallets(path: str):
    """Load a wallet file; returns list[core.Wallet]. Empty list if missing."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        raise WalletError(f'cannot load {path}: {e}') from None
    return [core.Wallet.from_json(w) for w in data.get('wallets', [])]


def save_wallets(path: str, wallets) -> None:
    data = {'wallets': [w.to_json() for w in wallets]}
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def create_wallet(name: str):
    return core.Wallet(name or 'default')


# ── scan / balance ───────────────────────────────────────────────────────────
def scan_wallets(wallets, url: str) -> dict:
    """Scan the chain for every wallet; returns per-wallet stats.
    Updates balances in-place (view-key recognition). Also AUTO-RELEASES
    pending reservations whose tx is dead (not on-chain AND not in the node's
    mempool — e.g. it was dropped by an old node bug or a restart)."""
    client = client_for(url)
    view = fetch_view(client)
    pending = [core.tx_load(t) for t in client.mempool_full()['txs']]
    onchain = {core.tx_txid(tx) for blk in view.blocks for tx in blk.transactions}
    mempool_ids = {core.tx_txid(t) for t in pending}
    out = {}
    for w in wallets:
        w.confirm_pending(onchain)
        # release reservations for txs that can no longer be mined
        released = 0
        for tid in list(w.pending_txs):
            if tid not in onchain and tid not in mempool_ids:
                ops = w.pending_txs.pop(tid)
                w.pending_ops.difference_update(ops)
                released += 1
        found = w.scan(view)
        w.pending_in = w.scan_pending_incoming(pending)
        out[w.name] = {'balance': w.balance(), 'pending_in': w.pending_in,
                       'blocks': len(view.blocks), 'released': released}
    return out


# ── send ─────────────────────────────────────────────────────────────────────
def send(wallet, url: str, recipient: str, amount: int, fee: int) -> dict:
    """Build a fully-signed confidential tx and broadcast it. Returns the
    node's response ({'ok': True, 'txid': ...})."""
    try:
        A, B = core.parse_address(recipient)
    except ValueError as e:
        raise WalletError(f'bad recipient address: {e}') from None
    if amount <= 0:
        raise WalletError('amount must be positive')
    if fee < core.MIN_FEE:
        raise WalletError(f'fee must be at least {core.MIN_FEE} units (0.001 COIN)')
    client = client_for(url)
    view = fetch_view(client)
    wallet.scan(view)
    if wallet.balance() < amount + fee:
        raise WalletError(
            f'insufficient balance (have {core.fmt_coin(wallet.balance())}, '
            f'need {core.fmt_coin(amount + fee)} including fee)')
    # recipients = [((A, B), amount)], fee separate
    tx = wallet.build_tx(view, [((A, B), amount)], fee, consume=False)
    ring_sizes = [len(ti.ring) for ti in tx.inputs]
    res = client.submit_tx(core.tx_json(tx))
    wallet.supersede_pending(tx.used_ops)
    wallet.register_pending(bytes.fromhex(res['txid']), tx.used_ops)
    return {'txid': res.get('txid', ''), 'inputs': len(tx.inputs),
            'ring_sizes': ring_sizes, 'outputs': len(tx.outputs), 'fee': fee}
