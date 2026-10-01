#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
miner_app.py — PrivateChain MINER (with UI)
================================================================================
Requests block templates from the node, grinds SHA-256d nonces locally in a
worker thread, and submits solved blocks. The block reward (subsidy + fees)
is paid to any wallet address you paste into "payout address" — exactly like
a mining pool payout.

    python miner_app.py

Requires the node app to be running (node_app.py).
================================================================================
"""
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext

import core

BG      = '#14161a'
PANEL   = '#1e222a'
FG      = '#e8e8e8'
MUTED   = '#8a93a3'
ACCENT  = '#f0b429'
ERR     = '#ef6b73'
FONT_M  = ('Consolas', 9)
FONT    = ('Segoe UI', 10)


class MinerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('PrivateChain - Miner')
        self.geometry('780x600')
        self.configure(bg=BG)
        self.running = False
        self.worker = None
        self.q: 'queue.Queue[tuple]' = queue.Queue()
        self.stats = {'height': '-', 'difficulty': '-', 'hashrate': '0 H/s',
                      'found': 0, 'accepted': 0, 'rejected': 0}
        self._build_style()
        self._build_ui()
        self.after(150, self._poll)

    # ── UI ──────────────────────────────────────────────────────────────────────
    def _build_style(self):
        st = ttk.Style(self)
        st.theme_use('clam')
        st.configure('.', background=BG, foreground=FG, font=FONT)
        st.configure('TFrame', background=BG)
        st.configure('Panel.TFrame', background=PANEL)
        st.configure('TLabel', background=BG, foreground=FG)
        st.configure('Muted.TLabel', background=BG, foreground=MUTED, font=FONT_M)
        st.configure('Stat.TLabel', background=PANEL, foreground=ACCENT,
                     font=('Consolas', 12, 'bold'))
        st.configure('TButton', background='#2a2f3a', foreground=FG, padding=6)
        st.map('TButton', background=[('active', '#39404e')])
        st.configure('TEntry', fieldbackground=PANEL, foreground=FG, insertcolor=FG)
        st.configure('TLabelFrame', background=BG, foreground=ACCENT)
        st.configure('TLabelFrame.Label', background=BG, foreground=ACCENT)

    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(fill='x', padx=10, pady=(10, 2))
        ttk.Label(top, text='MINER', font=('Consolas', 13, 'bold'),
                  foreground=ACCENT, background=BG).pack(side='left')
        ttk.Label(top, text='  node url', style='Muted.TLabel').pack(side='left', padx=(18, 4))
        self.url_var = tk.StringVar(value='http://127.0.0.1:8000')
        ttk.Entry(top, textvariable=self.url_var, width=26).pack(side='left')

        frm = ttk.Frame(self)
        frm.pack(fill='x', padx=10, pady=4)
        ttk.Label(frm, text='payout address (stealth address of any wallet)',
                  style='Muted.TLabel').pack(anchor='w')
        row = ttk.Frame(frm)
        row.pack(fill='x')
        self.payout_var = tk.StringVar()
        entry = ttk.Entry(row, textvariable=self.payout_var)
        entry.pack(side='left', fill='x', expand=True)
        self.btn_toggle = ttk.Button(row, text='START MINING', command=self.toggle)
        self.btn_toggle.pack(side='left', padx=8)

        stats = ttk.Frame(self)
        stats.pack(fill='x', padx=10, pady=6)
        self.stat_vars = {}
        for key, label in (('status', 'STATUS'), ('hashrate', 'HASHRATE'),
                           ('height', 'MINING HEIGHT'), ('difficulty', 'DIFFICULTY'),
                           ('found', 'BLOCKS FOUND'), ('accepted', 'ACCEPTED')):
            box = ttk.Frame(stats, style='Panel.TFrame', padding=(10, 6))
            box.pack(side='left', padx=4)
            ttk.Label(box, text=label, style='Muted.TLabel').pack(anchor='w')
            var = tk.StringVar(value='-' if key != 'found' and key != 'accepted' else '0')
            ttk.Label(box, textvariable=var, style='Stat.TLabel', width=14).pack(anchor='w')
            self.stat_vars[key] = var

        ttk.Label(self, text='event log', style='Muted.TLabel').pack(anchor='w', padx=12)
        self.log_text = scrolledtext.ScrolledText(self, bg=PANEL, fg=FG,
                                                  insertbackground=FG, font=FONT_M,
                                                  state='disabled', relief='flat')
        self.log_text.pack(fill='both', expand=True, padx=10, pady=(2, 10))
        for tag, color in (('ok', ACCENT), ('err', ERR), ('info', '#9fb4d0')):
            self.log_text.tag_configure(tag, foreground=color)

    # ── queue → UI (thread-safe) ───────────────────────────────────────────────
    def put(self, kind: str, msg: str):
        self.q.put((kind, msg))

    def _poll(self):
        try:
            while True:
                kind, msg = self.q.get_nowait()
                self.log_text.config(state='normal')
                self.log_text.insert('end', f'[{time.strftime("%H:%M:%S")}] {msg}\n', kind)
                self.log_text.see('end')
                self.log_text.config(state='disabled')
        except queue.Empty:
            pass
        self.stat_vars['hashrate'].set(self.stats['hashrate'])
        self.stat_vars['height'].set(self.stats['height'])
        self.stat_vars['difficulty'].set(self.stats['difficulty'])
        self.stat_vars['found'].set(str(self.stats['found']))
        self.stat_vars['accepted'].set(str(self.stats['accepted']))
        self.after(150, self._poll)

    # ── start / stop ───────────────────────────────────────────────────────────
    def toggle(self):
        if self.running:
            self.running = False
            self.btn_toggle.config(text='START MINING', state='disabled')
            self.stat_vars['status'].set('stopping...')
            return
        payout = self.payout_var.get().strip()
        try:
            core.parse_address(payout)
        except ValueError as e:
            self.put('err', f'invalid payout address: {e}')
            return
        self.running = True
        self.btn_toggle.config(text='STOP MINING', state='normal')
        self.stat_vars['status'].set('mining')
        self.put('info', f'mining started, payout {payout[:20]}...')
        # capture Tk variables NOW (Tcl calls are not thread-safe)
        url = self.url_var.get().strip()
        self.worker = threading.Thread(target=self._mine_loop, args=(url, payout),
                                       daemon=True)
        self.worker.start()

    # ── mining loop (worker thread) ─────────────────────────────────────────────
    def _mine_loop(self, url: str, payout: str):
        try:
            client = core.NodeClient(url)
            while self.running:
                try:
                    tpl = client.template(payout)
                except core.NodeError as e:
                    self.put('err', f'cannot get block template: {e}')
                    self.stats['height'] = '-'
                    time.sleep(2.0)
                    continue
                prefix = bytes.fromhex(tpl['prefix_hex'])
                target = ((1 << 256) - 1) // max(1, tpl['difficulty'])
                self.stats['height'] = str(tpl['height'])
                self.stats['difficulty'] = f"{tpl['difficulty']:,}"
                self.put('info', f'template for height {tpl["height"]} '
                                 f'(difficulty {tpl["difficulty"]:,}, '
                                 f'reward {core.fmt_coin(tpl["reward"])})')
                # ---- grind nonces ------------------------------------------------
                nonce = 0
                window = 100_000            # check stop flag / update hashrate
                t0 = time.perf_counter()
                counted = 0
                solved = False
                while self.running:
                    if int.from_bytes(core.sha256d(prefix + core.enc_u64(nonce)), 'big') < target:
                        solved = True
                        break
                    nonce += 1
                    counted += 1
                    if counted >= window:
                        now = time.perf_counter()
                        self.stats['hashrate'] = f'{counted / (now - t0):,.0f} H/s'
                        t0, counted = now, 0
                if not solved:               # stopped by user
                    break
                self.stats['hashrate'] = f'{counted / max(1e-9, time.perf_counter() - t0):,.0f} H/s'
                self.stats['found'] += 1
                try:
                    res = client.submit_block(tpl['template_id'], nonce)
                    self.stats['accepted'] += 1
                    self.put('ok', f'BLOCK #{res["height"]} ACCEPTED! '
                                   f'hash={res["hash"][:20]}... reward {core.fmt_coin(res["reward"])}')
                except core.NodeError as e:
                    self.stats['rejected'] += 1
                    self.put('err', f'block rejected: {e}')
        except Exception as e:                   # noqa: BLE001
            self.put('err', f'miner crashed: {e}')
        finally:
            self.stats['hashrate'] = '0 H/s'
            self.put('info', 'mining stopped')

    def on_close(self):
        self.running = False
        self.after(250, self.destroy)


if __name__ == '__main__':
    app = MinerApp()
    app.protocol('WM_DELETE_WINDOW', app.on_close)
    app.mainloop()
