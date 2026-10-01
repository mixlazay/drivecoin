#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
miner.py — DriveCoin standalone CLI miner (Windows / Linux / macOS)
================================================================================
Pure Python 3.8+, standard library only. Mines the public DriveCoin test chain
over HTTPS — no SSH, no local node needed.

Quick start (wallet mode — generates miner_wallet.json next to this file):
    python miner.py

Mine to an EXISTING address (no wallet/core.py needed):
    python miner.py --address 02ab...:03cd...

Options:
    --node URL        node base URL  (default: https://scan.drivecoinproject.online)
    --address ADDR    mine to this payout address; skip the wallet file
    --wallet FILE     wallet file    (default: miner_wallet.json)
    --threads N       worker processes (default: all CPU cores, capped at 8)
    --once            mine one block, then exit
    --max-blocks N    stop after N accepted blocks
    --timeout S       exit if no block found within S seconds (default: never)
    --user-agent S    override HTTP User-Agent (debug)

Ctrl+C stops cleanly. Rewards go to the wallet's stealth address; run
`--rescan` any time to re-scan the chain and print the balance:
    python miner.py --rescan
================================================================================
"""
import argparse
import multiprocessing
import os
import sys
import time

import miner_lib
from miner_lib import (DEFAULT_NODE, VERSION, MinerError, fmt_hashrate,
                       http_json, load_or_create_wallet, scan_wallet,
                       save_wallet)


def enable_vt():
    """Enable ANSI colours on Windows 10+ terminals (no-op elsewhere)."""
    if os.name == 'nt':
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:                                    # noqa: BLE001
            pass


C = {'dim': '\033[2m', 'teal': '\033[36m', 'gold': '\033[33m',
     'red': '\033[31m', 'green': '\033[32m', 'off': '\033[0m'}


def log(msg: str, color: str = ''):
    ts = time.strftime('%H:%M:%S')
    print(f'{C["dim"]}[{ts}]{C["off"]} {color}{msg}{C["off"]}', flush=True)


def main() -> int:
    multiprocessing.freeze_support()      # REQUIRED inside PyInstaller exes
    enable_vt()
    try:                                  # clean UTF-8 on Windows consoles
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:                                        # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(
        prog='drivecoin-miner',
        description='Standalone miner for the DriveCoin privacy blockchain '
                    '(prototype — no real value).')
    ap.add_argument('--node', default=DEFAULT_NODE)
    ap.add_argument('--address', help='payout address (skip wallet file)')
    ap.add_argument('--wallet', default='miner_wallet.json')
    ap.add_argument('--threads', type=int,
                    default=min(8, (os.cpu_count() or 1)))
    ap.add_argument('--once', action='store_true', help='mine one block, exit')
    ap.add_argument('--max-blocks', type=int, default=0)
    ap.add_argument('--timeout', type=float, default=0.0)
    ap.add_argument('--rescan', action='store_true',
                    help='scan the chain, print wallet balance, exit')
    ap.add_argument('--user-agent', default=miner_lib.USER_AGENT)
    args = ap.parse_args()

    print(f'{C["teal"]}DriveCoin miner v{VERSION} — prototype privacy chain'
          f'{C["off"]}')
    print(f'node: {args.node}   workers: {args.threads}\n')

    # -- chain reachability --------------------------------------------------
    try:
        info = http_json(args.node, 'GET', '/api/info',
                         user_agent=args.user_agent)
    except MinerError as e:
        log(f'cannot reach node: {e}', C['red'])
        return 1
    log(f'chain: height {info["height"]}  difficulty '
        f'{int(info["difficulty"]):,}  emission '
        f'{info["emission"] / 1e6:,.6g} / 50,000,000 DCC  '
        f'mempool {info["mempool"]} tx')

    # -- rescan-only mode ------------------------------------------------------
    if args.rescan:
        if args.address:
            log('--rescan needs a wallet file (not --address)', C['red'])
            return 1
        try:
            w = load_or_create_wallet(args.wallet)
        except MinerError as e:
            log(str(e), C['red'])
            return 1
        n = scan_wallet(w, args.node, user_agent=args.user_agent)
        log(f'wallet {w.name}: {n} new output(s) recognised')
        log(f'balance: {w.balance() / 1e6:g} DCC  '
            f'address: {w.address_text}')
        save_wallet(w, args.wallet)
        return 0

    # -- payout destination -----------------------------------------------------
    wallet = None
    if args.address:
        address = args.address.strip()
        log(f'mining to supplied address: {address[:24]}…')
    else:
        try:
            wallet = load_or_create_wallet(args.wallet)
        except MinerError as e:
            log(str(e), C['red'])
            log('put core.py next to miner.py for wallet mode, or pass '
                '--address <address>', C['dim'])
            return 1
        address = wallet.address_text
        log(f'wallet file: {args.wallet}')
        log(f'payout address: {address}')
        try:
            n = scan_wallet(wallet, args.node, user_agent=args.user_agent)
            save_wallet(wallet, args.wallet)
            log(f'balance: {wallet.balance() / 1e6:g} DCC '
                f'({n} new output(s) recognised)')
        except MinerError as e:
            log(f'(scan skipped: {e})', C['dim'])

    engine_holder = {'engine': None}

    def on_event(kind: str, data: dict):
        if kind == 'template':
            log(f'template for block #{data["height"]}  difficulty '
                f'{int(data["difficulty"]):,}  reward '
                f'{int(data["reward"]) / 1e6:g} DCC')
        elif kind == 'block':
            log(f'*** BLOCK #{data["height"]} ACCEPTED  reward '
                f'{int(data["reward"]) / 1e6:g} DCC  hash {data["hash"][:16]}…',
                C['gold'])
            if blocks_needed and engine_holder['engine'] is not None:
                engine_holder['engine'].request_stop()
        elif kind == 'stale':
            log(f'stale template at height {data["height"]} '
                f'(another miner was faster) — refreshing', C['red'])
        elif kind == 'error':
            log(f'node error: {data["error"]}', C['red'])

    blocks_needed = 1 if args.once else (args.max_blocks or 0)
    engine = miner_lib.MinerEngine(
        args.node, address, threads=args.threads, on_event=on_event,
        user_agent=args.user_agent)
    engine_holder['engine'] = engine

    try:
        engine.start()
    except MinerError as e:
        log(str(e), C['red'])
        return 1

    deadline = time.monotonic() + args.timeout if args.timeout > 0 else None
    print()

    try:
        next_print = 0.0
        while True:
            time.sleep(0.3)
            st = engine.get_stats()
            now = time.monotonic()
            if now >= next_print:
                next_print = now + 3.0
                eta = ''
                if st['hashrate'] > 0 and st['difficulty'] > 0:
                    eta = f'  ~{st["difficulty"] / st["hashrate"]:.0f}s/block'
                print(f"\r{C['teal']}mining{C['off']}  "
                      f"{fmt_hashrate(st['hashrate'])}  "
                      f"nonce-target diff {st['difficulty']:,}  "
                      f"blocks found {st['blocks_found']}  "
                      f"rewards {st['total_reward'] / 1e6:g} DCC"
                      f"{eta}        ", end='', flush=True)
            if st['status'] == 'stopped':
                break
            if blocks_needed and st['blocks_found'] >= blocks_needed:
                engine.request_stop()
            if deadline and time.monotonic() > deadline:
                log('timeout reached without a block', C['red'])
                break
    except KeyboardInterrupt:
        print()
        log('stopping…', C['dim'])
    finally:
        engine.stop()
        print()

    st = engine.get_stats()
    log(f'done: {st["blocks_found"]} block(s), '
        f'{st["total_reward"] / 1e6:g} DCC total rewards, '
        f'{st["total_hashes"]:,} hashes')

    if wallet is not None:
        try:
            scan_wallet(wallet, args.node, user_agent=args.user_agent)
            save_wallet(wallet, args.wallet)
            log(f'wallet balance: {wallet.balance() / 1e6:g} DCC',
                C['green'])
            log('spend it with wallet_app.py (SSH tunnel) — see README-MINER',
                C['dim'])
        except MinerError as e:
            log(f'(final scan failed: {e})', C['dim'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
