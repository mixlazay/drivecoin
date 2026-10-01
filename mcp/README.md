# MCP System — DriveCoin Project (`/drivecoinproject`)

ระบบ MCP (Model Context Protocol) สำหรับโปรเจกต์บล็อกเชน ประกอบด้วย **2 server**
ที่รันอยู่บนเครื่องเซิร์ฟเวอร์และเชื่อมผ่าน SSH (stdio):

| Server | หน้าที่ | ที่รัน |
|---|---|---|
| `drivecoin-fs` | เข้าถึงไฟล์ทั้งหมดใน `/drivecoinproject` (อ่าน/เขียน/แก้ไขโค้ด) — ใช้ official `@modelcontextprotocol/server-filesystem` | Node 18 |
| `drivecoin-chain` | ควบคุมบล็อกเชน: ดูยอด/บล็อก, สร้าง wallet, ขุด, โอน on-chain, Lightning instant pay — เขียนเองแบบ pure stdlib (`chain_mcp.py`) | Python 3.12 |

สถาปัตยกรรม:

```
AI client (opencode / Claude Desktop / ฯลฯ)
    │  ssh dritestudio@82.26.104.210  (public-key auth)
    ├─► node  .../mcp/filesystem/.../server-filesystem dist /drivecoinproject
    └─► python3 /drivecoinproject/chain_mcp.py
              │ HTTP (127.0.0.1:8000)
              ▼
        systemd: drivecoin-node  →  node_headless.py
              (chain + mempool + Lightning relay, data: node_chain.json)

อินเทอร์เน็ต ──Cloudflare──► systemd: drivecoin-web → web_server.py (:443)
              drivecoinproject.online      → web/        (landing page)
              scan.drivecoinproject.online → web/scan/   (block explorer)
              (proxy read-only API ไปที่ 127.0.0.1:8000)
```

## เครื่องมือของ `drivecoin-chain` (13 tools)

- **Chain**: `get_chain_info`, `list_blocks`, `get_mempool`
- **Wallets**: `create_wallet`, `list_wallets`, `get_wallet`, `scan_wallets`
- **Mining**: `mine_blocks` (ขุดแบบ headless — template → grind → submit)
- **On-chain**: `send_payment` (Pedersen + stealth + ring sig — ต้องขุดยืนยัน)
- **Lightning**: `open_channel` (ขุด 1 บล็อก) → `ln_pay` (**instant ~40ms ไม่ต้องขุด**)
  → `ln_close` (peer co-sign อัตโนมัติ, ขุด 1 บล็อก settle), `get_channel`

## การใช้งาน

### OpenCode (เครื่องที่รัน opencode)
คัดลอกจาก `opencode.json.example` ใส่ใน `opencode.json` ของโปรเจกต์ หรือ:
```sh
opencode mcp add drivecoin-fs -- ssh dritestudio@82.26.104.210 node /drivecoinproject/mcp/filesystem/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js /drivecoinproject
opencode mcp add drivecoin-chain -- ssh dritestudio@82.26.104.210 python3 /drivecoinproject/chain_mcp.py
opencode mcp list     # ต้องขึ้น connected ทั้งสองตัว
```

### Claude Desktop
คัดลอกจาก `claude-desktop.json.example` ใส่ใน `claude_desktop_config.json`

### ตัวอย่างบทสนทนากับ AI
> "สร้าง wallet ชื่อ alice กับ bob แล้วขุด 2 บล็อกจ่าย alice"
> → `create_wallet` × 2, `mine_blocks {count:2, payout_wallet:"alice"}`

> "โอน 5 COIN จาก alice ไป bob แบบไม่ต้องรอขุด"
> → `open_channel`, `mine_blocks`, `ln_pay` (instant)

## การดูแลเซิร์ฟเวอร์

```sh
systemctl status drivecoin-node          # สถานะ node
journalctl -u drivecoin-node -f          # log
sudo systemctl restart drivecoin-node
python3 /drivecoinproject/test_e2e.py    # ชุดทดสอบเต็ม (headless)
python3 /drivecoinproject/diagnose.py    # สรุปว่าเงินอยู่ไหน
```

## เว็บสาธารณะ + Block Explorer

`web_server.py` (systemd `drivecoin-web`, port 443/80) แยกไซต์ตาม Host header:

| Host | เนื้อหา |
|---|---|
| `drivecoinproject.online`, `www` | landing page (`web/`) + สถิติเชนสด |
| `scan.drivecoinproject.online` | **block explorer** (`web/scan/`) |

Explorer ทำอะไรได้:
- หน้าแรก: stats สด (height, difficulty, emission/50M, next reward, mempool,
  countdown halving) + ตารางบล็อกล่าสุด + mempool — refresh ทุก 5 วินาที
- คลิกบล็อก → header (hash, prev, merkle, difficulty, nonce) + รายการ tx
- คลิก tx → inputs (ring size, key image), outputs (one-time address,
  commitment, range proof ✓) — **ไม่แสดงจำนวนเงิน** เพราะซ่อนอยู่ใน commitment
- ช่องค้นหา: block height / txid (64 hex) / block hash (เต็มหรือ prefix)

API สาธารณะที่ web_server proxy (read-only เท่านั้น):
`GET /api/info`, `GET /api/chain` (ตัดเหลือ 100 บล็อกล่าสุด), `GET /api/mempool`,
`GET /api/block/<height>` และ `GET /api/tx/<txid>` (แบบ **compact** — ตัด range
proofs/signatures/encrypted payloads ทิ้ง ให้บล็อกละไม่กี่ KB แทนไม่กี่ MB;
node ต้นทางมี data เต็มอยู่ที่ `127.0.0.1:8000` ซึ่งเปิดเฉพาะภายใน)

การขุด/โอน/Lightning ยังเป็น private — ทำผ่าน MCP (`drivecoin-chain`)
หรือ SSH tunnel เท่านั้น

**TLS**: cert จาก Let's Encrypt (DNS-01 ผ่าน Cloudflare API) ครอบ 3 ชื่อ:
`drivecoinproject.online`, `www`, `scan` — หมดอายุ **27 Dec 2026**
ต่ออายุด้วย `acme_dns.py` บนเครื่อง dev (ต้องมี Cloudflare token
สิทธิ์ `dns_records:edit`) แล้วเรียก `install_cert.py` + restart `drivecoin-web`

```sh
sudo systemctl restart drivecoin-web    # หลังแก้ web/ หรือ web_server.py
journalctl -u drivecoin-web -f          # log ของ web
```

## Miner, Node และ Wallet แบบ standalone (สาธารณะ) — `/download/`

หน้า <https://drivecoinproject.online/download/> ให้ดาวน์โหลด 3 แพ็กเกจ
(build ด้วย PyInstaller — ไฟล์อยู่ที่ `web/download/`):

**Miner** (ขุดเชนสาธารณะผ่าน HTTPS):

| ไฟล์ | แพลตฟอร์ม |
|---|---|
| `drivecoin-miner-cli-windows-x64.exe` / `-linux-x64` | CLI |
| `drivecoin-miner-ui-windows-x64.exe` / `-linux-x64` | GUI (Tkinter) |
| `drivecoin-miner-src-1.0.zip` | source (miner.py, miner_lib.py, miner_ui.py, core.py, README-MINER.md) |

**Node** (รันเครือข่ายของตัวเองผ่าน P2P — `--peer` เพื่อ join เครือข่ายสาธารณะ
หรือรันเครือข่ายส่วนตัว):

| ไฟล์ | แพลตฟอร์ม |
|---|---|
| `drivecoin-node-cli-windows-x64.exe` / `-linux-x64` | CLI (node_headless.py) |
| `drivecoin-node-ui-windows-x64.exe` / `-linux-x64` | GUI (node_app.py) |
| `drivecoin-node-src-1.0.zip` | source (node_headless, node_app, wallet_app, core, README-NODE.md) |

**Wallet** (จัดการเงิน: สร้าง/สแกน/ส่ง/Lightning — format `wallets.json` เดียวกับ
`wallet_app.py`):

| ไฟล์ | แพลตฟอร์ม |
|---|---|
| `drivecoin-wallet-cli-windows-x64.exe` / `-linux-x64` | CLI (wallet_cli.py) |
| `drivecoin-wallet-ui-windows-x64.exe` / `-linux-x64` | GUI (wallet_ui.py) |
| `drivecoin-wallet-src-1.0.zip` | source (wallet_cli, wallet_ui, wallet_lib, core, README-WALLET.md) |

`checksums.txt` = SHA-256 ทั้งหมด (15 ไฟล์, สร้างด้วย `sha256sum` ในโฟลเดอร์)

หมายเหตุสำคัญเรื่อง path ของ PyInstaller: ใน onefile `__file__` ชี้โฟลเดอร์
temp ที่ถูกลบเมื่อปิดโปรแกรม — `node_headless.py` และ `node_app.py` จึงใช้
`app_dir()`/`sys.executable` ตรวจ `sys.frozen` เพื่อเก็บ `node_chain.json`
**ข้างตัว executable** (ทดสอบแล้ว: restart แล้วเชนอยู่ครบ)

การขุดสาธารณะ**เปิดโดยตั้งใจ** (เหมือนบล็อกเชนจริงทุกเหรียญ) — web_server
เปิด endpoint เขียน 2 ตัวสาธารณะ พร้อมกันคันโยก:

- `POST /api/template {address}` — token bucket 200 burst / 3 ต่อวินาที ต่อ IP
  (อ่าน IP จริงจาก `CF-Connecting-IP`), body จำกัด 4KB, address ต้องเป็น string
- `POST /api/submitblock {template_id, nonce}` — ตรวจ format เข้ม
  (template_id hex ≤32, nonce int < 2⁶⁴) แล้วส่งต่อให้ node
- `GET /api/scan` — chain view แบบ compact สำหรับ wallet scan ของ miner
  (มีแค่ txid/tx_pubkey/outputs — ไม่มี proofs; node ตรวจไปแล้ว)
- `GET /api/outputs` — output pool ทั้งหมด (decoy สำหรับ ring ตอน wallet ส่ง tx)
- `GET /api/blocks/<from>/<count>` — ดึงบล็อกช่วงแบบ full (path ล้วน ไม่มี
  query string ซึ่ง web proxy จะตัด — NodeClient ใช้ endpoint นี้ในการ
  fetch view สำหรับสร้าง ring)

การป้องกันฝั่ง node: `MAX_TEMPLATES=500` (template เก่าโดน prune ทันทีที่มี
บล็อกใหม่ + cap จำนวน), PoW validate ทุกบล็อก, template timeout 900 วินาที

**ที่ยังเป็น private** (127.0.0.1 เท่านั้น): Lightning relay, wallet management —
ผ่าน MCP หรือ SSH tunnel. (หมายเหตุ: `POST /api/tx` ฝั่ง node ยังเป็น internal —
การโอนเงินจริงทำผ่าน MCP `send_payment` หรือ SSH tunnel เท่านั้น;
`/api/outputs` + `/api/blocks` ที่เปิดสาธารณะเป็น read-only chain data ทั้งสิ้น
ไม่เปิดทางให้ใช้เงินโดยไม่ผ่าน node)

สร้าง wallet ใน miner แล้วอยากโอนเงิน: ไฟล์ `miner_wallet.json`
ใช้เปิดกับ `wallet_app.py` ได้ตรงๆ (SSH tunnel `-L 8000:127.0.0.1:8000`)

- Node API bind **127.0.0.1 เท่านั้น** (ไม่มี authentication — ห้ามเปิดสู่ภายนอก)
  ถ้าต้องการใช้ wallet GUI บนเครื่องตัวเองกับเชนนี้ ใช้ SSH tunnel:
  `ssh -L 8000:127.0.0.1:8000 dritestudio@82.26.104.210` แล้วชี้ wallet_app
  ไปที่ `http://127.0.0.1:8000`
- Wallet ฝั่งเซิร์ฟเวอร์เก็บใน `/drivecoinproject/wallets.json` (keys อยู่ในไฟล์นี้ —
  จำกัดสิทธิ์ `chmod 600` และอย่า commit ลง repo สาธารณะ)
- เชนนี้เป็น**เชนใหม่** (genesis ใหม่ ไม่เกี่ยวกับเชนสาธิตบนเครื่อง Windows)
