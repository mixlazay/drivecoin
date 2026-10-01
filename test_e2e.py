#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_e2e.py — headless end-to-end test of the 3-program architecture.

Starts a real node (NodeService + HTTP server), then plays the roles of the
miner app and the wallet apps through NodeClient, exactly as the GUIs do:

  1. mine block #1 with payout -> Alice            (miner app flow)
  2. Alice scans the chain and sees her balance    (wallet app flow)
  3. Alice -> Bob confidential payment             (wallet app flow)
  4. mine block #2 including the payment
  5. ATTACK: double spend -> rejected
  6. ATTACK: tampered commitment -> rejected
  7. node persistence: restart service, state must survive

Run:  python test_e2e.py
"""
import json
import os
import threading
import time
from http.server import ThreadingHTTPServer

import core

# tests use a fast block target on their own ephemeral chains (the public
# consensus value is 60s — mining dozens of blocks would take an hour)
core.TARGET_BLOCK_TIME = 0.15

# The HTTP handler lives in node_app (Tkinter UI). On headless servers there
# is no tkinter — fall back to the identical handler in node_headless.
try:
    import node_app as _node_mod
    from node_app import Handler
except ImportError:
    import node_headless as _node_mod
    from node_headless import Handler

PORT = 8777
DATAFILE = 'test_chain.json'


def mine_one_block(client: core.NodeClient, payout: str) -> dict:
    """Exactly what miner_app's worker does: template -> grind -> submit."""
    tpl = client.template(payout)
    prefix = bytes.fromhex(tpl['prefix_hex'])
    target = ((1 << 256) - 1) // max(1, tpl['difficulty'])
    nonce = 0
    t0 = time.perf_counter()
    while True:
        if int.from_bytes(core.sha256d(prefix + core.enc_u64(nonce)), 'big') < target:
            break
        nonce += 1
    dt = time.perf_counter() - t0
    res = client.submit_block(tpl['template_id'], nonce)
    print(f'  block #{res["height"]} mined in {dt:.2f}s ({nonce:,} nonces) '
          f'-> accepted, reward {core.fmt_coin(res["reward"])}')
    return res


def fetch_view(client: core.NodeClient) -> core.ChainView:
    """Exactly what wallet_app does to build its local chain view."""
    blocks = [core.block_load(b) for b in client.chain_full()['blocks']]
    outputs = {}
    for o in client.outputs():
        outputs[(bytes.fromhex(o['txid']), o['index'])] = core.OutputRec(
            core.point_load(o['dest']), core.point_load(o['commitment']),
            bool(o['is_coinbase']))
    return core.ChainView(blocks, outputs)


def main():
    ok = 0
    if os.path.exists(DATAFILE):
        os.remove(DATAFILE)

    # ---- 0. monetary policy: 50M hard cap + halving every 210,000 blocks ----
    assert core.MAX_SUPPLY == 50_000_000 * core.COIN
    assert core.HALVING_INTERVAL == 210_000
    # halving boundaries (in smallest units: fractional epochs stay exact)
    assert core.scheduled_subsidy(0) == 120 * core.COIN
    assert core.scheduled_subsidy(209_999) == 120 * core.COIN
    assert core.scheduled_subsidy(210_000) == 60 * core.COIN
    assert core.scheduled_subsidy(210_001) == 60 * core.COIN
    assert core.scheduled_subsidy(420_000) == 30 * core.COIN
    assert core.scheduled_subsidy(1_000_000) == 7_500_000          # 7.5 COIN
    # hard cap: fake chains that are nearly/exactly full
    b = core.Blockchain(initial_difficulty=1_000)
    b.blocks.append(core.Block(0, b'\x00' * 32, b'\x00' * 32, 0, 1, 0, []))
    b.blocks.append(core.Block(1, b'\x00' * 32, b'\x00' * 32, 0, 1, 0, [
        core.Transaction(is_coinbase=True, fee=0, coinbase_amount=core.MAX_SUPPLY - 10,
                         tx_pubkey=core.G, inputs=[], outputs=[])]))
    assert b.emission() == core.MAX_SUPPLY - 10
    assert b.subsidy(2) == 10                       # only 10 units left
    b.blocks[1].transactions[0].coinbase_amount = core.MAX_SUPPLY
    assert b.emission() == core.MAX_SUPPLY
    assert b.subsidy(2) == 0                        # cap reached -> nothing more
    print('[OK] monetary policy: 50M hard cap, halving every 210,000 blocks '
          '(120 -> 60 -> 30 -> ... COIN), cap clamps final emission exactly')
    ok += 1

    # ---- node process (service + real HTTP server) ---------------------------
    SERVICE = core.NodeService(DATAFILE)
    print(SERVICE.start())
    _node_mod.SERVICE = SERVICE               # the HTTP handler reads this global
    httpd = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    client = core.NodeClient(f'http://127.0.0.1:{PORT}')

    # ---- wallets (wallet app) -------------------------------------------------
    alice = core.Wallet('Alice')
    bob = core.Wallet('Bob')
    print(f'[OK] wallets created: Alice={alice.address_text[:20]}..., '
          f'Bob={bob.address_text[:20]}...')
    ok += 1

    # ---- 1. miner mines to Alice ----------------------------------------------
    res = mine_one_block(client, alice.address_text)
    assert res['height'] == 1
    ok += 1

    # ---- 2. Alice scans ----------------------------------------------------------
    view = fetch_view(client)
    alice.scan(view)
    assert alice.balance() == core.INITIAL_SUBSIDY, alice.balance()
    print(f'[OK] Alice scanned locally and sees {core.fmt_coin(alice.balance())} '
          '(amount invisible to everyone else)')
    ok += 1

    # ---- 3. Alice -> Bob (new pending flow: reserve, submit, confirm later) -----
    tx = alice.build_tx(view, [(bob.address, 20 * core.COIN)], fee=100_000, consume=False)
    txid_before = core.tx_txid(tx)
    res = client.submit_tx(core.tx_json(tx))
    assert res['ok']
    alice.register_pending(bytes.fromhex(res['txid']), tx.used_ops)
    # JSON round-trip must preserve the txid byte-for-byte
    assert core.tx_txid(core.tx_load(json.loads(json.dumps(core.tx_json(tx))))) == txid_before
    assert alice.balance() == 0        # the only UTXO is now reserved (PENDING OUT)
    print(f'[OK] Alice sent {core.fmt_coin(20 * core.COIN)} to Bob; '
          f'tx {res["txid"][:16]}... in mempool; JSON round-trip exact; '
          'inputs reserved as PENDING OUT')
    ok += 1

    # ---- 4. mine block #2 ----------------------------------------------------------
    res = mine_one_block(client, alice.address_text)
    assert res['height'] == 2
    view = fetch_view(client)
    alice.scan(view)
    bob.scan(view)
    onchain = {core.tx_txid(t) for blk in view.blocks for t in blk.transactions}
    alice.confirm_pending(onchain)
    bob.confirm_pending(onchain)
    assert bob.balance() == 20 * core.COIN
    # Alice: 120 - 20 (sent) - 0.1 (fee) + 120.1 (block-2 coinbase = subsidy + her
    # fee back, because this test's miner pays out to Alice herself)
    assert alice.balance() == core.INITIAL_SUBSIDY - 20 * core.COIN - 100_000 \
        + core.INITIAL_SUBSIDY + 100_000
    print(f'[OK] after block #2: Alice={core.fmt_coin(alice.balance())}, '
          f'Bob={core.fmt_coin(bob.balance())}')
    ok += 1

    # ---- 5. ATTACK: double spend ------------------------------------------------
    tx_b = alice.build_tx(view, [(bob.address, 5 * core.COIN)], fee=100_000,
                          consume=False, force_outputs=list(tx.used_ops))
    try:
        client.submit_tx(core.tx_json(tx_b))
        raise AssertionError('double spend was accepted!')
    except core.NodeError as e:
        print(f'[OK] double spend rejected: {e}')
    ok += 1

    # ---- 6. ATTACK: tamper a commitment (mint money) -------------------------------
    import copy
    tx_ok = bob.build_tx(view, [(alice.address, 2 * core.COIN)], fee=100_000, consume=False)
    res = client.submit_tx(core.tx_json(tx_ok))            # sanity: original is valid
    bob.register_pending(bytes.fromhex(res['txid']), tx_ok.used_ops)
    bad = copy.deepcopy(tx_ok)
    bad.outputs[0].commitment = core.pt_add(bad.outputs[0].commitment, core.G)  # +1 unit
    try:
        client.submit_tx(core.tx_json(bad))
        raise AssertionError('tampered tx was accepted!')
    except core.NodeError as e:
        print(f'[OK] tampered tx rejected: {e}')
    ok += 1

    # ---- 7. persistence: restart the node ------------------------------------------
    svc2 = core.NodeService(DATAFILE)
    svc2.start()
    assert len(svc2.chain.blocks) == 3
    assert len(svc2.mempool.txs) == 1, 'mempool must survive a node restart'
    spent = [ti.key_image for blk in svc2.chain.blocks
             for tx in blk.transactions for ti in tx.inputs]
    assert any(ki in svc2.chain.spent_images for ki in spent)
    print(f'[OK] node restarted from {DATAFILE}: height {len(svc2.chain.blocks) - 1}, '
          f'{len(svc2.chain.outputs)} outputs, {len(svc2.chain.spent_images)} spent key images, '
          f'{len(svc2.mempool.txs)} pending tx restored from the saved mempool')
    ok += 1

    # ---- 8. LIGHTNING: open a channel (on-chain, one block) ---------------------
    view = fetch_view(client)
    alice.scan(view)
    res = alice.ln_open_channel(view, client, bob.address_text,
                                30 * core.COIN, 100_000)
    ch_id = bytes.fromhex(res['ch_id'])
    res = mine_one_block(client, alice.address_text)          # funding block
    view = fetch_view(client)
    alice.scan(view)
    bob.scan(view)
    onchain = {core.tx_txid(t) for blk in view.blocks for t in blk.transactions}
    alice.confirm_pending(onchain)
    bob.ln_tick(client, bob.address_text)                     # invite -> share reply
    alice.ln_tick(client, alice.address_text)                 # share -> channel open
    assert alice.channels[ch_id]['status'] == 'open'
    assert bob.channels[ch_id]['status'] == 'open'
    assert alice.channels[ch_id]['bal_self'] == 30 * core.COIN
    print('[OK] Lightning channel OPEN (one mined block), capacity 30 COIN')
    ok += 1

    # ---- 9. LIGHTNING: instant payment (NO mining) -------------------------------
    t0 = time.perf_counter()
    alice.ln_pay(client, ch_id, 5 * core.COIN)
    bob.ln_tick(client, bob.address_text)                     # countersign + commit
    alice.ln_tick(client, alice.address_text)                 # learn committed state
    dt = (time.perf_counter() - t0) * 1000
    assert alice.channels[ch_id]['bal_self'] == 25 * core.COIN
    assert bob.channels[ch_id]['bal_self'] == 5 * core.COIN
    # on-chain balances unchanged by an off-chain payment. alice's chain UTXOs:
    # 99.9 change + 90 funding change + 120.1 cb3 + 2 (bob's tx_ok) + 120.1 cb4
    assert alice.balance() == 312_100_000
    assert bob.balance() == 17_900_000      # 20 - 2 (tx_ok) - 0.1 fee = change 17.9
    print(f'[OK] LIGHTNING INSTANT payment 5 COIN settled in {dt:.0f} ms '
          '(zero blocks mined)')
    ok += 1

    # insufficient channel balance rejected
    try:
        alice.ln_pay(client, ch_id, 99 * core.COIN)
        raise AssertionError('over-spend accepted!')
    except ValueError:
        print('[OK] over-spend inside channel rejected (balance check)')
    ok += 1

    # ---- 10. LIGHTNING: cooperative close (settle on-chain) ----------------------
    alice.ln_close_request(client, ch_id)
    bob.ln_tick(client, bob.address_text)                     # co-signature
    view = fetch_view(client)
    close_tx = alice.ln_build_close_tx(view, client, ch_id)
    res = client.submit_tx(core.tx_json(close_tx))
    alice.channels[ch_id]['closing_txid'] = bytes.fromhex(res['txid'])
    assert res['ok']
    res = mine_one_block(client, alice.address_text)          # settlement block
    view = fetch_view(client)
    alice.scan(view)
    bob.scan(view)
    onchain = {core.tx_txid(t) for blk in view.blocks for t in blk.transactions}
    alice.confirm_channel_closes(onchain)
    assert alice.channels[ch_id]['status'] == 'closed'
    # alice: 312.1 + 24.9 (settle output) + 120.1 (cb5 = subsidy + close fee)
    assert alice.balance() == 312_100_000 + 24_900_000 + (core.INITIAL_SUBSIDY + 100_000)
    assert bob.balance() == 17_900_000 + 5 * core.COIN
    print(f'[OK] channel CLOSED on-chain: Alice={core.fmt_coin(alice.balance())}, '
          f'Bob={core.fmt_coin(bob.balance())}')
    ok += 1

    os.remove(DATAFILE)
    print(f'\nALL {ok}/{ok} E2E CHECKS PASSED — 3-program architecture works over HTTP')


if __name__ == '__main__':
    main()
