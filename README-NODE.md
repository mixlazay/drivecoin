# DriveCoin Node — standalone CLI + GUI (Windows / Linux)

รัน **node ของ DriveCoin เอง** บนเครื่องคุณ — พร้อม **P2P**: node ต่อเข้าเครือข่าย
DriveCoin จริง sync บล็อกอัตโนมัติ ขุดแล้ว broadcast ถึงทุก node หรือจะรัน
เครือข่ายอิสระของคุณเองกับเพื่อนก็ได้ พร้อม REST API ครบไม่ต้องติดตั้งอะไร

> 🌐 **เครือข่ายสาธารณะ**: ใช้ `--peer https://scan.drivecoinproject.online`
> node ใหม่จะดึงเชนทั้งเส้นจาก seed แล้วอยู่ sync ตลอด (poll ทุก 5 วินาที)
> บล็อกที่คุณขุดได้จะถูก push ไป seed และปรากฏบน explorer ทันที

## ดาวน์โหลด

| ไฟล์ | แพลตฟอร์ม |
|---|---|
| `drivecoin-node-cli-windows-x64.exe` | Windows 10/11 (CMD/PowerShell) |
| `drivecoin-node-cli-linux-x64` | Linux x64 (terminal) |
| `drivecoin-node-ui-windows-x64.exe` | Windows 10/11 (GUI) |
| `drivecoin-node-ui-linux-x64` | Linux x64 (GUI) |
| `drivecoin-node-src-1.0.zip` | source (ทุกแพลตฟอร์ม ต้องมี Python 3.8+) |

checksum ทั้งหมดอยู่ใน `checksums.txt`

## ใช้งาน CLI (headless)

```
drivecoin-node-cli-windows-x64.exe                    # เครือข่ายส่วนตัวของคุณเอง (localhost)
drivecoin-node-cli-windows-x64.exe --peer https://scan.drivecoinproject.online
                                                      # ⭐ join เครือข่ายสาธารณะ
drivecoin-node-cli-windows-x64.exe --port 9000        # เปลี่ยน port
drivecoin-node-cli-windows-x64.exe --bind 0.0.0.0     # ให้เพื่อนใน LAN ต่อเข้าได้
drivecoin-node-cli-windows-x64.exe --peer http://192.168.1.50:9000
                                                      # สร้างเครือข่ายเฉพาะกลุ่มกับเพื่อน
```

ข้อมูลเชนถูกเก็บที่ไฟล์ `node_chain.json` **ข้างตัวโปรแกรม** ปิดแล้วเปิดใหม่
เชนยังอยู่ครบ Ctrl+C เพื่อหยุด (เชนบันทึกอัตโนมัติ) peer list ก็ถูกจำไว้ใน
ไฟล์เดียวกัน — เปิดครั้งหน้าต่อ sync ต่อเลย

## ใช้งาน GUI

เปิดโปรแกรม → กด **Start Node** → แท็บ **P2P peers** → ใส่ URL peer
(เช่น `https://scan.drivecoinproject.online`) กด **Add peer / sync** —
ดูสถานะ sync ต่อ peer แบบสดในตาราง

## P2P ทำงานอย่างไร

```
ทุก node poll peer ทุก 5 วิ (/api/info)
  ├── peer สูงกว่า → ดึงบล็อกมา validate ทุกบล็อก (PoW + tx ครบ) แล้ว apply
  ├── เจอ fork ที่ยาวกว่า → reorg (validate ทั้งสายใหม่แล้วสลับ)
  └── ได้บล็อกใหม่/tx ใหม่ → push ต่อให้ทุก peer (relay — duplicate โดนเมิน)
```

- node ใหม่ที่มีแค่ genesis จะ **adopt** เชนของ peer ทั้งเส้น (join network)
- genesis ต่างกันและมีประวัติแล้ว = เครือข่ายคนละเส้น → ไม่ sync
- บล็อกแข่งกันสูงเท่ากัน: ใครมาถึงก่อนชนะ (first-seen) สายที่ยาวกว่า
  ชนะในรอบถัดไปผ่าน reorg
- ทุกอย่างถูก **validate ใหม่ทั้งหมด** ฝั่งเรา — ไม่มีการ trust peer

## ขุดบน node ของคุณ

ดาวน์โหลด miner (หน้าเดียวกัน) แล้วชี้ `--node` มาที่เครื่องคุณ:

```
drivecoin-miner-cli-windows-x64.exe --node http://127.0.0.1:8000 --once
```

(miner GUI ก็ได้ — แก้ช่อง Node เป็น `http://127.0.0.1:8000`)

ถ้า node คุณต่อเครือข่ายสาธารณะอยู่ บล็อกที่ขุดได้จะ **broadcast ไปทั้งเครือข่าย**
และขึ้นบน explorer (`scan.drivecoinproject.online`) อัตโนมัติ

## ใช้จ่ายเงิน (source zip เท่านั้น)

```
python wallet_app.py
```

สร้าง wallet → กด Refresh ดูยอด → กรอก address ปลายทาง → SEND CONFIDENTIAL
TRANSACTION → รอขุด (หรือใช้แท็บ Lightning จ่าย instant) — tx ถูก relay
ไปทุก node ในเครือข่ายอัตโนมัติ

## REST API (ทั้งหมด localhost)

| Endpoint | หน้าที่ |
|---|---|
| `GET /api/info` | สรุปเชน (height, difficulty, emission, **genesis**) |
| `GET /api/chain` | รายการบล็อก |
| `GET /api/mempool` | tx ที่รอขุด |
| `GET /api/scan` | chain view แบบ compact (wallet scan) |
| `GET /api/block/<h>` / `GET /api/tx/<id>` | รายละเอียดบล็อก/tx |
| `GET /api/blocks/<from>/<count>` | ดึงบล็อกช่วง (P2P pull) |
| `POST /api/template` / `POST /api/submitblock` | ขุด (getwork/submit) |
| `POST /api/tx` | ส่ง transaction |
| `POST /api/p2p/block` / `POST /api/p2p/tx` | รับบล็อก/tx ที่ peer push มา |
| `GET/POST /api/peers` | ดู/เพิ่ม peer |
| `POST /api/ln/*` | Lightning relay (pay/confirm/close) |

รายละเอียดทุก endpoint + ฟอร์แมต JSON: ดู docstring ใน `core.py`
(NodeService) และ `node_headless.py`

## แก้ปัญหา

| อาการ | สาเหตุ/ทางแก้ |
|---|---|
| Windows ขึ้น "App Control policy blocked" | Smart App Control ของ Windows 11 บล็อกแอปไม่มี signature — ปิด SAC ใน Settings → Privacy & security → Windows Security → App & browser control หรือใช้ source zip รันด้วย Python |
| antivirus ดัก .exe | false positive ของ PyInstaller onefile — ใช้ source zip |
| `cannot bind port 8000` | port ไม่ว่าง → เปลี่ยนด้วย `--port` |
| อยากเริ่มเชนใหม่ | ลบ `node_chain.json` แล้วรันใหม่ — ถ้ามี peer อยู่มันจะ adopt เชนของ peer ทันที (อยากเครือข่ายส่วนตัวให้เอา `--peer` ออกด้วย) |
| GUI บน Linux เปิดไม่ได้ | ต้องมี `python3-tk` ถ้ารันจาก source; binary build มี Tk ติดมาแล้ว แค่ต้องมี display |
| peer ขึ้น "different network" | ต่าง genesis — เครือข่ายคนละเส้น ลบ node_chain.json แล้วรันใหม่เพื่อ join |

## ความปลอดภัยของ node ของคุณ

- API **ไม่มี authentication** — ค่า default bind 127.0.0.1 (localhost เท่านั้น)
  จะเปิด `--bind 0.0.0.0` เมื่อไร ใครในวงแลนก็ควบคุม node คุณได้ทุกอย่าง
  (ขุด ส่ง tx เข้า mempool เพิ่ม peer) — เปิดเฉพาะเครือข่ายที่คุณไว้ใจ
- ไฟล์ `node_chain.json` = เชนทั้งเส้นของคุณ สำรองไว้ถ้าสำคัญ
- Peer ส่งบล็อกมาทั้งหมดถูก validate (PoW + ทุก tx) ก่อนยอมรับเสมอ —
  peer ใจร้ายทำอันตรายเชนคุณไม่ได้ แย่สุดคือเปลือง CPU ตอน validate

