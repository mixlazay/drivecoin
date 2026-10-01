#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_gui_flow.py โ€” full app-level integration test (headless).

Instantiates the real Node/Miner/Wallet GUI classes and drives them exactly
like a user would (button handlers + worker threads), verifying the whole
3-program flow: mine -> scan -> send -> mine -> receive.

Run:  python test_gui_flow.py
"""
import os
import threading
import time

import core
import node_app
import miner_app
import wallet_app

# tests use a fast block target on their own ephemeral chains
core.TARGET_BLOCK_TIME = 0.15

PORT = 8802
DATAFILE = 'gui_test_chain.json'
WALLETS = 'gui_test_wallets.json'


def wait_until(pred, timeout=60, what='condition', pump=None):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pump is not None:
            pump.update()                      # drive Tk timers (poll handlers)
        if pred():
            return True
        time.sleep(0.05)
    raise TimeoutError(f'waited too long for: {what}')


def scan_until(wapp, pred, tries=20):
    """Drive wallet refresh cycles (deterministic, auto-refresh disabled)."""
    for _ in range(tries):
        wapp.refresh()
        wait_until(lambda: not wapp._busy, what='scan to finish', pump=wapp)
        if pred():
            return True
        time.sleep(0.4)
    return False


def main():
    for f in (DATAFILE, WALLETS):
        if os.path.exists(f):
            os.remove(f)
    # isolate the test node/wallets from the user's real files
    os.environ['NODE_DATAFILE'] = DATAFILE
    wallet_app.WALLET_FILE = WALLETS

    # ---- 1. node app: start server (as the Start button does) ------------------
    node = node_app.NodeApp()
    node.update()
    node.port_var.set(str(PORT))
    node.start_server()
    assert node.httpd is not None, 'node server failed to start'
    print('[OK] node app started, API on port', PORT)

    client = core.NodeClient(f'http://127.0.0.1:{PORT}')

    # ---- 2. wallet app: create Alice + Bob --------------------------------------
    wapp = wallet_app.WalletApp()
    wapp.url_var.set(f'http://127.0.0.1:{PORT}')
    wapp.auto_var.set(False)               # deterministic scans in this test
    wapp.update()
    wapp.new_wallet()                      # Alice (selected 0)
    alice = wapp.wallets[0]
    wapp.new_wallet()                      # Bob   (selected 1)
    bob = wapp.wallets[1]
    assert len(wapp.wallets) == 2, 'wallet isolation broken'
    print(f'[OK] wallets created via wallet app: Alice, Bob')

    # ---- 3. miner app: mine to Alice ----------------------------------------------
    miner = miner_app.MinerApp()
    miner.url_var.set(f'http://127.0.0.1:{PORT}')
    miner.payout_var.set(alice.address_text)
    miner.toggle()                         # START MINING (spawns worker thread)
    wait_until(lambda: client.info()['height'] >= 2, what='2 mined blocks')
    miner.running = False                  # STOP MINING
    miner.worker.join(timeout=10)
    print(f'[OK] miner found blocks; node height = {client.info()["height"]}')

    # ---- 4. wallet app: scan ---------------------------------------------------------
    wapp.selected = 0
    assert scan_until(wapp, lambda: alice.balance() >= 2 * core.INITIAL_SUBSIDY), \
        alice.balance()
    before_send = alice.balance()
    print(f'[OK] Alice scanned: balance {core.fmt_coin(before_send)}')

    # ---- 5. wallet app: Alice -> Bob 5 COIN --------------------------------------------
    wapp.selected = 0
    wapp.recipient_var.set(bob.address_text)
    wapp.amount_var.set('5')
    wapp.fee_var.set('0.1')
    wapp.send()
    wait_until(lambda: not wapp._busy, what='send to finish', pump=wapp)
    H0 = client.info()['height']            # height right after the send
    mem = client.mempool()
    assert len(mem) == 1 and mem[0]['fee'] == 100_000
    # greedy UTXO selection reserves the whole 120-COIN output (change comes
    # back only after the tx is mined) -> available drops by the reserved UTXO
    assert alice.balance() == before_send - core.INITIAL_SUBSIDY, alice.balance()
    print(f'[OK] confidential tx in mempool (fee {core.fmt_coin(mem[0]["fee"])}); '
          f'Alice balance dropped to {core.fmt_coin(alice.balance())} (PENDING OUT)')

    # ---- 6. miner mines the tx into a block ----------------------------------------------
    miner.toggle()                          # START MINING again
    wait_until(lambda: len(client.mempool()) == 0 and client.info()['height'] >= H0 + 1,
               what='mempool to be mined into a block')
    miner.running = False
    miner.worker.join(timeout=10)

    # ---- 7. Bob scans and must see the 5 COIN ---------------------------------------------
    wapp.selected = 1
    assert scan_until(wapp, lambda: bob.balance() == 5 * core.COIN), bob.balance()
    # exact accounting: reserved UTXO comes back as 114.9 change, then every new
    # block pays Alice its subsidy; her 0.1 fee is recycled exactly once (the
    # first block after the send)
    H = client.info()['height']
    expected_alice = before_send - core.INITIAL_SUBSIDY + 114_900_000 \
        + (H - H0) * core.INITIAL_SUBSIDY + 100_000
    assert alice.balance() == expected_alice, (alice.balance(), expected_alice)
    info = client.info()
    print(f'[OK] final: Bob balance {core.fmt_coin(bob.balance())}, '
          f'Alice {core.fmt_coin(alice.balance())}, '
          f'node height {info["height"]}, outputs {info["outputs"]}')

    # ---- cleanup ------------------------------------------------------------------------------
    for app in (miner, wapp, node):
        try:
            app.on_close()
        except Exception:                    # noqa: BLE001
            app.destroy()
    for f in (DATAFILE, WALLETS):
        if os.path.exists(f):
            os.remove(f)
    print('\nALL GUI-FLOW CHECKS PASSED โ€” the three programs work together')


if __name__ == '__main__':
    main()
