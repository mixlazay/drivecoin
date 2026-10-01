#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
node_app.py — PrivateChain BLOCKCHAIN NODE (with UI)
================================================================================
Runs the ledger: chain, mempool, validation, persistence, and a local REST API
that the Miner and Wallet programs talk to.

    python node_app.py            → UI opens, press "Start Node"

API endpoints (127.0.0.1 only):
    GET  /api/info                 node/chain summary
    GET  /api/chain[?full=1]       block summaries / full blocks (for scanning)
    GET  /api/outputs              every output ever created (ring/decoy pool)
    GET  /api/mempool              pending transactions
    POST /api/tx                   submit a signed transaction
    POST /api/template             request a block template (miner)
    POST /api/submitblock          submit a mined block (miner)
================================================================================
"""
import json
import os
import sys
import threading
import time
import tkinter as tk
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tkinter import ttk, scrolledtext

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

SERVICE: core.NodeService = None          # set once the server starts


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get('Content-Length', 0))
        return json.loads(self.rfile.read(length).decode()) if length else {}

    def do_GET(self):
        path = self.path.split('?')[0]
        try:
            if path == '/api/info':
                self._send(200, SERVICE.info())
            elif path == '/api/chain':
                full = 'full=1' in self.path
                self._send(200, SERVICE.full_chain() if full
                           else {'blocks': SERVICE.blocks_summary()})
            elif path == '/api/outputs':
                self._send(200, SERVICE.outputs_list())
            elif path == '/api/mempool':
                full = 'full=1' in self.path
                self._send(200, SERVICE.mempool_full() if full
                           else SERVICE.mempool_summary())
            elif path == '/api/inbox':
                self._send(200, SERVICE.inbox_pop(
                    self.path.split('address=')[-1].split('&')[0]))
            elif path == '/api/ln/state':
                self._send(200, SERVICE.ln_state(
                    self.path.split('ch=')[-1].split('&')[0]))
            elif path == '/api/ln/close-status':
                self._send(200, SERVICE.ln_close_status(
                    self.path.split('ch=')[-1].split('&')[0]))
            elif path.startswith('/api/blocks/'):
                parts = path[len('/api/blocks/'):].split('/')
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    self._send(200, SERVICE.blocks_range(int(parts[0]),
                                                         int(parts[1])))
                else:
                    self._send(400, {'error': 'expected /api/blocks/<from>/<count>'})
            elif path == '/api/p2p/mempool':
                self._send(200, SERVICE.mempool_full())
            elif path == '/api/peers':
                self._send(200, SERVICE.peers_status())
            else:
                self._send(404, {'error': 'unknown endpoint'})
        except core.NodeError as e:
            self._send(400, {'error': str(e)})
        except Exception as e:                       # noqa: BLE001
            self._send(500, {'error': f'internal error: {e}'})

    def do_POST(self):
        path = self.path.split('?')[0]
        try:
            body = self._body()
            if path == '/api/tx':
                self._send(200, SERVICE.submit_tx(body))
            elif path == '/api/p2p/tx':
                self._send(200, SERVICE.receive_peer_tx(body.get('tx', {})))
            elif path == '/api/p2p/block':
                self._send(200, SERVICE.accept_external_block(
                    body.get('block', {})))
            elif path == '/api/peers':
                self._send(200, SERVICE.add_peer(body.get('url', '')))
            elif path == '/api/template':
                self._send(200, SERVICE.block_template(body.get('address', '')))
            elif path == '/api/submitblock':
                self._send(200, SERVICE.submit_block(body.get('template_id', ''),
                                                     body.get('nonce', 0)))
            elif path == '/api/inbox/send':
                self._send(200, SERVICE.inbox_send(body.get('to', ''),
                                                   body.get('msg', {})))
            elif path == '/api/ln/pay':
                self._send(200, SERVICE.ln_pay(body))
            elif path == '/api/ln/confirm':
                self._send(200, SERVICE.ln_confirm(body))
            elif path == '/api/ln/close-req':
                self._send(200, SERVICE.ln_close_req(body))
            elif path == '/api/ln/close-sig':
                self._send(200, SERVICE.ln_close_sig(body))
            else:
                self._send(404, {'error': 'unknown endpoint'})
        except core.NodeError as e:
            self._send(400, {'error': str(e)})
        except Exception as e:                       # noqa: BLE001
            self._send(500, {'error': f'internal error: {e}'})

    def log_message(self, *args):                    # silence default stderr log
        pass


class NodeApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('PrivateChain - Blockchain Node')
        self.geometry('1060x640')
        self.configure(bg=BG)
        self.httpd = None
        self._log_seq = 0
        self._build_style()
        self._build_ui()
        self.after(800, self._poll)

    # ── UI construction ────────────────────────────────────────────────────────
    def _build_style(self):
        st = ttk.Style(self)
        st.theme_use('clam')
        st.configure('.', background=BG, foreground=FG, font=FONT)
        st.configure('TFrame', background=BG)
        st.configure('Panel.TFrame', background=PANEL)
        st.configure('TLabel', background=BG, foreground=FG)
        st.configure('Muted.TLabel', background=BG, foreground=MUTED, font=FONT_M)
        st.configure('Stat.TLabel', background=PANEL, foreground=ACCENT,
                     font=('Consolas', 11, 'bold'))
        st.configure('TButton', background='#2a2f3a', foreground=FG, padding=6)
        st.map('TButton', background=[('active', '#39404e')])
        st.configure('TNotebook', background=BG, borderwidth=0)
        st.configure('TNotebook.Tab', background=PANEL, foreground=MUTED, padding=(14, 6))
        st.map('TNotebook.Tab', background=[('selected', '#2a2f3a')],
               foreground=[('selected', ACCENT)])
        st.configure('Treeview', background=PANEL, foreground=FG, fieldbackground=PANEL,
                     rowheight=22, font=FONT_M)
        st.configure('Treeview.Heading', background='#2a2f3a', foreground=FG, font=FONT)
        st.map('Treeview', background=[('selected', '#33455e')])
        st.configure('TEntry', fieldbackground=PANEL, foreground=FG, insertcolor=FG)

    def _build_ui(self):
        # top bar ------------------------------------------------------------
        top = ttk.Frame(self)
        top.pack(fill='x', padx=10, pady=(10, 4))
        ttk.Label(top, text='BLOCKCHAIN NODE', font=('Consolas', 13, 'bold'),
                  foreground=ACCENT, background=BG).pack(side='left')
        ttk.Label(top, text='  bind 127.0.0.1  port', style='Muted.TLabel').pack(side='left', padx=(18, 4))
        self.port_var = tk.StringVar(value='8000')
        ttk.Entry(top, textvariable=self.port_var, width=7).pack(side='left')
        self.btn_start = ttk.Button(top, text='Start Node', command=self.start_server)
        self.btn_start.pack(side='left', padx=8)
        self.btn_save = ttk.Button(top, text='Save chain', command=self._save, state='disabled')
        self.btn_save.pack(side='left', padx=2)
        self.btn_reset_d = ttk.Button(top, text='Reset difficulty', command=self._reset_difficulty,
                                      state='disabled')
        self.btn_reset_d.pack(side='left', padx=2)
        self.btn_test = ttk.Button(top, text='Run self-test', command=self._self_test)
        self.btn_test.pack(side='left', padx=2)
        self.status_var = tk.StringVar(value='server stopped — chain data: node_chain.json')
        ttk.Label(self, textvariable=self.status_var, style='Muted.TLabel').pack(anchor='w', padx=12)

        # stats strip ----------------------------------------------------------
        stats = ttk.Frame(self)
        stats.pack(fill='x', padx=10, pady=6)
        self.stat_vars = {}
        for key, label, width in (('height', 'HEIGHT', 12),
                                  ('tip_hash', 'TIP', 18),
                                  ('difficulty', 'NEXT DIFFICULTY', 16),
                                  ('mempool', 'MEMPOOL', 10),
                                  ('outputs', 'OUTPUTS', 10),
                                  ('spent_images', 'SPENT IMAGES', 12),
                                  ('emission', 'EMISSION / 50M CAP', 22),
                                  ('next_subsidy', 'NEXT REWARD', 14)):
            box = ttk.Frame(stats, style='Panel.TFrame', padding=(10, 6))
            box.pack(side='left', padx=4)
            ttk.Label(box, text=label, style='Muted.TLabel').pack(anchor='w')
            var = tk.StringVar(value='-')
            ttk.Label(box, textvariable=var, style='Stat.TLabel', width=width).pack(anchor='w')
            self.stat_vars[key] = var

        # tabs -------------------------------------------------------------------
        nb = ttk.Notebook(self)
        nb.pack(fill='both', expand=True, padx=10, pady=6)
        self.tree_blocks = self._tree(nb, ('height', 'hash', 'txs', 'difficulty', 'time', 'reward'),
                                      (60, 170, 50, 110, 90, 110))
        self.tree_mempool = self._tree(nb, ('txid', 'fee', 'inputs', 'rings', 'outputs'),
                                       (170, 90, 70, 90, 80))
        log_frame = ttk.Frame(nb)
        self.log_text = scrolledtext.ScrolledText(log_frame, bg=PANEL, fg=FG,
                                                  insertbackground=FG, font=FONT_M,
                                                  state='disabled', relief='flat')
        self.log_text.pack(fill='both', expand=True)
        for tag, color in (('ok', ACCENT), ('err', ERR), ('info', '#9fb4d0')):
            self.log_text.tag_configure(tag, foreground=color)
        nb.add(log_frame, text=' Event log ')

        # P2P peers tab ----------------------------------------------------
        peers = ttk.Frame(nb)
        row = ttk.Frame(peers)
        row.pack(fill='x', padx=8, pady=8)
        ttk.Label(row, text='peer URL:', style='Muted.TLabel').pack(side='left')
        self.peer_var = tk.StringVar(
            value='https://scan.drivecoinproject.online')
        ttk.Entry(row, textvariable=self.peer_var, width=42).pack(side='left', padx=6)
        ttk.Button(row, text='Add peer / sync', command=self._add_peer).pack(side='left')
        ttk.Button(row, text='Remove selected', command=self._remove_peer).pack(side='left', padx=4)
        self.tree_peers = ttk.Treeview(peers, columns=('url', 'status', 'height'),
                                       show='headings', height=6)
        for col, w, txt in (('url', 320, 'URL'), ('status', 180, 'STATUS'),
                            ('height', 80, 'HEIGHT')):
            self.tree_peers.heading(col, text=txt)
            self.tree_peers.column(col, width=w, anchor='w')
        self.tree_peers.pack(fill='both', expand=True, padx=8, pady=(0, 8))
        ttk.Label(peers, text='a fresh node (own genesis only) JOINS the peer\'s network; '
                              'after that blocks/txs stay in sync both ways',
                  style='Muted.TLabel').pack(anchor='w', padx=8, pady=(0, 8))
        nb.add(peers, text=' P2P peers ')

    def _tree(self, nb, columns, widths):
        frame = ttk.Frame(nb)
        tree = ttk.Treeview(frame, columns=columns, show='headings')
        for col, w in zip(columns, widths):
            tree.heading(col, text=col.upper())
            tree.column(col, width=w, anchor='w')
        ys = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=ys.set)
        tree.pack(side='left', fill='both', expand=True)
        ys.pack(side='right', fill='y')
        nb.add(frame, text=f' {columns[0].capitalize()}s ')
        return tree

    # ── server lifecycle ───────────────────────────────────────────────────────
    def start_server(self):
        global SERVICE
        if self.httpd is not None:
            return
        try:
            port = int(self.port_var.get())
        except ValueError:
            self.status_var.set('invalid port')
            return
        # NODE_DATAFILE lets tests run against an isolated chain file.
        # Default: next to the executable (frozen-aware — __file__ would point
        # into PyInstaller's temp dir which is deleted on exit).
        if getattr(sys, 'frozen', False):
            default_data = os.path.join(
                os.path.dirname(os.path.abspath(sys.executable)),
                'node_chain.json')
        else:
            default_data = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), 'node_chain.json')
        datafile = os.environ.get('NODE_DATAFILE', default_data)
        SERVICE = core.NodeService(datafile)
        try:
            msg = SERVICE.start()
        except Exception as e:                       # noqa: BLE001
            self.status_var.set(f'failed to init chain: {e}')
            return
        try:
            self.httpd = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        except OSError as e:
            self.status_var.set(f'cannot bind port {port}: {e}')
            self.httpd = None
            return
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.btn_start.config(state='disabled')
        self.btn_save.config(state='normal')
        self.btn_reset_d.config(state='normal')
        self.status_var.set(f'{msg} — API running at http://127.0.0.1:{port}')
        self._append_log('ok', msg)
        self._append_log('info', f'REST API listening on http://127.0.0.1:{port} '
                                 '(miner_app and wallet_app connect here)')

    def _save(self):
        with SERVICE.lock:
            SERVICE._save()
        self._append_log('info', 'chain saved to node_chain.json')

    def _reset_difficulty(self):
        """Escape hatch: if idle time ramped the difficulty up (blocks feel
        slow), snap the NEXT block's difficulty back to the initial value."""
        with SERVICE.lock:
            tip = SERVICE.chain.blocks[-1]
            tip.difficulty = SERVICE.chain.initial_difficulty
            SERVICE._save()
            SERVICE.log('info', f'difficulty reset to {tip.difficulty:,} '
                                '(applies to the next block)')
        self._append_log('info', 'difficulty reset')

    def _self_test(self):
        def work():
            try:
                msg = core._self_test()
                SERVICE.log('ok', msg)
            except Exception as e:                   # noqa: BLE001
                SERVICE.log('err', f'self-test FAILED: {e}')
        threading.Thread(target=work, daemon=True).start()

    # ── polling ────────────────────────────────────────────────────────────────
    def _append_log(self, kind: str, msg: str):
        self.log_text.config(state='normal')
        self.log_text.insert('end', f'[{time.strftime("%H:%M:%S")}] {msg}\n', kind)
        self.log_text.see('end')
        self.log_text.config(state='disabled')

    def _poll(self):
        try:
            if self.httpd is not None:
                for ev in SERVICE.events_since(self._log_seq):
                    self._log_seq = ev['seq']
                    self._append_log(ev['kind'], ev['msg'])
                info = SERVICE.info()
                self.stat_vars['height'].set(str(info['height']))
                self.stat_vars['tip_hash'].set(info['tip_hash'][:18] + '...')
                self.stat_vars['difficulty'].set(f"{info['difficulty']:,}")
                self.stat_vars['mempool'].set(str(info['mempool']))
                self.stat_vars['outputs'].set(str(info['outputs']))
                self.stat_vars['spent_images'].set(str(info['spent_images']))
                self.stat_vars['emission'].set(
                    f"{info['emission'] / core.COIN:,.1f} / "
                    f"{info['max_supply'] // core.COIN:,} COIN")
                self.stat_vars['next_subsidy'].set(core.fmt_coin(info['next_subsidy']))
                # refresh tables
                rows = SERVICE.blocks_summary()
                self.tree_blocks.delete(*self.tree_blocks.get_children())
                for r in rows:
                    self.tree_blocks.insert('', 'end', values=(
                        r['height'], r['hash'][:20] + '...', r['txs'],
                        f"{r['difficulty']:,}",
                        time.strftime('%H:%M:%S', time.localtime(r['timestamp'])),
                        core.fmt_coin(r['reward'])))
                mrows = SERVICE.mempool_summary()
                self.tree_mempool.delete(*self.tree_mempool.get_children())
                for r in mrows:
                    self.tree_mempool.insert('', 'end', values=(
                        r['txid'][:20] + '...', core.fmt_coin(r['fee']), r['inputs'],
                        str(r['rings']), r['outputs']))
                # refresh P2P peer table
                prows = SERVICE.peers_status()
                self.tree_peers.delete(*self.tree_peers.get_children())
                for r in prows:
                    self.tree_peers.insert('', 'end', values=(
                        r['url'], r['status'], r['height']))
        except Exception:                            # noqa: BLE001
            pass
        self.after(800, self._poll)

    # ── P2P actions ────────────────────────────────────────────────────────────
    def _add_peer(self):
        if SERVICE is None:
            self._append_log('err', 'start the node first')
            return
        url = self.peer_var.get().strip()
        try:
            SERVICE.add_peer(url)
        except core.NodeError as e:
            self._append_log('err', f'bad peer: {e}')

    def _remove_peer(self):
        if SERVICE is None:
            return
        sel = self.tree_peers.selection()
        if not sel:
            return
        url = self.tree_peers.item(sel[0])['values'][0]
        with SERVICE.lock:
            if url in SERVICE.peers:
                SERVICE.peers.remove(url)
                SERVICE.peer_status.pop(url, None)
                SERVICE._save()
        self._append_log('info', f'peer removed: {url}')

    def on_close(self):
        if self.httpd is not None:
            try:
                with SERVICE.lock:
                    SERVICE._save()
                self.httpd.shutdown()
            except Exception:                        # noqa: BLE001
                pass
        self.destroy()


if __name__ == '__main__':
    app = NodeApp()
    app.protocol('WM_DELETE_WINDOW', app.on_close)
    app.mainloop()
