# PrivateChain Prototype — Private Blockchain แบบ CryptoNote (Monero-style)

Prototype ของบล็อกเชนที่เป็นส่วนตัว (privacy-preserving) สถาปัตยกรรมแบบ
**3 โปรแกรมแยกกันพร้อม UI** ใช้ Python 3.8+ และ standard library เท่านั้น

> **เว็บสาธารณะ**: <https://drivecoinproject.online> (landing page + API
> แบบ read-only + หน้าดาวน์โหลด miner `/download/`) และ **block explorer**
> ที่ <https://scan.drivecoinproject.online> — ดูบล็อก/tx/mempool ได้จากทุกที่
> (ผ่าน Cloudflare, TLS จาก Let's Encrypt)

> **Miner แบบ standalone**: CLI + GUI สำหรับ Windows/Linux ขุดเชนสาธารณะ
> ผ่าน HTTPS ได้เลยไม่ต้องมี node — ดาวน์โหลดที่
> <https://drivecoinproject.online/download/> หรือดู `README-MINER.md`

> **Node แบบ standalone**: รันเครือข่าย DriveCoin ของตัวเองผ่าน **P2P**
> (`--peer` เพื่อ join เครือข่ายสาธารณะ หรือรันเครือข่ายส่วนตัว) — CLI + GUI
> สำหรับ Windows/Linux + source zip (รวม `wallet_app.py` ไว้ใช้จ่ายเงิน) ที่หน้า
> เดียวกัน ดู `README-NODE.md`

> **Wallet แบบ standalone**: CLI + GUI สำหรับ Windows/Linux — สร้าง wallet,
> สแกนยอด, ส่ง tx แบบ private, Lightning channel — ดาวน์โหลดที่หน้าเดียวกัน
> ดู `README-WALLET.md`

## 1. การรัน 3 โปรแกรมพร้อมกัน

เปิด 3 terminal (หรือ double-click ทีละไฟล์) — เปิด node ก่อนเสมอ:

```bash
python node_app.py      # 1) Blockchain Node — กด "Start Node" (เปิด REST API port 8000)
python miner_app.py     # 2) Miner — วาง payout address, กด "START MINING"
python wallet_app.py    # 3) Wallet — สร้าง wallet, ส่งเงิน, ดูยอด
```

Workflow แนะนำ:

1. **node_app** → กด `Start Node` (สร้าง genesis อัตโนมัติ, บันทึกเชนลง `node_chain.json`)
2. **wallet_app** → `+ New wallet` สองรอบ (เช่น Alice, Bob) → เลือก Alice แล้วกด `Copy` ที่ address
3. **miner_app** → วาง address ของ Alice ลงช่อง payout → `START MINING`
   รางวัลบล็อก (50 COIN + fee) จะถูกจ่ายเข้า stealth address ของ Alice โดยตรง
4. กลับที่ **wallet_app** → กด `Refresh / scan` → Alice จะ "มองเห็น" ยอดด้วย view key
   (บนเชนไม่มีใครรู้ว่า output ไหนเป็นของใคร)
5. เลือก Alice → วาง address ของ Bob ในช่อง recipient, ใส่จำนวน → `SEND CONFIDENTIAL TRANSACTION`
6. **miner_app** → กด START MINING อีกครั้งเพื่อขุด transaction ลงบล็อก
7. Bob กด `Refresh` → ยอดเข้า — ทั้งหมดนี้จำนวนเงินถูกซ่อนด้วย Pedersen commitment
   ตัวตนผู้ส่งซ่อนใน ring signature และผู้รับซ่อนหลัง one-time stealth address

### ไฟล์

| ไฟล์ | บทบาท |
|---|---|
| `core.py` | เอนจินร่วม: crypto ทั้งหมด, เชน, validation, wallet, LN, JSON/API, NodeService |
| `node_app.py` | โปรแกรม **Blockchain** (UI: blocks/mempool/log + REST API + LN relay) |
| `miner_app.py` | โปรแกรม **Miner** (UI: start/stop, hashrate, payout address) |
| `wallet_app.py` | โปรแกรม **Wallet** (UI: หลาย wallet, ส่งเงิน, แท็บ Lightning, ประวัติ) |
| `privatechain.py` | เวอร์ชันไฟล์เดียวดั้งเดิม (demo รวม + attack simulation; reward คงที่ 50 COIN ก่อนมี halving/cap — เก็บไว้เป็นตัวอย่าง) |
| `test_e2e.py` | ทดสอบ end-to-end ผ่าน HTTP จริง รวม Lightning ครบวงจร (headless) |
| `node_headless.py` | REST API node แบบไม่มี UI สำหรับเซิร์ฟเวอร์ (systemd) |
| `miner_lib.py` | เอนจินขุดแบบ standalone (HTTP + multiprocess grinding + wallet scan) — ใช้ร่วมโดย miner.py/miner_ui.py |
| `miner.py` | **Miner CLI** แบบพกพา (Windows/Linux, pure stdlib) — ขุดเชนสาธารณะผ่าน HTTPS |
| `miner_ui.py` | **Miner GUI** แบบพกพา (Tkinter) — สร้าง/เปิด wallet, ขุด, ดูยอดสด |
| `README-MINER.md` | เอกสาร miner แบบ standalone (ใช้งาน, build, แก้ปัญหา) |
| `wallet_lib.py` | เอนจิน wallet แบบ standalone (fetch view, scan, send, LN helpers) |
| `wallet_cli.py` | **Wallet CLI** แบบพกพา (new/list/scan/send/ln) |
| `wallet_ui.py` | **Wallet GUI** แบบพกพา (Tkinter) — หลาย wallet, สด, ส่ง tx |
| `README-WALLET.md` | เอกสาร wallet แบบ standalone (ใช้งาน, build, แก้ปัญหา) |
| `README-NODE.md` | เอกสาร node แบบ standalone (รันเครือข่ายเอง, P2P, API, ความปลอดภัย) |
| `test_gui_flow.py` | ทดสอบ flow ของ 3 แอป UI ร่วมกัน (headless) |
| `diagnose.py` | เครื่องมือตรวจว่าเงินอยู่ไหน (on-chain / pending / unclaimed) |

การสื่อสารระหว่างโปรแกรม: Node เปิด REST API เฉพาะ `127.0.0.1`
(`GET /api/info`, `GET /api/chain?full=1`, `POST /api/tx`,
`POST /api/template`, `POST /api/submitblock` ฯลฯ) — wallet สแกนและเซ็น
transaction **ในเครื่องตัวเอง** (private key ไม่เคยออกจาก wallet) เหมือน
สถาปัตยกรรม wallet-node ของเชนจริง

### ทำไมส่งเงินแล้วยอดปลายทางไม่ขึ้นทันที?

ธุรกรรมมี 3 สถานะ — เหมือนเชนจริงทุกประการ:

```
ส่ง → [MEMPOOL: unconfirmed] → miner ขุดลงบล็อก → [CONFIRMED]
```

1. กด SEND แล้ว tx เข้า **mempool** ของ node (จำนวนเงินถูก "จอง" เป็น
   PENDING OUT ในฝั่งผู้ส่ง และผู้รับจะเห็น "unconfirmed incoming")
2. **ต้องมีคนขุด** — เปิด miner_app กด START MINING จน tx ถูกใส่บล็อก
   (tx ที่ค้างจะอยู่รอดแม้ปิด node เพราะ mempool ถูกบันทึกลงดิสก์แล้ว)
3. ผู้รับกด Refresh (หรือรอ auto-refresh 5 วิ) → ยอดเข้า

ถ้า node ถูกรีสตาร์ตก่อน tx ถูกขุด **ด้วยโค้ดเวอร์ชันเก่า** tx จะหาย —
เวอร์ชันใหม่เก็บ mempool ลงดิสก์แล้ว ส่วนกรณีค้างจริง ๆ ให้ใช้ปุ่ม
"Release pending" ใน wallet และตรวจสอบทั้งหมดด้วย:

```bash
python diagnose.py     # อ่าน node_chain.json + wallets.json แล้วสรุปว่าเงินอยู่ไหน
```

ถ้า difficulty พุ่งสูงจนขุดช้า (จากการเปิด-ปิด miner บ่อย) กด
**Reset difficulty** ใน node_app เพื่อรีเซ็ตเป็นค่าเริ่มต้น

### โอนเร็วแบบไม่ต้องรอ miner: Lightning (แท็บ "Lightning (instant)")

การโอน on-chain ต้องรอบล็อกถัดไปเสมอ (ธรรมชาติของบล็อกเชน) ถ้าต้องการ
**instant** ให้เปิด payment channel ครั้งเดียวแล้วโอนกันในนั้น:

```
เปิด channel (ขุด 1 บล็อก) ──► โอนใน channel: ~40 ms ไม่มีการขุด ──► ปิด channel (ขุด 1 บล็อก settle)
```

1. ฝ่าย A (ต้องมียอด on-chain) → แท็บ Lightning → วาง address ฝ่าย B,
   ใส่ capacity → **Open channel** (จ่าย fee 0.1 ครั้งเดียว คือค่ารถไป-กลับ)
2. ขุด 1 บล็อก → ทั้งสองฝ่ายเห็นสถานะ **open** ในตาราง channel อัตโนมัติ
   (wallet มี worker ตรวจ inbox ทุก ~1.2 วิ)
3. เลือก channel ในตาราง → ใส่ amount → **PAY INSTANTLY** — เงินเข้าทันที
   ทั้งสองฝ่ายเห็น "LIGHTNING INSTANT: received ..." ใน log โดยไม่มีบล็อกใหม่
4. **Close channel** เมื่อใช้งานเสร็จ → อีกฝ่าย co-sign อัตโนมัติ →
   settlement tx ลงเชนหลังขุด 1 บล็อก ยอดสุดท้ายกลับเป็น UTXO ปกติ

หลักการ: funding output เป็น 2-of-2 (`P = Hs(r·(Aₐ+A_b))·G + (Bₐ+B_b)`,
private key แยกเป็น share สองชิ้นแลกกันผ่าน ECDH) — จ่ายออกต้องได้ลายเซ็น
**Schnorr ทั้งสองฝ่าย** บน state `(seq, bal_a, bal_b)` เดียวกัน ทุก payment
คือการต่อ seq แล้วเซ็นกันใหม่ node relay ถือเฉพาะ state ล่าสุด
(ข้อจำกัด: ยังไม่มี revocation/timelock แบบ LN จริง — ดู §5)

---

## 1b. สถาปัตยกรรมระบบ

```
┌───────────────────────────── Wallet Layer ─────────────────────────────┐
│  view key a (สแกนหาเงินเข้า)   spend key b (ใช้จ่าย)                    │
│  สแกนบล็อกด้วย ECDH: s = Hs(a·R) → รู้จำนวนเงิน/decrypt payload        │
│  สร้าง tx: เลือก UTXO → ring → pseudo-commitment → proof ทั้งหมด        │
└──────────────────────────────┬─────────────────────────────────────────┘
                               ▼
┌────────────────────────── Cryptographic Layer ─────────────────────────┐
│  • Pedersen Commitment  C = v·G + m·H     (ซ่อนจำนวนเงิน, homomorphic) │
│  • Stealth Address      P = Hs(r·A)·G + B         (one-time address)   │
│  • bLSAG Ring Signature (ซ่อนตัวผู้ส่งท่ามกลาง decoy keys)              │
│  • Key Image  I = x·Hp(P)                 (กัน double-spend)           │
│  • Commitment-to-Zero proof              (ผูก C_p เข้ากับ input จริง)   │
└──────────────────────────────┬─────────────────────────────────────────┘
                               ▼
┌─────────────────────────── ZK / Proof Layer ───────────────────────────┐
│  Range Proof (mini-Bulletproofs):                                      │
│   v = Σ 2ⁱ·bᵢ  →  commit แต่ละบิต Cᵢ = bᵢ·G + rᵢ·H                     │
│   • OR-proof ต่อบิต (พิสูจน์ bᵢ ∈ {0,1})  • linking proof ผูกเข้ากับ C   │
│   ⇒ พิสูจน์ 0 ≤ v < 2³² โดย node ไม่รู้ v                                │
└──────────────────────────────┬─────────────────────────────────────────┘
                               ▼
┌──────────────────────── Blockchain / Node Layer ───────────────────────┐
│  Block (header + merkle root + txs)   Mempool (tx ที่ validate แล้ว)    │
│  PoW: SHA-256d(header) < target     Difficulty retarget ทุก 3 บล็อก    │
│  Validation: โครงสร้าง → key image → ring sig → zero-proof →          │
│              range proof → balance equation: ΣC_p − ΣC_out = fee·G    │
└────────────────────────────────────────────────────────────────────────┘
```

## 2. หลักคณิตศาสตร์ที่ใช้ (สรุปสั้น)

| เทคนิค | สมการหลัก | ทำหน้าที่ |
|---|---|---|
| Pedersen Commitment | `C = v·G + m·H` | ซ่อนจำนวนเงิน; ผลรวม commitment บวกกันได้ (homomorphic) |
| Stealth Address | `P = Hs(r·A)·G + B`, `x = Hs(a·R) + b` | ผู้รับได้ one-time key ใหม่ทุกครั้ง ตามหาไม่เจอ |
| bLSAG Ring Signature | `s_π = α − c_π·x`, `I = x·Hp(P)` | พิสูจน์ว่าเป็นเจ้าของ 1 ใน n keys โดยไม่บอกว่าตัวไหน |
| Key Image | `I = x·Hp(P)` (deterministic) | ใช้ซ้ำ → I ซ้ำ → node ปัดทิ้ง = กัน double-spend |
| Balance Equation | `ΣC_in − ΣC_out = fee·G` | ตรวจว่าไม่มีการพิมพ์เงิน โดยไม่เห็นยอด |
| Range Proof | bit-decomposition + OR sigma proof | พิสูจน์ `0 ≤ v < 2³²` แบบ zero-knowledge |
| Schnorr (Lightning) | `s = k − e·x`, ตรวจ `s·G + e·P = R` | ลายเซ็น state ของ channel — เร็วและเซ็นร่วมสองฝ่ายได้ |
| Channel 2-of-2 | `P = Hs(r·(Aₐ+A_b))·G + Bₐ+B_b` | funding output ที่ใครจ่ายออกฝ่ายเดียวไม่ได้ |

จุดที่ผู้อ่านมักสงสัย: **ทำไมต้องมี pseudo-commitment `C_p`?**
เพราะ validator ต้องรวมยอดด้วย commitment ของ input จริง แต่ห้ามเปิดเผยว่า
input ไหนคือตัวจริง ผู้ส่งจึง "re-mask" `C_p = v·G + a′·H` (mask ใหม่) แล้ว
พิสูจน์ด้วย ring signature อีกชั้นว่า `C_p − C_j` เป็นจุดที่รู้ discrete log
ฐาน H ได้เฉพาะเมื่อ `v_p = v_j` — คือ C_p มีมูลค่าเท่ากับ input จริงตัวใดตัวหนึ่ง
ใน ring โดยไม่บอกว่าตัวไหน (เทคนิคเดียวกับ Monero)

### นโยบายการเงิน (Monetary Policy)

```
reward(h) = min( 120 COIN >> (h ÷ 210,000) ,  50,000,000 COIN − emitted(h) )
```

- **Hard cap 50,000,000 COIN** — เหรียญถูกสร้างได้ครั้งเดียวผ่าน coinbase
  เมื่อ emitted ถึง cap แล้ว `subsidy()` คืน 0 ตลอดไป
- **Halving ทุก 210,000 บล็อก**: 120 → 60 → 30 → 15 → 7.5 → ... COIN/บล็อก
  (shift ในหน่วยเล็กสุด ทำให้ทศนิยม exact เสมอ)
- **ทำไมเริ่มที่ 120**: อนุกรมเรขาคณิต S₀ + S₀/2 + ... ลู่เข้า 2·S₀ ต่อช่วง
  จึงต้องกำหนด S₀ = ceil(50M ÷ 2 ÷ 210,000) = 120 — ตาราง halving เพียงลำพัง
  จะให้ ~50.4M แล้ว hard cap ตัดให้หยุดพอดี **50,000,000.000000 พอดี**
  (เป็นเทคนิคเดียวกับที่ Bitcoin ใช้ให้ได้ 21M)
- **Emission ตรวจสอบได้สาธารณะ**: net emission = Σ(coinbase) − Σ(fee ที่หมุน
  กลับ) คำนวณจากบล็อกจริงทั้งหมด — แสดงใน node_app (EMISSION / 50M CAP)
  และใน `python diagnose.py`
- **เชนเดิมใช้ต่อได้ทันที**: emission อ่านจากข้อมูลจริงบนเชน (เชนที่ขุดสมัย
  reward คงที่ 50 COIN จะถูกนับตามที่ขุดจริง) บล็อกใหม่ขึ้นไปใช้กฎใหม่ 120 COIN

## 3. โครงสร้าง `core.py`

| หัวข้อ | เนื้อหา |
|---|---|
| §1 | Elliptic curve secp256k1 + Jacobian coordinates (เร่งความเร็ว 5 เท่า) |
| §2 | Hashing, canonical encoding, hash_to_point (Hp) |
| §3 | Pedersen Commitments |
| §4 | Stealth Addresses + encrypted payload (ส่งยอด/ให้ receiver ไว้ใช้จ่ายต่อ) |
| §5 | bLSAG Ring Signatures + Key Images |
| §6 | Range Proofs (OR sigma proofs ต่อบิต + linking proof) |
| §6b | **Lightning: Schnorr, channel state, 2-of-2 funding, x-share แลกผ่าน ECDH** |
| §7 | โครงสร้าง Transaction + canonical serialization (รวม channel payload) |
| §8 | Block, Mempool, validation (รวม open/close channel), ChainView, genesis |
| §9 | JSON serialization (HTTP transport) + address encoding |
| §10 | Wallet (view-key scanning, tx construction, channels, persistence) |
| §11 | NodeService (API + LN relay/mailbox) + NodeClient |

## 4. คุณสมบัติด้านความเป็นส่วนตัวที่ระบบรับประกัน

1. **Amount privacy** — บนเชนมีแค่ commitment + proof ไม่มีจำนวนเงิน
   (ยกเว้น fee และ coinbase subsidy ที่ต้องตรวจ emission ได้)
2. **Receiver privacy** — ทุก output เป็น one-time stealth address
   ผู้ส่งเองก็ไม่รู้ key ของผู้รับนอกจาก address ที่ได้รับมา
3. **Sender privacy** — ring signature ซ่อน input จริงท่ามกลาง decoys
   (key image ไม่สามารถย้อนไปหา key ได้ — discrete log)
4. **Unforgeability** — ใช้เงินคนอื่นไม่ได้เพราะต้องรู้ one-time private key
5. **No inflation** — balance equation + range proof ปิดช่องพิมพ์เงิน
6. **No double-spend** — key image ซ้ำถูกปฏิเสธทั้งใน mempool และบนเชน

## 5. ข้อจำกัดของ Prototype (ตั้งใจให้เห็นชัด)

- Pure-Python crypto (ช้ากว่าไลบรารีจริง ~1000×), ring size ปรับตามจำนวน output
  ที่มี (production ต้อง fix ที่ 16+), ไม่มีเครือข่าย P2P, ไม่มี reorg handling,
  wallet อยู่ใน memory, การเลือก decoy ยังไม่มี anonymity set ที่ดี,
  ไม่มี timelock/multisig และยังไม่ผ่าน audit ใด ๆ — **ห้ามใช้กับเงินจริง**
- **Lightning ในรุ่นนี้เป็นแบบ cooperative-only**: node relay เก็บเฉพาะ state
  ล่าสุด และการ close ต้องได้ลายเซ็นทั้งสองฝ่ายเสมอ — ยังไม่มี
  revocation + timelock (penalty tx) แบบ LN จริง จึงต้อง "เชื่อใจ relay"
  ว่าจะไม่ยอมรับ state เก่า และอีกฝ่ายออนไลน์อยู่เสมอ (ผู้รับ co-sign
  เองใน ~1.2 วิ) ใน production ต้องเพิ่ม: LN-penalty (revocation key),
  HTLC + routing หลาย hop, watchtower, และ unilateral close พร้อม delay

## 6. Roadmap สู่ Production

**ระดับ Crypto:**
- เปลี่ยนไปใช้ curve25519 + Ristretto ผ่านไลบรารี audit แล้ว
  (`monero-crypto`, `curve25519-dalek`,libsodium)
- bLSAG → **CLSAG** (ลดขนาด ~25%, เร็วกว่า) และ Bulletproofs เต็มรูปแบบ
  (log-size) แทน bit-decomposition
- เพิ่ม multisig (Monero-style keys image aggregation), timelock,
  view-key auditing สำหรับ exchange/regulator

**ระดับ Protocol/Node:**
- P2P networking (libp2p / TCP gossip), transaction relay แบบ **Dandelion++**
  (กัน deanonymization จาก IP), พร้อมกัน DDoS ที่ mempool
- **RandomX** หรือ PoW ที่ ASIC-resistant, difficulty แบบ LWMA,
  reorg handling + chain fork choice rule (cumulative difficulty)
- Decoy selection algorithm แบบ Monero (กระจายตามอายุ output จริง),
  ring size คงที่, wallet sync แบบ prune + checkpoint
- กัน key-image spam/fee market, block size/weight limit, mempool eviction policy
- ทดสอบ: property-based testing, fuzzing, formal verification ของโปรโตคอล
  และ **security audit ภายนอก** ก่อน mainnet

**ระดับ Lightning (ต่อยอดจากของที่มี):**
- **LN-penalty**: revocation secret ต่อ state + penalty tx เพื่อลงโทษฝ่ายที่
  settle state เก่า (แทนการเชื่อ relay) พร้อม timelock ให้ฝ่ายที่ถูกขัดแต่ง
  ตอบโต้ได้
- **HTLC + multi-hop routing**: จ่ายข้ามหลาย channel (onion routing แบบ
  Sphinx) ไม่ต้องเปิด channel ตรงกับทุกร้าน
- **Unilateral close + watchtower**: ปิดช่องทางได้แม้อีกฝ่ายหาย
  (watchtower คอยตรวจแทน), splice-in/out เติม/ถอน capacity ไม่ต้องปิดช่อง
- ทำตามมาตรฐาน **BOLT #2/#3** และทดสอบ interop กับ implementation อื่น
  (lnd, c-lightning, eclair) บน testnet ก่อนใช้จริง
