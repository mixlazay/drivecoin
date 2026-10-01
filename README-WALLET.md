# DriveCoin Wallet — standalone CLI + GUI (Windows / Linux)

โปรแกรม Wallet แบบพกพา สำหรับจัดการเงินบนบล็อกเชน DriveCoin — สร้าง wallet,
สแกนหาเงินเข้า (view key), ส่ง transaction แบบ private (ring sig + stealth +
range proofs), จัดการ Lightning channel

> ⚠️ **Prototype เพื่อการศึกษา** — เหรียญไม่มีมูลค่าจริง คีย์ wallet อยู่ที่
> เครื่องคุณเท่านั้น เก็บไฟล์ `wallets.json` ให้ดี หาย = เงินหาย

## ดาวน์โหลด

| ไฟล์ | แพลตฟอร์ม |
|---|---|
| `drivecoin-wallet-cli-windows-x64.exe` | Windows 10/11 (CMD/PowerShell) |
| `drivecoin-wallet-cli-linux-x64` | Linux x64 (terminal) |
| `drivecoin-wallet-ui-windows-x64.exe` | Windows 10/11 (GUI) |
| `drivecoin-wallet-ui-linux-x64` | Linux x64 (GUI) |
| `drivecoin-wallet-src-1.0.zip` | source (ทุกแพลตฟอร์ม ต้องมี Python 3.8+) |

checksum ทั้งหมดอยู่ใน `checksums.txt`

> ไฟล์ .exe บางครั้งโดน antivirus ตั้งความสงสัย (PyInstaller false positive) —
> ถ้าไม่มั่นใจใช้ source zip แล้วรันด้วย Python ฟังก์ชันเหมือนกัน 100%

## ใช้งาน CLI

```
drivecoin-wallet-cli-windows-x64.exe new alice           # สร้าง wallet
drivecoin-wallet-cli-windows-x64.exe address alice       # ดู address รับเงิน
drivecoin-wallet-cli-windows-x64.exe list                # รายการ + ยอด (สแกน chain)
drivecoin-wallet-cli-windows-x64.exe scan                # รีเฟรชยอดทุก wallet
drivecoin-wallet-cli-windows-x64.exe send alice <addr> 10 --fee 0.001   # ส่งเงิน
drivecoin-wallet-cli-windows-x64.exe ln alice status     # ดู Lightning balance
drivecoin-wallet-cli-windows-x64.exe ln alice open <peer> 50   # เปิด channel
drivecoin-wallet-cli-windows-x64.exe ln alice pay <ch_id> 1    # จ่าย instant
drivecoin-wallet-cli-windows-x64.exe ln alice close <ch_id>    # ปิด channel
```

ตัวเลือก: `--node URL` (default `http://127.0.0.1:8000`), `--wallet FILE`
(default `wallets.json`)

**หมายเหตุ node สาธารณะ**: ถ้าใช้ `--node https://scan.drivecoinproject.online`
ต้องมี wallet ที่มีเงินบนเชนนั้นก่อน (ขุดด้วย miner หรือโอนเข้า)

## ใช้งาน GUI

1. เปิดโปรแกรม → **Node:** ใส่ URL (ค่า default `127.0.0.1:8000`)
2. **New wallet…** (ครั้งแรก) → ตั้งชื่อ → สร้างไฟล์ `wallets.json`
3. **Refresh / scan** → ดูยอด (สแกน chain ด้วย view key)
4. ส่งเงิน: ใส่ To (address ปลายทาง) + Amount + Fee → **SEND**
5. ดูผลใน Log ล่าง — tx เข้า mempool → รอ miner ขุด → Refresh อีกทีเห็นยอดใหม่

## Lightning channel (CLI เท่านั้น)

```
ln <wallet> open <peer_address> <capacity>   # เปิด channel (รอ 1 block)
ln <wallet> pay <ch_id> <amount>             # จ่าย ~40ms ไม่ต้องขุด
ln <wallet> close <ch_id>                    # ปิด + settle บน chain
ln <wallet> status                           # ดู channel ทั้งหมด
```

Wallet GUI ยังไม่มีแท็บ Lightning — ใช้ CLI หรือ `wallet_app.py` (จาก source
zip หลัก) ถ้าต้องการ GUI แบบมี Lightning เต็มรูปแบบ

## หลักการทำงาน (ความเป็นส่วนตัว)

- **สแกนด้วย view key**: wallet คำนวณ `s = Hs(a·R)` ทุก tx → ถ้า one-time
  address ตรงกับของเรา → ถอด payload (จำนวนเงิน + mask) ด้วย ECDH — เฉพาะ
  เจ้าของ view key เท่านั้นที่รู้ยอด
- **ส่งด้วย ring signature**: เลือก input จริง + decoy จาก output pool →
  สร้าง pseudo-commitment `C_p = v·G + a'·G` → bLSAG sign ซ่อนตัวผู้ส่ง →
  range proof พิสูจน์ `0 ≤ v < 2³²` โดย node ไม่รู้ `v`
- **Stealth address**: ทุก output จ่ายเข้า one-time key `P = Hs(r·A)·G + B`
  — explorer เห็นแค่ address ชั่วคราว ไม่ผูกกับ wallet คุณ

## Build เองจาก source

```
pip install pyinstaller
pyinstaller --onefile --name drivecoin-wallet-cli --hidden-import core --hidden-import wallet_lib wallet_cli.py
pyinstaller --onefile --windowed --name drivecoin-wallet-ui --hidden-import core --hidden-import wallet_lib wallet_ui.py
```

หรือรันตรงๆ ด้วย `python wallet_cli.py` / `python wallet_ui.py`
(GUI บน Linux ต้องมีแพ็กเกจ `python3-tk` ของ distro)

## แก้ปัญหา

| อาการ | สาเหตุ/ทางแก้ |
|---|---|
| `node unreachable` / 403 | เช็ค `--node`; node สาธารณะ: `https://scan.drivecoinproject.online` |
| `insufficient funds` | ยอดไม่พอ — เช็คด้วย `list` หรือ `scan` ก่อน |
| ยอดไม่อัปเดต | รัน `scan` ใหม่ (CLI) หรือ Refresh (GUI) — tx ค้างรอ miner ขุด |
| tx ค้าง unconfirmed | เริ่ม miner (`miner_cli.exe`) รอขุดแล้ว Refresh |
| อยากใช้ wallet เดียวกับ wallet_app | ไฟล์ `wallets.json` format เดียวกัน — เปิดได้เลย |
| ตัว .exe โดนดัก | false positive PyInstaller — ใช้ source zip |
