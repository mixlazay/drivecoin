#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 PrivateChain v1.0 -- CryptoNote-style Confidential Blockchain (Prototype)
================================================================================
A self-contained, runnable prototype of a privacy-preserving blockchain that
combines the core technologies of Monero / CryptoNote and Confidential
Transactions:

  1. CRYPTOGRAPHIC LAYER
     - Pedersen Commitments           -> amounts are hidden (homomorphic)
     - CryptoNote Stealth Addresses   -> one-time destination per output
     - bLSAG Ring Signatures          -> sender hidden among decoy keys
     - Key Images  I = x*Hp(P)        -> double-spend prevention

  2. ZERO-KNOWLEDGE / PROOF LAYER
     - Range Proofs (bit-decomposition + OR sigma-protocols; a "mini
       Bulletproofs")                 -> prove 0 <= amount < 2^32 to any node
       without revealing the amount

  3. BLOCKCHAIN / MINER NODE LAYER
     - Block + Merkle tree, Mempool, Proof-of-Work (SHA-256d),
       difficulty retargeting, full transaction & block validation

The core value-conservation invariant every node checks on each transaction:

        sum(C_pseudo_inputs)  -  sum(C_outputs)  ==  fee * G

where every C is a Pedersen commitment. Amounts never appear in the clear
(except the public fee and the coinbase subsidy, which must be auditable).

Run:    python privatechain.py
Deps:   Python >= 3.8, standard library only.

DISCLAIMER: educational prototype -- pure-Python crypto, adaptive small rings,
no P2P networking, no wallet persistence. See README.md for the roadmap that
takes this to production grade.
================================================================================
Architecture -- data flow of one confidential transaction:

  Wallet (sender)                                   Node / Chain
  -------------------------                         ----------------------------
  pick UTXO  (one-time key P, x, value v, mask m)
  build ring  [decoy, ..., REAL, ...]  (outpoints only)
  pseudo-commitment  C_p = v*G + a'*H   (fresh mask a')
  outputs:  stealth one-time key  P_j = Hs(r*A)*G + B
            commitments          C_j = v_j*G + b_j*H
  proofs:   bLSAG ring signature     (knows x for one ring member; key image I)
            commitment-to-zero proof (C_p matches SOME ring commitment)
            range proofs             (every output value in [0, 2^32))
  ---------------------------------------------------------------------------->
  node verifies: ring sigs, zero-proofs, range proofs, key-image freshness,
                 and the balance equation above  ->  mempool  ->  mine  ->  chain
================================================================================
"""
from __future__ import annotations

import copy
import hashlib
import random as _random
import secrets
import struct
import time
from dataclasses import dataclass, field
from typing import Dict, List, NamedTuple, Optional, Set, Tuple

# ──────────────────────────────────────────────────────────────────────────────
# §0  PROTOCOL CONSTANTS
# ──────────────────────────────────────────────────────────────────────────────
COIN               = 1_000_000          # smallest displayable unit (like piconero)
SUBSIDY            = 50 * COIN          # block reward (production: halving schedule)
AMOUNT_BITS        = 32                 # range-proof width: amount < 2^32
RING_TARGET        = 5                  # target ring size (production: >= 16)
MIN_FEE            = 1_000              # fee floor (units)
MAX_TXS_PER_BLOCK  = 10
TARGET_BLOCK_TIME  = 1.0                # seconds (demo value; production: 120s)
RETARGET_INTERVAL  = 3                  # retarget every N blocks
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
# §1  ELLIPTIC CURVE -- secp256k1:  y^2 = x^3 + 7  over F_p   (cofactor 1)
#
#     All privacy primitives live on this curve. Points are affine tuples,
#     the identity (point at infinity) is None.
#     NOTE: cofactor 1 means every curve point is in the prime-order subgroup --
#     this is why secp256k1 needs no extra "subgroup checks" (Monero needs them
#     on ed25519 because its cofactor is 8).
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
# Affine addition needs one modular inversion per addition — the single most
# expensive operation (slow extended-gcd). Jacobian coordinates (X:Y:Z) with
# x = X/Z^2, y = Y/Z^3 defer ALL inversions to exactly one, at the very end of
# a scalar multiplication. This is the standard production technique.
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
# §2  HASHING / ENCODING  (canonical byte encodings -- everything that gets
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
    (Monero uses ge_fromfe_frombytes -- same purpose.)

    Math: pick candidate x, compute rhs = x^3 + 7; if rhs is a quadratic
    residue mod p, then y = rhs^((p+1)/4) is its square root (valid because
    p ≡ 3 (mod 4) for secp256k1). ~50% of candidates succeed, loop terminates
    fast. Cofactor 1 guarantees the result is in the prime-order subgroup.
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
#     H is a second generator with unknown discrete-log w.r.t. G -- otherwise
#     commitments would not be binding.
#
#     HOMOMORPHISM (the property the whole balance system rests on):
#        C(v1,m1) + C(v2,m2) = (v1+v2)*G + (m1+m2)*H = C(v1+v2, m1+m2)
#     => a node can add up all inputs and outputs WITHOUT seeing any amount,
#        and check:  sum(C_in) - sum(C_out) = fee*G   (no money created).
# ──────────────────────────────────────────────────────────────────────────────
H_PED = hash_to_point(b'Pedersen generator H')   # dlog_G(H) unknown by construction


def commit(v: int, m: int) -> Point:
    return pt_add(pt_mul(v, G), pt_mul(m, H_PED))


# ──────────────────────────────────────────────────────────────────────────────
# §4  STEALTH ADDRESSES  (CryptoNote one-time destinations)
#
#     Receiver long-term keys:  spend key  b  (public B = b*G)
#                               view  key  a  (public A = a*G)
#
#     Sender picks random r, publishes tx pubkey R = r*G and pays to
#        P = Hs(r*A)*G + B                (one-time address, unlinkable)
#     Receiver scans with the view key:  s = Hs(a*R)   (= Hs(r*A), ECDH!)
#        P == s*G + B  ?                  -> it's mine
#        one-time PRIVATE key:  x = s + b (mod N)   since P = (s + b)*G
#
#     Because r is fresh per transaction, every payment gets a brand-new
#     one-time address; on-chain observers cannot cluster outputs to a receiver.
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


def recognize_output(a: int, b: int, R: Point, P: Point) -> Optional[int]:
    """Receiver scan: is output key P addressed to me? Returns one-time key x."""
    s = hs(pt_mul(a, R))
    B = pt_mul(b, G)                       # receiver's spend public key
    if one_time_address(s, B) == P:
        return (s + b) % N                 # spend key for this output
    return None


def payload_keystream(s: int, out_idx: int) -> bytes:
    k1 = sha256(b'PCTX/PAYL1' + enc_scalar(s) + enc_u32(out_idx))
    k2 = sha256(b'PCTX/PAYL2' + k1)
    return k1 + k2[:8]                     # 40 bytes = 8 (amount) + 32 (mask)


def encrypt_payload(s: int, out_idx: int, v: int, mask: int) -> bytes:
    """Encrypt (amount, mask) to the receiver under the ECDH secret s.
    The commitment C alone hides the amount; this lets the RECEIVER learn
    (v, mask) later so they can re-mask it when spending (proving balance)."""
    plain = v.to_bytes(8, 'big') + int(mask).to_bytes(32, 'big')
    ks = payload_keystream(s, out_idx)
    return bytes(p ^ k for p, k in zip(plain, ks))


def decrypt_payload(s: int, out_idx: int, payload: bytes) -> Tuple[int, int]:
    ks = payload_keystream(s, out_idx)
    plain = bytes(p ^ k for p, k in zip(payload, ks))
    return int.from_bytes(plain[:8], 'big'), int.from_bytes(plain[8:], 'big')


# ──────────────────────────────────────────────────────────────────────────────
# §5  RING SIGNATURES -- bLSAG (Back's Linkable Spontaneous Anonymous Group)
#
#     Ring: public keys P_0..P_{n-1}; signer owns x with P_pi = x*G at hidden
#     index pi. Key image:  I = x*Hp(P_pi)   (Hp = hash_to_point)
#
#     Signing (Fiat-Shamir in the loop):
#       alpha <- random;  L_pi = alpha*G;  R_pi = alpha*Hp(P_pi)
#       c_{pi+1} = H(msg, I, L_pi, R_pi)
#       for i = pi+1 .. pi+n-1 (cyclic):
#           s_i <- random
#           L_i = s_i*G + c_i*P_i          R_i = s_i*Hp(P_i) + c_i*I
#           c_{i+1} = H(msg, I, L_i, R_i)
#       s_pi = alpha - c_pi*x   (mod N)        <- only the real signer can do this
#     Signature = (I, c_0, [s_0..s_{n-1}])
#
#     Verification recomputes the challenge chain around the ring; it closes
#     iff the signer knew SOME private key. Everyone else's position is
#     information-theoretically hidden (all (c_i, s_i) are random-looking).
#
#     LINKABILITY / DOUBLE-SPEND PROTECTION:
#       I = x*Hp(P) is deterministic in (x, P). Spending output P twice in two
#       different transactions yields the SAME I -> nodes reject the second one.
#       Yet I cannot be matched to any ring member without solving discrete log.
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class LSAG:
    key_image: Point
    c0: int
    s: List[int]


def lsag_sign(msg: bytes, ring: List[Point], pi: int, x: int, base: Point = G) -> LSAG:
    """`base` is the generator the secret key x refers to:
       - spend signatures:  base=G, ring = one-time output keys P_j
       - zero-proofs:       base=H, ring = {C_p - C_j} (mask difference lives
         on the H-axis: D_pi = (a'-m)*H)"""
    n = len(ring)
    hps = [hash_to_point(enc_point(p)) for p in ring]
    I = pt_mul(x, hps[pi])
    alpha = random_scalar()
    c: List[int] = [0] * n
    s: List[int] = [0] * n

    L = pt_mul(alpha, base)
    R = pt_mul(alpha, hps[pi])                       # R_pi binds I via x*Hp(P_pi)
    c[(pi + 1) % n] = H_scalar(TAG_LSAG, msg, enc_point(I), enc_point(L), enc_point(R))

    for k in range(1, n):
        i = (pi + k) % n
        s[i] = random_scalar()
        Li = pt_add(pt_mul(s[i], base), pt_mul(c[i], ring[i]))
        Ri = pt_add(pt_mul(s[i], hps[i]), pt_mul(c[i], I))
        c[(i + 1) % n] = H_scalar(TAG_LSAG, msg, enc_point(I), enc_point(Li), enc_point(Ri))

    s[pi] = (alpha - c[pi] * x) % N                  # closes the loop
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
    return c == sig.c0                               # chain must close the loop


# ──────────────────────────────────────────────────────────────────────────────
# §6  ZERO-KNOWLEDGE RANGE PROOF  ("mini Bulletproofs")
#
#     Goal: prove  0 <= v < 2^AMOUNT_BITS  for commitment  C = v*G + m*H
#     without revealing v or m.
#
#     Technique (Greg Maxwell's CT scheme, ancestor of Bulletproofs):
#     1) Bit decomposition:  v = sum_i 2^i * b_i,  commit each bit
#            C_i = b_i*G + r_i*H
#     2) Per-bit ZK proof that b_i in {0,1} -- an OR-composition (Cramer-Damgard)
#        of two Schnorr sigma-protocols over base H:
#            branch 0:  C_i - 0*G = r*H        (bit is 0)
#            branch 1:  C_i - 1*G = r*H        (bit is 1)
#        The prover is honest for exactly one branch; the other is faked with a
#        random challenge/response pair. A joint Fiat-Shamir challenge
#        e = H(..., A_0, A_1) with e_0 + e_1 = e makes both branches verify
#        simultaneously while hiding which one was real.
#     3) Linking proof:  D = sum_i 2^i*C_i - C  must equal (sum 2^i r_i - m)*H,
#        i.e. a plain Schnorr proof of knowledge of that scalar over base H.
#        This binds the bit-commitments to the REAL output commitment C --
#        without it, anyone could attach arbitrary C_i.
#
#     Production note: Bulletproofs replace the n bit-proofs with ONE
#     logarithmic-size inner-product argument (n -> log2(n) proof size).
#     The security argument (0 <= v < 2^n) is identical.
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class RangeProof:
    bit_points: List[Point]                    # C_i = b_i*G + r_i*H
    bit_proofs: List[Tuple[int, int, int, int]]  # (e0, z0, e1, z1) per bit
    link_A: Point                              # Schnorr commitment for linking
    link_e: int
    link_z: int


def _prove_bit(C_i: Point, b: int, r: int, msg: bytes, idx: int) -> Tuple[int, int, int, int]:
    """OR-proof:  C_i = b*G + r*H  with b in {0,1}."""
    Q = [C_i, pt_sub(C_i, G)]                  # Q_b = C_i - b*G  (statement per branch)
    fake = 1 - b
    e = [0, 0]
    z = [0, 0]
    # ---- fake the branch we are NOT proving (random challenge/response) ----
    e[fake] = random_scalar()
    z[fake] = random_scalar()
    A_fake = pt_sub(pt_mul(z[fake], H_PED), pt_mul(e[fake], Q[fake]))
    # ---- honest sigma-protocol on the real branch ----
    t = random_scalar()
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
    # rebuild both sigma-commitments from the revealed (e, z) pairs
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

    # linking: D = sum(2^i * C_i) - C = (sum(2^i r_i) - m) * H  -> prove dlog
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
    # (3) linking proof first -- binds bit-commitments to C
    acc: Point = None
    for i, cp in enumerate(rp.bit_points):
        acc = pt_add(acc, pt_mul(1 << i, cp))
    D = pt_sub(acc, C)
    lhs = pt_mul(rp.link_z, H_PED)
    rhs = pt_add(rp.link_A, pt_mul(rp.link_e, D))
    if lhs != rhs:
        return False
    # (2) every bit must be a valid 0/1
    for i, cp in enumerate(rp.bit_points):
        if not _verify_bit(cp, i, rp.bit_proofs[i], msg):
            return False
    return True


# ──────────────────────────────────────────────────────────────────────────────
# §7  TRANSACTION STRUCTURE + CANONICAL SERIALIZATION
#
#     Commitment-to-zero proof (per input): Monero's trick that lets a node
#     check balances while inputs stay hidden in rings. Define ring points
#         D_j = C_p - C_j      (pseudo-commitment minus each ring commitment)
#     For the real input j=pi:  D_pi = (a' - m)*H      <- lives on the H axis!
#     -> its discrete log w.r.t. the generator H is KNOWN to the spender
#        (= a' - m), so the zero-proof ring signature uses base H.
#     For decoys with v_p != v_j, D_j = dV*G + dM*H contains a G-component,
#     so its dlog w.r.t. H is unknown to everyone. A ring signature over
#     {D_j} therefore proves "C_p carries the SAME value as one ring member"
#     -- without saying which. Combined with the balance equation, money
#     creation is impossible.
# ──────────────────────────────────────────────────────────────────────────────
OutPoint = Tuple[bytes, int]                   # (txid, output index)


@dataclass
class TxIn:
    ring: List[OutPoint]                       # decoy + real outpoints (order shuffled)
    key_image: Point                           # I = x*Hp(P_real)
    pseudo_commitment: Point                   # C_p = v*G + a'*H (fresh mask)
    zero_proof: Optional[LSAG] = None          # ring sig over {C_p - C_j}
    lsag: Optional[LSAG] = None                # spend authorization over {P_j}


@dataclass
class TxOut:
    dest: Point                                # one-time address P_j
    commitment: Point                          # C_j = v_j*G + b_j*H
    payload: bytes                             # ECDH-encrypted (v_j, b_j)
    range_proof: Optional[RangeProof] = None


@dataclass
class Transaction:
    is_coinbase: bool
    fee: int                                   # public -- the only visible amount
    coinbase_amount: int                       # >0 only for coinbase (auditability)
    tx_pubkey: Point                           # R = r*G (stealth derivation hint)
    inputs: List[TxIn]
    outputs: List[TxOut]
    used_ops: List[OutPoint] = field(default_factory=list, repr=False)  # wallet bookkeeping


def tx_core_bytes(tx: Transaction) -> bytes:
    """Canonical serialization of everything EXCEPT signatures/proofs.
    This is what gets signed -> no circular dependency, and every proof
    binds itself to this hash."""
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
    """The hash every proof signs over (binds all inputs/outputs together)."""
    return sha256(TAG_MSG + tx_core_bytes(tx))


def tx_txid(tx: Transaction) -> bytes:
    return sha256d(TAG_TXID + tx_full_bytes(tx))


def coinbase_mask(P: Point) -> int:
    """Deterministic mask for coinbase outputs -> nodes can AUDIT emission:
    C_cb must equal subsidy*G + coinbase_mask(P)*H."""
    return H_scalar(TAG_CB, enc_point(P))


# ──────────────────────────────────────────────────────────────────────────────
# §8  BLOCK / CHAIN / MEMPOOL / MINER / VALIDATION
# ──────────────────────────────────────────────────────────────────────────────
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


class Blockchain:
    """The node's ledger state: blocks, the global output set (UTXO candidates)
    and the spent key-image set (the double-spend firewall)."""

    def __init__(self, initial_difficulty: Optional[int] = None):
        self.blocks: List[Block] = []
        self.outputs: Dict[OutPoint, OutputRec] = {}
        self.spent_images: Set[Point] = set()
        self.initial_difficulty = initial_difficulty or calibrate_difficulty(TARGET_BLOCK_TIME)

    # -- difficulty -----------------------------------------------------------
    def subsidy(self, height: int) -> int:
        return SUBSIDY                         # production: 50e12 >> (h // 2100000)

    def next_difficulty(self) -> int:
        """DASL-style simple retarget: D *= actual_time / expected_time,
        clamped to ±4x per retarget to resist timestamp manipulation."""
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
        nd = d * actual // int(expected)
        nd = max(d // 2, min(nd, d * 2))       # clamp the adjustment (±2x)
        return max(1, min(nd, 2 ** 24))

    @staticmethod
    def pow_target(difficulty: int):
        return ((1 << 256) - 1) // max(1, difficulty)

    # -- transaction validation ----------------------------------------------
    def validate_tx(self, tx: Transaction, *,
                    outputs: Optional[Dict[OutPoint, OutputRec]] = None,
                    spent_images: Optional[Set[Point]] = None,
                    pool_images: frozenset = frozenset()) -> None:
        """Full semantic validation. Raises ValueError with the reason.
        `outputs`/`spent_images` can be injected snapshots so blocks can be
        validated as if their earlier transactions were already applied."""
        outputs = self.outputs if outputs is None else outputs
        spent_images = self.spent_images if spent_images is None else spent_images

        if tx.is_coinbase:
            raise ValueError('coinbase transaction is only valid inside a block')
        if not tx.inputs or not tx.outputs:
            raise ValueError('transaction must have >=1 input and >=1 output')
        if tx.fee < MIN_FEE:
            raise ValueError(f'fee below minimum (fee={tx.fee})')

        # -- structural checks on outputs ------------------------------------
        for j, o in enumerate(tx.outputs):
            if not is_on_curve(o.dest) or not is_on_curve(o.commitment):
                raise ValueError(f'output {j}: malformed curve point')
            if len(o.payload) != PAYLOAD_LEN:
                raise ValueError(f'output {j}: bad encrypted payload length')

        msg = tx_message(tx)

        # -- per-input checks: ring sig + zero-proof + key image freshness ---
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

        # -- range proofs on every output (0 <= v_j < 2^32) -------------------
        for j, o in enumerate(tx.outputs):
            if o.range_proof is None or not range_verify(o.commitment, o.range_proof, msg):
                raise ValueError(f'range proof INVALID (output {j})')

        # -- balance equation: sum(C_p) - sum(C_out) == fee*G ------------------
        #    Homomorphic Pedersen check => outputs sum to inputs minus the
        #    public fee. Any attempt to mint value breaks this equality.
        acc: Point = None
        for ti in tx.inputs:
            acc = pt_add(acc, ti.pseudo_commitment)
        for o in tx.outputs:
            acc = pt_sub(acc, o.commitment)
        acc = pt_sub(acc, pt_mul(tx.fee, G))
        if acc is not None:
            raise ValueError('BALANCE EQUATION VIOLATED -- money creation attempt')

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
        for t in blk.transactions[1:]:
            self.validate_tx(t, outputs=st_outputs, spent_images=st_spent)
            self._apply_tx(t, st_outputs, st_spent)   # later txs can spend earlier ones

    def _apply_tx(self, tx: Transaction,
                  outputs: Dict[OutPoint, OutputRec], spent: Set[Point]) -> None:
        tid = tx_txid(tx)
        for j, o in enumerate(tx.outputs):
            outputs[(tid, j)] = OutputRec(o.dest, o.commitment, tx.is_coinbase)
        for ti in tx.inputs:
            spent.add(ti.key_image)

    def accept_block(self, blk: Block) -> None:
        self.validate_block(blk)
        for t in blk.transactions:
            self._apply_tx(t, self.outputs, self.spent_images)
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
        """After a new block, drop txs whose inputs were just consumed."""
        for tid in list(self.txs):
            try:
                self.chain.validate_tx(self.txs[tid], pool_images=self.pool_images())
            except ValueError:
                del self.txs[tid]


# -- Proof of Work --------------------------------------------------------------
def mine_block(chain: Blockchain, mempool: Mempool, miner: 'Wallet') -> Block:
    """Assemble a block: coinbase + mempool txs, then grind SHA-256d nonce."""
    txs = sorted(mempool.txs.values(), key=lambda t: -t.fee)[:MAX_TXS_PER_BLOCK]
    fees = sum(t.fee for t in txs)
    reward = chain.subsidy(len(chain.blocks)) + fees
    coinbase = build_coinbase_tx(miner, reward)

    all_txs = [coinbase] + txs
    root = merkle_root([tx_txid(t) for t in all_txs])
    difficulty = chain.next_difficulty()
    prev = chain.blocks[-1].hash if chain.blocks else b'\x00' * 32
    target = chain.pow_target(difficulty)
    ts = int(time.time())

    # Header prefix is fixed; only the nonce varies -> minimal per-nonce cost.
    prefix = (enc_u32(len(chain.blocks)) + prev + root +
              enc_u64(ts) + enc_u64(difficulty))
    nonce = 0
    while int.from_bytes(sha256d(prefix + enc_u64(nonce)), 'big') >= target:
        nonce += 1
    blk = Block(len(chain.blocks), prev, root, ts, difficulty, nonce, all_txs)

    chain.accept_block(blk)
    mempool.revalidate()
    return blk


def make_genesis() -> Block:
    blk = Block(0, b'\x00' * 32, merkle_root([]), int(time.time()), 1, 0, [])
    target = Blockchain.pow_target(1)          # difficulty 1 -> found instantly
    while int.from_bytes(sha256d(blk.header_bytes()), 'big') >= target:
        blk.nonce += 1
    return blk


def calibrate_difficulty(target_seconds: float) -> int:
    """Measure this machine's SHA-256d rate and pick a starting difficulty so
    demo blocks take roughly `target_seconds`."""
    t0 = time.perf_counter()
    x = b'calibration'
    n = 20_000
    for _ in range(n):
        x = sha256d(x)
    rate = n / (time.perf_counter() - t0)
    return max(1_000, min(int(rate * target_seconds * 0.9), 2 ** 24))


# ──────────────────────────────────────────────────────────────────────────────
# §9  WALLET  (view key scanning + transaction construction)
# ──────────────────────────────────────────────────────────────────────────────
class Wallet:
    """Holds (view key a, spend key b). Scans the chain with `a` only -- exactly
    like a Monero view-only wallet -- and derives one-time spend keys x = s + b."""

    def __init__(self, name: str):
        self.name = name
        self.a = random_scalar()               # view key (can see incoming txs)
        self.b = random_scalar()               # spend key (can move funds)
        self.A = pt_mul(self.a, G)
        self.B = pt_mul(self.b, G)
        self.known: Dict[OutPoint, Dict] = {}  # every output ever owned
        self.spent_ops: Set[OutPoint] = set()  # outputs this wallet consumed

    @property
    def address(self) -> Tuple[Point, Point]:
        return (self.A, self.B)

    @property
    def available(self) -> Dict[OutPoint, Dict]:
        return {op: rec for op, rec in self.known.items() if op not in self.spent_ops}

    def balance(self) -> int:
        return sum(rec['v'] for rec in self.available.values())

    # -- scanning (view-key only) ----------------------------------------------
    def scan(self, chain: Blockchain) -> int:
        found = 0
        for blk in chain.blocks:
            for tx in blk.transactions:
                tid = tx_txid(tx)
                s = hs(pt_mul(self.a, tx.tx_pubkey))       # ECDH from view key
                target = one_time_address(s, self.B)
                for j, o in enumerate(tx.outputs):
                    if o.dest != target:
                        continue                        # not ours -- skip silently
                    v, m = decrypt_payload(s, j, o.payload)
                    if commit(v, m) != o.commitment:
                        continue                        # tampered payload -- reject
                    op = (tid, j)
                    if op not in self.known:
                        self.known[op] = {'x': (s + self.b) % N, 'v': v, 'm': m,
                                          'P': o.dest, 'C': o.commitment}
                        found += 1
        return found

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
            raise ValueError(f'{self.name}: insufficient funds (have {total}, need {need})')
        return chosen

    def build_tx(self, chain: Blockchain,
                 recipients: List[Tuple[Tuple[Point, Point], int]], fee: int, *,
                 consume: bool = True,
                 force_outputs: Optional[List[OutPoint]] = None) -> Transaction:
        """Build a fully-signed confidential transaction.

        recipients: [((A,B), amount), ...] -- stealth addresses of receivers
        Value conservation: output masks b_j are chosen so that sum(b_j) equals
        sum of the input pseudo-masks a'_i; then
            sum(C_p) - sum(C_out) = fee*G   exactly.
        """
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
            P = one_time_address(s, B_r)
            C = commit(amt, masks[j])
            payload = encrypt_payload(s, j, amt, masks[j])
            tx_outs.append(TxOut(dest=P, commitment=C, payload=payload))
            amounts.append(amt)

        tx = Transaction(is_coinbase=False, fee=fee, coinbase_amount=0, tx_pubkey=R,
                         inputs=inputs, outputs=tx_outs,
                         used_ops=[op for op in selected] if consume else [])

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


def build_coinbase_tx(miner: 'Wallet', amount: int) -> Transaction:
    """Coinbase: amount is PUBLIC (emission must be auditable), but it is still
    wrapped in a Pedersen commitment with a deterministic mask so the output
    can be spent exactly like any confidential output."""
    r = random_scalar()
    R, s = stealth_pubkey(r, miner.A)
    P = one_time_address(s, miner.B)
    m = coinbase_mask(P)
    tx = Transaction(is_coinbase=True, fee=0, coinbase_amount=amount, tx_pubkey=R,
                     inputs=[], outputs=[TxOut(dest=P, commitment=commit(amount, m),
                                               payload=encrypt_payload(s, 0, amount, m))])
    msg = tx_message(tx)
    tx.outputs[0].range_proof = range_prove(amount, m, tx.outputs[0].commitment, msg)
    return tx


# ──────────────────────────────────────────────────────────────────────────────
# §10  SELF-TEST  (fast algebraic sanity checks of every primitive)
# ──────────────────────────────────────────────────────────────────────────────
def _self_test() -> None:
    # group order
    assert pt_mul(N, G) is None
    assert pt_add(pt_mul(5, G), pt_mul(7, G)) == pt_mul(12, G)
    # Pedersen homomorphism
    assert pt_add(commit(5, 11), commit(7, 3)) == commit(12, 14)
    assert commit(5, 11) != commit(5, 12)                       # binding
    # stealth address round-trip
    a, b = random_scalar(), random_scalar()
    A, B = pt_mul(a, G), pt_mul(b, G)
    r = random_scalar()
    R, _ = stealth_pubkey(r, A)
    P = one_time_address(hs(pt_mul(r, A)), B)
    x = recognize_output(a, b, R, P)
    assert x is not None and pt_mul(x, G) == P
    assert recognize_output(random_scalar(), random_scalar(), R, P) is None
    # ring signature round-trip + tamper detection
    keys = [random_scalar() for _ in range(5)]
    ring = [pt_mul(k, G) for k in keys]
    msg = b'self-test message'
    sig = lsag_sign(msg, ring, 2, keys[2])
    assert lsag_verify(msg, ring, sig)
    bad = LSAG(sig.key_image, sig.c0, list(sig.s))
    bad.s[0] = (bad.s[0] + 1) % N
    assert not lsag_verify(msg, ring, bad)
    assert sig.key_image == lsag_sign(b'other', ring, 2, keys[2]).key_image   # linkability
    # range proof round-trip + tamper detection
    C = commit(123_456, 777)
    rp = range_prove(123_456, 777, C, b'm')
    assert range_verify(C, rp, b'm')
    assert not range_verify(pt_add(C, G), rp, b'm')             # +1 unit breaks proof
    print('[self-test] all cryptographic primitives OK '
          '(curve, Pedersen, stealth, bLSAG, range proofs)')


# ──────────────────────────────────────────────────────────────────────────────
# §11  END-TO-END DEMO
# ──────────────────────────────────────────────────────────────────────────────
def hr(title: str) -> None:
    print('\n' + '=' * 78 + '\n ' + title + '\n' + '=' * 78)


def _short(h: bytes) -> str:
    return h.hex()[:16] + '...'


def _describe_block(blk: Block, mine_ms: float) -> None:
    print(f'  block #{blk.height:<2} hash={_short(blk.hash)} '
          f'txs={len(blk.transactions)} difficulty={blk.difficulty:,} '
          f'nonce={blk.nonce:,} mined in {mine_ms / 1000:.2f}s')


def demo() -> None:
    hr('PRIVATECHAIN DEMO -- confidential transactions end to end')

    # ---- §1 bootstrap --------------------------------------------------------
    hr('STEP 1 - Node bootstrap')
    chain = Blockchain()
    mempool = Mempool(chain)
    miner, alice, bob, carol = Wallet('Miner'), Wallet('Alice'), Wallet('Bob'), Wallet('Carol')
    print(f'  calibrated initial difficulty: {chain.initial_difficulty:,} '
          f'(target ~{TARGET_BLOCK_TIME:.0f}s/block on this machine)')
    for w in (miner, alice, bob, carol):
        print(f'  wallet {w.name:<6} address A={_short(sha256(enc_point(w.A)))} '
              f'B={_short(sha256(enc_point(w.B)))}')

    gen = make_genesis()
    chain.accept_block(gen)
    print(f'  genesis block hash={_short(gen.hash)}')

    # ---- §2 block #1: coinbase ------------------------------------------------
    hr('STEP 2 - Block #1 -- coinbase (50.0 COIN subsidy -> Miner, stealth output)')
    t0 = time.perf_counter()
    b1 = mine_block(chain, mempool, miner)
    _describe_block(b1, (time.perf_counter() - t0) * 1000)
    for w in (miner, alice, bob, carol):
        w.scan(chain)
    print(f'  Miner balance: {miner.balance() / COIN:.2f} COIN '
          f'(amount is hidden on-chain; only the Miner can see it)')

    # ---- §3 block #2: miner splits funds (faucet UTXOs) ------------------------
    hr('STEP 3 - Block #2 -- Miner splits funds into 5 UTXOs '
       '(note: ring size 1, only 1 output exists on-chain yet)')
    tx_split = miner.build_tx(chain, [(miner.address, 9_980_000)] * 5, fee=100_000)
    tid = mempool.add(tx_split)
    print(f'  [OK] tx {_short(tid)} accepted into mempool')
    t0 = time.perf_counter()
    b2 = mine_block(chain, mempool, miner)
    _describe_block(b2, (time.perf_counter() - t0) * 1000)
    for w in (miner, alice, bob, carol):
        w.scan(chain)

    # ---- §4 block #3: first real privacy payment -------------------------------
    hr('STEP 4 - Block #3 -- Miner -> Alice 20.0 COIN '
       '(ring signature, stealth address, range proofs)')
    tx_pay = miner.build_tx(chain, [(alice.address, 20_000_000)], fee=100_000)
    ti = tx_pay.inputs[0]
    print(f'  ring size={len(ti.ring)} (1 real + {len(ti.ring) - 1} decoys), '
          f'key image I={_short(sha256(enc_point(ti.key_image)))}')
    t0 = time.perf_counter()
    tid = mempool.add(tx_pay)
    print(f'  [OK] full validation in {(time.perf_counter() - t0) * 1000:.0f} ms '
          f'(ring sigs + zero-proofs + {AMOUNT_BITS}-bit range proofs + balance)')
    t0 = time.perf_counter()
    b3 = mine_block(chain, mempool, miner)
    _describe_block(b3, (time.perf_counter() - t0) * 1000)
    for w in (miner, alice, bob, carol):
        w.scan(chain)
    print(f'  Alice scanned with her VIEW KEY and found the payment invisibly: '
          f'{alice.balance() / COIN:.2f} COIN')

    # ---- §5 mempool double-spend conflict --------------------------------------
    hr('STEP 5 - ATTACK 1 -- double spend in the mempool (same UTXO, two txs)')
    tx_b = alice.build_tx(chain, [(carol.address, 8_000_000)], fee=100_000, consume=False)
    tx_a = alice.build_tx(chain, [(bob.address, 8_000_000)], fee=100_000)   # will be mined
    tid = mempool.add(tx_a)
    print(f'  [OK] tx A {_short(tid)} accepted (spends Alice\'s 20 COIN UTXO)')
    try:
        mempool.add(tx_b)
        print('  [FAIL] conflicting tx was accepted!')
    except ValueError as e:
        print(f'  [REJECTED] tx B -> {e}')

    # ---- §6 block #4 ------------------------------------------------------------
    hr('STEP 6 - Block #4 -- Alice -> Bob 8.0 COIN (tx A included)')
    t0 = time.perf_counter()
    b4 = mine_block(chain, mempool, miner)
    _describe_block(b4, (time.perf_counter() - t0) * 1000)
    for w in (miner, alice, bob, carol):
        w.scan(chain)

    hr('STEP 7 - ATTACK 2 -- replaying the SAME spent UTXO on-chain')
    try:
        chain.validate_tx(tx_b, pool_images=mempool.pool_images())
        print('  [FAIL] spent key image accepted!')
    except ValueError as e:
        print(f'  [REJECTED] -> {e}')

    # ---- §8 inflation attack -----------------------------------------------------
    hr('STEP 8 - ATTACK 3 -- minting money out of thin air (tampered commitment)')
    tx_ok = alice.build_tx(chain, [(carol.address, 2_000_000)], fee=100_000, consume=False)
    chain.validate_tx(tx_ok)                                   # sanity: original is valid
    bad = copy.deepcopy(tx_ok)
    bad.outputs[0].commitment = pt_add(bad.outputs[0].commitment, G)   # +1 unit
    try:
        chain.validate_tx(bad)
        print('  [FAIL] forged commitment accepted!')
    except ValueError as e:
        print(f'  [REJECTED] -> {e}')
    print('  (defense line 1: any tampering invalidates the signatures, because the')
    print('   ring signatures sign the hash of the whole transaction core)')

    # Defense in depth: EVEN IF an attacker could forge signatures, the
    # homomorphic Pedersen balance equation catches inflation on its own.
    # Balancing  out = in + minted  would require a mask shift
    #   m2 - m1 = minted * dlog_G(H)
    # and dlog_G(H) is unknown to everyone -- H was created by hashing, not by
    # multiplying G. So no mask choice can hide a minted amount.
    m1, m2 = random_scalar(), random_scalar()
    c_in, c_out = commit(20_000_000, m1), commit(20_000_001, m2)   # mint 1 unit
    residual = pt_sub(c_in, c_out)
    verdict = ('IDENTITY -- inflation slipped through (BAD!)' if residual is None
               else 'non-identity -> inflation caught by the balance equation')
    print(f'  (defense line 2: C_in - C_out = {verdict})')

    # ---- §9 block #5 + empty block (retarget demo) --------------------------------
    hr('STEP 9 - Block #5 -- Bob -> Carol 5.0 COIN, then an empty block (retarget)')
    tx_c = bob.build_tx(chain, [(carol.address, 5_000_000)], fee=100_000)
    tid = mempool.add(tx_c)
    print(f'  [OK] tx {_short(tid)} in mempool')
    t0 = time.perf_counter()
    b5 = mine_block(chain, mempool, miner)
    _describe_block(b5, (time.perf_counter() - t0) * 1000)
    t0 = time.perf_counter()
    b6 = mine_block(chain, mempool, miner)      # coinbase-only block
    _describe_block(b6, (time.perf_counter() - t0) * 1000)
    for w in (miner, alice, bob, carol):
        w.scan(chain)

    # ---- §10 final state ------------------------------------------------------------
    hr('FINAL STATE')
    print(f'  {"height":>6}  {"hash":<18} {"txs":>3}  {"difficulty":>12}')
    for blk in chain.blocks:
        print(f'  {blk.height:>6}  {_short(blk.hash):<18} {len(blk.transactions):>3}  '
              f'{blk.difficulty:>12,}')
    print()
    # Supply audit. Fees are burned by senders (leave the UTXO set) and
    # re-minted inside the including block's coinbase, so:
    #   net supply == sum(coinbase amounts) - sum(fees) == sum(subsidy)
    total_cb = sum(blk.transactions[0].coinbase_amount
                   for blk in chain.blocks if blk.height > 0)
    fees_recycled = sum(t.fee for blk in chain.blocks for t in blk.transactions[1:])
    minted = total_cb - fees_recycled
    total = 0
    for w in (miner, alice, bob, carol):
        bal = w.balance()
        total += bal
        print(f'  wallet {w.name:<6} balance {bal / COIN:>9.2f} COIN  '
              f'(UTXOs: {len(w.available)})')
    print(f'\n  coinbase outputs minted: {total_cb / COIN:.2f} COIN '
          f'(incl. {fees_recycled / COIN:.2f} COIN recycled fees)')
    print(f'  net new supply (= sum of block subsidies): {minted / COIN:.2f} COIN')
    print(f'  total held by wallets (read from hidden commitments): {total / COIN:.2f} COIN')
    print('  supply audit', 'PASSED -- no money was created or destroyed'
          if minted == total else 'FAILED')
    print('\nDemo complete. Every amount above was hidden on-chain behind Pedersen')
    print('commitments; every receiver behind a one-time stealth address; every')
    print('spender hidden inside a ring -- yet no double-spend or inflation passed.')


if __name__ == '__main__':
    _self_test()
    demo()
