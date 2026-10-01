#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 core.py — PrivateChain engine shared by the three programs:
           node_app.py (blockchain), miner_app.py (miner), wallet_app.py (wallet)
================================================================================
 Contains everything from the original single-file prototype (crypto layer,
 ZK range proofs, chain/mempool/validation, wallet) plus:

   • JSON (de)serialization for transactions and blocks  (HTTP API transport)
   • NodeService   — thread-safe node logic behind the node's REST API
   • NodeClient    — tiny HTTP client used by miner/wallet apps
   • ChainView     — read-only chain snapshot for wallet scanning

 All math notes are inline at each section; see README.md for the overview.
================================================================================
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random as _random
import secrets
import struct
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, NamedTuple, Optional, Set, Tuple

# ──────────────────────────────────────────────────────────────────────────────
# §0  PROTOCOL CONSTANTS
# ──────────────────────────────────────────────────────────────────────────────
COIN               = 1_000_000          # smallest displayable unit (like piconero)

# ── Monetary policy: Bitcoin-style halving with a hard supply cap ─────────────
#   reward(h) = min( INITIAL_SUBSIDY >> (h // HALVING_INTERVAL),
#                    MAX_SUPPLY - emission_so_far )
#
#   INITIAL_SUBSIDY = ceil(MAX_SUPPLY / (2 * HALVING_INTERVAL)) = 120 COIN:
#   the pure halving series would emit ~50.4M coins, so the hard cap stops
#   emission at EXACTLY 50,000,000 — the same trick Bitcoin uses for 21M
#   (a geometric series  S0 + S0/2 + S0/4 + ...  converges to 2*S0 per
#   interval, hence the  50M / (2 * 210,000)  base).
MAX_SUPPLY         = 50_000_000 * COIN  # hard cap: 50 million coins, ever
HALVING_INTERVAL   = 210_000            # subsidy halves every 210,000 blocks
INITIAL_SUBSIDY    = 120 * COIN         # epoch-0 block reward
AMOUNT_BITS        = 32                 # range-proof width: amount < 2^32
RING_TARGET        = 5                  # target ring size (production: >= 16)
MIN_FEE            = 1_000              # fee floor (units)
CLOSE_FEE          = 100_000            # fixed settlement fee for channel close
MAX_TXS_PER_BLOCK  = 10
MAX_TEMPLATES      = 500                # in-flight block templates (anti-spam cap)
TARGET_BLOCK_TIME  = float(os.environ.get('DCC_BLOCK_TIME', '60.0'))
                                        # seconds per block (Monero-style; the old
                                        # 1.0 demo value made blocks absurdly fast).
                                        # DCC_BLOCK_TIME env override is for TESTS
                                        # only — never set it on a public node:
                                        # consensus must be identical everywhere.
RETARGET_INTERVAL  = 5                  # retarget every N blocks
MAX_DIFFICULTY     = 2 ** 32            # ceiling (~71 MH/s at 60s blocks)
FUTURE_SKEW        = 120                # max block timestamp drift (seconds)
PAYLOAD_LEN        = 40                 # encrypted (amount 8B + mask 32B)

TAG_LSAG    = b'PCTX/LSAG1'
TAG_RBIT    = b'PCTX/RBIT1'
TAG_RLNK    = b'PCTX/RLNK1'
TAG_STEALTH = b'PCTX/STEA1'
TAG_CB      = b'PCTX/CBMK1'
TAG_MSG     = b'PCTX/MSG01'
TAG_TXID    = b'PCTX/TXID1'
TAG_HP      = b'PCTX/HP01'

# ──────────────────────────────────────────────────────────────────────────────
# §1  ELLIPTIC CURVE — secp256k1:  y^2 = x^3 + 7  over F_p   (cofactor 1)
#     Affine points are tuples; identity (point at infinity) is None.
#     Scalar multiplication runs internally on JACOBIAN coordinates
#     (x = X/Z^2, y = Y/Z^3) so the expensive modular inversion happens only
#     once per multiplication instead of once per point addition.
# ──────────────────────────────────────────────────────────────────────────────
P = 2**256 - 2**32 - 977                                    # field prime
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141  # group order
CURVE_B = 7
G  = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
      0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)  # generator

Point = Optional[Tuple[int, int]]


def inv(a: int, m: int = P) -> int:
    """Modular inverse (Python >= 3.8 pow with exponent -1)."""
    return pow(a % m, -1, m)


def pt_add(p1: Point, p2: Point) -> Point:
    """Affine point addition (handles identity and doubling)."""
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2:
        if (y1 + y2) % P == 0:            # vertical line -> identity
            return None
        lam = (3 * x1 * x1) * inv(2 * y1) % P     # tangent slope (doubling)
    else:
        lam = (y2 - y1) * inv(x2 - x1) % P        # chord slope
    x3 = (lam * lam - x1 - x2) % P
    y3 = (lam * (x1 - x3) - y1) % P
    return (x3, y3)


def pt_sub(a: Point, b: Point) -> Point:
    return pt_add(a, pt_neg(b))


def pt_neg(p: Point) -> Point:
    return None if p is None else (p[0], (-p[1]) % P)


# ─── Jacobian coordinates (internal to scalar multiplication) ─────────────────
# Formulas: "dbl-2009-l", "add-2007-bl", "madd-2007-bl" from the
# Explicit-Formulas Database (hyperelliptic.org), curve parameter a = 0.

JacPoint = Optional[Tuple[int, int, int]]


def _jdbl(p: Tuple[int, int, int]) -> Tuple[int, int, int]:
    """Jacobian doubling (2M + 5S for a = 0)."""
    X, Y, Z = p
    A = X * X % P
    B = Y * Y % P
    C = B * B % P
    D = 2 * ((X + B) * (X + B) - A - C) % P
    E = 3 * A % P
    F = E * E % P
    X3 = (F - 2 * D) % P
    Y3 = (E * (D - X3) - 8 * C) % P
    Z3 = 2 * Y * Z % P
    return (X3, Y3, Z3)


def _jmadd(p: Tuple[int, int, int], q: Point) -> Tuple[int, int, int]:
    """Mixed addition: Jacobian p + affine q (8M + 3S)."""
    X1, Y1, Z1 = p
    X2, Y2 = q
    Z1Z1 = Z1 * Z1 % P
    U2 = X2 * Z1Z1 % P
    S2 = Y2 * Z1 % P * Z1Z1 % P
    H = (U2 - X1) % P
    R = 2 * (S2 - Y1) % P
    if H == 0:
        return _jdbl(p) if R == 0 else None    # q == ±p
    HH = H * H % P
    I = 4 * HH % P
    J = H * I % P
    V = X1 * I % P
    X3 = (R * R - J - 2 * V) % P
    Y3 = (R * (V - X3) - 2 * Y1 * J) % P
    Z3 = ((Z1 + H) * (Z1 + H) - Z1Z1 - HH) % P
    return (X3, Y3, Z3)


def _jadd(p: Tuple[int, int, int], q: Tuple[int, int, int]) -> JacPoint:
    """General Jacobian addition (12M + 4S)."""
    X1, Y1, Z1 = p
    X2, Y2, Z2 = q
    Z1Z1 = Z1 * Z1 % P
    Z2Z2 = Z2 * Z2 % P
    U1 = X1 * Z2Z2 % P
    U2 = X2 * Z1Z1 % P
    S1 = Y1 * Z2 % P * Z2Z2 % P
    S2 = Y2 * Z1 % P * Z1Z1 % P
    H = (U2 - U1) % P
    R = 2 * (S2 - S1) % P
    if H == 0:
        return _jdbl(p) if R == 0 else None
    HH = H * H % P
    I = 4 * HH % P
    J = H * I % P
    V = U1 * I % P
    X3 = (R * R - J - 2 * V) % P
    Y3 = (R * (V - X3) - 2 * S1 * J) % P
    Z3 = ((Z1 + Z2) * (Z1 + Z2) - Z1Z1 - Z2Z2) % P * H % P
    return (X3, Y3, Z3)


def _jaff(p: Tuple[int, int, int]) -> Point:
    """Jacobian -> affine (the single inversion of the whole multiplication)."""
    X, Y, Z = p
    Zi = inv(Z)
    Zi2 = Zi * Zi % P
    return (X * Zi2 % P, Y * Zi2 % P * Zi % P)


# 5-bit window tables per base point (Jacobian entries), cached for hot points.
_WINDOW_CACHE: Dict[Point, list] = {}
_WINDOW_SIZE = 5


def _window_table(pt: Point) -> list:
    """table[i] = i * pt in Jacobian coordinates, i in [0, 32)."""
    tbl = _WINDOW_CACHE.get(pt)
    if tbl is None:
        tbl = [None] * (1 << _WINDOW_SIZE)
        tbl[1] = (pt[0], pt[1], 1)
        for i in range(2, 1 << _WINDOW_SIZE):
            tbl[i] = _jmadd(tbl[i - 1], pt)
        if len(_WINDOW_CACHE) < 512:       # bounded cache (DoS-safe)
            _WINDOW_CACHE[pt] = tbl
    return tbl


def pt_mul(k: int, pt: Point) -> Point:
    """Scalar multiplication  k*P  (5-bit windows, MSB first, Jacobian core).
    Horner form per window:  result = 32*result + table[nibble]."""
    k %= N
    if k == 0 or pt is None:
        return None
    table = _window_table(pt)
    result = None
    shift = (k.bit_length() + _WINDOW_SIZE - 1) // _WINDOW_SIZE * _WINDOW_SIZE
    while shift > 0:
        shift -= _WINDOW_SIZE
        nib = (k >> shift) & ((1 << _WINDOW_SIZE) - 1)
        if result is None:
            if nib:                         # skip leading zero windows
                result = table[nib]
            continue
        for _ in range(_WINDOW_SIZE):
            result = _jdbl(result)
        if nib:
            result = _jadd(result, table[nib])
    return _jaff(result)


def is_on_curve(pt: Point) -> bool:
    if pt is None:
        return False
    x, y = pt
    if not (0 <= x < P and 0 <= y < P):
        return False
    return (y * y - x * x * x - CURVE_B) % P == 0


def random_scalar() -> int:
    """Cryptographically secure nonzero scalar in [1, N-1]."""
    return 1 + secrets.randbelow(N - 1)


# ──────────────────────────────────────────────────────────────────────────────
# §2  HASHING / ENCODING  (canonical byte encodings — everything that gets
#     hashed must be unambiguous, or signatures become malleable)
# ──────────────────────────────────────────────────────────────────────────────
def sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def sha256d(b: bytes) -> bytes:            # Bitcoin-style double hash (PoW, ids)
    return sha256(sha256(b))


def H_scalar(*parts: bytes) -> int:
    """Tagged hash -> scalar in Z_N (Fiat-Shamir challenges, derivations)."""
    return int.from_bytes(sha256(b''.join(parts)), 'big') % N


def enc_point(pt: Point) -> bytes:
    """33-byte compressed encoding; identity is a single 0x00 byte."""
    if pt is None:
        return b'\x00'
    x, y = pt
    return (b'\x03' if y & 1 else b'\x02') + x.to_bytes(32, 'big')


def enc_scalar(s: int) -> bytes:
    return int(s % N).to_bytes(32, 'big')


def enc_u32(v: int) -> bytes:
    return struct.pack('>I', v)


def enc_u64(v: int) -> bytes:
    return struct.pack('>Q', v)


def hash_to_point(data: bytes) -> Point:
    """
    Hp: bytes -> curve point, deterministic "try-and-increment" map.
    (Monero uses ge_fromfe_frombytes — same purpose.)

    Math: pick candidate x, compute rhs = x^3 + 7; if rhs is a quadratic
    residue mod p, then y = rhs^((p+1)/4) is its square root (valid because
    p ≡ 3 (mod 4) for secp256k1). ~50% of candidates succeed. Cofactor 1
    guarantees the result is in the prime-order subgroup.
    """
    seed = sha256(TAG_HP + data)
    for counter in range(1 << 20):
        x = int.from_bytes(sha256(seed + enc_u32(counter)), 'big') % P
        rhs = (pow(x, 3, P) + CURVE_B) % P
        y = pow(rhs, (P + 1) // 4, P)
        if (y * y) % P == rhs:
            return (x, y)
    raise RuntimeError('hash_to_point: no candidate found')


# ──────────────────────────────────────────────────────────────────────────────
# §3  PEDERSEN COMMITMENTS  (Confidential Transactions core)
#
#     C = v*G + m*H        v = amount (hidden), m = blinding factor (random)
#     H is a second generator with unknown discrete-log w.r.t. G — otherwise
#     commitments would not be binding.
#
#     HOMOMORPHISM:  C(v1,m1) + C(v2,m2) = C(v1+v2, m1+m2)
#     => nodes can sum commitments and check
#            sum(C_in) - sum(C_out) = fee*G     WITHOUT seeing any amount.
# ──────────────────────────────────────────────────────────────────────────────
H_PED = hash_to_point(b'Pedersen generator H')   # dlog_G(H) unknown by construction


def commit(v: int, m: int) -> Point:
    return pt_add(pt_mul(v, G), pt_mul(m, H_PED))


# ──────────────────────────────────────────────────────────────────────────────
# §4  STEALTH ADDRESSES  (CryptoNote one-time destinations)
#
#     Receiver keys:  spend b (public B = b*G), view a (public A = a*G)
#     Sender:  random r, publishes R = r*G, pays to  P = Hs(r*A)*G + B
#     Receiver scan:  s = Hs(a*R)  (= Hs(r*A), ECDH)  ->  x = s + b  (mod N)
# ──────────────────────────────────────────────────────────────────────────────
def hs(pt: Point) -> int:
    """Hs: ECDH shared point -> derived scalar (Monero-style key derivation)."""
    return H_scalar(TAG_STEALTH, enc_point(pt))


def stealth_pubkey(r: int, A: Point) -> Tuple[Point, Point]:
    """Sender side: returns (tx_pubkey R, shared scalar s = Hs(r*A))."""
    R = pt_mul(r, G)
    s = hs(pt_mul(r, A))                   # = hs(a*R) on the receiver side
    return R, s


def one_time_address(s: int, B: Point) -> Point:
    return pt_add(pt_mul(s, G), B)


def recognize_output(a: int, b: int, R: Point, P_out: Point) -> Optional[int]:
    """Receiver scan: is output key P addressed to me? Returns one-time key x."""
    s = hs(pt_mul(a, R))
    B = pt_mul(b, G)                       # receiver's spend public key
    if one_time_address(s, B) == P_out:
        return (s + b) % N                 # spend key for this output
    return None


def payload_keystream(s: int, out_idx: int) -> bytes:
    k1 = sha256(b'PCTX/PAYL1' + enc_scalar(s) + enc_u32(out_idx))
    k2 = sha256(b'PCTX/PAYL2' + k1)
    return k1 + k2[:8]                     # 40 bytes = 8 (amount) + 32 (mask)


def encrypt_payload(s: int, out_idx: int, v: int, mask: int) -> bytes:
    """Encrypt (amount, mask) to the receiver under the ECDH secret s."""
    plain = v.to_bytes(8, 'big') + int(mask).to_bytes(32, 'big')
    ks = payload_keystream(s, out_idx)
    return bytes(p ^ k for p, k in zip(plain, ks))


def decrypt_payload(s: int, out_idx: int, payload: bytes) -> Tuple[int, int]:
    ks = payload_keystream(s, out_idx)
    plain = bytes(p ^ k for p, k in zip(payload, ks))
    return int.from_bytes(plain[:8], 'big'), int.from_bytes(plain[8:], 'big')


# ──────────────────────────────────────────────────────────────────────────────
# §5  RING SIGNATURES — bLSAG  (sender anonymity + key images)
#
#     Ring: public keys P_0..P_{n-1}; signer owns x with P_pi = base*x at
#     hidden index pi. Key image:  I = x*Hp(P_pi).
#     s_pi = alpha - c_pi*x closes the Fiat-Shamir challenge chain; verifiers
#     recompute the chain around the ring — it closes iff the signer knew
#     SOME private key, and the position pi stays hidden.
#     `base` selects the generator: G for spend signatures, H for zero-proofs.
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class LSAG:
    key_image: Point
    c0: int
    s: List[int]


def lsag_sign(msg: bytes, ring: List[Point], pi: int, x: int, base: Point = G) -> LSAG:
    n = len(ring)
    hps = [hash_to_point(enc_point(p)) for p in ring]
    I = pt_mul(x, hps[pi])
    alpha = random_scalar()
    c: List[int] = [0] * n
    s: List[int] = [0] * n

    L = pt_mul(alpha, base)
    R = pt_mul(alpha, hps[pi])
    c[(pi + 1) % n] = H_scalar(TAG_LSAG, msg, enc_point(I), enc_point(L), enc_point(R))

    for k in range(1, n):
        i = (pi + k) % n
        s[i] = random_scalar()
        Li = pt_add(pt_mul(s[i], base), pt_mul(c[i], ring[i]))
        Ri = pt_add(pt_mul(s[i], hps[i]), pt_mul(c[i], I))
        c[(i + 1) % n] = H_scalar(TAG_LSAG, msg, enc_point(I), enc_point(Li), enc_point(Ri))

    s[pi] = (alpha - c[pi] * x) % N
    return LSAG(I, c[0], s)


def lsag_verify(msg: bytes, ring: List[Point], sig: LSAG, base: Point = G) -> bool:
    n = len(ring)
    if len(sig.s) != n:
        return False
    if not is_on_curve(sig.key_image):
        return False
    if not (0 <= sig.c0 < N) or any(not (0 <= si < N) for si in sig.s):
        return False
    c = sig.c0
    for i in range(n):
        hpi = hash_to_point(enc_point(ring[i]))
        Li = pt_add(pt_mul(sig.s[i], base), pt_mul(c, ring[i]))
        Ri = pt_add(pt_mul(sig.s[i], hpi), pt_mul(c, sig.key_image))
        c = H_scalar(TAG_LSAG, msg, enc_point(sig.key_image), enc_point(Li), enc_point(Ri))
    return c == sig.c0


# ──────────────────────────────────────────────────────────────────────────────
# §6  ZERO-KNOWLEDGE RANGE PROOF  ("mini Bulletproofs")
#
#     Prove  0 <= v < 2^AMOUNT_BITS  for C = v*G + m*H  without revealing v, m.
#     1) bit decomposition  C_i = b_i*G + r_i*H
#     2) per-bit OR-proof (Cramer-Damgard) that b_i in {0,1}
#     3) linking proof binding  sum(2^i C_i) - C = delta*H
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class RangeProof:
    bit_points: List[Point]                    # C_i = b_i*G + r_i*H
    bit_proofs: List[Tuple[int, int, int, int]]  # (e0, z0, e1, z1) per bit
    link_A: Point
    link_e: int
    link_z: int


def _prove_bit(C_i: Point, b: int, r: int, msg: bytes, idx: int) -> Tuple[int, int, int, int]:
    """OR-proof:  C_i = b*G + r*H  with b in {0,1}."""
    Q = [C_i, pt_sub(C_i, G)]                  # Q_b = C_i - b*G
    fake = 1 - b
    e = [0, 0]
    z = [0, 0]
    e[fake] = random_scalar()                  # fake branch: random challenge/response
    z[fake] = random_scalar()
    A_fake = pt_sub(pt_mul(z[fake], H_PED), pt_mul(e[fake], Q[fake]))
    t = random_scalar()                        # honest sigma-protocol on real branch
    A = [None, None]
    A[b] = pt_mul(t, H_PED)
    A[fake] = A_fake
    chall = H_scalar(TAG_RBIT, msg, enc_u32(idx), enc_point(C_i), enc_point(A[0]), enc_point(A[1]))
    e[b] = (chall - e[fake]) % N               # split challenge e = e0 + e1
    z[b] = (t + e[b] * r) % N
    return (e[0], z[0], e[1], z[1])


def _verify_bit(C_i: Point, idx: int, proof: Tuple[int, int, int, int], msg: bytes) -> bool:
    e0, z0, e1, z1 = proof
    Q0 = C_i
    Q1 = pt_sub(C_i, G)
    A0 = pt_sub(pt_mul(z0, H_PED), pt_mul(e0, Q0))
    A1 = pt_sub(pt_mul(z1, H_PED), pt_mul(e1, Q1))
    chall = H_scalar(TAG_RBIT, msg, enc_u32(idx), enc_point(C_i), enc_point(A0), enc_point(A1))
    return (e0 + e1) % N == chall


def range_prove(v: int, m: int, C: Point, msg: bytes) -> RangeProof:
    if not (0 <= v < (1 << AMOUNT_BITS)):
        raise ValueError('amount out of range for proof system')
    bits = [(v >> i) & 1 for i in range(AMOUNT_BITS)]
    rs = [random_scalar() for _ in range(AMOUNT_BITS)]
    pts = [pt_add(pt_mul(bits[i], G), pt_mul(rs[i], H_PED)) for i in range(AMOUNT_BITS)]

    acc: Point = None
    for i, cp in enumerate(pts):
        acc = pt_add(acc, pt_mul(1 << i, cp))
    D = pt_sub(acc, C)
    delta = (sum((1 << i) * rs[i] for i in range(AMOUNT_BITS)) - m) % N
    t = random_scalar()
    A = pt_mul(t, H_PED)
    e = H_scalar(TAG_RLNK, msg, enc_point(C), enc_point(D), enc_point(A))
    z = (t + e * delta) % N

    bit_proofs = [_prove_bit(pts[i], bits[i], rs[i], msg, i) for i in range(AMOUNT_BITS)]
    return RangeProof(pts, bit_proofs, A, e, z)


def range_verify(C: Point, rp: RangeProof, msg: bytes) -> bool:
    if len(rp.bit_points) != AMOUNT_BITS or len(rp.bit_proofs) != AMOUNT_BITS:
        return False
    if not all(is_on_curve(p) for p in rp.bit_points) or not is_on_curve(rp.link_A):
        return False
    acc: Point = None
    for i, cp in enumerate(rp.bit_points):
        acc = pt_add(acc, pt_mul(1 << i, cp))
    D = pt_sub(acc, C)
    lhs = pt_mul(rp.link_z, H_PED)
    rhs = pt_add(rp.link_A, pt_mul(rp.link_e, D))
    if lhs != rhs:
        return False
    for i, cp in enumerate(rp.bit_points):
        if not _verify_bit(cp, i, rp.bit_proofs[i], msg):
            return False
    return True


# ──────────────────────────────────────────────────────────────────────────────
# §6b  LIGHTNING PAYMENT CHANNELS  (off-chain instant payments)
#
#     A channel is opened by an ON-CHAIN funding tx with a 2-of-2 joint output:
#         P = Hs(r·(A_a + A_b))·G + (B_a + B_b)
#     Neither party alone can spend it (the spend key x = s + b_a + b_b is
#     split as x_a = s + b_a / x_b = s + b_b, exchanged encrypted via ECDH).
#     Payments INSIDE the channel are off-chain state updates
#         (seq, bal_a, bal_b)  signed by BOTH parties (Schnorr)
#     -> instant, no miner involved. Only open/close touch the chain.
#     The node relays states and keeps the latest seq (watchtower
#     simplification: a party cannot settle an old state — production LN uses
#     revocation + timelocks instead, see README roadmap).
# ──────────────────────────────────────────────────────────────────────────────
def schnorr_sign(msg: bytes, x: int) -> Tuple[Point, int]:
    """Schnorr signature (R, s):  R = k·G,  s = k - e·x,  e = H(R || P || msg)."""
    k = random_scalar()
    R = pt_mul(k, G)
    P = pt_mul(x, G)
    e = H_scalar(b'LN-SIG1', enc_point(R), enc_point(P), msg)
    s = (k - e * x) % N
    return R, s


def schnorr_verify(msg: bytes, P: Point, R: Point, s: int) -> bool:
    """Verify:  s·G + e·P == R."""
    if not is_on_curve(P) or not is_on_curve(R) or not (0 <= s < N):
        return False
    e = H_scalar(b'LN-SIG1', enc_point(R), enc_point(P), msg)
    return pt_add(pt_mul(s, G), pt_mul(e, P)) == R


def ln_state_bytes(ch_id: bytes, seq: int, bal_a: int, bal_b: int) -> bytes:
    """Canonical bytes of an off-chain channel state (what both parties sign)."""
    return b'LN-STATE1' + ch_id + enc_u64(seq) + enc_u64(bal_a) + enc_u64(bal_b)


def ln_share_encrypt(shared_scalar: int, x_share: int, ch_id: bytes) -> str:
    """Encrypt an x-share to the peer under the ECDH scalar hs(a·B_peer)."""
    ks = sha256(b'LN-SHARE1' + enc_scalar(shared_scalar) + ch_id)
    plain = enc_scalar(x_share)
    return bytes(p ^ k for p, k in zip(plain, ks)).hex()


def ln_share_decrypt(shared_scalar: int, blob_hex: str, ch_id: bytes) -> int:
    ks = sha256(b'LN-SHARE1' + enc_scalar(shared_scalar) + ch_id)
    blob = bytes.fromhex(blob_hex)
    return int.from_bytes(bytes(p ^ k for p, k in zip(blob, ks)), 'big')


# ──────────────────────────────────────────────────────────────────────────────
# §7  TRANSACTION STRUCTURE + CANONICAL SERIALIZATION
# ──────────────────────────────────────────────────────────────────────────────
OutPoint = Tuple[bytes, int]                   # (txid, output index)


@dataclass
class TxIn:
    ring: List[OutPoint]
    key_image: Point
    pseudo_commitment: Point
    zero_proof: Optional[LSAG] = None
    lsag: Optional[LSAG] = None


@dataclass
class TxOut:
    dest: Point
    commitment: Point
    payload: bytes
    range_proof: Optional[RangeProof] = None


@dataclass
class Transaction:
    is_coinbase: bool
    fee: int
    coinbase_amount: int
    tx_pubkey: Point
    inputs: List[TxIn]
    outputs: List[TxOut]
    used_ops: List[OutPoint] = field(default_factory=list, repr=False)  # wallet bookkeeping
    channel: Optional[dict] = None      # {'type': 'open'|'close', ...} for Lightning


def tx_core_bytes(tx: Transaction) -> bytes:
    """Canonical serialization of everything EXCEPT signatures/proofs."""
    b = b'PCTX' + enc_u32(1)
    b += b'\x01' if tx.is_coinbase else b'\x00'
    b += enc_u64(tx.fee) + enc_u64(tx.coinbase_amount)
    b += enc_point(tx.tx_pubkey)
    b += enc_u32(len(tx.inputs))
    for ti in tx.inputs:
        b += enc_u32(len(ti.ring))
        for op in ti.ring:
            b += op[0] + enc_u32(op[1])
        b += enc_point(ti.key_image) + enc_point(ti.pseudo_commitment)
    b += enc_u32(len(tx.outputs))
    for to in tx.outputs:
        b += enc_point(to.dest) + enc_point(to.commitment)
        b += enc_u32(len(to.payload)) + to.payload
    if tx.channel is None:
        b += b'\x00'
    else:                               # Lightning open/close payload
        b += b'\x01' + json.dumps(tx.channel, sort_keys=True).encode()
    return b


def lsag_bytes(sig: LSAG) -> bytes:
    b = enc_point(sig.key_image) + enc_scalar(sig.c0) + enc_u32(len(sig.s))
    return b + b''.join(enc_scalar(si) for si in sig.s)


def range_proof_bytes(rp: RangeProof) -> bytes:
    b = enc_u32(len(rp.bit_points))
    for cp, (e0, z0, e1, z1) in zip(rp.bit_points, rp.bit_proofs):
        b += enc_point(cp) + enc_scalar(e0) + enc_scalar(z0) + enc_scalar(e1) + enc_scalar(z1)
    b += enc_point(rp.link_A) + enc_scalar(rp.link_e) + enc_scalar(rp.link_z)
    return b


def tx_full_bytes(tx: Transaction) -> bytes:
    b = tx_core_bytes(tx)
    for ti in tx.inputs:
        b += lsag_bytes(ti.zero_proof) + lsag_bytes(ti.lsag)
    for to in tx.outputs:
        b += range_proof_bytes(to.range_proof)
    return b


def tx_message(tx: Transaction) -> bytes:
    return sha256(TAG_MSG + tx_core_bytes(tx))


def tx_txid(tx: Transaction) -> bytes:
    return sha256d(TAG_TXID + tx_full_bytes(tx))


def coinbase_mask(P_out: Point) -> int:
    """Deterministic mask for coinbase outputs -> nodes can AUDIT emission."""
    return H_scalar(TAG_CB, enc_point(P_out))


def build_coinbase_tx(address: Tuple[Point, Point], amount: int) -> Transaction:
    """Coinbase: amount is PUBLIC (emission must be auditable), still wrapped
    in a Pedersen commitment with a deterministic mask so the output can be
    spent exactly like any confidential output. Pays to a stealth address of
    `address` = (A, B)."""
    A, B = address
    r = random_scalar()
    R, s = stealth_pubkey(r, A)
    P_out = one_time_address(s, B)
    m = coinbase_mask(P_out)
    tx = Transaction(is_coinbase=True, fee=0, coinbase_amount=amount, tx_pubkey=R,
                     inputs=[], outputs=[TxOut(dest=P_out, commitment=commit(amount, m),
                                               payload=encrypt_payload(s, 0, amount, m))])
    msg = tx_message(tx)
    tx.outputs[0].range_proof = range_prove(amount, m, tx.outputs[0].commitment, msg)
    return tx


# ──────────────────────────────────────────────────────────────────────────────
# §8  BLOCK / CHAIN / MEMPOOL / VALIDATION
# ──────────────────────────────────────────────────────────────────────────────
def scheduled_subsidy(height: int) -> int:
    """Pure halving schedule (before the hard cap):
    INITIAL_SUBSIDY >> (height // HALVING_INTERVAL), in smallest units.
    Shifting in *units* (not coins) keeps fractional rewards exact
    (e.g. epoch 4 pays 7.5 COIN = 7,500,000 units)."""
    return INITIAL_SUBSIDY >> (height // HALVING_INTERVAL)


class OutputRec(NamedTuple):
    dest: Point
    commitment: Point
    is_coinbase: bool


def merkle_root(leaves: List[bytes]) -> bytes:
    if not leaves:
        return sha256d(b'')
    level = list(leaves)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [sha256d(level[i] + level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


@dataclass
class Block:
    height: int
    prev_hash: bytes
    merkle: bytes
    timestamp: int
    difficulty: int
    nonce: int
    transactions: List[Transaction]

    def header_bytes(self) -> bytes:
        # PoW commits to: height, prev hash, merkle root, time, difficulty, nonce
        return (enc_u32(self.height) + self.prev_hash + self.merkle +
                enc_u64(self.timestamp) + enc_u64(self.difficulty) + enc_u64(self.nonce))

    @property
    def hash(self) -> bytes:
        return sha256d(self.header_bytes())


class ChainView:
    """Read-only chain snapshot for wallets (blocks + global output set)."""

    def __init__(self, blocks: List[Block], outputs: Dict[OutPoint, OutputRec]):
        self.blocks = blocks
        self.outputs = outputs


class Blockchain:
    """The node's ledger state: blocks, the global output set (UTXO candidates)
    and the spent key-image set (the double-spend firewall)."""

    def __init__(self, initial_difficulty: Optional[int] = None):
        self.blocks: List[Block] = []
        self.outputs: Dict[OutPoint, OutputRec] = {}
        self.spent_images: Set[Point] = set()
        # Lightning channels: ch_id (funding txid) -> channel record.
        # Derived purely from mined channel_open/channel_close transactions.
        self.channels: Dict[bytes, dict] = {}
        self.initial_difficulty = initial_difficulty or calibrate_difficulty(TARGET_BLOCK_TIME)

    # -- monetary policy --------------------------------------------------------
    def emission(self) -> int:
        """Net emission so far = sum(coinbase amounts) - sum(recycled fees).
        Fees are burned by senders and re-minted inside the coinbase, so only
        the subsidy portion is new money. Computed from the actual blocks, so
        chains mined under older rules are accounted correctly."""
        total = 0
        for blk in self.blocks:
            if not blk.transactions or not blk.transactions[0].is_coinbase:
                continue
            total += blk.transactions[0].coinbase_amount
            total -= sum(t.fee for t in blk.transactions[1:])
        return max(0, total)

    def subsidy(self, height: int) -> int:
        """Block reward at `height`: halving schedule clamped by the hard cap
        (remaining = MAX_SUPPLY - emission so far). Returns 0 once the cap is
        reached — no more coins can ever be created."""
        return max(0, min(scheduled_subsidy(height),
                          MAX_SUPPLY - self.emission()))

    # -- difficulty -----------------------------------------------------------

    def next_difficulty(self) -> int:
        """Retarget every RETARGET_INTERVAL blocks, Bitcoin-style:
        D *= expected_time / actual_time. Blocks too FAST -> difficulty rises
        (harder); too SLOW -> falls. Negative feedback converges on
        TARGET_BLOCK_TIME. (BUGFIX: the old D *= actual/expected was
        inverted — network latency made blocks slower than the demo target so
        difficulty ratcheted up to MAX_DIFFICULTY and stayed pinned there
        regardless of hashrate.) The clamp is asymmetric (down /2, up x1.5)
        so idle time between sessions cannot crash difficulty quickly."""
        h = len(self.blocks)                   # height of the NEXT block
        if h <= 1:
            return self.initial_difficulty
        if h % RETARGET_INTERVAL != 0:
            return self.blocks[-1].difficulty
        anchor = max(0, h - 1 - RETARGET_INTERVAL)
        intervals = h - 1 - anchor
        if intervals <= 0:
            return self.blocks[-1].difficulty
        actual = max(1, self.blocks[h - 1].timestamp - self.blocks[anchor].timestamp)
        expected = intervals * TARGET_BLOCK_TIME
        d = self.blocks[-1].difficulty
        nd = d * int(expected) // actual              # expected/actual (fixed direction)
        nd = max(d // 2, min(nd, int(d * 1.5)))   # asymmetric clamp
        return max(1, min(nd, MAX_DIFFICULTY))

    @staticmethod
    def pow_target(difficulty: int):
        return ((1 << 256) - 1) // max(1, difficulty)

    # -- transaction validation ----------------------------------------------
    def validate_tx(self, tx: Transaction, *,
                    outputs: Optional[Dict[OutPoint, OutputRec]] = None,
                    spent_images: Optional[Set[Point]] = None,
                    channels: Optional[Dict[bytes, dict]] = None,
                    pool_images: frozenset = frozenset()) -> None:
        """Full semantic validation. Raises ValueError with the reason."""
        outputs = self.outputs if outputs is None else outputs
        spent_images = self.spent_images if spent_images is None else spent_images
        channels = self.channels if channels is None else channels

        if tx.is_coinbase:
            raise ValueError('coinbase transaction is only valid inside a block')
        if not tx.inputs or not tx.outputs:
            raise ValueError('transaction must have >=1 input and >=1 output')
        if tx.fee < MIN_FEE:
            raise ValueError(f'fee below minimum (fee={tx.fee})')

        for j, o in enumerate(tx.outputs):
            if not is_on_curve(o.dest) or not is_on_curve(o.commitment):
                raise ValueError(f'output {j}: malformed curve point')
            if len(o.payload) != PAYLOAD_LEN:
                raise ValueError(f'output {j}: bad encrypted payload length')

        msg = tx_message(tx)

        seen_images = set()
        for ti in tx.inputs:
            if len(ti.ring) < 1:
                raise ValueError('empty ring (prototype allows 1; production >= 16)')
            if len(set(ti.ring)) != len(ti.ring):
                raise ValueError('duplicate outpoint inside ring')
            for op in ti.ring:
                if op not in outputs:
                    raise ValueError('ring references unknown outpoint')
            ki = ti.key_image
            if not is_on_curve(ki):
                raise ValueError('key image is not a valid curve point')
            if ki in spent_images:
                # THE double-spend firewall: same UTXO => same I = x*Hp(P)
                raise ValueError('KEY IMAGE ALREADY SPENT -- double-spend detected')
            if ki in pool_images or ki in seen_images:
                raise ValueError('key image conflicts with another transaction')
            seen_images.add(ki)

            ring_Ps = [outputs[op].dest for op in ti.ring]
            ring_Cs = [outputs[op].commitment for op in ti.ring]

            if ti.lsag is None or ti.lsag.key_image != ki:
                raise ValueError('ring signature missing / key image mismatch')
            if not lsag_verify(msg, ring_Ps, ti.lsag):
                raise ValueError('ring signature INVALID (no ring member authorized)')

            diffs = [pt_sub(ti.pseudo_commitment, Cj) for Cj in ring_Cs]
            if ti.zero_proof is None or not lsag_verify(msg, diffs, ti.zero_proof, base=H_PED):
                raise ValueError('commitment-to-zero proof INVALID')

        for j, o in enumerate(tx.outputs):
            if o.range_proof is None or not range_verify(o.commitment, o.range_proof, msg):
                raise ValueError(f'range proof INVALID (output {j})')

        # balance equation: sum(C_p) - sum(C_out) == fee*G
        acc: Point = None
        for ti in tx.inputs:
            acc = pt_add(acc, ti.pseudo_commitment)
        for o in tx.outputs:
            acc = pt_sub(acc, o.commitment)
        acc = pt_sub(acc, pt_mul(tx.fee, G))
        if acc is not None:
            raise ValueError('BALANCE EQUATION VIOLATED -- money creation attempt')

        # -- Lightning channel transactions ------------------------------------
        if tx.channel is not None:
            ch = tx.channel
            if ch.get('type') == 'open':
                fi = int(ch.get('out_index', 0))
                if fi >= len(tx.outputs):
                    raise ValueError('channel funding output missing')
                if int(ch['capacity']) <= 0:
                    raise ValueError('channel capacity must be positive')
                o = tx.outputs[fi]
                # the funder reveals (capacity, mask) -> publicly checkable
                if o.commitment != commit(int(ch['capacity']), int(ch['mask'])):
                    raise ValueError('channel funding commitment mismatch')
            elif ch.get('type') == 'close':
                ch_id = bytes.fromhex(ch['channel_id'])
                info = channels.get(ch_id)
                if info is None:
                    raise ValueError('close references unknown/unmined channel')
                if info['closed']:
                    raise ValueError('channel already closed')
                if len(tx.inputs) != 1 or tx.inputs[0].ring != [info['outpoint']]:
                    raise ValueError('close tx must spend exactly the funding output')
                bal_a, bal_b = int(ch['bal_a']), int(ch['bal_b'])
                if bal_a < 0 or bal_b < 0:
                    raise ValueError('negative close balance')
                if bal_a + bal_b + tx.fee != info['capacity']:
                    raise ValueError('close balances != capacity - fee')
                msg = ln_state_bytes(ch_id, int(ch['seq']), bal_a, bal_b)
                try:
                    sig_a = (point_load(ch['sig_a']['R']), int(ch['sig_a']['s'], 16))
                    sig_b = (point_load(ch['sig_b']['R']), int(ch['sig_b']['s'], 16))
                except Exception:
                    raise ValueError('malformed close signatures')
                if not schnorr_verify(msg, info['B_a'], *sig_a):
                    raise ValueError('party A close signature INVALID')
                if not schnorr_verify(msg, info['B_b'], *sig_b):
                    raise ValueError('party B close signature INVALID')

    # -- block validation ------------------------------------------------------
    def validate_block(self, blk: Block) -> None:
        if blk.height != len(self.blocks):
            raise ValueError('bad block height')
        expected_prev = self.blocks[-1].hash if self.blocks else b'\x00' * 32
        if blk.prev_hash != expected_prev:
            raise ValueError('prev_hash does not chain to tip')

        if blk.height > 0:   # genesis is trusted by definition
            if blk.difficulty != self.next_difficulty():
                raise ValueError('block difficulty does not match retarget rule')
        target = self.pow_target(blk.difficulty)
        if int.from_bytes(sha256d(blk.header_bytes()), 'big') >= target:
            raise ValueError('Proof-of-Work INVALID')

        now = int(time.time())
        if blk.timestamp > now + FUTURE_SKEW:
            raise ValueError('block timestamp too far in the future')
        if self.blocks and blk.timestamp < self.blocks[-1].timestamp:
            raise ValueError('block timestamp older than parent')

        if not blk.transactions and blk.height > 0:
            raise ValueError('empty block')   # genesis (height 0) may be empty
        root = merkle_root([tx_txid(t) for t in blk.transactions])
        if root != blk.merkle:
            raise ValueError('merkle root mismatch')

        # -- coinbase rules (emission audit) ----------------------------------
        if blk.height == 0:                    # genesis: no coinbase, trusted
            return
        cb = blk.transactions[0]
        if not cb.is_coinbase:
            raise ValueError('first transaction must be the coinbase')
        if any(t.is_coinbase for t in blk.transactions[1:]):
            raise ValueError('multiple coinbase transactions')
        fees = sum(t.fee for t in blk.transactions[1:])
        if cb.coinbase_amount != self.subsidy(blk.height) + fees:
            raise ValueError('coinbase exceeds block subsidy + fees (inflation!)')
        if cb.inputs:
            raise ValueError('coinbase cannot spend inputs')
        if len(cb.outputs) != 1:
            raise ValueError('prototype coinbase has exactly 1 output')
        o = cb.outputs[0]
        m_cb = coinbase_mask(o.dest)           # deterministic -> publicly checkable
        if o.commitment != pt_add(pt_mul(cb.coinbase_amount, G), pt_mul(m_cb, H_PED)):
            raise ValueError('coinbase commitment does not match emission')

        # -- validate remaining txs sequentially, chaining state --------------
        st_outputs = dict(self.outputs)
        st_spent = set(self.spent_images)
        st_channels = dict(self.channels)
        for t in blk.transactions[1:]:
            self.validate_tx(t, outputs=st_outputs, spent_images=st_spent,
                             channels=st_channels)
            self._apply_tx(t, st_outputs, st_spent, st_channels)

    def _apply_tx(self, tx: Transaction, outputs: Dict[OutPoint, OutputRec],
                  spent: Set[Point], channels: Optional[Dict[bytes, dict]] = None) -> None:
        tid = tx_txid(tx)
        for j, o in enumerate(tx.outputs):
            outputs[(tid, j)] = OutputRec(o.dest, o.commitment, tx.is_coinbase)
        for ti in tx.inputs:
            spent.add(ti.key_image)
        if channels is not None and tx.channel is not None:
            ch = tx.channel
            if ch.get('type') == 'open':
                A_a, B_a = parse_address(ch['party_a'])
                _A_b, B_b = parse_address(ch['party_b'])
                channels[tid] = {'party_a': ch['party_a'], 'party_b': ch['party_b'],
                                 'B_a': B_a, 'B_b': B_b, 'capacity': int(ch['capacity']),
                                 'outpoint': (tid, int(ch.get('out_index', 0))),
                                 'closed': False}
            elif ch.get('type') == 'close':
                ch_id = bytes.fromhex(ch['channel_id'])
                if ch_id in channels:
                    channels[ch_id]['closed'] = True

    def accept_block(self, blk: Block) -> None:
        self.validate_block(blk)
        for t in blk.transactions:
            self._apply_tx(t, self.outputs, self.spent_images, self.channels)
        self.blocks.append(blk)


class Mempool:
    """Pending, fully-validated transactions awaiting a block."""

    def __init__(self, chain: Blockchain):
        self.chain = chain
        self.txs: Dict[bytes, Transaction] = {}

    def pool_images(self) -> frozenset:
        return frozenset(ti.key_image for tx in self.txs.values() for ti in tx.inputs)

    def add(self, tx: Transaction) -> bytes:
        tid = tx_txid(tx)
        if tid in self.txs:
            raise ValueError('already in mempool')
        self.chain.validate_tx(tx, pool_images=self.pool_images())
        self.txs[tid] = tx
        return tid

    def revalidate(self) -> None:
        """After a new block, drop txs whose inputs were just consumed.
        BUGFIX: pool_images must EXCLUDE the tx's own key images — otherwise
        every tx self-collides and gets dropped the moment any new block is
        accepted (which killed mempool txs whenever a miner was running
        continuously)."""
        for tid in list(self.txs):
            tx = self.txs[tid]
            own = frozenset(ti.key_image for ti in tx.inputs)
            others = self.pool_images() - own
            try:
                self.chain.validate_tx(tx, pool_images=others)
            except ValueError:
                del self.txs[tid]


def make_genesis() -> Block:
    blk = Block(0, b'\x00' * 32, merkle_root([]), int(time.time()), 1, 0, [])
    target = Blockchain.pow_target(1)          # difficulty 1 -> found instantly
    while int.from_bytes(sha256d(blk.header_bytes()), 'big') >= target:
        blk.nonce += 1
    return blk


def calibrate_difficulty(target_seconds: float) -> int:
    """Measure this machine's SHA-256d rate and pick a starting difficulty so
    blocks take roughly `target_seconds`."""
    t0 = time.perf_counter()
    x = b'calibration'
    n = 20_000
    for _ in range(n):
        x = sha256d(x)
    rate = n / (time.perf_counter() - t0)
    return max(1_000, min(int(rate * target_seconds * 0.9), 2 ** 24))


# ──────────────────────────────────────────────────────────────────────────────
# §9  JSON SERIALIZATION  (HTTP API transport — exact round-trips, since txids
#     are recomputed from the reconstructed objects and must match byte-for-byte)
# ──────────────────────────────────────────────────────────────────────────────
def _h32(i: int) -> str:
    return format(i % N, '064x')


def point_json(p: Point):
    return None if p is None else {'x': format(p[0], '064x'), 'y': format(p[1], '064x')}


def point_load(d):
    return None if d is None else (int(d['x'], 16), int(d['y'], 16))


def lsag_json(sig: LSAG) -> dict:
    return {'key_image': point_json(sig.key_image),
            'c0': _h32(sig.c0),
            's': [_h32(si) for si in sig.s]}


def lsag_load(d: dict) -> LSAG:
    return LSAG(point_load(d['key_image']), int(d['c0'], 16),
                [int(s, 16) for s in d['s']])


def rp_json(rp: RangeProof) -> dict:
    return {'bit_points': [point_json(p) for p in rp.bit_points],
            'bit_proofs': [[_h32(x) for x in bp] for bp in rp.bit_proofs],
            'link_A': point_json(rp.link_A),
            'link_e': _h32(rp.link_e), 'link_z': _h32(rp.link_z)}


def rp_load(d: dict) -> RangeProof:
    return RangeProof([point_load(p) for p in d['bit_points']],
                      [tuple(int(x, 16) for x in bp) for bp in d['bit_proofs']],
                      point_load(d['link_A']), int(d['link_e'], 16), int(d['link_z'], 16))


def tx_json(tx: Transaction) -> dict:
    return {'is_coinbase': tx.is_coinbase, 'fee': tx.fee,
            'coinbase_amount': tx.coinbase_amount,
            'tx_pubkey': point_json(tx.tx_pubkey),
            'channel': tx.channel,
            'inputs': [{'ring': [[op[0].hex(), op[1]] for op in ti.ring],
                        'key_image': point_json(ti.key_image),
                        'pseudo_commitment': point_json(ti.pseudo_commitment),
                        'zero_proof': lsag_json(ti.zero_proof) if ti.zero_proof else None,
                        'lsag': lsag_json(ti.lsag) if ti.lsag else None} for ti in tx.inputs],
            'outputs': [{'dest': point_json(o.dest),
                         'commitment': point_json(o.commitment),
                         'payload': base64.b64encode(o.payload).decode(),
                         'range_proof': rp_json(o.range_proof) if o.range_proof else None}
                        for o in tx.outputs]}


def tx_load(d: dict) -> Transaction:
    return Transaction(
        is_coinbase=bool(d['is_coinbase']), fee=int(d['fee']),
        coinbase_amount=int(d['coinbase_amount']),
        tx_pubkey=point_load(d['tx_pubkey']),
        channel=d.get('channel'),
        inputs=[TxIn(ring=[(bytes.fromhex(tid), int(idx)) for tid, idx in ti['ring']],
                     key_image=point_load(ti['key_image']),
                     pseudo_commitment=point_load(ti['pseudo_commitment']),
                     zero_proof=lsag_load(ti['zero_proof']) if ti['zero_proof'] else None,
                     lsag=lsag_load(ti['lsag']) if ti['lsag'] else None)
                for ti in d['inputs']],
        outputs=[TxOut(dest=point_load(o['dest']), commitment=point_load(o['commitment']),
                       payload=base64.b64decode(o['payload']),
                       range_proof=rp_load(o['range_proof']) if o['range_proof'] else None)
                 for o in d['outputs']])


def block_json(blk: Block) -> dict:
    return {'height': blk.height, 'prev_hash': blk.prev_hash.hex(),
            'merkle': blk.merkle.hex(), 'timestamp': blk.timestamp,
            'difficulty': blk.difficulty, 'nonce': blk.nonce,
            'transactions': [tx_json(t) for t in blk.transactions]}


def block_load(d: dict) -> Block:
    return Block(int(d['height']), bytes.fromhex(d['prev_hash']), bytes.fromhex(d['merkle']),
                 int(d['timestamp']), int(d['difficulty']), int(d['nonce']),
                 [tx_load(t) for t in d['transactions']])


def decode_point(hex33: str) -> Point:
    """Decompress a 33-byte encoded point (prefix || x):  y = ±sqrt(x^3 + 7).
    Valid because p ≡ 3 (mod 4) for secp256k1, so the square root is
    y = rhs^((p+1)/4); the prefix byte selects the y parity."""
    data = bytes.fromhex(hex33.strip())
    if len(data) != 33 or data[0] not in (2, 3):
        raise ValueError('point must be 33-byte compressed (02/03 prefix)')
    x = int.from_bytes(data[1:], 'big')
    rhs = (pow(x, 3, P) + CURVE_B) % P
    y = pow(rhs, (P + 1) // 4, P)
    if (y * y) % P != rhs:
        raise ValueError('point is not on the curve')
    if (y & 1) != (data[0] & 1):
        y = P - y
    return (x, y)


def address_str(A: Point, B: Point) -> str:
    """Wire format of a stealth address: '<compressed A hex>:<compressed B hex>'."""
    return f'{enc_point(A).hex()}:{enc_point(B).hex()}'


def parse_address(text: str) -> Tuple[Point, Point]:
    text = text.strip()
    parts = text.split(':')
    if len(parts) != 2:
        raise ValueError('address must be "<66-hex>:<66-hex>" (compressed points)')
    A = decode_point(parts[0])
    B = decode_point(parts[1])
    return A, B


def fmt_coin(units: int) -> str:
    s = f'{units / COIN:.6f}'.rstrip('0').rstrip('.')
    return s + ' COIN'


def parse_coin(text: str) -> int:
    """'2.5' -> 2_500_000 units. Raises ValueError on bad input."""
    try:
        val = float(str(text).strip())
    except ValueError:
        raise ValueError('amount must be a number')
    if val < 0 or val != val:  # negative or NaN
        raise ValueError('amount must be positive')
    units = int(round(val * COIN))
    return units


def short_hash(h: bytes, n: int = 12) -> str:
    return h.hex()[:n] + ('…' if len(h) > n // 2 else '')


# ──────────────────────────────────────────────────────────────────────────────
# §10  WALLET  (view-key scanning + transaction construction)
# ──────────────────────────────────────────────────────────────────────────────
class Wallet:
    """Holds (view key a, spend key b). Scans the chain with `a` only."""

    def __init__(self, name: str):
        self.name = name
        self.a = random_scalar()               # view key (can see incoming txs)
        self.b = random_scalar()               # spend key (can move funds)
        self.A = pt_mul(self.a, G)
        self.B = pt_mul(self.b, G)
        self.known: Dict[OutPoint, Dict] = {}  # every output ever owned
        self.spent_ops: Set[OutPoint] = set()  # outputs consumed by MINED txs
        self.pending_txs: Dict[bytes, List[OutPoint]] = {}   # txid -> inputs used
        self.pending_ops: Set[OutPoint] = set()              # inputs of unconfirmed txs
        self.channels: Dict[bytes, dict] = {}  # Lightning channel records
        self.pending_in = 0                    # unconfirmed on-chain incoming

    @property
    def address(self) -> Tuple[Point, Point]:
        return (self.A, self.B)

    @property
    def address_text(self) -> str:
        return address_str(self.A, self.B)

    @property
    def available(self) -> Dict[OutPoint, Dict]:
        return {op: rec for op, rec in self.known.items()
                if op not in self.spent_ops and op not in self.pending_ops}

    def balance(self) -> int:
        return sum(rec['v'] for rec in self.available.values())

    # -- pending (unconfirmed) transaction bookkeeping ---------------------------
    def register_pending(self, txid: bytes, used_ops) -> None:
        """Reserve the inputs of a transaction that was accepted into the
        mempool but is not mined yet (they cannot be re-spent)."""
        ops = [op for op in used_ops if op in self.known]
        self.pending_txs[txid] = ops
        self.pending_ops.update(ops)

    def supersede_pending(self, used_ops) -> None:
        """A fresh tx consumes the same inputs as some older unconfirmed tx —
        the older one can never be mined, release its reservations."""
        used = set(used_ops)
        for tid in list(self.pending_txs):
            if used & set(self.pending_txs[tid]):
                ops = self.pending_txs.pop(tid)
                self.pending_ops.difference_update(ops)

    def confirm_pending(self, onchain_txids: set) -> int:
        """Pending txs that appear in a mined block become real spends."""
        confirmed = 0
        for tid in list(self.pending_txs):
            if tid in onchain_txids:
                ops = self.pending_txs.pop(tid)
                self.spent_ops.update(ops)
                self.pending_ops.difference_update(ops)
                confirmed += 1
        return confirmed

    def release_all_pending(self) -> int:
        """Recovery button: forget all unconfirmed reservations."""
        n = len(self.pending_txs)
        self.pending_txs.clear()
        self.pending_ops.clear()
        return n

    # -- persistence -----------------------------------------------------------
    def to_json(self) -> dict:
        return {'name': self.name, 'a': format(self.a, '064x'), 'b': format(self.b, '064x'),
                'spent_ops': [[op[0].hex(), op[1]] for op in self.spent_ops],
                'pending_txs': {tid.hex(): [[op[0].hex(), op[1]] for op in ops]
                                for tid, ops in self.pending_txs.items()},
                'channels': {ch.hex(): {
                    'peer_address': rec['peer_address'], 'role': rec['role'],
                    'capacity': rec['capacity'],
                    'x': format(rec['x'], '064x') if rec['x'] is not None else None,
                    's': format(rec['s'], '064x'), 'mask_f': format(rec['mask_f'], '064x'),
                    'share_self': format(rec['share_self'], '064x'),
                    'share_peer': format(rec['share_peer'], '064x')
                                  if rec['share_peer'] is not None else None,
                    'funding_outpoint': [rec['funding_outpoint'][0].hex(),
                                         rec['funding_outpoint'][1]],
                    'funding_P': point_json(rec['funding_P']),
                    'peer_B': point_json(rec['peer_B']),
                    'seq': rec['seq'], 'bal_self': rec['bal_self'],
                    'bal_peer': rec['bal_peer'], 'status': rec['status'],
                    'closing_txid': rec['closing_txid'].hex()
                                    if rec['closing_txid'] else None}
                    for ch, rec in self.channels.items()}}

    @classmethod
    def from_json(cls, d: dict) -> 'Wallet':
        w = cls.__new__(cls)
        w.name = d['name']
        w.a = int(d['a'], 16)
        w.b = int(d['b'], 16)
        w.A = pt_mul(w.a, G)
        w.B = pt_mul(w.b, G)
        w.known = {}
        w.spent_ops = {(bytes.fromhex(tid), int(idx)) for tid, idx in d.get('spent_ops', [])}
        w.pending_txs = {bytes.fromhex(tid): [(bytes.fromhex(o), int(i)) for o, i in ops]
                         for tid, ops in d.get('pending_txs', {}).items()}
        w.pending_ops = {op for ops in w.pending_txs.values() for op in ops}
        w.channels = {}
        for ch_hex, c in d.get('channels', {}).items():
            ch = bytes.fromhex(ch_hex)
            w.channels[ch] = {
                'peer_address': c['peer_address'], 'role': c['role'],
                'capacity': int(c['capacity']),
                'x': int(c['x'], 16) if c['x'] else None,
                's': int(c['s'], 16), 'mask_f': int(c['mask_f'], 16),
                'share_self': int(c['share_self'], 16),
                'share_peer': int(c['share_peer'], 16) if c['share_peer'] else None,
                'funding_outpoint': (bytes.fromhex(c['funding_outpoint'][0]),
                                     int(c['funding_outpoint'][1])),
                'funding_P': point_load(c['funding_P']),
                'peer_B': point_load(c['peer_B']),
                'seq': int(c['seq']), 'bal_self': int(c['bal_self']),
                'bal_peer': int(c['bal_peer']), 'status': c['status'],
                'closing_txid': bytes.fromhex(c['closing_txid'])
                                if c['closing_txid'] else None}
        w.pending_in = 0
        return w

    # -- scanning (view-key only) ----------------------------------------------
    def _outputs_of(self, tx: Transaction):
        """Yield (index, value, mask, ecdh_scalar) for every output of `tx`
        addressed to this wallet (recognition via the view key + ECDH)."""
        s = hs(pt_mul(self.a, tx.tx_pubkey))
        target = one_time_address(s, self.B)
        for j, o in enumerate(tx.outputs):
            if o.dest == target:
                v, m = decrypt_payload(s, j, o.payload)
                if commit(v, m) == o.commitment:
                    yield j, v, m, s

    def scan(self, chain) -> int:
        """Scan mined blocks; returns how many NEW outputs were recognised."""
        found = 0
        for blk in chain.blocks:
            for tx in blk.transactions:
                tid = tx_txid(tx)
                for j, v, m, s in self._outputs_of(tx):
                    op = (tid, j)
                    if op not in self.known:
                        self.known[op] = {'x': (s + self.b) % N, 'v': v, 'm': m,
                                          'P': tx.outputs[j].dest,
                                          'C': tx.outputs[j].commitment}
                        found += 1
        return found

    def scan_pending_incoming(self, txs) -> int:
        """Recognise outputs of UNCONFIRMED (mempool) transactions addressed to
        this wallet. Returns the total incoming amount. They are not added to
        `known` until the transaction is actually mined."""
        total = 0
        for tx in txs:
            for _j, v, _m, _s in self._outputs_of(tx):
                total += v
        return total

    # -- spending ----------------------------------------------------------------
    def _select(self, need: int, force: Optional[List[OutPoint]]) -> Dict[OutPoint, Dict]:
        if force is not None:
            return {op: self.known[op] for op in force}
        chosen: Dict[OutPoint, Dict] = {}
        total = 0
        for op, rec in sorted(self.available.items(), key=lambda kv: -kv[1]['v']):
            if total >= need:
                break
            chosen[op] = rec
            total += rec['v']
        if total < need:
            raise ValueError(f'{self.name}: insufficient funds '
                             f'(have {fmt_coin(total)}, need {fmt_coin(need)})')
        return chosen

    def build_tx(self, chain,
                 recipients: List[Tuple[Tuple[Point, Point], int]], fee: int, *,
                 consume: bool = True,
                 force_outputs: Optional[List[OutPoint]] = None) -> Transaction:
        """Build a fully-signed confidential transaction against a chain view
        (needs `chain.outputs` for rings/decoys)."""
        need = sum(amt for _, amt in recipients) + fee
        if need <= 0:
            raise ValueError('nothing to send')
        selected = self._select(need, force_outputs)

        # ---- inputs: rings, key images, pseudo-commitments -------------------
        pool = [op for op in chain.outputs.keys() if op not in selected]
        inputs: List[TxIn] = []
        meta: List[Dict] = []
        a_primes: List[int] = []
        for op, rec in selected.items():
            k = max(0, min(RING_TARGET - 1, len(pool)))       # adaptive ring size
            decoys = _random.sample(pool, k) if k else []
            ring = decoys + [op]
            _random.shuffle(ring)                              # hide the real position
            pi = ring.index(op)
            image = pt_mul(rec['x'], hash_to_point(enc_point(rec['P'])))
            a_prime = random_scalar()
            inputs.append(TxIn(ring=ring, key_image=image,
                               pseudo_commitment=commit(rec['v'], a_prime)))
            meta.append({'pi': pi, 'rec': rec, 'a_prime': a_prime})
            a_primes.append(a_prime)

        # ---- outputs: stealth addresses + commitments + encrypted payloads ---
        outs = [(addr, amt) for addr, amt in recipients]
        change = sum(rec['v'] for rec in selected.values()) - need
        if change > 0:
            outs.append((self.address, change))               # change returns to self

        r = random_scalar()
        tx_outs: List[TxOut] = []
        amounts: List[int] = []
        masks = [random_scalar() for _ in range(len(outs) - 1)]
        masks.append((sum(a_primes) - sum(masks)) % N)         # mask-sum conservation!
        for j, ((A_r, B_r), amt) in enumerate(outs):
            R, s = stealth_pubkey(r, A_r)                      # R = r*G (same for all outputs)
            P_out = one_time_address(s, B_r)
            C = commit(amt, masks[j])
            payload = encrypt_payload(s, j, amt, masks[j])
            tx_outs.append(TxOut(dest=P_out, commitment=C, payload=payload))
            amounts.append(amt)

        tx = Transaction(is_coinbase=False, fee=fee, coinbase_amount=0, tx_pubkey=R,
                         inputs=inputs, outputs=tx_outs,
                         used_ops=[op for op in selected])   # wallet bookkeeping (always)

        # ---- proofs & signatures (all bound to the tx core hash) -------------
        msg = tx_message(tx)
        for o, amt, mask in zip(tx.outputs, amounts, masks):
            o.range_proof = range_prove(amt, mask, o.commitment, msg)

        for ti, mt in zip(tx.inputs, meta):
            ring_Cs = [chain.outputs[op].commitment for op in ti.ring]
            ring_Ps = [chain.outputs[op].dest for op in ti.ring]
            diffs = [pt_sub(ti.pseudo_commitment, Cj) for Cj in ring_Cs]
            ti.zero_proof = lsag_sign(msg, diffs, mt['pi'],
                                      (mt['a_prime'] - mt['rec']['m']) % N, base=H_PED)
            ti.lsag = lsag_sign(msg, ring_Ps, mt['pi'], mt['rec']['x'])

        if consume:
            self.spent_ops.update(tx.used_ops)
        return tx

    # ── Lightning payment channels ─────────────────────────────────────────────
    def ln_open_channel(self, view, client, peer_addr: str,
                        capacity: int, fee: int) -> dict:
        """Funding tx (on-chain, needs 1 mined block) with a 2-of-2 joint
        output + an encrypted invite so the peer can co-control the channel."""
        A_p, B_p = parse_address(peer_addr)
        need = capacity + fee
        selected = self._select(need, None)

        inputs: List[TxIn] = []
        meta: List[dict] = []
        a_primes: List[int] = []
        pool = [op for op in view.outputs.keys() if op not in selected]
        for op, rec in selected.items():
            k = max(0, min(RING_TARGET - 1, len(pool)))
            decoys = _random.sample(pool, k) if k else []
            ring = decoys + [op]
            _random.shuffle(ring)
            pi = ring.index(op)
            image = pt_mul(rec['x'], hash_to_point(enc_point(rec['P'])))
            a_prime = random_scalar()
            inputs.append(TxIn(ring=ring, key_image=image,
                               pseudo_commitment=commit(rec['v'], a_prime)))
            meta.append({'pi': pi, 'rec': rec, 'a_prime': a_prime})
            a_primes.append(a_prime)

        # joint one-time address: P = Hs(r*(A_a+A_b))*G + (B_a+B_b)
        r = random_scalar()
        R, s = stealth_pubkey(r, pt_add(self.A, A_p))
        P_fund = one_time_address(s, pt_add(self.B, B_p))
        mask_f = random_scalar()
        C_f = commit(capacity, mask_f)

        change = sum(rec['v'] for rec in selected.values()) - need
        outs = [TxOut(dest=P_fund, commitment=C_f,
                      payload=encrypt_payload(s, 0, capacity, mask_f))]
        amounts = [capacity]
        masks = [mask_f]
        if change > 0:
            R_c, s_c = stealth_pubkey(r, self.A)
            m_c = (sum(a_primes) - mask_f) % N
            outs.append(TxOut(dest=one_time_address(s_c, self.B),
                              commitment=commit(change, m_c),
                              payload=encrypt_payload(s_c, 1, change, m_c)))
            amounts.append(change)
            masks.append(m_c)

        tx = Transaction(is_coinbase=False, fee=fee, coinbase_amount=0, tx_pubkey=R,
                         inputs=inputs, outputs=outs, used_ops=list(selected),
                         channel={'type': 'open', 'party_a': self.address_text,
                                  'party_b': peer_addr, 'capacity': capacity,
                                  'mask': mask_f, 'out_index': 0})
        msg = tx_message(tx)
        for o, amt, mask in zip(tx.outputs, amounts, masks):
            o.range_proof = range_prove(amt, mask, o.commitment, msg)
        for ti, mt in zip(tx.inputs, meta):
            ring_Cs = [view.outputs[op].commitment for op in ti.ring]
            ring_Ps = [view.outputs[op].dest for op in ti.ring]
            diffs = [pt_sub(ti.pseudo_commitment, Cj) for Cj in ring_Cs]
            ti.zero_proof = lsag_sign(msg, diffs, mt['pi'],
                                      (mt['a_prime'] - mt['rec']['m']) % N, base=H_PED)
            ti.lsag = lsag_sign(msg, ring_Ps, mt['pi'], mt['rec']['x'])

        res = client.submit_tx(tx_json(tx))
        ch_id = bytes.fromhex(res['txid'])
        self.register_pending(ch_id, list(selected))
        share_self = (s + self.b) % N
        sh = hs(pt_mul(self.a, B_p))               # ECDH scalar to the peer
        client.inbox_send(peer_addr, {
            'type': 'ln_invite', 'ch_id': ch_id.hex(),
            's': format(s, '064x'), 'capacity': capacity,
            'mask': format(mask_f, '064x'), 'party_a': self.address_text,
            'out_index': 0, 'share_enc': ln_share_encrypt(sh, share_self, ch_id)})
        self.channels[ch_id] = {
            'peer_address': peer_addr, 'role': 'a', 'capacity': capacity,
            'x': None, 's': s, 'mask_f': mask_f, 'share_self': share_self,
            'share_peer': None, 'funding_outpoint': (ch_id, 0), 'funding_P': P_fund,
            'peer_B': B_p, 'seq': 0, 'bal_self': capacity, 'bal_peer': 0,
            'status': 'opening', 'closing_txid': None}
        return {'ch_id': ch_id.hex(), 'txid': res['txid']}

    def ln_pay(self, client, ch_id: bytes, amount: int) -> dict:
        """INSTANT off-chain payment: propose the next state signed by us."""
        rec = self.channels.get(ch_id)
        if rec is None or rec['status'] != 'open':
            raise ValueError('channel is not open')
        if amount <= 0 or amount > rec['bal_self']:
            raise ValueError(f'insufficient channel balance ({fmt_coin(rec["bal_self"])})')
        seq = rec['seq'] + 1
        if rec['role'] == 'a':
            bal_a, bal_b = rec['bal_self'] - amount, rec['bal_peer'] + amount
        else:
            bal_a, bal_b = rec['bal_peer'] + amount, rec['bal_self'] - amount
        msg = ln_state_bytes(ch_id, seq, bal_a, bal_b)
        R, s = schnorr_sign(msg, self.b)
        res = client.ln_pay({'ch_id': ch_id.hex(), 'seq': seq, 'bal_a': bal_a,
                             'bal_b': bal_b,
                             'sig': {'R': point_json(R), 's': format(s, '064x')}})
        rec['seq'] = seq                           # balances update on ln_committed
        return res

    def ln_close_request(self, client, ch_id: bytes) -> dict:
        rec = self.channels.get(ch_id)
        if rec is None or rec['status'] != 'open':
            raise ValueError('channel is not open')
        if rec['bal_self'] < CLOSE_FEE:
            raise ValueError('channel balance cannot cover the settlement fee')
        seq = rec['seq'] + 1
        if rec['role'] == 'a':
            bal_a, bal_b = rec['bal_self'] - CLOSE_FEE, rec['bal_peer']
        else:
            bal_a, bal_b = rec['bal_peer'], rec['bal_self'] - CLOSE_FEE
        msg = ln_state_bytes(ch_id, seq, bal_a, bal_b)
        R, s = schnorr_sign(msg, self.b)
        res = client.ln_close_req({'ch_id': ch_id.hex(), 'seq': seq,
                                   'bal_a': bal_a, 'bal_b': bal_b,
                                   'sig': {'R': point_json(R), 's': format(s, '064x')}})
        rec['status'] = 'closing'
        rec['seq'] = seq
        return res

    def ln_build_close_tx(self, view, client, ch_id: bytes) -> Transaction:
        """Cooperative settle: spend the funding output per the co-signed
        final state, paying each party their balance on-chain."""
        rec = self.channels[ch_id]
        st = client.ln_close_status(ch_id.hex())
        if not st.get('ready'):
            raise ValueError('peer co-signature not ready yet')
        state = st['state']
        bal_a, bal_b = int(state['bal_a']), int(state['bal_b'])
        a_prime = random_scalar()
        C_p = commit(rec['capacity'], a_prime)
        image = pt_mul(rec['x'], hash_to_point(enc_point(rec['funding_P'])))
        inputs = [TxIn(ring=[rec['funding_outpoint']], key_image=image,
                       pseudo_commitment=C_p)]

        party_addrs = [(self.address_text if rec['role'] == 'a' else rec['peer_address'],
                        bal_a),
                       (rec['peer_address'] if rec['role'] == 'a' else self.address_text,
                        bal_b)]
        m0 = random_scalar()
        masks = [m0, (a_prime - m0) % N]
        # ONE ephemeral r for the whole tx (same as build_tx): the single
        # published tx_pubkey R lets EVERY recipient scan their output via
        # s = Hs(a*R) — per-output r's would break recognition.
        r = random_scalar()
        R = pt_mul(r, G)
        final_outs = []
        for j, (party_addr, bal) in enumerate(party_addrs):
            A_x, B_x = parse_address(party_addr)
            s = hs(pt_mul(r, A_x))
            final_outs.append(TxOut(dest=one_time_address(s, B_x),
                                    commitment=commit(bal, masks[j]),
                                    payload=encrypt_payload(s, j, bal, masks[j])))
        # the settlement fee is already encoded in the co-signed close state
        fee = rec['capacity'] - bal_a - bal_b
        tx = Transaction(is_coinbase=False, fee=fee, coinbase_amount=0, tx_pubkey=R,
                         inputs=inputs, outputs=final_outs,
                         channel={'type': 'close', 'channel_id': ch_id.hex(),
                                  'seq': int(state['seq']), 'bal_a': bal_a,
                                  'bal_b': bal_b, 'sig_a': state['sig_a'],
                                  'sig_b': state['sig_b']})
        msg = tx_message(tx)
        for o, (party_addr, bal), mask in zip(tx.outputs, party_addrs, masks):
            o.range_proof = range_prove(bal, mask, o.commitment, msg)
        for ti in tx.inputs:
            ring_Ps = [view.outputs[op].dest for op in ti.ring]
            ring_Cs = [view.outputs[op].commitment for op in ti.ring]
            ti.lsag = lsag_sign(msg, ring_Ps, 0, rec['x'])
            diffs = [pt_sub(ti.pseudo_commitment, Cj) for Cj in ring_Cs]
            ti.zero_proof = lsag_sign(msg, diffs, 0,
                                      (a_prime - rec['mask_f']) % N, base=H_PED)
        return tx

    def ln_process_inbox(self, client, address: str) -> List[str]:
        """Fetch and handle Lightning relay messages. Returns log lines."""
        logs: List[str] = []
        for m in client.inbox_pop(address):
            t = m.get('type')
            ch_id = bytes.fromhex(m['ch_id']) if m.get('ch_id') else None
            rec = self.channels.get(ch_id) if ch_id else None
            if t == 'ln_invite' and rec is None:
                s = int(m['s'], 16)
                share_b = (s + self.b) % N
                A_a, B_a = parse_address(m['party_a'])
                sh = hs(pt_mul(self.b, A_a))            # ECDH: b_b*A_a == a_a*B_b
                share_a = ln_share_decrypt(sh, m['share_enc'], ch_id)
                x = (share_a + share_b - s) % N
                P_fund = pt_add(pt_mul(s, G), pt_add(B_a, self.B))
                self.channels[ch_id] = {
                    'peer_address': m['party_a'], 'role': 'b',
                    'capacity': int(m['capacity']), 'x': x, 's': s,
                    'mask_f': int(m['mask'], 16), 'share_self': share_b,
                    'share_peer': share_a,
                    'funding_outpoint': (ch_id, int(m['out_index'])),
                    'funding_P': P_fund, 'peer_B': B_a, 'seq': 0,
                    'bal_self': 0, 'bal_peer': int(m['capacity']),
                    'status': 'opening', 'closing_txid': None}
                # send our x-share back so the opener can compute the full key
                client.inbox_send(m['party_a'], {
                    'type': 'ln_share', 'ch_id': m['ch_id'],
                    'share_enc': ln_share_encrypt(sh, share_b, ch_id)})
                logs.append(f'LIGHTNING: channel invite accepted '
                            f'(capacity {fmt_coin(int(m["capacity"]))})')
            elif t == 'ln_share' and rec is not None and rec.get('x') is None:
                sh = hs(pt_mul(self.a, parse_address(rec['peer_address'])[1]))
                rec['share_peer'] = ln_share_decrypt(sh, m['share_enc'], ch_id)
                rec['x'] = (rec['share_self'] + rec['share_peer'] - rec['s']) % N
                logs.append('LIGHTNING: channel key exchange complete')
            elif t == 'ln_pay' and rec is not None:
                if rec['role'] == 'a':
                    amt = int(m['bal_b']) - rec['bal_peer']
                else:
                    amt = int(m['bal_a']) - rec['bal_peer']
                msg = ln_state_bytes(ch_id, int(m['seq']),
                                     int(m['bal_a']), int(m['bal_b']))
                R, s = schnorr_sign(msg, self.b)
                own_sig = {'R': point_json(R), 's': format(s, '064x')}
                if m.get('payer_is_a'):
                    sig_a, sig_b = m['sig'], own_sig
                else:
                    sig_a, sig_b = own_sig, m['sig']
                client.ln_confirm({'ch_id': m['ch_id'], 'seq': int(m['seq']),
                                   'bal_a': int(m['bal_a']), 'bal_b': int(m['bal_b']),
                                   'sig_a': sig_a, 'sig_b': sig_b})
                rec['seq'] = int(m['seq'])
                if rec['role'] == 'a':
                    rec['bal_self'], rec['bal_peer'] = int(m['bal_a']), int(m['bal_b'])
                else:
                    rec['bal_self'], rec['bal_peer'] = int(m['bal_b']), int(m['bal_a'])
                logs.append(f'LIGHTNING INSTANT: received {fmt_coin(abs(amt))} '
                            '-- no mining involved')
            elif t == 'ln_committed' and rec is not None:
                rec['seq'] = int(m['seq'])
                if rec['role'] == 'a':
                    rec['bal_self'], rec['bal_peer'] = int(m['bal_a']), int(m['bal_b'])
                else:
                    rec['bal_self'], rec['bal_peer'] = int(m['bal_b']), int(m['bal_a'])
            elif t == 'ln_close_req' and rec is not None:
                msg = ln_state_bytes(ch_id, int(m['seq']),
                                     int(m['bal_a']), int(m['bal_b']))
                R, s = schnorr_sign(msg, self.b)
                client.ln_close_sig({'ch_id': m['ch_id'],
                                     'sig': {'R': point_json(R),
                                             's': format(s, '064x')}})
                rec['status'] = 'closing'
                logs.append('LIGHTNING: channel close co-signed '
                            '(settlement tx can be mined)')
        return logs

    def ln_tick(self, client, address: str) -> List[str]:
        """One Lightning housekeeping round: inbox, funding confirmation,
        close co-signature pickup. Returns log lines."""
        logs: List[str] = []
        try:
            logs.extend(self.ln_process_inbox(client, address))
            for ch_id, rec in self.channels.items():
                st = client.ln_state(ch_id.hex())
                if rec['status'] == 'opening' and st.get('open') \
                        and rec.get('x') is not None:
                    rec['status'] = 'open'
                    logs.append(f'LIGHTNING: channel to '
                                f'"{rec["peer_address"][:16]}..." is OPEN '
                                f'(capacity {fmt_coin(rec["capacity"])}) '
                                '-- payments are now instant')
                if rec['status'] == 'closing':
                    cst = client.ln_close_status(ch_id.hex())
                    if cst.get('ready'):
                        logs.append('!close_ready:' + ch_id.hex())
        except NodeError:
            pass
        return logs

    def confirm_channel_closes(self, onchain_txids: set) -> int:
        n = 0
        for rec in self.channels.values():
            if rec['status'] == 'closing' and rec['closing_txid'] \
                    and rec['closing_txid'] in onchain_txids:
                rec['status'] = 'closed'
                n += 1
        return n

    def ln_total(self) -> int:
        return sum(rec['bal_self'] for rec in self.channels.values()
                   if rec['status'] == 'open')


def _self_test() -> str:
    """Fast algebraic sanity checks of every primitive. Returns report text."""
    assert pt_mul(N, G) is None
    assert pt_add(pt_mul(5, G), pt_mul(7, G)) == pt_mul(12, G)
    assert pt_add(commit(5, 11), commit(7, 3)) == commit(12, 14)
    assert commit(5, 11) != commit(5, 12)                       # binding
    a, b = random_scalar(), random_scalar()
    A, B = pt_mul(a, G), pt_mul(b, G)
    r = random_scalar()
    R, _ = stealth_pubkey(r, A)
    P_out = one_time_address(hs(pt_mul(r, A)), B)
    x = recognize_output(a, b, R, P_out)
    assert x is not None and pt_mul(x, G) == P_out
    assert recognize_output(random_scalar(), random_scalar(), R, P_out) is None
    keys = [random_scalar() for _ in range(5)]
    ring = [pt_mul(k, G) for k in keys]
    msg = b'self-test message'
    sig = lsag_sign(msg, ring, 2, keys[2])
    assert lsag_verify(msg, ring, sig)
    bad = LSAG(sig.key_image, sig.c0, list(sig.s))
    bad.s[0] = (bad.s[0] + 1) % N
    assert not lsag_verify(msg, ring, bad)
    C = commit(123_456, 777)
    rp = range_prove(123_456, 777, C, b'm')
    assert range_verify(C, rp, b'm')
    assert not range_verify(pt_add(C, G), rp, b'm')
    # JSON round-trips must be exact (txids are recomputed from reconstructed txs)
    t = build_coinbase_tx((A, B), 12345)
    assert tx_txid(tx_load(tx_json(t))) == tx_txid(t)
    # stealth address wire format round-trip
    A2, B2 = parse_address(address_str(A, B))
    assert (A2, B2) == (A, B)
    return ('[self-test] OK: curve, Pedersen, stealth, bLSAG, '
            'range proofs, JSON/address round-trips')


# ──────────────────────────────────────────────────────────────────────────────
# §11  NODE SERVICE  (thread-safe logic behind the node's REST API)
# ──────────────────────────────────────────────────────────────────────────────
class NodeService:
    """Owns the chain + mempool. The node app wraps this with an HTTP server;
    miner/wallet apps talk to it through NodeClient."""

    def __init__(self, datafile: str = 'node_chain.json'):
        self.datafile = datafile
        self.lock = threading.RLock()
        self.events: deque = deque(maxlen=1000)
        self._event_seq = 0
        self.chain: Optional[Blockchain] = None
        self.mempool: Optional[Mempool] = None
        self.templates: Dict[str, dict] = {}
        self.started = False
        # Lightning relay state (off-chain; persisted alongside the chain)
        self.ln_states: Dict[str, dict] = {}    # ch_id hex -> latest committed state
        self.ln_pending: Dict[str, dict] = {}   # ch_id hex -> state awaiting countersig
        self.ln_close_sigs: Dict[str, dict] = {}
        self.mailboxes: Dict[str, List[dict]] = {}
        # -- P2P sync (poll+push over HTTP; see the P2P section below) --------
        self.peers: List[str] = []                 # peer base URLs
        self.peer_status: Dict[str, dict] = {}     # url -> {'status', 'height'}
        self._peer_thread: Optional[threading.Thread] = None

    # -- event log (UIs poll this) ---------------------------------------------
    def log(self, kind: str, msg: str) -> None:
        self._event_seq += 1
        self.events.append({'seq': self._event_seq,
                            't': time.strftime('%H:%M:%S'), 'kind': kind, 'msg': msg})

    def events_since(self, seq: int) -> List[dict]:
        with self.lock:
            return [e for e in self.events if e['seq'] > seq]

    # -- lifecycle ---------------------------------------------------------------
    def start(self) -> str:
        with self.lock:
            if os.path.exists(self.datafile):
                self._load()
                self.mempool = Mempool(self.chain)
                # restore pending transactions that were waiting for a miner
                restored = 0
                for tj in getattr(self, '_saved_mempool', []):
                    try:
                        self.mempool.add(tx_load(tj))
                        restored += 1
                    except ValueError:
                        self.log('info', 'dropped a stale mempool tx on load')
                if restored:
                    self.log('ok', f'restored {restored} pending transaction(s) '
                                   'from the saved mempool')
                msg = f'loaded chain from {self.datafile} (height {len(self.chain.blocks) - 1})'
                self.log('info', msg)
            else:
                self.chain = Blockchain(calibrate_difficulty(TARGET_BLOCK_TIME))
                self.chain.accept_block(make_genesis())
                self.mempool = Mempool(self.chain)
                msg = (f'new chain: genesis created, '
                       f'initial difficulty {self.chain.initial_difficulty:,}')
                self.log('info', msg)
                self._save()
            self.started = True
            if self.peers:
                self._ensure_peer_thread()
            return msg

    def _save(self) -> None:
        data = {'initial_difficulty': self.chain.initial_difficulty,
                'blocks': [block_json(b) for b in self.chain.blocks],
                'mempool': [tx_json(t) for t in self.mempool.txs.values()],
                'peers': list(self.peers),
                'ln_states': self.ln_states,
                'ln_pending': self.ln_pending,
                'ln_close_sigs': self.ln_close_sigs,
                'mailboxes': self.mailboxes}
        with open(self.datafile, 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def _load(self) -> None:
        with open(self.datafile, 'r', encoding='utf-8') as f:
            data = json.load(f)
        chain = Blockchain(int(data.get('initial_difficulty', 0)) or None)
        for bd in data['blocks']:
            blk = block_load(bd)
            for t in blk.transactions:
                chain._apply_tx(t, chain.outputs, chain.spent_images, chain.channels)
            chain.blocks.append(blk)
        self.chain = chain
        self._saved_mempool = data.get('mempool', [])
        self.peers = list(data.get('peers', []))
        self.ln_states = data.get('ln_states', {})
        self.ln_pending = data.get('ln_pending', {})
        self.ln_close_sigs = data.get('ln_close_sigs', {})
        self.mailboxes = data.get('mailboxes', {})

    # -- read APIs -----------------------------------------------------------------
    def info(self) -> dict:
        with self.lock:
            tip = self.chain.blocks[-1].hash.hex() if self.chain.blocks else '-'
            return {'height': len(self.chain.blocks) - 1,
                    'tip_hash': tip,
                    'genesis': self.chain.blocks[0].hash.hex() if self.chain.blocks else '',
                    'difficulty': self.chain.next_difficulty(),
                    'mempool': len(self.mempool.txs),
                    'outputs': len(self.chain.outputs),
                    'spent_images': len(self.chain.spent_images),
                    'initial_difficulty': self.chain.initial_difficulty,
                    'emission': self.chain.emission(),
                    'max_supply': MAX_SUPPLY,
                    'next_subsidy': self.chain.subsidy(len(self.chain.blocks))}

    def blocks_summary(self) -> List[dict]:
        with self.lock:
            rows = []
            for blk in self.chain.blocks:
                cb = blk.transactions[0].coinbase_amount if blk.transactions else 0
                rows.append({'height': blk.height, 'hash': blk.hash.hex(),
                             'txs': len(blk.transactions), 'difficulty': blk.difficulty,
                             'timestamp': blk.timestamp, 'reward': cb})
            return rows

    def mempool_summary(self) -> List[dict]:
        with self.lock:
            rows = []
            for tid, tx in self.mempool.txs.items():
                rows.append({'txid': tid.hex(), 'fee': tx.fee,
                             'inputs': len(tx.inputs),
                             'rings': [len(ti.ring) for ti in tx.inputs],
                             'outputs': len(tx.outputs)})
            return rows

    def mempool_full(self) -> dict:
        """Full mempool transactions — lets wallets recognise UNCONFIRMED
        incoming payments (they are not spendable until mined)."""
        with self.lock:
            return {'txs': [tx_json(t) for t in self.mempool.txs.values()]}

    # -- P2P sync (poll + push over HTTP) ---------------------------------------
    #  Each node polls its peers (/api/info) every few seconds; whoever is
    #  behind pulls blocks (validated here from scratch — nothing is trusted),
    #  and newly accepted blocks/txs are pushed to all peers. Duplicates are
    #  ignored, so relay loops die immediately. Fork choice: the LONGER valid
    #  chain wins; switching requires a full re-validation (reorg by replay).
    # -------------------------------------------------------------------------

    def add_peer(self, url: str) -> dict:
        url = url.strip().rstrip('/')
        if '://' not in url:
            url = 'http://' + url
        if not url.startswith(('http://', 'https://')):
            raise NodeError('peer must be http:// or https:// URL')
        with self.lock:
            if url in self.peers:
                return {'ok': True, 'added': False, 'peers': len(self.peers)}
            self.peers.append(url)
            if self.started:
                self._save()
        self.log('ok', f'peer added: {url}')
        self._ensure_peer_thread()
        return {'ok': True, 'added': True, 'peers': len(self.peers)}

    def peers_status(self) -> List[dict]:
        with self.lock:
            return [{'url': u,
                     'status': self.peer_status.get(u, {}).get('status', 'connecting…'),
                     'height': self.peer_status.get(u, {}).get('height')}
                    for u in self.peers]

    def _ensure_peer_thread(self):
        if self._peer_thread is None or not self._peer_thread.is_alive():
            self._peer_thread = threading.Thread(target=self._peer_loop, daemon=True,
                                                 name='drivecoin-p2p')
            self._peer_thread.start()
            self.log('info', f'P2P sync started with {len(self.peers)} peer(s)')

    def _peer_loop(self):
        while True:
            for url in list(self.peers):
                try:
                    self._sync_one(url)
                except OSError:
                    self._peer_note(url, 'unreachable', None)
                except (ValueError, KeyError) as e:
                    self._peer_note(url, f'bad data: {e}', None)
            time.sleep(PEER_POLL_SECS)

    def _peer_note(self, url: str, status: str, height) -> None:
        with self.lock:
            prev = self.peer_status.get(url, {}).get('status')
            self.peer_status[url] = {'status': status, 'height': height}
        if prev != status:                      # log only on change — no spam
            self.log('info', f'peer {url}: {status}')

    def _sync_one(self, url: str) -> None:
        info = peer_http(url, 'GET', '/api/info')
        peer_len = int(info['height']) + 1
        with self.lock:
            my_len = len(self.chain.blocks)
            my_genesis = (self.chain.blocks[0].hash.hex() if my_len else '')
        if info.get('genesis', '') != my_genesis:
            if my_len <= 1:
                self._adopt_chain(url, info)    # fresh node joining a network
            else:
                self._peer_note(url, 'different network (other genesis)', None)
            return
        if peer_len <= my_len:
            self._pull_mempool(url)
            self._peer_note(url, 'in sync' if peer_len == my_len else 'behind us',
                            peer_len - 1)
            return
        # peer is ahead — find the common ancestor by walking back block hashes
        c = my_len - 1
        while c >= 0:
            pb = peer_http(url, 'GET', f'/api/block/{c}')
            with self.lock:
                my_hash = self.chain.blocks[c].hash.hex()
            if pb.get('hash') == my_hash:
                break
            c -= 1
        if c < 0:
            self._peer_note(url, 'no common ancestor', None)
            return
        branch = self._pull_blocks(url, c + 1, peer_len)
        if not branch:
            self._peer_note(url, 'pull failed', None)
            return
        with self.lock:
            if c == len(self.chain.blocks) - 1:
                applied = 0
                for bj in branch:
                    blk = block_load(bj)
                    try:
                        self._intake_block(blk, f'p2p sync from {url}')
                        applied += 1
                    except ValueError as e:
                        self.log('err', f'p2p: rejected block from peer: {e}')
                        break
            else:
                applied = len(branch) if self._reorg(c, branch) else 0
        self._pull_mempool(url)
        self._peer_note(url, 'synced', peer_len - 1)

    def _pull_blocks(self, url: str, start: int, end: int) -> List[dict]:
        """Fetch full blocks [start, end) from a peer in batches."""
        out: List[dict] = []
        while start < end:
            count = min(PULL_BATCH, end - start)
            d = peer_http(url, 'GET', f'/api/blocks/{start}/{count}')
            batch = d.get('blocks', [])
            if not batch:
                break
            out.extend(batch)
            start += len(batch)
        return out

    def _adopt_chain(self, url: str, info: dict) -> None:
        """A fresh node (own genesis only) joining a network: replace the
        whole chain with the peer's, validating every block from scratch."""
        init_diff = int(info.get('initial_difficulty') or 0) or None
        blocks = self._pull_blocks(url, 0, int(info['height']) + 1)
        if not blocks:
            self._peer_note(url, 'adoption failed (no blocks)', None)
            return
        candidate = Blockchain(init_diff)
        try:
            for bj in blocks:
                candidate.accept_block(block_load(bj))
        except ValueError as e:
            self.log('err', f'adoption rejected: {e}')
            self._peer_note(url, 'adoption failed (invalid chain)', None)
            return
        with self.lock:
            self.chain = candidate
            self.mempool = Mempool(candidate)
            self.templates.clear()
            self._save()
        self.log('ok', f'ADOPTED peer chain: height {len(candidate.blocks) - 1} '
                       f'from {url}')
        self._pull_mempool(url)
        self._peer_note(url, 'adopted', len(candidate.blocks) - 1)

    def _reorg(self, common: int, branch_json: List[dict]) -> bool:
        """Peer's fork is longer than ours: rebuild the candidate chain by
        REPLAYING our common prefix + the branch (every block re-validated),
        then swap. Mempool txs are re-added where still valid."""
        branch = [block_load(bj) for bj in branch_json]
        if not branch or branch[0].height != common + 1:
            return False
        with self.lock:
            prefix = list(self.chain.blocks[:common + 1])
            init_diff = self.chain.initial_difficulty
        candidate = Blockchain(init_diff)
        try:
            for blk in prefix:
                candidate.accept_block(blk)
            for blk in branch:
                candidate.accept_block(blk)
        except ValueError as e:
            self.log('err', f'reorg rejected (invalid branch): {e}')
            return False
        with self.lock:
            old_txs = list(self.mempool.txs.values())
            self.chain = candidate
            self.mempool = Mempool(candidate)
            for tx in old_txs:
                try:
                    self.mempool.add(tx)
                except ValueError:
                    pass                     # spent on the winning branch
            self.templates.clear()
            self._save()
        self.log('ok', f'REORG: adopted peer branch — new height '
                       f'{len(candidate.blocks) - 1} (common ancestor #{common})')
        return True

    def accept_external_block(self, bj: dict) -> dict:
        """Handle a block pushed by a peer. Keeps ours on equal-height
        competition; ignores duplicates; validates everything else."""
        try:
            blk = block_load(bj)
        except (ValueError, KeyError, TypeError) as e:
            raise NodeError(f'malformed block: {e}') from None
        with self.lock:
            my_len = len(self.chain.blocks)
            if blk.height < my_len:
                if self.chain.blocks[blk.height].hash == blk.hash:
                    return {'ok': True, 'height': blk.height, 'duplicate': True}
                return {'ok': False, 'reason': 'stale fork'}
            if blk.height > my_len:
                return {'ok': False, 'reason': 'future height — will pull later'}
            if blk.prev_hash != self.chain.blocks[-1].hash:
                return {'ok': False, 'reason': 'competing block — keeping ours'}
            self._intake_block(blk, 'p2p push')       # raises ValueError if bad
        self._relay(blk)
        return {'ok': True, 'height': blk.height, 'hash': blk.hash.hex()}

    def receive_peer_tx(self, txj: dict) -> dict:
        """Handle a mempool transaction pushed by a peer (relayed onward)."""
        try:
            tx = tx_load(txj)
        except (ValueError, KeyError, TypeError) as e:
            raise NodeError(f'malformed tx: {e}') from None
        try:
            tx = tx_load(txj)
            with self.lock:
                if tx_txid(tx) in self.mempool.txs:
                    return {'ok': True, 'duplicate': True}
                self.mempool.add(tx)
                self._save()
                self.log('ok', f'tx from peer accepted into mempool '
                               f'(fee {fmt_coin(tx.fee)})')
            self._relay_tx(tx)
            return {'ok': True, 'txid': tx_txid(tx).hex()}
        except ValueError as e:
            raise NodeError(str(e)) from None

    def _relay(self, blk: Block) -> None:
        """Push a newly accepted block to every peer (background thread —
        duplicates come back as {ok, duplicate} and are ignored)."""
        if not self.peers:
            return
        def push():
            with self.lock:
                bj = block_json(blk)
            for url in list(self.peers):
                try:
                    peer_http(url, 'POST', '/api/p2p/block', {'block': bj}, timeout=30)
                except OSError:
                    pass
        threading.Thread(target=push, daemon=True).start()

    def _relay_tx(self, tx: Transaction) -> None:
        if not self.peers:
            return
        tj = tx_json(tx)
        def push():
            for url in list(self.peers):
                try:
                    peer_http(url, 'POST', '/api/p2p/tx', {'tx': tj}, timeout=20)
                except OSError:
                    pass
        threading.Thread(target=push, daemon=True).start()

    def _pull_mempool(self, url: str) -> None:
        """Adopt still-valid transactions from a peer's mempool."""
        try:
            d = peer_http(url, 'GET', '/api/p2p/mempool', timeout=15)
        except OSError:
            return
        added = 0
        with self.lock:
            for tj in d.get('txs', []):
                try:
                    tx = tx_load(tj)
                    if tx_txid(tx) not in self.mempool.txs:
                        self.mempool.add(tx)
                        added += 1
                except ValueError:
                    continue
            if added:
                self._save()
        if added:
            self.log('ok', f'pulled {added} mempool tx(s) from a peer')

    # -- Lightning relay (off-chain messages; the chain is only touched on
    #    open/close) ----------------------------------------------------------------
    def inbox_send(self, to: str, msg: dict) -> dict:
        try:
            parse_address(to)
        except ValueError as e:
            raise NodeError(f'bad mailbox address: {e}')
        with self.lock:
            self.mailboxes.setdefault(to, []).append(msg)
        return {'ok': True}

    def inbox_pop(self, address: str) -> list:
        with self.lock:
            return self.mailboxes.pop(address, [])

    def _ch_info(self, ch_id_hex: str) -> dict:
        info = self.chain.channels.get(bytes.fromhex(ch_id_hex))
        if info is None:
            raise NodeError('unknown channel (funding tx not mined yet?)')
        return info

    def ln_pay(self, d: dict) -> dict:
        """Payer submits the next state, signed by the payer. Held until the
        receiver countersigns (ln_confirm) -- then it is committed instantly."""
        with self.lock:
            info = self._ch_info(d['ch_id'])
            if info['closed']:
                raise NodeError('channel is closed')
            cur = self.ln_states.get(d['ch_id'],
                                     {'seq': 0, 'bal_a': info['capacity'], 'bal_b': 0})
            seq, bal_a, bal_b = int(d['seq']), int(d['bal_a']), int(d['bal_b'])
            if seq != cur['seq'] + 1:
                raise NodeError(f'bad sequence number (want {cur["seq"] + 1})')
            if bal_a + bal_b != cur['bal_a'] + cur['bal_b']:
                raise NodeError('payment amount conservation violated')
            if min(bal_a, bal_b) < 0:
                raise NodeError('negative channel balance')
            if bal_a == cur['bal_a']:
                raise NodeError('zero-value payment')
            msg = ln_state_bytes(bytes.fromhex(d['ch_id']), seq, bal_a, bal_b)
            payer_is_a = bal_a < cur['bal_a']
            try:
                sig = (point_load(d['sig']['R']), int(d['sig']['s'], 16))
            except Exception:
                raise NodeError('malformed signature')
            vk = info['B_a'] if payer_is_a else info['B_b']
            if not schnorr_verify(msg, vk, *sig):
                raise NodeError('payer signature INVALID')
            self.ln_pending[d['ch_id']] = {'seq': seq, 'bal_a': bal_a, 'bal_b': bal_b,
                                           'sig_a': d['sig'] if payer_is_a else None,
                                           'sig_b': None if payer_is_a else d['sig']}
            to = info['party_b'] if payer_is_a else info['party_a']
            self.mailboxes.setdefault(to, []).append(
                {'type': 'ln_pay', 'ch_id': d['ch_id'], 'seq': seq,
                 'bal_a': bal_a, 'bal_b': bal_b, 'sig': d['sig'],
                 'payer_is_a': payer_is_a})
            return {'ok': True, 'status': 'pending countersignature'}

    def ln_confirm(self, d: dict) -> dict:
        """Receiver countersigns -> the state is committed instantly."""
        with self.lock:
            info = self._ch_info(d['ch_id'])
            pend = self.ln_pending.get(d['ch_id'])
            if pend is None:
                raise NodeError('no pending payment for this channel')
            seq, bal_a, bal_b = int(d['seq']), int(d['bal_a']), int(d['bal_b'])
            if (seq, bal_a, bal_b) != (pend['seq'], pend['bal_a'], pend['bal_b']):
                raise NodeError('confirmation does not match the pending payment')
            msg = ln_state_bytes(bytes.fromhex(d['ch_id']), seq, bal_a, bal_b)
            try:
                sig_a = (point_load(d['sig_a']['R']), int(d['sig_a']['s'], 16))
                sig_b = (point_load(d['sig_b']['R']), int(d['sig_b']['s'], 16))
            except Exception:
                raise NodeError('malformed signatures')
            if not schnorr_verify(msg, info['B_a'], *sig_a):
                raise NodeError('party A signature INVALID')
            if not schnorr_verify(msg, info['B_b'], *sig_b):
                raise NodeError('party B signature INVALID')
            self.ln_states[d['ch_id']] = {'seq': seq, 'bal_a': bal_a, 'bal_b': bal_b}
            self.ln_pending.pop(d['ch_id'], None)
            self._save()
            for to in (info['party_a'], info['party_b']):
                self.mailboxes.setdefault(to, []).append(
                    {'type': 'ln_committed', 'ch_id': d['ch_id'], 'seq': seq,
                     'bal_a': bal_a, 'bal_b': bal_b})
            return {'ok': True, 'status': 'committed instantly (no mining)'}

    def ln_close_req(self, d: dict) -> dict:
        with self.lock:
            info = self._ch_info(d['ch_id'])
            if info['closed']:
                raise NodeError('channel is closed')
            cur = self.ln_states.get(d['ch_id'],
                                     {'seq': 0, 'bal_a': info['capacity'], 'bal_b': 0})
            seq, bal_a, bal_b = int(d['seq']), int(d['bal_a']), int(d['bal_b'])
            # the closer pays CLOSE_FEE out of their own balance
            if min(bal_a, bal_b) < 0 or bal_a > cur['bal_a'] or bal_b > cur['bal_b'] \
                    or bal_a + bal_b != cur['bal_a'] + cur['bal_b'] - CLOSE_FEE:
                raise NodeError('close must settle the latest committed state '
                                '(closer pays the fixed settlement fee)')
            msg = ln_state_bytes(bytes.fromhex(d['ch_id']), seq, bal_a, bal_b)
            try:
                sig = (point_load(d['sig']['R']), int(d['sig']['s'], 16))
            except Exception:
                raise NodeError('malformed signature')
            if not (schnorr_verify(msg, info['B_a'], *sig)
                    or schnorr_verify(msg, info['B_b'], *sig)):
                raise NodeError('close signature INVALID')
            closer_is_a = schnorr_verify(msg, info['B_a'], *sig)
            self.ln_close_sigs[d['ch_id']] = {
                'seq': seq, 'bal_a': bal_a, 'bal_b': bal_b,
                'sig_a': d['sig'] if closer_is_a else None,
                'sig_b': None if closer_is_a else d['sig']}
            to = info['party_b'] if closer_is_a else info['party_a']
            self.mailboxes.setdefault(to, []).append(
                {'type': 'ln_close_req', 'ch_id': d['ch_id'], 'seq': seq,
                 'bal_a': bal_a, 'bal_b': bal_b})
            return {'ok': True, 'status': 'waiting for peer co-signature'}

    def ln_close_sig(self, d: dict) -> dict:
        with self.lock:
            st = self.ln_close_sigs.get(d['ch_id'])
            if st is None:
                raise NodeError('no close request for this channel')
            missing = 'sig_a' if st['sig_a'] is None else ('sig_b' if st['sig_b'] is None
                                                           else None)
            if missing is None:
                raise NodeError('already co-signed')
            info = self._ch_info(d['ch_id'])
            msg = ln_state_bytes(bytes.fromhex(d['ch_id']), int(st['seq']),
                                 int(st['bal_a']), int(st['bal_b']))
            try:
                sig = (point_load(d['sig']['R']), int(d['sig']['s'], 16))
            except Exception:
                raise NodeError('malformed signature')
            vk = info['B_a'] if missing == 'sig_a' else info['B_b']
            if not schnorr_verify(msg, vk, *sig):
                raise NodeError('co-signature INVALID')
            st[missing] = d['sig']
            self._save()
            return {'ok': True, 'status': 'ready to settle on-chain'}

    def ln_close_status(self, ch_id: str) -> dict:
        with self.lock:
            st = self.ln_close_sigs.get(ch_id)
            if st is None:
                return {'ready': False}
            return {'ready': st['sig_a'] is not None and st['sig_b'] is not None,
                    'state': st}

    def ln_state(self, ch_id: str) -> dict:
        with self.lock:
            info = self.chain.channels.get(bytes.fromhex(ch_id))
            if info is None:
                return {'open': False}
            st = self.ln_states.get(ch_id, {'seq': 0, 'bal_a': info['capacity'],
                                            'bal_b': 0})
            return {'open': not info['closed'], 'capacity': info['capacity'],
                    'party_a': info['party_a'], 'party_b': info['party_b'], **st}

    def full_chain(self) -> dict:
        with self.lock:
            return {'blocks': [block_json(b) for b in self.chain.blocks]}

    def outputs_list(self) -> List[dict]:
        with self.lock:
            return [{'txid': op[0].hex(), 'index': op[1],
                     'dest': point_json(rec.dest), 'commitment': point_json(rec.commitment),
                     'is_coinbase': rec.is_coinbase}
                    for op, rec in self.chain.outputs.items()]

    def scan_view(self) -> List[dict]:
        """Compact chain view for WALLET SCANNING (view-key recognition):
        per transaction -> txid, tx_pubkey and outputs (one-time dest,
        commitment, encrypted payload). No signatures/proofs — the node has
        already validated those; wallets only need recognition data. Privacy
        is preserved: amounts stay sealed until decrypted with the
        recipient's view key. This is the public 'rescan' surface used by
        the standalone miners (a few KB per block instead of MBs)."""
        out: List[dict] = []
        with self.lock:
            for blk in self.chain.blocks:
                for t in blk.transactions:
                    out.append({'txid': tx_txid(t).hex(),
                                'tx_pubkey': point_json(t.tx_pubkey),
                                'outputs': [{'dest': point_json(o.dest),
                                             'commitment': point_json(o.commitment),
                                             'payload': base64.b64encode(
                                                 o.payload).decode()}
                                            for o in t.outputs]})
        return out

    def block_detail(self, h: int) -> dict:
        """Full block JSON + own hash + txids (explorer & P2P sync use this)."""
        with self.lock:
            if not isinstance(h, int) or not (0 <= h < len(self.chain.blocks)):
                raise NodeError(f'block {h} not found')
            blk = self.chain.blocks[h]
            return {'height': h, 'hash': blk.hash.hex(), 'block': block_json(blk),
                    'txids': [tx_txid(t).hex() for t in blk.transactions]}

    def tx_detail(self, txid_hex: str) -> dict:
        """Find a transaction on chain (or in the mempool); height -1 = pending."""
        if len(txid_hex) != 64 or any(c not in '0123456789abcdef' for c in txid_hex):
            raise NodeError('txid must be 64 hex characters')
        tid = bytes.fromhex(txid_hex)
        with self.lock:
            for blk in self.chain.blocks:
                for t in blk.transactions:
                    if tx_txid(t) == tid:
                        return {'height': blk.height, 'txid': txid_hex,
                                'tx': tx_json(t)}
            for t in self.mempool.txs.values():
                if tx_txid(t) == tid:
                    return {'height': -1, 'txid': txid_hex, 'tx': tx_json(t)}
        raise NodeError('tx not found')

    def blocks_range(self, start: int, count: int) -> dict:
        """Full blocks [start, start+count) — the P2P pull endpoint."""
        with self.lock:
            n = len(self.chain.blocks)
            start = max(0, min(start, n))
            count = max(1, min(count, PULL_BATCH))
            return {'from': start,
                    'blocks': [block_json(b)
                               for b in self.chain.blocks[start:start + count]]}

    # -- write APIs -------------------------------------------------------------------
    def submit_tx(self, txj: dict) -> dict:
        try:
            tx = tx_load(txj)
            with self.lock:
                tid = self.mempool.add(tx)
                self._save()          # persist immediately -- survives node restarts
                self.log('ok', f'tx {tid.hex()[:16]}... accepted into mempool '
                               f'(fee {fmt_coin(tx.fee)}, {len(tx.inputs)} input(s))')
            self._relay_tx(tx)        # propagate to peers (P2P)
            return {'ok': True, 'txid': tid.hex()}
        except ValueError as e:
            self.log('err', f'tx REJECTED: {e}')
            raise NodeError(str(e))

    def block_template(self, address: str) -> dict:
        try:
            A, B = parse_address(address)
        except ValueError as e:
            raise NodeError(f'bad payout address: {e}')
        with self.lock:
            fees = sum(t.fee for t in self.mempool.txs.values())
            txs = sorted(self.mempool.txs.values(), key=lambda t: -t.fee)[:MAX_TXS_PER_BLOCK]
            reward = self.chain.subsidy(len(self.chain.blocks)) + fees
            coinbase = build_coinbase_tx((A, B), reward)
            all_txs = [coinbase] + txs
            root = merkle_root([tx_txid(t) for t in all_txs])
            height = len(self.chain.blocks)
            prev = self.chain.blocks[-1].hash if self.chain.blocks else b'\x00' * 32
            difficulty = self.chain.next_difficulty()
            ts = int(time.time())
            prefix = (enc_u32(height) + prev + root + enc_u64(ts) + enc_u64(difficulty))
            tid = secrets.token_hex(6)
            self.templates[tid] = {'coinbase': coinbase, 'txs': txs, 'height': height,
                                   'prev': prev, 'root': root, 'ts': ts,
                                   'difficulty': difficulty, 'reward': reward}
            # prune stale templates + cap total (public mining endpoint --
            # unbounded template spam must never grow node memory)
            for k in [k for k, v in self.templates.items() if v['height'] < height]:
                del self.templates[k]
            while len(self.templates) > MAX_TEMPLATES:
                del self.templates[next(iter(self.templates))]
            self.log('info', f'block template {tid} for height {height} '
                             f'(difficulty {difficulty:,}, reward {fmt_coin(reward)})')
            return {'template_id': tid, 'height': height, 'difficulty': difficulty,
                    'prefix_hex': prefix.hex(), 'reward': reward}

    def _intake_block(self, blk: Block, source: str) -> None:
        """Accept a fully-validated-format block onto the chain (caller must
        hold the lock; raises ValueError on any invalidity)."""
        self.chain.accept_block(blk)
        self.mempool.revalidate()
        self.templates.clear()
        self._save()
        self.log('ok', f'block #{blk.height} ACCEPTED ({source})  '
                       f'hash={blk.hash.hex()[:16]}...  txs={len(blk.transactions)}')

    def submit_block(self, template_id: str, nonce: int) -> dict:
        with self.lock:
            tpl = self.templates.get(template_id)
            if tpl is None:
                self.log('err', f'block rejected: unknown/stale template {template_id}')
                raise NodeError('unknown or stale template -- request a new one')
            blk = Block(tpl['height'], tpl['prev'], tpl['root'], tpl['ts'],
                        tpl['difficulty'], int(nonce), [tpl['coinbase']] + tpl['txs'])
            try:
                self._intake_block(blk, f'mined, nonce={nonce:,}')
            except ValueError as e:
                self.log('err', f'block #{tpl["height"]} REJECTED: {e}')
                raise NodeError(str(e))
            reward = tpl['reward']     # _intake_block clears all templates
        self._relay(blk)          # propagate to peers (P2P)
        return {'ok': True, 'height': blk.height, 'hash': blk.hash.hex(),
                'reward': reward}


class NodeError(Exception):
    """Raised client-side when the node rejects a request."""


# ── P2P transport ────────────────────────────────────────────────────────────
PEER_POLL_SECS = 5.0          # how often each node polls its peers
PULL_BATCH = 100              # blocks fetched per peer request


def peer_http(url: str, method: str, path: str, body=None,
              timeout: float = 20.0) -> dict:
    """JSON call to a peer node (custom User-Agent — Cloudflare's edge
    rejects the default Python UA on the public seed URL)."""
    base = url.strip().rstrip('/')
    if '://' not in base:
        base = 'http://' + base
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={'Content-Type': 'application/json',
                 'User-Agent': 'DriveCoinNode/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


class NodeClient:
    """Minimal HTTP/JSON client used by the miner and wallet apps."""

    def __init__(self, base: str = 'http://127.0.0.1:8000', timeout: float = 30):
        self.base = base.strip().rstrip('/')
        if '://' not in self.base:
            self.base = 'http://' + self.base
        self.timeout = timeout

    def _call(self, method: str, path: str, body: Optional[dict] = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={'Content-Type': 'application/json',
                     'User-Agent': 'DriveCoinClient/1.0'})   # Cloudflare rejects the default Python UA
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode()).get('error', f'HTTP {e.code}')
            except Exception:
                msg = f'HTTP {e.code}'
            raise NodeError(msg)
        except urllib.error.URLError as e:
            raise NodeError(f'cannot reach node at {self.base} ({e.reason})')
        except OSError as e:
            raise NodeError(f'cannot reach node at {self.base} ({e})')

    def info(self) -> dict:
        return self._call('GET', '/api/info')

    def blocks_summary(self) -> dict:
        return self._call('GET', '/api/chain')

    def chain_full(self) -> dict:
        return self._call('GET', '/api/chain?full=1')

    def outputs(self) -> list:
        return self._call('GET', '/api/outputs')

    def mempool(self) -> list:
        return self._call('GET', '/api/mempool')

    def mempool_full(self) -> dict:
        # path-based (no query) so it survives web proxies that strip query strings
        return self._call('GET', '/api/p2p/mempool')

    # -- Lightning relay -------------------------------------------------------
    def inbox_send(self, to: str, msg: dict) -> dict:
        return self._call('POST', '/api/inbox/send', {'to': to, 'msg': msg})

    def inbox_pop(self, address: str) -> list:
        return self._call('GET', '/api/inbox?address=' + address)

    def ln_pay(self, d: dict) -> dict:
        return self._call('POST', '/api/ln/pay', d)

    def ln_confirm(self, d: dict) -> dict:
        return self._call('POST', '/api/ln/confirm', d)

    def ln_close_req(self, d: dict) -> dict:
        return self._call('POST', '/api/ln/close-req', d)

    def ln_close_sig(self, d: dict) -> dict:
        return self._call('POST', '/api/ln/close-sig', d)

    def ln_close_status(self, ch_id: str) -> dict:
        return self._call('GET', '/api/ln/close-status?ch=' + ch_id)

    def ln_state(self, ch_id: str) -> dict:
        return self._call('GET', '/api/ln/state?ch=' + ch_id)

    def fetch_view(self) -> 'ChainView':
        """Convenience: full chain + output set in one ChainView (wallet ops)."""
        blocks = []
        # pull full blocks in batches via path-based endpoint (works through
        # web proxies that strip query strings — /api/blocks/<from>/<count>)
        start = 0
        while True:
            batch = self._call('GET', f'/api/blocks/{start}/{PULL_BATCH}')
            bjs = batch.get('blocks', [])
            if not bjs:
                break
            blocks.extend(block_load(b) for b in bjs)
            start += len(bjs)
            if len(bjs) < PULL_BATCH:
                break
        outputs: Dict[OutPoint, OutputRec] = {}
        for o in self.outputs():
            outputs[(bytes.fromhex(o['txid']), int(o['index']))] = OutputRec(
                point_load(o['dest']), point_load(o['commitment']),
                bool(o['is_coinbase']))
        return ChainView(blocks, outputs)

    def mine_block(self, payout_address: str) -> dict:
        """Grind and submit one block (the miner-app flow, headless-friendly)."""
        tpl = self.template(payout_address)
        prefix = bytes.fromhex(tpl['prefix_hex'])
        target = ((1 << 256) - 1) // max(1, tpl['difficulty'])
        nonce = 0
        while int.from_bytes(sha256d(prefix + enc_u64(nonce)), 'big') >= target:
            nonce += 1
        return self.submit_block(tpl['template_id'], nonce)

    def submit_tx(self, txj: dict) -> dict:
        return self._call('POST', '/api/tx', txj)

    def template(self, address: str) -> dict:
        return self._call('POST', '/api/template', {'address': address})

    def submit_block(self, template_id: str, nonce: int) -> dict:
        return self._call('POST', '/api/submitblock',
                          {'template_id': template_id, 'nonce': nonce})
