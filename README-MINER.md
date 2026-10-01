# DriveCoin Miner — standalone CLI + GUI (Windows / Linux)

โปรแกรมขุดเหรียญ DriveCoin (prototype privacy chain) แบบพกพา — ไม่ต้องติดตั้ง
node ไม่ต้องมี SSH แค่เชื่อมเน็ตแล้วขุดผ่าน HTTPS ไปที่ node สาธารณะ

> ⚠️ **เชนนี้เป็น prototype เพื่อการศึกษา** — เหรียญไม่มีมูลค่าจริง
> อย่าใช้กับเงินจริงเด็ดขาด

## ดาวน์โหลด

| ไฟล์ | แพลตฟอร์ม |
|---|---|
| `drivecoin-miner-cli-windows-x64.exe` | Windows 10/11 (CMD/PowerShell) |
| `drivecoin-miner-cli-linux-x64` | Linux x64 (terminal) |
| `drivecoin-miner-ui-windows-x64.exe` | Windows 10/11 (GUI) |
| `drivecoin-miner-ui-linux-x64` | Linux x64 (GUI) |
| `drivecoin-miner-src-1.0.zip` | source (ทุกแพลตฟอร์ม ต้องมี Python 3.8+) |

checksum ทั้งหมดอยู่ใน `checksums.txt` (ตรวจด้วย
`sha256sum <file>` บน Linux หรือ `Get-FileHash <file>` บน PowerShell)

> หมายเหตุ: ไฟล์ .exe ที่ build ด้วย PyInstaller บางครั้งถูก antivirus
> ตั้งความสงสัย (false positive เพราะ onefile unpack) — ถ้าไม่มั่นใจ
> ใช้ source zip แล้วรันด้วย Python ได้เลย ฟังก์ชันเหมือนกัน 100%

## ใช้งาน CLI

```
drivecoin-miner-cli-windows-x64.exe                # ขุดไปเรื่อยๆ ด้วย wallet ใหม่
drivecoin-miner-cli-windows-x64.exe --once         # ขุด 1 บล็อกแล้วหยุด
drivecoin-miner-cli-windows-x64.exe --threads 4    # ระบุจำนวน worker processes
drivecoin-miner-cli-windows-x64.exe --rescan       # สแกน chain แล้วดูยอด
drivecoin-miner-cli-windows-x64.exe --address <addr> --once   # ขุดไปที่ address เดิม
```

ตัวเลือกเต็ม (`--help`): `--node`, `--address`, `--wallet`, `--threads`,
`--once`, `--max-blocks N`, `--timeout S`, `--rescan`, `--user-agent`

ครั้งแรกที่รัน โปรแกรมจะสร้าง `miner_wallet.json` (มี keys ของคุณเอง —
**เก็บไฟล์นี้ให้ดี หาย = เงินหาย**) แล้วขุดส่ง reward เข้า stealth address
ของ wallet นั้น ยอดจะโชว์หลังจบการทำงาน หรือเช็คได้ตลอดด้วย `--rescan`

## ใช้งาน GUI

1. เปิดโปรแกรม → กด **New wallet…** (ครั้งแรก) หรือ **Open wallet…**
2. กด **▶ Start mining** — ดู hashrate / บล็อกที่ได้ / ยอดเงินแบบสด
3. กด **■ Stop** เมื่อเลิก — wallet บันทึกอัตโนมัติ

## หลักการทำงาน

```
POST /api/template {address}  →  ได้ prefix + difficulty จาก node
grind: sha256d(prefix + be64(nonce)) < (2^256-1) / difficulty   (multi-core)
POST /api/submitblock         →  node ตรวจ PoW แล้วตอบรับบล็อก
GET  /api/scan                →  สแกนหา output ที่เป็นของเราด้วย view key
```

- **ความเป็นส่วนตัวครบ**: reward จ่ายเข้า stealth address
  `P = Hs(r·A)·G + B` — explorer เห็นแค่ one-time address ไม่มีใครผูกกับ
  wallet คุณได้ จำนวนเงินซ่อนอยู่ใน Pedersen commitment
  `C = v·G + m·H` ถอดอ่านได้เฉพาะเจ้าของ view key
- **คีย์อยู่ที่เครื่องคุณเสมอ** — wallet file เป็น JSON เก็บ view key (a)
  กับ spend key (b) ในเครื่องคุณเท่านั้น server ไม่รู้
- **ไฟล์ wallet ใช้ร่วมกับ wallet_app.py ได้** — อยากโอนเงินที่ขุดได้
  ให้รัน node/wallet ผ่าน SSH tunnel แล้วเปิดไฟล์นี้ (ดู README หลัก)

## Build เองจาก source

ต้องมี Python 3.8+:

```
pip install pyinstaller
pyinstaller --onefile --name drivecoin-miner-cli miner.py
pyinstaller --onefile --windowed --name drivecoin-miner-ui miner_ui.py
```

หรือรันตรงๆ ด้วย `python miner.py` / `python miner_ui.py`
(GUI บน Linux ต้องมีแพ็กเกจ `python3-tk` ของ distro)

## แก้ปัญหา

| อาการ | สาเหตุ/ทางแก้ |
|---|---|
| `cannot reach node` | เช็คเน็ต/DNS; ลอง `--node https://scan.drivecoinproject.online` |
| `stale template ... refreshing` | ปกติ — มีคนขุดเจาะก่อน (difficulty ปรับตาม hashrate รวมของเครือข่าย) |
| ยอดไม่ขึ้นหลังขุด | รัน `--rescan` (CLI) หรือกด Rescan balance (GUI) |
| GUI บน Linux เปิดไม่ได้ | `sudo apt install python3-tk` หรือใช้ build ที่ให้มา |
| antivirus ดัก .exe | false positive จาก PyInstaller onefile — ใช้ source zip |
