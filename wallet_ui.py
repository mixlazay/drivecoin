#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wallet_ui.py — standalone DriveCoin wallet with a graphical UI (Windows/Linux)
================================================================================
Python 3.8+ with Tkinter. Same engine as wallet_cli.py (wallet_lib) and the
same wallet file format as wallet_app.py (wallets.json).

Features:
  • multiple wallets (create/switch), view-key balance scan
  • send confidential transactions (ring sig + stealth + range proofs)
  • live balance / pending, node URL selector (local or remote)

Run:  python wallet_ui.py
================================================================================
"""
import multiprocessing
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import core
import wallet_lib
from wallet_lib import WalletError, DEFAULT_NODE


class WalletApp:
    UPDATE_MS = 400

    def __init__(self, root: tk.Tk):
        self.root = root
        root.title('DriveCoin Wallet — private payments')
        root.geometry('900x620')
        root.minsize(780, 520)

        self.wallets = []
        self.wallet_path = os.path.join(
            os.path.dirname(os.path.abspath(sys.argv[0])), 'wallets.json')
        self.q = queue.Queue()
        self.busy = False

        self._build()
        self.root.after(self.UPDATE_MS, self._poll)
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)
        self._load_file(self.wallet_path)

    # ── UI construction ───────────────────────────────────────────────────────
    def _build(self):
        pad = {'padx': 10, 'pady': 6}
        top = ttk.LabelFrame(self.root, text='Connection')
        top.pack(fill='x', **pad)
        ttk.Label(top, text='Node:').grid(row=0, column=0, sticky='w', padx=8, pady=8)
        self.node_var = tk.StringVar(value=DEFAULT_NODE)
        ttk.Entry(top, textvariable=self.node_var, width=44).grid(
            row=0, column=1, sticky='we', padx=4)
        ttk.Label(top, text='').grid(row=0, column=2, padx=6)
        top.columnconfigure(1, weight=1)

        wf = ttk.LabelFrame(self.root, text='Wallets  (keys stay on this machine)')
        wf.pack(fill='x', **pad)
        row = ttk.Frame(wf)
        row.pack(fill='x', padx=8, pady=6)
        ttk.Button(row, text='New wallet…', command=self._new_wallet).pack(side='left')
        ttk.Button(row, text='Open file…', command=self._open_file).pack(side='left', padx=4)
        ttk.Button(row, text='Refresh / scan', command=self._refresh).pack(side='left', padx=4)
        ttk.Button(row, text='Save', command=self._save_file).pack(side='left', padx=4)
        self.file_label = ttk.Label(row, text='no wallet file', foreground='#777')
        self.file_label.pack(side='left', padx=12)

        sel = ttk.Frame(wf)
        sel.pack(fill='x', padx=8, pady=(0, 6))
        ttk.Label(sel, text='Active:').pack(side='left')
        self.wallet_combo = ttk.Combobox(sel, state='readonly', width=18)
        self.wallet_combo.pack(side='left', padx=6)
        self.wallet_combo.bind('<<ComboboxSelected>>', lambda e: self._show_address())
        self.bal_var = tk.StringVar(value='—')
        ttk.Label(sel, textvariable=self.bal_var, font=('Consolas', 11, 'bold'),
                  foreground='#4fc3a1').pack(side='left', padx=14)
        self.pending_var = tk.StringVar(value='')
        ttk.Label(sel, textvariable=self.pending_var, foreground='#f0b429').pack(side='left')

        af = ttk.Frame(wf)
        af.pack(fill='x', padx=8, pady=(0, 8))
        ttk.Label(af, text='Address:').pack(side='left')
        self.addr_var = tk.StringVar()
        ttk.Entry(af, textvariable=self.addr_var).pack(side='left', fill='x',
                                                       expand=True, padx=6)

        sf = ttk.LabelFrame(self.root, text='Send confidential transaction')
        sf.pack(fill='x', **pad)
        r = ttk.Frame(sf)
        r.pack(fill='x', padx=8, pady=8)
        ttk.Label(r, text='To:').grid(row=0, column=0, sticky='w')
        self.to_var = tk.StringVar()
        ttk.Entry(r, textvariable=self.to_var, width=52).grid(row=0, column=1,
                                                              sticky='we', padx=6)
        ttk.Label(r, text='Amount:').grid(row=0, column=2)
        self.amt_var = tk.StringVar()
        ttk.Entry(r, textvariable=self.amt_var, width=10).grid(row=0, column=3, padx=4)
        ttk.Label(r, text='Fee:').grid(row=0, column=4)
        self.fee_var = tk.StringVar(value='0.001')
        ttk.Entry(r, textvariable=self.fee_var, width=8).grid(row=0, column=5, padx=4)
        self.btn_send = ttk.Button(r, text='SEND', command=self._send)
        self.btn_send.grid(row=0, column=6, padx=8)
        r.columnconfigure(1, weight=1)

        self.status_var = tk.StringVar(value='idle')
        ttk.Label(self.root, textvariable=self.status_var).pack(anchor='w', padx=12)

        lf = ttk.LabelFrame(self.root, text='Log')
        lf.pack(fill='both', expand=True, **pad)
        self.log_text = tk.Text(lf, height=10, state='disabled', font=('Consolas', 9),
                                background='#10151f', foreground='#cde3d8', wrap='word')
        self.log_text.pack(fill='both', expand=True, padx=8, pady=8)

    # ── helpers ───────────────────────────────────────────────────────────────
    def log(self, msg: str, kind='info'):
        colors = {'info': '#8b98a9', 'ok': '#4fc3a1', 'err': '#ef6b73'}
        self.log_text.config(state='normal')
        self.log_text.insert('end', f'[{__import__("time").strftime("%H:%M:%S")}] {msg}\n')
        self.log_text.see('end')
        self.log_text.config(state='disabled')

    def _refresh_wallet_combo(self, keep=True):
        names = [w.name for w in self.wallets]
        cur = self.wallet_combo.get()
        self.wallet_combo['values'] = names
        if keep and cur in names:
            self.wallet_combo.set(cur)
        elif names:
            self.wallet_combo.set(names[0])
        self._show_address()

    def _active(self):
        name = self.wallet_combo.get()
        for w in self.wallets:
            if w.name == name:
                return w
        return None

    def _show_address(self):
        w = self._active()
        if w:
            self.addr_var.set(w.address_text)
            self.bal_var.set(f'{core.fmt_coin(w.balance())} DCC')
            pend = w.pending_in
            self.pending_var.set(f'(+{core.fmt_coin(pend)} pending)' if pend else '')

    # ── file ops ──────────────────────────────────────────────────────────────
    def _load_file(self, path):
        try:
            self.wallets = wallet_lib.load_wallets(path)
            self.wallet_path = path
            self.file_label.config(text=os.path.basename(path))
            self._refresh_wallet_combo()
            if self.wallets:
                self.log(f'loaded {len(self.wallets)} wallet(s) from {os.path.basename(path)}')
        except WalletError as e:
            messagebox.showerror('Wallet', str(e))

    def _save_file(self):
        try:
            wallet_lib.save_wallets(self.wallet_path, self.wallets)
            self.log(f'saved → {self.wallet_path}', 'ok')
        except WalletError as e:
            messagebox.showerror('Save', str(e))

    def _open_file(self):
        path = filedialog.askopenfilename(
            title='Open wallet file',
            filetypes=[('DriveCoin wallet', '*.json'), ('All files', '*.*')])
        if path:
            self._load_file(path)

    def _new_wallet(self):
        name = self._ask_text('New wallet', 'Wallet name:')
        if not name:
            return
        if any(w.name == name for w in self.wallets):
            messagebox.showinfo('New wallet', f'"{name}" already exists')
            return
        if not self.wallets:
            # first wallet: choose/create the file
            path = filedialog.asksaveasfilename(
                title='Create wallet file', defaultextension='.json',
                initialfile='wallets.json',
                filetypes=[('DriveCoin wallet', '*.json')])
            if not path:
                return
            self.wallet_path = path
        w = wallet_lib.create_wallet(name)
        self.wallets.append(w)
        self.wallet_path = self.wallet_path  # keep
        try:
            wallet_lib.save_wallets(self.wallet_path, self.wallets)
        except WalletError as e:
            messagebox.showerror('New wallet', str(e))
            return
        self.file_label.config(text=os.path.basename(self.wallet_path))
        self._refresh_wallet_combo()
        self.wallet_combo.set(name)
        self._show_address()
        self.log(f'created wallet "{name}" — keep this file safe', 'ok')

    def _ask_text(self, title, prompt):
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.geometry('520x110')
        dlg.transient(self.root)
        ttk.Label(dlg, text=prompt).pack(padx=12, pady=(12, 4), anchor='w')
        var = tk.StringVar()
        entry = ttk.Entry(dlg, textvariable=var)
        entry.pack(fill='x', padx=12)
        result = []
        def ok():
            result.append(var.get().strip())
            dlg.destroy()
        ttk.Button(dlg, text='OK', command=ok).pack(pady=10)
        entry.focus_set()
        dlg.wait_window()
        return result[0] if result else ''

    # ── background worker ─────────────────────────────────────────────────────
    def _run(self, label, fn):
        if self.busy:
            return
        self.busy = True
        self.status_var.set(label)
        def work():
            try:
                fn()
            except (WalletError, core.NodeError) as e:
                self.q.put(('err', str(e)))
            except Exception as e:                       # noqa: BLE001
                self.q.put(('err', f'failed: {e}'))
            finally:
                self.q.put(('idle', ''))
        threading.Thread(target=work, daemon=True).start()

    def _refresh(self):
        node = self.node_var.get().strip()
        def fn():
            stats = wallet_lib.scan_wallets(self.wallets, node)
            wallet_lib.save_wallets(self.wallet_path, self.wallets)
            for name, s in stats.items():
                pend = f' (+{core.fmt_coin(s["pending_in"])} pending)' if s['pending_in'] else ''
                self.q.put(('ok', f'{name}: {core.fmt_coin(s["balance"])}{pend} '
                                  f'({s["blocks"]} blocks)'))
                if s.get('released'):
                    self.q.put(('ok', f'{name}: released {s["released"]} dead '
                                      f'pending tx(s) — funds unlocked'))
            self.q.put(('done', ''))
        self._run('scanning chain with view key…', fn)

    def _send(self):
        w = self._active()
        if not w:
            messagebox.showinfo('Send', 'create/select a wallet first')
            return
        try:
            amount = core.parse_coin(self.amt_var.get())
            fee = core.parse_coin(self.fee_var.get())
        except ValueError as e:
            messagebox.showerror('Send', str(e))
            return
        to = self.to_var.get().strip()
        node = self.node_var.get().strip()
        def fn():
            res = wallet_lib.send(w, node, to, amount, fee)
            wallet_lib.save_wallets(self.wallet_path, self.wallets)
            self.q.put(('ok', f'tx {res["txid"][:24]}… accepted — sent '
                              f'{core.fmt_coin(amount)}'))
            self.q.put(('info', 'unconfirmed until a miner includes it '
                                '(Refresh after mining)'))
            self.q.put(('done', ''))
        self._run('building tx (rings + range proofs)…', fn)

    # ── event pump ────────────────────────────────────────────────────────────
    def _poll(self):
        try:
            while True:
                kind, msg = self.q.get_nowait()
                if kind == 'idle':
                    self.busy = False
                    self.status_var.set('idle')
                elif kind == 'done':
                    self.busy = False
                    self._show_address()
                    self._refresh_wallet_combo()
                elif kind == 'err':
                    self.log(msg, 'err')
                elif kind == 'ok':
                    self.log(msg, 'ok')
                elif kind == 'info':
                    self.log(msg, 'info')
        except queue.Empty:
            pass
        self.root.after(self.UPDATE_MS, self._poll)

    def _on_close(self):
        try:
            if self.wallets:
                wallet_lib.save_wallets(self.wallet_path, self.wallets)
        except Exception:                                # noqa: BLE001
            pass
        self.root.destroy()


def main():
    multiprocessing.freeze_support()      # REQUIRED inside PyInstaller exes
    root = tk.Tk()
    WalletApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
