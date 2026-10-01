#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
miner_ui.py — DriveCoin standalone miner with a graphical UI (Windows/Linux)
================================================================================
Python 3.8+ with Tkinter (bundled in the downloadable builds). Same engine as
miner.py: fetch a template over HTTPS → grind in worker processes → submit →
rewards arrive at the wallet's stealth address. The wallet file is fully
compatible with wallet_app.py (use an SSH tunnel to spend).

Run:  python miner_ui.py
================================================================================
"""
import json
import multiprocessing
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import miner_lib
from miner_lib import (DEFAULT_NODE, VERSION, MinerError, fmt_hashrate,
                       http_json, load_or_create_wallet, scan_wallet,
                       save_wallet)


class MinerApp:
    UPDATE_MS = 300

    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(f'DriveCoin Miner v{VERSION} — prototype privacy chain')
        root.geometry('880x640')
        root.minsize(760, 560)

        self.wallet = None
        self.wallet_path = os.path.join(os.path.dirname(
            os.path.abspath(sys.argv[0])), 'miner_wallet.json')
        self.engine = None
        self.ui_queue = queue.Queue()      # engine/callback events -> Tk thread

        self._build_ui()
        self.root.after(self.UPDATE_MS, self._poll_queue)
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

    # ---- UI construction -------------------------------------------------
    def _build_ui(self):
        pad = {'padx': 10, 'pady': 6}
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass

        top = ttk.LabelFrame(self.root, text='Connection')
        top.pack(fill='x', **pad)
        ttk.Label(top, text='Node:').grid(row=0, column=0, sticky='w',
                                          padx=8, pady=8)
        self.node_var = tk.StringVar(value=DEFAULT_NODE)
        ttk.Entry(top, textvariable=self.node_var, width=48).grid(
            row=0, column=1, sticky='we', padx=4)
        ttk.Label(top, text='Workers:').grid(row=0, column=2, padx=8)
        self.threads_var = tk.IntVar(
            value=min(8, os.cpu_count() or 1))
        ttk.Spinbox(top, from_=1, to=32, width=4,
                    textvariable=self.threads_var).grid(row=0, column=3)
        top.columnconfigure(1, weight=1)

        wpad = {'padx': 10, 'pady': 4}
        wallet = ttk.LabelFrame(
            self.root, text='Payout wallet  (rewards are yours — keys never '
                            'leave this machine)')
        wallet.pack(fill='x', **wpad)
        row = ttk.Frame(wallet)
        row.pack(fill='x', padx=8, pady=6)
        ttk.Button(row, text='New wallet…', command=self._new_wallet).pack(
            side='left', padx=(0, 6))
        ttk.Button(row, text='Open wallet…', command=self._open_wallet).pack(
            side='left', padx=(0, 6))
        ttk.Button(row, text='Rescan balance', command=self._rescan).pack(
            side='left', padx=(0, 10))
        self.wallet_label = ttk.Label(row, text='no wallet loaded',
                                      foreground='#777')
        self.wallet_label.pack(side='left')

        arow = ttk.Frame(wallet)
        arow.pack(fill='x', padx=8, pady=(0, 8))
        ttk.Label(arow, text='Address:').pack(side='left')
        self.address_var = tk.StringVar(value='')
        self.address_entry = ttk.Entry(arow, textvariable=self.address_var)
        self.address_entry.pack(side='left', fill='x', expand=True, padx=6)

        ctrl = ttk.Frame(self.root)
        ctrl.pack(fill='x', **wpad)
        self.start_btn = ttk.Button(ctrl, text='▶  Start mining',
                                    command=self._start)
        self.start_btn.pack(side='left')
        self.stop_btn = ttk.Button(ctrl, text='■  Stop',
                                   command=self._stop, state='disabled')
        self.stop_btn.pack(side='left', padx=6)
        self.status_var = tk.StringVar(value='idle')
        ttk.Label(ctrl, textvariable=self.status_var, foreground='#0a7').pack(
            side='left', padx=12)

        stats = ttk.LabelFrame(self.root, text='Mining')
        stats.pack(fill='x', **wpad)
        self.stat_vars = {}
        cells = [('hashrate', 'Hashrate'), ('blocks', 'Blocks found'),
                 ('rewards', 'Rewards'), ('balance', 'Wallet balance'),
                 ('height', 'Chain height'), ('difficulty', 'Difficulty')]
        for i, (key, label) in enumerate(cells):
            col, row_i = i % 3, i // 3
            frame = ttk.Frame(stats)
            frame.grid(row=row_i, column=col, sticky='we', padx=10, pady=6)
            ttk.Label(frame, text=label, foreground='#777').pack(anchor='w')
            var = tk.StringVar(value='–')
            ttk.Label(frame, textvariable=var, font=('TkDefaultFont', 12,
                                                     'bold')).pack(anchor='w')
            self.stat_vars[key] = var
        for c in range(3):
            stats.columnconfigure(c, weight=1)

        logf = ttk.LabelFrame(self.root, text='Log')
        logf.pack(fill='both', expand=True, **wpad)
        self.log_text = tk.Text(logf, height=12, state='disabled',
                                font=('Consolas', 9), wrap='word',
                                background='#10151f', foreground='#cde3d8')
        self.log_text.pack(fill='both', expand=True, padx=8, pady=8)

    # ---- helpers ----------------------------------------------------------
    def log(self, msg: str):
        self.log_text.configure(state='normal')
        self.log_text.insert('end', f'[{time.strftime("%H:%M:%S")}] {msg}\n')
        self.log_text.see('end')
        self.log_text.configure(state='disabled')

    def _set_status(self, text: str):
        self.status_var.set(text)

    def _wallet_summary(self) -> str:
        if self.wallet is None:
            return 'no wallet loaded'
        return f'{os.path.basename(self.wallet_path)} — {self.wallet.name}'

    def _refresh_wallet_display(self):
        self.wallet_label.config(text=self._wallet_summary())
        if self.wallet is not None:
            self.address_var.set(self.wallet.address_text)
            self.stat_vars['balance'].set(f'{self.wallet.balance() / 1e6:g} DCC')

    # ---- wallet actions -----------------------------------------------------
    def _new_wallet(self):
        path = filedialog.asksaveasfilename(
            title='Create miner wallet', defaultextension='.json',
            initialfile='miner_wallet.json',
            filetypes=[('DriveCoin wallet', '*.json'), ('All files', '*.*')])
        if not path:
            return
        try:
            core = miner_lib.core_or_none()
            if core is None:
                raise MinerError('core.py not found next to the program')
            self.wallet = core.Wallet('miner')
            self.wallet_path = path
            save_wallet(self.wallet, path)
        except MinerError as e:
            messagebox.showerror('Wallet', str(e))
            return
        self._refresh_wallet_display()
        self.log(f'new wallet created: {path}')
        self.log('keep this file safe — it holds your spend keys')

    def _open_wallet(self):
        path = filedialog.askopenfilename(
            title='Open miner wallet',
            filetypes=[('DriveCoin wallet', '*.json'), ('All files', '*.*')])
        if not path:
            return
        try:
            self.wallet = load_or_create_wallet(path)
            self.wallet_path = path
        except MinerError as e:
            messagebox.showerror('Wallet', str(e))
            return
        self._refresh_wallet_display()
        self.log(f'wallet loaded: {path}')
        self._rescan()

    def _rescan(self):
        if self.wallet is None:
            messagebox.showinfo('Rescan', 'load a wallet first')
            return
        node = self.node_var.get().strip()   # capture BEFORE the thread —
        wallet = self.wallet                 # Tk variables are main-thread only

        def work():
            try:
                n = scan_wallet(wallet, node)
                save_wallet(wallet, self.wallet_path)
                self.ui_queue.put(('scan_done', {'new': n}))
            except MinerError as e:
                self.ui_queue.put(('scan_error', {'error': str(e)}))

        threading.Thread(target=work, daemon=True).start()
        self.log('scanning chain for wallet outputs…')

    # ---- mining ---------------------------------------------------------------
    def _start(self):
        address = self.address_var.get().strip()
        if not address:
            messagebox.showinfo('Start', 'create or open a wallet first '
                                         '(or paste a payout address)')
            return
        if self.engine is not None:
            return
        self.engine = miner_lib.MinerEngine(
            self.node_var.get().strip(), address,
            threads=self.threads_var.get(),
            on_event=lambda k, d: self.ui_queue.put((k, d)))
        try:
            self.engine.start()
        except MinerError as e:
            messagebox.showerror('Start', str(e))
            self.engine = None
            return
        self.start_btn.config(state='disabled')
        self.stop_btn.config(state='normal')
        self.log(f'mining started ({self.threads_var.get()} workers) → '
                 f'{self.node_var.get().strip()}')
        self._set_status('mining')

    def _stop(self):
        if self.engine is None:
            return
        self.log('stopping…')
        self.engine.stop()
        self.engine = None
        self.start_btn.config(state='normal')
        self.stop_btn.config(state='disabled')
        self._set_status('stopped')

    def _on_close(self):
        self._stop()
        self.root.destroy()

    # ---- event pump --------------------------------------------------------------
    def _poll_queue(self):
        try:
            while True:
                kind, data = self.ui_queue.get_nowait()
                self._handle_event(kind, data)
        except queue.Empty:
            pass
        if self.engine is not None:
            st = self.engine.get_stats()
            self.stat_vars['hashrate'].set(fmt_hashrate(st['hashrate']))
            self.stat_vars['blocks'].set(str(st['blocks_found']))
            self.stat_vars['rewards'].set(f'{st["total_reward"] / 1e6:g} DCC')
            self.stat_vars['height'].set(f'{st["height"]:,}')
            self.stat_vars['difficulty'].set(f'{st["difficulty"]:,}')
            self._set_status(f'{st["status"]}  ·  '
                             f'{fmt_hashrate(st["hashrate"])}')
        self.root.after(self.UPDATE_MS, self._poll_queue)

    def _handle_event(self, kind: str, data: dict):
        if kind == 'template':
            self.log(f'template for block #{data["height"]}  difficulty '
                     f'{int(data["difficulty"]):,}  reward '
                     f'{int(data["reward"]) / 1e6:g} DCC')
        elif kind == 'block':
            self.log(f'*** BLOCK #{data["height"]} ACCEPTED — reward '
                     f'{int(data["reward"]) / 1e6:g} DCC')
            if self.wallet is not None:
                self._rescan()
        elif kind == 'stale':
            self.log(f'stale template (another miner was faster) — refreshing')
        elif kind == 'error':
            self.log(f'node error: {data.get("error", "")}')
        elif kind == 'scan_done':
            self._refresh_wallet_display()
            self.log(f'scan: {data["new"]} new output(s) — balance '
                     f'{self.wallet.balance() / 1e6:g} DCC')
        elif kind == 'scan_error':
            self.log(f'scan failed: {data.get("error", "")}')


def main():
    multiprocessing.freeze_support()      # REQUIRED inside PyInstaller exes
    root = tk.Tk()
    MinerApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
