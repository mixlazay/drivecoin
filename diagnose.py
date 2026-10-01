#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diagnose.py — inspect node_chain.json + wallets.json and explain where
money is: on-chain, pending (in a miner's mempool), or unclaimed."""
import glob
import json
import os
import sys

import core


def load_chain(path):
    data = json.load(open(path, encoding='utf-8'))
    chain = core.Blockchain(int(data.get('initial_difficulty', 0)) or None)
    for bd in data['blocks']:
        blk = core.block_load(bd)
        for t in blk.transactions:
            chain._apply_tx(t, chain.outputs, chain.spent_images)
        chain.blocks.append(blk)
    return chain


def main():
    chain_file = sys.argv[1] if len(sys.argv) > 1 else 'node_chain.json'
    wallet_file = sys.argv[2] if len(sys.argv) > 2 else 'wallets.json'
    if not os.path.exists(chain_file):
        print(f'no chain file at {chain_file} — node has not been started yet')
        return
    chain = load_chain(chain_file)
    print(f'chain: height {len(chain.blocks) - 1}, outputs {len(chain.outputs)}, '
          f'spent key images {len(chain.spent_images)}')
    emission = chain.emission()
    print(f'supply: emitted {core.fmt_coin(emission)} of '
          f'{core.fmt_coin(core.MAX_SUPPLY)} cap '
          f'({emission / core.MAX_SUPPLY * 100:.4f}%) | next block reward '
          f'{core.fmt_coin(chain.subsidy(len(chain.blocks)))} '
          f'(halving every {core.HALVING_INTERVAL:,} blocks)')
    for i, blk in enumerate(chain.blocks):
        parts = []
        for t in blk.transactions:
            tid = core.tx_txid(t).hex()[:10]
            if t.is_coinbase:
                parts.append(f'COINBASE {core.fmt_coin(t.coinbase_amount)}')
            else:
                parts.append(f'tx {tid} fee={core.fmt_coin(t.fee)} '
                             f'in={len(t.inputs)} out={len(t.outputs)}')
        print(f'  block {i}: ' + ('; '.join(parts) if parts else '(empty)'))

    wallets = []
    if os.path.exists(wallet_file):
        wdata = json.load(open(wallet_file, encoding='utf-8'))
        wallets = [core.Wallet.from_json(w) for w in wdata.get('wallets', [])]
    print(f'\nwallet file: {len(wallets)} wallet(s) in {wallet_file}')

    view = core.ChainView(chain.blocks, chain.outputs)
    claimed = set()
    for w in wallets:
        w.scan(view)
        print(f'\nwallet "{w.name}": balance(available) = {core.fmt_coin(w.balance())}')
        for op, rec in sorted(w.known.items(), key=lambda kv: (kv[0][0].hex(), kv[0][1])):
            image = core.pt_mul(rec['x'], core.hash_to_point(core.enc_point(rec['P'])))
            onchain_spent = image in chain.spent_images
            local_spent = op in w.spent_ops
            if onchain_spent:
                status = 'SPENT ON-CHAIN'
            elif local_spent:
                status = 'LOCALLY MARKED SPENT but NOT mined yet -> pending in mempool '
                status += 'or LOST if the node was restarted without mining it'
            else:
                status = 'available'
            print(f'   out {op[0].hex()[:12]}...#{op[1]}  {core.fmt_coin(rec["v"]):>12}  {status}')
        claimed.update(w.known.keys())

    unclaimed = [op for op in chain.outputs if op not in claimed]
    total_unclaimed = sum(0 for _ in unclaimed)
    print(f'\noutputs NO wallet recognises: {len(unclaimed)}')
    for op in unclaimed:
        rec = chain.outputs[op]
        # work out the plaintext value only if it is a coinbase (mask is public)
        if rec.is_coinbase:
            m = core.coinbase_mask(rec.dest)
            print(f'   {op[0].hex()[:12]}...#{op[1]}  coinbase output — keys not in wallet file')
        else:
            print(f'   {op[0].hex()[:12]}...#{op[1]}  confidential output — '
                  'recipient keys not in wallet file (sent to a deleted/foreign address?)')


if __name__ == '__main__':
    main()
