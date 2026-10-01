#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wallet_app.py — PrivateChain WALLET (with UI)
================================================================================
Manages any number of stealth wallets. Balances and incoming payments are
found by scanning the chain LOCALLY with the view key (nobody else learns
which outputs are yours). Spending builds fully-signed confidential
transactions and submits them to the node.

    python wallet_app.py

Requires the node app to be running (node_app.py).
================================================================================
"""
import json
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext
from typing import Optional

import core

BG      = '#14161a'
PANEL   = '#1e222a'
FG      = '#e8e8e8'
MUTED   = '#8a93a3'
ACCENT  = '#4fc3a1'
ERR     = '#ef6b73'
FONT_M  = ('Consolas', 9)
FONT    = ('Segoe UI', 10)
FONT_B  = ('Segoe UI', 10, 'bold')
WALLET_FILE = 'wallets.json'


class WalletApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('PrivateChain - Wallet')
        self.geometry('960x640')
        self.configure(bg=BG)
        self.wallets: list = []                # list[core.Wallet]
        self.selected = 0
        self.q: 'queue.Queue[tuple]' = queue.Queue()
        self._busy = False
        self._build_style()
        self._build_ui()
        self._load_wallets()
        self.after(150, self._poll)
        self.after(5000, self._auto_tick)
        self._ln_url = 'http://127.0.0.1:8000'
        self._ln_order: list = []
        threading.Thread(target=self._ln_worker, daemon=True).start()

    # ── UI ──────────────────────────────────────────────────────────────────────
    def _build_style(self):
        st = ttk.Style(self)
        st.theme_use('clam')
        st.configure('.', background=BG, foreground=FG, font=FONT)
        st.configure('TFrame', background=BG)
        st.configure('Panel.TFrame', background=PANEL)
        st.configure('TLabel', background=BG, foreground=FG)
        st.configure('Muted.TLabel', background=BG, foreground=MUTED, font=FONT_M)
        st.configure('Balance.TLabel', background=BG, foreground=ACCENT,
                     font=('Consolas', 15, 'bold'))
        st.configure('TButton', background='#2a2f3a', foreground=FG, padding=6)
        st.map('TButton', background=[('active', '#39404e')])
        st.configure('TEntry', fieldbackground=PANEL, foreground=FG, insertcolor=FG)
        st.configure('TNotebook', background=BG, borderwidth=0)
        st.configure('TNotebook.Tab', background=PANEL, foreground=MUTED, padding=(14, 6))
        st.map('TNotebook.Tab', background=[('selected', '#2a2f3a')],
               foreground=[('selected', ACCENT)])
        st.configure('Treeview', background=PANEL, foreground=FG, fieldbackground=PANEL,
                     rowheight=22, font=FONT_M)
        st.configure('Treeview.Heading', background='#2a2f3a', foreground=FG, font=FONT)
        st.map('Treeview', background=[('selected', '#33455e')])
        st.configure('TListbox', background=PANEL, foreground=FG, rowheight=22)

    def _build_ui(self):
        # top bar --------------------------------------------------------------
        top = ttk.Frame(self)
        top.pack(fill='x', padx=10, pady=(10, 2))
        ttk.Label(top, text='WALLET', font=('Consolas', 13, 'bold'),
                  foreground=ACCENT, background=BG).pack(side='left')
        ttk.Label(top, text='  node url', style='Muted.TLabel').pack(side='left', padx=(18, 4))
        self.url_var = tk.StringVar(value='http://127.0.0.1:8000')
        ttk.Entry(top, textvariable=self.url_var, width=26).pack(side='left')
        self.btn_refresh = ttk.Button(top, text='Refresh / scan', command=self.refresh)
        self.btn_refresh.pack(side='left', padx=8)

        # left: wallet list -------------------------------------------------------
        left = ttk.Frame(self)
        left.pack(side='left', fill='y', padx=(10, 4), pady=8)
        ttk.Label(left, text='my wallets', style='Muted.TLabel').pack(anchor='w')
        self.listbox = tk.Listbox(left, bg=PANEL, fg=FG, selectbackground='#33455e',
                                  font=FONT_M, width=24, height=10, relief='flat',
                                  highlightthickness=0, exportselection=False)
        self.listbox.pack(fill='y', expand=True, pady=4)
        self.listbox.bind('<<ListboxSelect>>', self._on_select)
        btns = ttk.Frame(left)
        btns.pack(fill='x')
        ttk.Button(btns, text='+ New wallet', command=self.new_wallet).pack(fill='x', pady=2)
        ttk.Button(btns, text='Delete', command=self.del_wallet).pack(fill='x', pady=2)
        ttk.Button(btns, text='Release pending', command=self.release_pending).pack(fill='x', pady=2)
        self.auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(btns, text='auto-refresh 5s', variable=self.auto_var).pack(anchor='w', pady=(4, 0))

        # right: details + tabs ------------------------------------------------------
        right = ttk.Frame(self)
        right.pack(side='left', fill='both', expand=True, padx=(4, 10), pady=8)

        det = ttk.Frame(right, style='Panel.TFrame', padding=10)
        det.pack(fill='x')
        self.name_var = tk.StringVar(value='-')
        self.addr_var = tk.StringVar(value='-')
        self.bal_var = tk.StringVar(value='0')
        self.pending_var = tk.StringVar(value='unconfirmed: none')
        row1 = ttk.Frame(det, style='Panel.TFrame')
        row1.pack(fill='x')
        ttk.Label(row1, text='NAME', style='Muted.TLabel').pack(side='left')
        ttk.Label(row1, textvariable=self.name_var, font=FONT_B,
                  background=PANEL, foreground=FG).pack(side='left', padx=8)
        ttk.Label(row1, text='BALANCE', style='Muted.TLabel').pack(side='left', padx=(24, 8))
        ttk.Label(row1, textvariable=self.bal_var, style='Balance.TLabel').pack(side='left')
        row2 = ttk.Frame(det, style='Panel.TFrame')
        row2.pack(fill='x', pady=(8, 0))
        ttk.Label(row2, text='ADDRESS', style='Muted.TLabel').pack(anchor='w')
        arow = ttk.Frame(row2, style='Panel.TFrame')
        arow.pack(fill='x')
        ae = ttk.Entry(arow, textvariable=self.addr_var, font=FONT_M)
        ae.pack(side='left', fill='x', expand=True)
        ttk.Button(arow, text='Copy', width=7, command=self._copy_addr).pack(side='left', padx=(6, 0))
        row3 = ttk.Frame(det, style='Panel.TFrame')
        row3.pack(fill='x', pady=(8, 0))
        ttk.Label(row3, textvariable=self.pending_var, style='Muted.TLabel').pack(side='left')

        nb = ttk.Notebook(right)
        nb.pack(fill='both', expand=True, pady=(8, 0))

        # -- send tab -----------------------------------------------------------------
        send = ttk.Frame(nb)
        nb.add(send, text=' Send ')
        self.recipient_var = tk.StringVar()
        self.amount_var = tk.StringVar(value='1.0')
        self.fee_var = tk.StringVar(value='0.1')
        self.ring_var = tk.StringVar(value='-')
        f = ttk.Frame(send)
        f.pack(fill='x', padx=14, pady=14)
        ttk.Label(f, text='recipient address (stealth address of another wallet)',
                  style='Muted.TLabel').pack(anchor='w')
        ttk.Entry(f, textvariable=self.recipient_var).pack(fill='x', pady=(2, 8))
        row = ttk.Frame(f)
        row.pack(fill='x')
        ttk.Label(row, text='amount (COIN)', style='Muted.TLabel').pack(side='left')
        ttk.Entry(row, textvariable=self.amount_var, width=14).pack(side='left', padx=(4, 18))
        ttk.Label(row, text='fee (COIN)', style='Muted.TLabel').pack(side='left')
        ttk.Entry(row, textvariable=self.fee_var, width=10).pack(side='left', padx=4)
        ttk.Label(f, textvariable=self.ring_var, style='Muted.TLabel').pack(anchor='w', pady=(8, 2))
        self.btn_send = ttk.Button(f, text='SEND CONFIDENTIAL TRANSACTION',
                                   command=self.send)
        self.btn_send.pack(anchor='w', pady=(6, 0))
        ttk.Label(f, text='Note: an on-chain send confirms when the miner finds '
                          'the next block (~1-6 s). For INSTANT transfers use the '
                          'Lightning tab.', style='Muted.TLabel',
                  wraplength=520, justify='left').pack(anchor='w', pady=(8, 0))

        # -- outputs/history tab ---------------------------------------------------------
        hist = ttk.Frame(nb)
        nb.add(hist, text=' Outputs ')
        cols = ('txid', 'index', 'amount', 'status')
        self.tree_out = ttk.Treeview(hist, columns=cols, show='headings')
        for col, w in zip(cols, (170, 60, 140, 100)):
            self.tree_out.heading(col, text=col.upper())
            self.tree_out.column(col, width=w, anchor='w')
        ys = ttk.Scrollbar(hist, orient='vertical', command=self.tree_out.yview)
        self.tree_out.configure(yscrollcommand=ys.set)
        self.tree_out.pack(side='left', fill='both', expand=True, padx=(0, 0))
        ys.pack(side='right', fill='y')

        # -- lightning tab ----------------------------------------------------------
        ln = ttk.Frame(nb)
        nb.add(ln, text=' Lightning (instant) ')
        self.tree_ln = ttk.Treeview(ln, columns=('peer', 'capacity', 'my bal',
                                                 'peer bal', 'seq', 'status'),
                                    show='headings', height=6)
        for col, w in zip(('peer', 'capacity', 'my bal', 'peer bal', 'seq', 'status'),
                          (150, 100, 100, 100, 50, 90)):
            self.tree_ln.heading(col, text=col.upper())
            self.tree_ln.column(col, width=w, anchor='w')
        self.tree_ln.pack(fill='x', padx=8, pady=(8, 2))
        lf = ttk.Frame(ln)
        lf.pack(fill='x', padx=8, pady=6)
        ttk.Label(lf, text='peer address', style='Muted.TLabel').grid(row=0, column=0,
                                                                      sticky='w')
        self.ln_peer_var = tk.StringVar()
        ttk.Entry(lf, textvariable=self.ln_peer_var, width=46).grid(row=1, column=0,
                                                                    sticky='we', pady=2)
        ttk.Label(lf, text='capacity (COIN)', style='Muted.TLabel').grid(row=0, column=1,
                                                                         sticky='w', padx=(12, 0))
        self.ln_cap_var = tk.StringVar(value='50')
        ttk.Entry(lf, textvariable=self.ln_cap_var, width=10).grid(row=1, column=1,
                                                                   sticky='w', padx=(12, 0))
        ttk.Button(lf, text='Open channel', command=self.ln_open).grid(row=1, column=2,
                                                                       padx=(10, 0))
        ttk.Label(lf, text='amount (COIN)', style='Muted.TLabel').grid(row=2, column=0,
                                                                       sticky='w', pady=(8, 0))
        self.ln_amt_var = tk.StringVar(value='1')
        ttk.Entry(lf, textvariable=self.ln_amt_var, width=10).grid(row=3, column=0,
                                                                   sticky='w', pady=2)
        self.btn_ln_pay = ttk.Button(lf, text='PAY INSTANTLY', command=self.ln_send)
        self.btn_ln_pay.grid(row=3, column=1, sticky='w', padx=(12, 0), pady=2)
        self.btn_ln_close = ttk.Button(lf, text='Close channel (settle on-chain)',
                                       command=self.ln_close)
        self.btn_ln_close.grid(row=3, column=2, sticky='w', padx=(10, 0), pady=2)
        lf.columnconfigure(0, weight=1)

        # -- log tab ----------------------------------------------------------------------
        logf = ttk.Frame(nb)
        nb.add(logf, text=' Log ')
        self.log_text = scrolledtext.ScrolledText(logf, bg=PANEL, fg=FG,
                                                  insertbackground=FG, font=FONT_M,
                                                  state='disabled', relief='flat')
        self.log_text.pack(fill='both', expand=True)
        for tag, color in (('ok', ACCENT), ('err', ERR), ('info', '#9fb4d0')):
            self.log_text.tag_configure(tag, foreground=color)

        self.status_var = tk.StringVar(value='idle — make sure node_app.py is running')
        ttk.Label(self, textvariable=self.status_var, style='Muted.TLabel').pack(
            anchor='w', padx=12, pady=(0, 8))
        self.ln_var = tk.StringVar(value='Lightning balance: 0 COIN (in 0 channels)')
        ttk.Label(self, textvariable=self.ln_var, font=('Consolas', 10, 'bold'),
                  background=BG, foreground=ACCENT).pack(anchor='w', padx=12)

    # ── wallet list management ──────────────────────────────────────────────────
    def _load_wallets(self):
        try:
            with open(WALLET_FILE, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
            self.wallets = [core.Wallet.from_json(w) for w in data.get('wallets', [])]
        except FileNotFoundError:
            self.wallets = []
        except Exception as e:                       # noqa: BLE001
            self.wallets = []
            self.put('err', f'could not load {WALLET_FILE}: {e}')
        self._redraw_list()
        if not self.wallets:
            self.put('info', 'no wallets yet — click "+ New wallet" to create one')

    def _save_wallets(self):
        data = {'wallets': [w.to_json() for w in self.wallets]}
        with open(WALLET_FILE, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, indent=1)

    def new_wallet(self):
        base = 'Wallet'
        name = base
        i = 1
        while any(w.name == name for w in self.wallets):
            i += 1
            name = f'{base}{i}'
        w = core.Wallet(name)
        self.wallets.append(w)
        self.selected = len(self.wallets) - 1
        self._save_wallets()
        self._redraw_list()
        self.put('ok', f'wallet "{name}" created — new stealth keys generated')

    def del_wallet(self):
        if not self.wallets:
            return
        w = self.wallets.pop(self.selected)
        self.selected = max(0, min(self.selected, len(self.wallets) - 1))
        self._save_wallets()
        self._redraw_list()
        self.put('info', f'wallet "{w.name}" removed (its outputs stay on-chain, '
                         'but can never be spent again without the keys)')

    def _redraw_list(self):
        self.listbox.delete(0, 'end')
        for w in self.wallets:
            self.listbox.insert('end', f'{w.name}  —  {core.fmt_coin(w.balance())}')
        if self.wallets:
            self.listbox.selection_clear(0, 'end')
            self.listbox.selection_set(self.selected)
            self.listbox.see(self.selected)
        self._show_selected()

    def _on_select(self, _event=None):
        sel = self.listbox.curselection()
        if sel:
            self.selected = sel[0]
        self._show_selected()

    def _show_selected(self):
        if not self.wallets:
            self.name_var.set('-')
            self.addr_var.set('-')
            self.bal_var.set('0')
            self.pending_var.set('unconfirmed: none')
            self.tree_out.delete(*self.tree_out.get_children())
            return
        w = self.wallets[self.selected]
        self.name_var.set(w.name)
        self.addr_var.set(w.address_text)
        self.bal_var.set(core.fmt_coin(w.balance()))
        pin = getattr(w, 'pending_in', 0)
        pout = sum(w.known[op]['v'] for op in w.pending_ops if op in w.known)
        parts = []
        if pin:
            parts.append(f'in {core.fmt_coin(pin)} (waiting for a miner)')
        if pout:
            parts.append(f'out {core.fmt_coin(pout)} (waiting for a miner)')
        self.pending_var.set('unconfirmed: ' + ('; '.join(parts) if parts else 'none'))
        self.tree_out.delete(*self.tree_out.get_children())
        for op, rec in sorted(w.known.items(), key=lambda kv: (kv[0][0], kv[0][1])):
            if op in w.spent_ops:
                status = 'SPENT'
            elif op in w.pending_ops:
                status = 'PENDING OUT'
            else:
                status = 'available'
            self.tree_out.insert('', 'end', values=(
                op[0].hex()[:24] + '...', op[1], core.fmt_coin(rec['v']), status))

    def release_pending(self):
        if not self.wallets:
            return
        w = self.wallets[self.selected]
        n = w.release_all_pending()
        self._save_wallets()
        self._redraw_list()
        self.put('info', f'"{w.name}": released {n} unconfirmed transaction '
                         'reservation(s) — use only if a tx was lost (node restarted '
                         'before it was mined)')

    def _auto_tick(self):
        """Periodically rescan so confirmed/pending balances stay fresh."""
        if self.auto_var.get() and not self._busy and self.wallets:
            self.refresh()
        self.after(5000, self._auto_tick)

    def _copy_addr(self):
        if self.wallets:
            self.clipboard_clear()
            self.clipboard_append(self.wallets[self.selected].address_text)
            self.put('info', 'address copied to clipboard')

    # ── queue → UI ───────────────────────────────────────────────────────────────
    def put(self, kind: str, msg: str):
        self.q.put((kind, msg))

    def _poll(self):
        try:
            self._ln_url = self.url_var.get().strip()   # plain attr for the ln worker
            while True:
                kind, msg = self.q.get_nowait()
                if msg == '!ln':
                    continue
                if msg == '!ln_done':
                    self._busy = False
                    self.btn_send.config(state='normal')
                    self.status_var.set('ready')
                    continue
                self.log_text.config(state='normal')
                self.log_text.insert('end', f'[{time.strftime("%H:%M:%S")}] {msg}\n', kind)
                self.log_text.see('end')
                self.log_text.config(state='disabled')
                if msg.startswith('!sent'):
                    self._redraw_list()
                    self.status_var.set('tx in mempool — the miner will confirm it '
                                        'in the next block(s)')
                    self._busy = False
                    self.btn_send.config(state='normal')
                    if self.auto_var.get():
                        self.after(1000, self.refresh)   # see the PENDING state
                if msg.startswith('!refresh'):
                    self._redraw_list()
                    self.status_var.set('scan complete')
                    self._busy = False
        except queue.Empty:
            pass
        self._redraw_ln()
        self.after(150, self._poll)

    # ── Lightning UI ────────────────────────────────────────────────────────────
    def _redraw_ln(self):
        w = self.wallets[self.selected] if self.wallets else None
        self.tree_ln.delete(*self.tree_ln.get_children())
        self._ln_order = []
        if w is not None:
            for ch_id, rec in w.channels.items():
                self._ln_order.append(ch_id)
                peer = rec['peer_address'][:22] + '...'
                self.tree_ln.insert('', 'end', values=(
                    peer, core.fmt_coin(rec['capacity']), core.fmt_coin(rec['bal_self']),
                    core.fmt_coin(rec['bal_peer']), rec['seq'], rec['status']))
        total = w.ln_total() if w is not None else 0
        n = len(w.channels) if w is not None else 0
        self.ln_var.set(f'Lightning balance: {core.fmt_coin(total)} COIN '
                        f'(in {n} channel(s)) — payments here are INSTANT')

    def _selected_channel(self) -> Optional[bytes]:
        sel = self.tree_ln.selection()
        if not sel or not self.wallets:
            return None
        idx = self.tree_ln.index(sel[0])
        if 0 <= idx < len(self._ln_order):
            return self._ln_order[idx]
        return None

    def ln_open(self):
        if self._busy or not self.wallets:
            return
        w = self.wallets[self.selected]
        try:
            core.parse_address(self.ln_peer_var.get())
            capacity = core.parse_coin(self.ln_cap_var.get())
            if capacity <= 0:
                raise ValueError('capacity must be positive')
            fee = 100_000
            if w.balance() < capacity + fee:
                raise ValueError(f'on-chain balance too low '
                                 f'({core.fmt_coin(w.balance())} available)')
        except ValueError as e:
            self.put('err', f'invalid input: {e}')
            return
        self._busy = True
        self.status_var.set('building channel funding transaction...')
        url = self.url_var.get().strip()
        threading.Thread(target=self._ln_open_worker,
                         args=(w, self.ln_peer_var.get().strip(), capacity, fee, url),
                         daemon=True).start()

    def _ln_open_worker(self, w, peer_addr, capacity, fee, url):
        try:
            client = core.NodeClient(url)
            view = self._fetch_view(client)
            w.scan(view)
            res = w.ln_open_channel(view, client, peer_addr, capacity, fee)
            self.put('ok', f'channel funding tx {res["txid"][:12]}... in mempool — '
                           'mine 1 block to OPEN the channel (Lightning payments '
                           'after that are instant)')
        except core.NodeError as e:
            self.put('err', f'node rejected the channel: {e}')
        except ValueError as e:
            self.put('err', f'could not open channel: {e}')
        except Exception as e:                       # noqa: BLE001
            self.put('err', f'open channel failed: {e}')
        finally:
            self.q.put(('info', '!ln_done'))

    def ln_send(self):
        if not self.wallets:
            return
        w = self.wallets[self.selected]
        ch_id = self._selected_channel()
        if ch_id is None:
            self.put('err', 'select a channel in the table first')
            return
        try:
            amount = core.parse_coin(self.ln_amt_var.get())
        except ValueError as e:
            self.put('err', f'invalid amount: {e}')
            return
        try:
            w.ln_pay(core.NodeClient(self._ln_url, timeout=8), ch_id, amount)
            self.put('ok', f'LIGHTNING INSTANT: {core.fmt_coin(amount)} sent '
                           '-- no miner involved')
        except (core.NodeError, ValueError) as e:
            self.put('err', f'lightning payment failed: {e}')

    def ln_close(self):
        if not self.wallets:
            return
        w = self.wallets[self.selected]
        ch_id = self._selected_channel()
        if ch_id is None:
            self.put('err', 'select a channel first')
            return
        try:
            w.ln_close_request(core.NodeClient(self._ln_url, timeout=8), ch_id)
            self.put('info', 'close requested — waiting for the peer co-signature, '
                             'then the settlement tx is mined')
        except (core.NodeError, ValueError) as e:
            self.put('err', f'close failed: {e}')

    def _ln_worker(self):
        """Background Lightning housekeeping: inbox, funding confirmations,
        close co-signature pickup. Runs every ~1.2 s -> payments feel instant."""
        while True:
            time.sleep(1.2)
            url = getattr(self, '_ln_url', '')
            if not url:
                continue
            try:
                client = core.NodeClient(url, timeout=8)
                for w in list(self.wallets):
                    logs = w.ln_tick(client, w.address_text)
                    for line in logs:
                        if line.startswith('!close_ready:'):
                            ch_id = bytes.fromhex(line.split(':', 1)[1])
                            try:
                                view = self._fetch_view(client)
                                tx = w.ln_build_close_tx(view, client, ch_id)
                                res = client.submit_tx(core.tx_json(tx))
                                w.channels[ch_id]['closing_txid'] = \
                                    bytes.fromhex(res['txid'])
                                self.put('ok', f'channel settlement tx '
                                               f'{res["txid"][:12]}... submitted — '
                                               'mine 1 block to finalize')
                            except Exception as e:   # noqa: BLE001
                                self.put('err', f'settlement failed: {e}')
                        elif 'INSTANT' in line or 'OPEN' in line:
                            self.put('ok', line)
                        else:
                            self.put('info', line)
                self.q.put(('info', '!ln'))
            except core.NodeError:
                pass
            except Exception:                        # noqa: BLE001
                pass

    # ── chain view over HTTP ─────────────────────────────────────────────────────
    def _fetch_view(self, client) -> core.ChainView:
        data = client.chain_full()
        blocks = [core.block_load(b) for b in data['blocks']]
        outputs = {}
        for o in client.outputs():
            op = (bytes.fromhex(o['txid']), int(o['index']))
            outputs[op] = core.OutputRec(core.point_load(o['dest']),
                                         core.point_load(o['commitment']),
                                         bool(o['is_coinbase']))
        return core.ChainView(blocks, outputs)

    # ── refresh / scan ───────────────────────────────────────────────────────────
    def refresh(self):
        if self._busy:
            return
        self._busy = True
        self.status_var.set('scanning chain with view keys...')
        url = self.url_var.get().strip()          # capture before the thread starts
        threading.Thread(target=self._scan_worker, args=(url,), daemon=True).start()

    def _scan_worker(self, url: str):
        try:
            client = core.NodeClient(url)
            view = self._fetch_view(client)
            # transactions waiting in the mempool (unconfirmed)
            pending = [core.tx_load(t) for t in client.mempool_full()['txs']]
            onchain = {core.tx_txid(tx) for blk in view.blocks for tx in blk.transactions}
            for w in self.wallets:
                w.confirm_pending(onchain)          # mined pending -> real spends
                found = w.scan(view)
                w.pending_in = w.scan_pending_incoming(pending)   # unconfirmed incoming
                if found:
                    self.put('ok', f'"{w.name}": {found} new output(s) recognised '
                                   f'— balance {core.fmt_coin(w.balance())}')
                elif getattr(w, 'pending_in', 0):
                    self.put('info', f'"{w.name}": {core.fmt_coin(w.pending_in)} '
                                     'UNCONFIRMED incoming (waiting for a miner)')
            self.put('info', f'!refresh scanned {len(view.blocks)} blocks, '
                             f'{len(view.outputs)} outputs, {len(pending)} mempool tx(s)')
        except core.NodeError as e:
            self.put('err', str(e))
            self.put('info', '!refresh')
        except Exception as e:                       # noqa: BLE001
            self.put('err', f'scan failed: {e}')
            self.put('info', '!refresh')

    # ── send ─────────────────────────────────────────────────────────────────────
    def send(self):
        if self._busy:
            return
        if not self.wallets:
            self.put('err', 'create a wallet first')
            return
        w = self.wallets[self.selected]
        try:
            A, B = core.parse_address(self.recipient_var.get())
            amount = core.parse_coin(self.amount_var.get())
            fee = core.parse_coin(self.fee_var.get())
            if amount <= 0:
                raise ValueError('amount must be positive')
            if fee < core.MIN_FEE:
                raise ValueError(f'fee must be at least {core.MIN_FEE} units '
                                 f'(0.001 COIN)')
        except ValueError as e:
            self.put('err', f'invalid input: {e}')
            return
        if w.balance() < amount + fee:
            self.put('err', f'insufficient balance ({core.fmt_coin(w.balance())} available; '
                            f'need {core.fmt_coin(amount + fee)} including fee)')
            return
        self._busy = True
        self.status_var.set('building transaction (range proofs + ring signatures)...')
        self.btn_send.config(state='disabled')
        url = self.url_var.get().strip()          # capture before the thread starts
        threading.Thread(target=self._send_worker,
                         args=(w, (A, B), amount, fee, url), daemon=True).start()

    def _send_worker(self, w, recipient, amount, fee, url):
        tx = None
        try:
            client = core.NodeClient(url)
            view = self._fetch_view(client)
            w.scan(view)
            tx = w.build_tx(view, [(recipient, amount)], fee, consume=False)
            ring_sizes = [len(ti.ring) for ti in tx.inputs]
            self.put('info', f'tx built: {len(tx.inputs)} input(s), ring sizes {ring_sizes}, '
                             f'{len(tx.outputs)} output(s), fee {core.fmt_coin(fee)}')
            res = client.submit_tx(core.tx_json(tx))
            # reserve the inputs as UNCONFIRMED until the tx is mined
            w.supersede_pending(tx.used_ops)
            w.register_pending(bytes.fromhex(res['txid']), tx.used_ops)
            self.put('ok', f'tx {res["txid"][:16]}... accepted into MEMPOOL — '
                           f'sent {core.fmt_coin(amount)}')
            self.put('info', 'IMPORTANT: unconfirmed until a miner includes it in a '
                             'block — start mining in miner_app, then Refresh here')
            self.put('info', '!sent')
        except core.NodeError as e:
            self.put('err', f'tx rejected by node: {e}')
            self.put('info', '!sent')
        except ValueError as e:
            self.put('err', f'could not build transaction: {e}')
            self.put('info', '!sent')
        except Exception as e:                       # noqa: BLE001
            self.put('err', f'send failed: {e}')
            self.put('info', '!sent')

    def on_close(self):
        try:
            self._save_wallets()
        except Exception:                            # noqa: BLE001
            pass
        self.destroy()


if __name__ == '__main__':
    app = WalletApp()
    app.protocol('WM_DELETE_WINDOW', app.on_close)
    app.mainloop()
