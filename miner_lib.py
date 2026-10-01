#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
miner_lib.py — shared engine for the standalone DriveCoin miners (CLI + UI)
================================================================================
Pure Python 3.8+ standard library (wallet mode additionally needs core.py
next to this file). Talks to a remote node over HTTPS:

    POST /api/template      {"address": ...}  -> template_id, prefix_hex, difficulty
    grind   sha256d(prefix + be64(nonce)) < (2^256-1)//difficulty   [multiprocess]
    POST /api/submitblock   {"template_id", "nonce"}
    GET  /api/scan          compact chain view -> local view-key wallet scan

Grinding uses real worker PROCESSES (the GIL would serialise hashing in
threads); each worker owns a nonce residue class so they never overlap.
Workers survive across templates: the controller publishes the current
"round" (prefix + target) in shared memory and workers re-arm on change.
================================================================================
"""
import base64
import json
import multiprocessing
import os
import queue as _queue
import secrets
import struct
import threading
import time
import urllib.error
import urllib.request

VERSION = '1.0'
DEFAULT_NODE = 'https://scan.drivecoinproject.online'
USER_AGENT = f'DriveCoinMiner/{VERSION} (+https://drivecoinproject.online)'

TEMPLATE_TIMEOUT = 900.0     # abandon a template and refresh after this
TIP_POLL_SECS = 5.0          # while mining, poll chain height this often
PREFIX_CAP = 128             # shared prefix buffer (real prefix is 84 bytes)


class MinerError(Exception):
    pass


# ── HTTP ──────────────────────────────────────────────────────────────────────
def http_json(base: str, method: str, path: str, body=None, timeout: float = 30.0,
              user_agent: str = USER_AGENT) -> dict:
    """JSON POST/GET to the node. Cloudflare rejects the default Python
    User-Agent, so always send our own."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base.rstrip('/') + path, data=data, method=method,
        headers={'Content-Type': 'application/json',
                 'User-Agent': user_agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get('error', f'HTTP {e.code}')
        except Exception:                                   # noqa: BLE001
            msg = f'HTTP {e.code}'
        raise MinerError(msg) from None
    except urllib.error.URLError as e:
        raise MinerError(f'cannot reach node ({e.reason})') from None
    except OSError as e:
        raise MinerError(f'cannot reach node ({e})') from None


# ── grinding worker (runs in its own process) ─────────────────────────────────
def grind_worker(worker_id: int, nworkers: int, round_v, prefix_a, plen_v,
                 target_a, counts_a, result_q):
    """Grind nonces for the current round. Protocol:
      round_v:  0 = idle, -1 = shutdown, >0 = round id
      prefix/target written by the controller BEFORE round_v is bumped.
    Worker `i` only grinds nonces ≡ i (mod nworkers)."""
    import hashlib
    sha = hashlib.sha256
    pack = struct.Struct('>Q').pack
    local_round = 0
    prefix = b''
    target = b'\xff' * 32
    nonce = 0
    step = max(1, nworkers)
    while True:
        r = round_v.value
        if r < 0:
            return
        if r == 0:
            time.sleep(0.05)
            local_round = 0
            continue
        if r != local_round:
            local_round = r
            n = plen_v.value
            with prefix_a.get_lock():
                prefix = bytes(prefix_a[:n])
            with target_a.get_lock():
                target = bytes(target_a[:32])
            # random base inside this worker's residue class
            nonce = (secrets.randbits(44) // step) * step + worker_id
        # grind one batch of 256 nonces, then re-check the round
        n0 = nonce
        found = None
        for _ in range(256):
            h = sha(sha(prefix + pack(nonce)).digest()).digest()
            if h < target:
                found = (nonce, h)
                break
            nonce += step
        counts_a[worker_id] += nonce - n0
        if found is not None:
            result_q.put((local_round, found[0], found[1].hex()))
            # hold until the controller reacts (round changes or shutdown)
            while round_v.value == local_round and round_v.value >= 0:
                time.sleep(0.02)
            continue


# ── engine ────────────────────────────────────────────────────────────────────
class MinerEngine:
    """Controller: fetch templates, publish rounds to the worker pool,
    submit solved blocks, keep stats. Runs its own thread; `on_event`
    callbacks fire from that thread (UIs must marshal to their own thread)."""

    def request_stop(self):
        """Ask the controller to stop WITHOUT joining — safe to call from
        inside an on_event callback (engine.stop() would deadlock there)."""
        self._stop.set()

    def __init__(self, node_url: str, address: str, threads: int = 1,
                 on_event=None, user_agent: str = USER_AGENT):
        self.node = node_url.strip()
        if '://' not in self.node:
            self.node = 'https://' + self.node
        self.address = address.strip()
        self.threads = max(1, min(64, int(threads)))
        self.on_event = on_event
        self.user_agent = user_agent

        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._stats = {'status': 'idle', 'hashrate': 0.0, 'total_hashes': 0,
                       'blocks_found': 0, 'total_reward': 0, 'height': 0,
                       'difficulty': 0, 'template_height': 0,
                       'template_reward': 0, 'last_error': ''}
        self._rate_prev = (time.monotonic(), 0)

        ctx = multiprocessing.get_context('spawn')
        self._round_v = ctx.Value('q', 0)
        self._prefix_a = ctx.Array('B', PREFIX_CAP)
        self._plen_v = ctx.Value('i', 0)
        self._target_a = ctx.Array('B', 32)
        self._counts_a = ctx.Array('q', self.threads)
        self._result_q = ctx.Queue()
        self._workers = []
        self._controller = None

    # ---- stats -----------------------------------------------------------
    def get_stats(self) -> dict:
        total = sum(self._counts_a)
        with self._lock:
            now = time.monotonic()
            prev_t, prev_h = self._rate_prev
            dt = now - prev_t
            rate = (total - prev_h) / dt if dt > 0.25 else self._stats['hashrate']
            if dt > 0.25:
                self._rate_prev = (now, total)
            self._stats['total_hashes'] = total
            self._stats['hashrate'] = rate
            return dict(self._stats)

    def _set(self, **kw):
        with self._lock:
            self._stats.update(kw)

    def _event(self, kind: str, data=None):
        if self.on_event:
            try:
                self.on_event(kind, data or {})
            except Exception:                                # noqa: BLE001
                pass

    # ---- lifecycle ---------------------------------------------------------
    def start(self):
        if self._controller:
            raise MinerError('engine already started')
        for wid in range(self.threads):
            p = multiprocessing.get_context('spawn').Process(
                target=grind_worker,
                args=(wid, self.threads, self._round_v, self._prefix_a,
                      self._plen_v, self._target_a, self._counts_a,
                      self._result_q),
                daemon=True)
            p.start()
            self._workers.append(p)
        self._controller = threading.Thread(target=self._controller_loop,
                                            daemon=True)
        self._controller.start()

    def stop(self):
        self._stop.set()
        self._round_v.value = -1
        try:
            self._result_q.close()
        except Exception:                                    # noqa: BLE001
            pass
        for p in self._workers:
            p.join(timeout=3)
        for p in self._workers:
            if p.is_alive():
                p.terminate()
        if self._controller:
            self._controller.join(timeout=3)
        self._set(status='stopped')

    # ---- controller --------------------------------------------------------
    def _controller_loop(self):
        round_id = 0
        self._set(status='starting')
        self._event('status')
        while not self._stop.is_set():
            # 1. fetch a fresh template
            self._set(status='requesting template')
            try:
                tpl = http_json(self.node, 'POST', '/api/template',
                                {'address': self.address}, timeout=30,
                                user_agent=self.user_agent)
            except MinerError as e:
                self._set(status='error', last_error=str(e))
                self._event('error', {'error': str(e)})
                time.sleep(3)
                continue
            try:
                prefix = bytes.fromhex(tpl['prefix_hex'])
                difficulty = int(tpl['difficulty'])
            except Exception as e:                            # noqa: BLE001
                self._set(status='error', last_error=f'bad template: {e}')
                time.sleep(3)
                continue
            target = (((1 << 256) - 1) // max(1, difficulty)).to_bytes(32, 'big')

            # 2. publish the round (data first, round id LAST)
            round_id += 1
            with self._prefix_a.get_lock():
                self._prefix_a[:len(prefix)] = prefix
            self._plen_v.value = len(prefix)
            with self._target_a.get_lock():
                self._target_a[:] = target
            self._round_v.value = round_id
            self._set(status='mining', difficulty=difficulty,
                      template_height=int(tpl['height']),
                      template_reward=int(tpl['reward']))
            self._event('template', tpl)

            # 3. wait for a solve / stale tip / timeout
            deadline = time.monotonic() + TEMPLATE_TIMEOUT
            next_poll = time.monotonic() + TIP_POLL_SECS
            solved = None
            while not self._stop.is_set():
                try:
                    r, nonce, hhex = self._result_q.get(timeout=0.4)
                except _queue.Empty:
                    r = None
                if r == round_id and r is not None:
                    solved = (nonce, hhex)
                    break
                if time.monotonic() >= deadline:
                    break                     # refresh template
                if time.monotonic() >= next_poll:
                    next_poll = time.monotonic() + TIP_POLL_SECS
                    try:
                        info = http_json(self.node, 'GET', '/api/info',
                                         timeout=10, user_agent=self.user_agent)
                        self._set(height=int(info['height']),
                                  difficulty=int(info['difficulty']))
                        self._event('tip', info)
                        if int(info['height']) >= int(tpl['height']):
                            break             # someone else mined it — refresh
                    except MinerError:
                        pass                  # transient — keep grinding
            if self._stop.is_set():
                break
            self._round_v.value = 0           # park workers while we submit
            if solved is None:
                continue

            # 4. submit
            self._set(status='submitting')
            try:
                res = http_json(self.node, 'POST', '/api/submitblock',
                                {'template_id': tpl['template_id'],
                                 'nonce': solved[0]}, timeout=30,
                                user_agent=self.user_agent)
            except MinerError as e:
                # almost always a stale template: another miner got there first
                self._set(status='mining', last_error=str(e))
                self._event('stale', {'error': str(e), 'height': tpl['height']})
                continue
            with self._lock:
                self._stats['blocks_found'] += 1
                self._stats['total_reward'] += int(res.get('reward', 0))
                self._stats['height'] = int(res.get('height', tpl['height']))
            self._event('block', res)
        self._set(status='stopped')


# ── wallet support (needs core.py) ────────────────────────────────────────────
def core_or_none():
    try:
        import core
        return core
    except ImportError:
        return None


def load_or_create_wallet(path: str, name: str = 'miner'):
    """Load wallet JSON or create a fresh one. The file format is IDENTICAL to
    the wallet_app wallet files, so a miner wallet can later be opened with
    wallet_app (e.g. through an SSH tunnel) to spend funds."""
    core = core_or_none()
    if core is None:
        raise MinerError('wallet mode needs core.py next to the miner '
                         '(or use --address with an existing address)')
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                w = core.Wallet.from_json(json.load(f))
        except Exception as e:                               # noqa: BLE001
            raise MinerError(f'cannot load wallet file: {e}') from None
        return w
    w = core.Wallet(name)
    save_wallet(w, path)
    return w


def save_wallet(w, path: str) -> None:
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(w.to_json(), f, indent=1)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def scan_wallet(w, node_url: str, user_agent: str = USER_AGENT) -> int:
    """Recognise outputs addressed to `w` using the compact public /api/scan
    view + local view-key ECDH — exactly what core.Wallet.scan does, minus
    the proofs the node has already validated. Returns NEW outputs found.
    After this, w.balance() reflects on-chain funds."""
    core = core_or_none()
    if core is None:
        raise MinerError('wallet scanning needs core.py')
    view = http_json(node_url, 'GET', '/api/scan', timeout=60,
                     user_agent=user_agent)
    new = 0
    for t in view:
        R = core.point_load(t['tx_pubkey'])
        s = core.hs(core.pt_mul(w.a, R))
        target = core.one_time_address(s, w.B)
        for j, o in enumerate(t['outputs']):
            dest = core.point_load(o['dest'])
            if dest != target:
                continue
            v, m = core.decrypt_payload(s, j, base64.b64decode(o['payload']))
            C = core.point_load(o['commitment'])
            if core.commit(v, m) == C:
                op = (bytes.fromhex(t['txid']), j)
                if op not in w.known:
                    w.known[op] = {'x': (s + w.b) % core.N, 'v': v, 'm': m,
                                   'P': dest, 'C': C}
                    new += 1
    return new


def fmt_hashrate(hps: float) -> str:
    if hps >= 1e6:
        return f'{hps / 1e6:.2f} MH/s'
    if hps >= 1e3:
        return f'{hps / 1e3:.1f} kH/s'
    return f'{hps:.0f} H/s'
