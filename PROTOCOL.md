# DeepCool LT360 VISION — Protocol Reverse-Engineering Notes

Status: **Phase 1 — Reconnaissance complete. No write packets sent to the device yet.**

## 1. Device Identity

| Field | Value |
|---|---|
| Vendor ID | `0x3633` ("DC" / DeepCool) |
| Product ID | `0x002e` |
| lsusb name | `DC LT360 VISION` |
| iManufacturer | `DC` |
| iProduct | `LT360 VISION` |
| iSerial | `1204a0000a02100c031d822a16789012` |
| bcdDevice | `1.02` |
| bcdUSB | `2.00` (negotiated High Speed, 480 Mbps) |
| Bus location (this host) | Bus 008, Device 006, sysfs `8-12` |

## 2. USB Descriptor Layout

- 1 configuration, bus-powered, MaxPower 100 mA.
- **1 interface** (`bInterfaceNumber 0`), class `255` (**Vendor Specific**), subclass `255`, protocol `0`.
  - No HID interface is exposed at all (bDeviceClass/bInterfaceClass are not `03`).
  - Confirmed via `udevadm`: no driver is bound to `8-12:1.0` (`ls .../driver` → No such file). The kernel does not attach a class driver — this is a raw libusb/vendor-specific target, matching a usbfs-based userspace driver, not hidraw.
- **4 bulk endpoints**, all `wMaxPacketSize 0x0200` (512 bytes), no interrupt/isochronous endpoints:

| Endpoint | Direction | Type | Max Packet |
|---|---|---|---|
| `0x81` (EP1) | IN | Bulk | 512 B |
| `0x02` (EP2) | OUT | Bulk | 512 B |
| `0x83` (EP3) | IN | Bulk | 512 B |
| `0x04` (EP4) | OUT | Bulk | 512 B |

sysfs confirms endpoint files `ep_02`, `ep_04`, `ep_81`, `ep_83` under `8-12:1.0`.

No `/dev/hidraw*` node corresponds to this device (all present hidraw nodes belong to other peripherals — mice, keyboard, etc.); this device is bulk-only and must be driven via libusb/usbfs, not the HID API.

## 3. Comparison Against Reference Projects

### 3.1 `daedlock/deepcool-lm` (targets DeepCool LM360, PID `0x0026`)

| | `deepcool-lm` (LM360) | Our LT360 VISION |
|---|---|---|
| PID | `0x0026` | `0x002e` |
| Interface class | Vendor Specific | Vendor Specific (**match**) |
| Endpoints | **1**: `0x01` OUT only | **4**: `0x81`/`0x83` IN, `0x02`/`0x04` OUT |
| Transfer type | Bulk | Bulk (**match**) |
| Display | 320×240 RGB565 | Not yet confirmed (LT360 panels are commonly 480×480 for VISION-tier AIOs — TBD, do not assume) |
| Frame header | 13 bytes: `aa 08 00 00 01 00 58 02 00 2c 01 bc 11` | Unknown — not probed yet (Phase 1 is read-only) |

**Verdict**: Transport class matches (vendor-specific bulk), but the endpoint topology does **not** match 1:1. `deepcool-lm` only ever writes to a single OUT endpoint and never reads back. Our device exposes two IN endpoints in addition to two OUT endpoints, implying:
- a command/status or handshake channel distinct from the image-push channel (e.g. EP2 OUT = control/command, EP4 OUT = framebuffer bulk push, EP1/EP3 IN = ACK/status/telemetry readback), or
- some other bulk-pipe split DeepCool introduced in newer VISION-series firmware that LM360 doesn't have.

This must be confirmed empirically (Phase 2+) by capturing real traffic (e.g. `usbmon` / Wireshark against the Windows/official app) before assuming which OUT endpoint carries pixel data.

### 3.2 `Nortank12/deepcool-digital-linux`

This project does **not** support our device. Findings:

- It drives DeepCool's *status-readout* AIOs/cases (LD, LQ, LS, CH, AK-G2 series) exclusively over **HID** (`hidapi`), with 64-byte HID reports (`report ID 16`, fixed headers, checksum byte, termination byte). This is architecturally unrelated to our bulk-only vendor-specific interface.
- The README explicitly calls out a **"MYSTIQUE Series"** section (LCD-equipped models) as unsupported: *"These devices are unique since they have an LCD display, and I do not personally own one... if you can figure out how to make it work, you can share it there or create a pull request."* The `mystique-series.md` table is empty (`? ? ?` placeholder).
- No VID/PID `3633:002e` reference anywhere in the repo. No LT360 reference anywhere in the repo.

**Verdict**: This repo has **zero applicable protocol data** for the LT360 VISION — it's a different device family (HID status displays, not full-color LCD bulk transfer). It's useful only as a negative reference (rules out HID) and confirms upstream awareness that DeepCool's LCD ("VISION"/"MYSTIQUE") tier is unreverse-engineered territory.

## 4. Summary / Answer to the Recon Question

- **VID:PID**: `3633:002e`
- **Interfaces**: 1 (vendor-specific, class `0xFF`)
- **Endpoints**: 4 bulk, all 512-byte max packet — `0x81` IN, `0x02` OUT, `0x83` IN, `0x04` OUT
- **Matches `deepcool-lm`?** Partially. Same transport family (vendor-specific bulk, not HID), but `deepcool-lm`'s LM360 uses a single OUT-only endpoint while our LT360 VISION has a 4-endpoint (2 IN / 2 OUT) layout. Endpoint layout is **not identical** — the extra IN endpoints suggest bidirectional handshake/status traffic that LM360 doesn't do, or a split control/data channel. Not HID at all — `deepcool-digital-linux` (the other reference) doesn't apply to this device family.

## 5. Next Steps (Phase 2, not yet started)

- Capture real USB traffic from the vendor Windows/mobile app via `usbmon`/Wireshark to see which endpoint(s) carry image data vs. control/status, and to get the real frame header/checksum format for this PID.
- Do not send arbitrary write packets to EP2/EP4 blind — risk of bricking/hanging the AIO's onboard controller without a known-good init sequence.

---

# Phase 2 — Static Extraction (Linux, no device writes)

Status: **Complete for what's staticaly recoverable. Still no write packets sent to the device.**

## 6. `qt-deepcool` (mymymy1303) — reference comparison

Repo: `ref/qt-deepcool/` (targets **MYSTIQUE 240/360, PID `0x0009`** — not our `0x002e`).

- **Packet format** (`PROTOCOL.md` + `deepcooldevice.cpp` in that repo): fixed **48-byte** packets.
  - Bytes 0-1: header `AA 2E`
  - Byte 2: command
  - Bytes 3-41: payload (≤39 bytes)
  - Bytes 42-45: footer `"HIDC"` (`48 49 44 43`)
  - Bytes 46-47: 16-bit little-endian checksum = sum of bytes 0-45
  - Device responses use header `55 2E` instead of `AA 2E`, command byte echoed.
- **Endpoints**: only **2 pairs** — `0x01` OUT / `0x81` IN for init/config, `0x02` OUT / `0x82` IN for periodic display-data pushes. This is a *2-endpoint-pair* device, not 4.
- **Cold-boot init sequence** (sent on EP `0x01`, response read from EP `0x81`): commands `0x12` (device info) → `0x02` (config: rotation+settings) → `0x03,0x04,0x07,0x08,0x05,0x0B,0x06` (setup) → `0x15,0x16,0x17` (display labels "--") → **`0x0A` with payload `EA 07 02 02 02 27 21`** (mode switch to Machine Info) → `0x10` (status poll, byte 5 = `0xFF` on success). This is exactly the command/payload the user's brief cited, confirming it's the MYSTIQUE-family "activate custom display mode" command, not something specific to LT360 VISION.
- **Verdict for our device**: transport class (vendor-specific, bulk, 512B/64B max-packet HID-shaped framing) matches, and it's very likely LT360 VISION reuses the same `AA 2E ... HIDC ...checksum` 48-byte command framing over **one** OUT/IN pair (probably `0x02`/`0x81` in our layout) for control/config, while the *second* OUT/IN pair (`0x04`/`0x83`) is the extra bulk channel MYSTIQUE doesn't need — added because LT360 VISION pushes full-color image/video frames (not just small telemetry ints), which needs a dedicated bulk-image pipe separate from the control channel. This is a hypothesis, not confirmed — no capture of real LT360 VISION traffic exists yet.

## 7. DeepCool's own download API (bypassing the JS-rendered download button)

The download page (`https://www.deepcool.com/downloadpage/`) calls a JSON API from `downloadpage.js`:

```
GET https://downloads.deepcool.com/official/software/official/software-info?version=latest&name=deep-creative
GET https://downloads.deepcool.com/official/software/official/download-path?version=<ver>&name=deep-creative
```

(`name` must be the lowercase slug `deep-creative`, not `DeepCreative` — the latter returns "软件不存在"/"software does not exist".)

- Latest version as of this extraction: **DeepCreative 1.2.13**, built 2026-09-16.
- `deviceSupport` list for 1.2.13 includes `"LT360"`, `"LT360 VISION INFINIARC EDITION-WHITE"`, `"LT360 VISION INFINIARC EDITION-BLACK"` — **not** a bare `"LT360 VISION"` entry in that particular metadata field (see §8 for why this doesn't matter — the UI's device-name routing table still has a dedicated `"LT360 VISION"` entry).
- Direct installer URLs returned by `download-path`:
  - External: `https://deepcool.io/downloads/DeepCool-1.2.13-setup.exe`
  - Internal (China CDN): `https://deepcool.omnictl.cn/downloads/DeepCool-1.2.13-setup.exe`
- Downloaded to `/tmp/deepcreative/DeepCool-1.2.13-setup.exe` — a **510 MB NSIS self-extracting installer** (not Inno Setup). Extracted with `7z x` (NSIS support built in); the real payload is a nested 7z blob `$PLUGINSDIR/app-64.7z` (301 MB, LZMA2/BCJ2), which itself contains the Electron app under `resources/`.

## 8. What's inside DeepCreative 1.2.13 — and which file handles PID `0x002e`

- `resources/app.asar` (293 MB) is the Electron app bundle (Vue 3 renderer + compiled Electron main process).
- **Device routing table** (recovered from the renderer bundle, `index-*.js`, unminified Vue/Vite output): the device list UI maps `productName` strings to native modules by literal route path. The relevant entries:
  ```js
  LT360: () => router.push({ path: `/devices/L136/${item.serialNumber}`, ... }),
  "LT360 VISION": () => router.push({ path: `/devices/L136/${item.serialNumber}`, ... }),
  "LT360 VISION INFINIARC EDITION": () => router.push({ path: `/devices/L142/${item.serialNumber}`, ... }),
  ```
  So DeepCool's internal code name for plain **"LT360 VISION" (our exact device, PID `0x002e`) is `L136`**. The INFINIARC EDITION variant is a *different* PID handled by a separate module, `L142` — do not conflate the two when reading DeepCool's docs/code.
- **The file that actually talks to PID `0x002e`**: `resources/app.asar.unpacked/resources/L136/L136.node` — a compiled N-API addon (PE32+ DLL, x86-64) statically linking libusb across all its Windows backends (WinUSB, libusbK, libusb0, HID). String inspection shows only generic libusb/node-`usb`-module internals (`winusbx_claim_interface`, `do_sync_bulk_transfer`, `LIBUSB_ERROR_*`, etc.) — **no literal `3633`/`002e`/`LT360` ASCII strings are baked into this binary**. That's expected: VID/PID and endpoint numbers are passed in as arguments/immediates, not string constants, so `strings` can't recover them from this file.
- **Where the actual protocol logic (VID/PID match, endpoint numbers, opcodes, frame header/checksum, image encoding) lives**: the Electron **main process**, compiled to V8 bytecode at `out/main/index.jsc` inside the leaked dev build (see §9). `strings` on that file surfaces the *identifiers* — `L136Controller`, `L136UsbService`, `L136DBService`, `L136DisplyMode`, `L136TopicType`, `L136PlayAnimation`, `L136AppService`, IPC channel names like `l136/modelConfigurationSet` / `l136/displayConfigurationSet` — confirming the architecture (an `L136Controller` registers `l136/*` IPC handlers that a renderer calls; `L136UsbService` wraps `L136.node`), but **not** the numeric constants themselves, since V8 bytecode stores those as immediate operands in the bytecode stream, not as string literals `strings` can extract. Recovering the exact opcode/frame bytes for LT360 VISION would need either a V8 bytecode disassembler pass on `index.jsc`, or (as originally planned) a real USB capture — static extraction has reached its ceiling here.
- **`deep_service.exe`** (`resources/service/x86/deep_service.exe`, x86-only in this build) is a **red herring for the LCD protocol**: its PDB path (`C:\Users\tyy20\Git\UCS_Ext\deep_service\...\mq_pub_server.cpp` / `mq_rep_server.cpp`) and strings (GPU/Video/Memory sensor labels) show it's a **ZeroMQ pub/rep sensor-telemetry helper** (reads CPU/GPU/RAM stats and publishes them over ZeroMQ for the Electron app's fan-curve/sensor UI) — it has nothing to do with driving the LCD panel. The LT360 VISION USB traffic is generated entirely in-process by the Electron main process + `L136.node`, not by this external service.

## 9. Incidental finding: DeepCool shipped their internal dev repo inside the installer

`app.asar` isn't just the built app — it's DeepCool's **entire Electron project source tree**, apparently bundled by accident (probably a build-config glob that didn't exclude dev-only paths). Alongside `resources/` and `out/` (the real build output), it contains:

- `node_modules/` — full unminified dependency tree (~240 packages, including `usb`, `node-hid`, `zeromq`, `classic-level`, `electron-edge-js`).
- `.claude/commands/figma/*.md` — Claude Code slash-commands for Figma-to-UI codegen.
- `.mcp.json` — an MCP server config.
- `docs/L140-full-chain-review.md` and `FAN_CONTROL_REVIEW_TODO.md` — AI-assisted architecture review notes (for the **L140 = SILENTNOX PRO 360** device, not L136, but structurally identical pattern). This doc explicitly states:
  - `WinUsbDevice` detects **VID `0x3633`, PID `0x0036`** for the L140 screen device (confirms the shared DeepCool VID and the one-PID-per-screen-model pattern our `0x002e` also follows).
  - Pipeline diagram: `Device Worker -> L140.node -> USB PID 0036`, plus a parallel `Fan Worker -> node-hid -> HID PID 0037` — i.e., fan control on these newer boards is a **separate HID device**, entirely unrelated to the bulk-vendor LCD interface.
  - Source paths referenced (not shipped, but named): `src/main/bootstrap.ts` registers `L140Controller`, which calls `L140Controller.registerEvents()` to wire up `l140/*` IPC handlers — same pattern we can infer for `L136Controller`/`l136/*`.
- `report.xml`, `logs/`, `mocked/` — a Jest/Vitest test report and log/fixture files. The log only contains **mocked unit-test traffic** for a fake device `test-L136` (schema-validation errors, no real USB byte traffic) — not useful for protocol recovery.

None of this is being kept in our project repo (per the task's storage constraint); it's parked under `/tmp/deepcreative/` for this session only.

## 10. Summary / Answer to the Phase 2 question

- **`qt-deepcool`** targets a different PID (`0x0009`, MYSTIQUE) with a 2-pair endpoint layout and a well-documented 48-byte `AA 2E .. HIDC .. checksum` command protocol. Useful as a *strong structural hint* (likely shared control-channel framing) but not a byte-exact match for our device.
- **The DeepCreative file that handles PID `0x002e` (LT360 VISION) is `resources/app.asar.unpacked/resources/L136/L136.node`**, driven by the Electron main-process controller `L136Controller`/`L136UsbService` (compiled to bytecode in `out/main/index.jsc`) via IPC channel prefix `l136/*`. `deep_service.exe` is unrelated (ZeroMQ sensor telemetry only).
- Exact opcode bytes, checksum algorithm, and per-endpoint roles for LT360 VISION specifically are **still not recovered** — `L136.node` has no literal protocol strings, and the logic that does contain them is compiled to V8 bytecode, not plaintext JS. Next step to actually get bytes would be either disassembling `index.jsc`'s bytecode or a real USB capture (Phase 3, Windows/Wireshark), as originally planned.

---

# Phase 3 — Static Disassembly of `L136.node` (no device writes)

Status: **Native transport layer fully recovered from `L136.node`. `index.jsc` (V8 bytecode) is decoded in Phase 3B (§18–24).** Still no packets sent to the device. Analysis scripts are in `/tmp/deepcreative/re/`.

## 11. What `L136.node` actually is

- It's a **Rust** cdylib, not C++. It uses `neon 1.0.0` for the N-API bindings and `rusb 0.9.4` with libusb statically linked. The PDB name is `napi_l086.pdb` and the crate source files are `src\lib.rs`, `src\usb.rs` and `src\utils.rs`. It's an unoptimized (debug-profile) build.
- The crate name is misleading: `L136.node` is crate `napi_l086`, while `L142/index.node` and `CH690.node` are crate `napi_l136`. All of them share the same framing code, and each **hardcodes its own PID**:

| File | Hardcoded match |
|---|---|
| `L136/L136.node` (**LT360 VISION**) | `VID == 0x3633 && PID == 0x002e` (at `0x1800091f8`) |
| `L142/index.node` (INFINIARC) | `0x3633` / `0x31`, `0x32` |
| `CH690/CH690.node` | `0x3633` / `0x30` |
| `L086/index.node` | `0x3633` / `0x27` |

- **The native module only writes.** The only bulk wrapper in the binary is rusb `write_bulk` (`0x180003bd0`), and crate code never calls `read_bulk`, interrupt transfers or control transfers. **IN endpoints `0x81` and `0x83` are enumerated but never read.**

## 12. Exported JS API (4 functions)

| JS name | Args | Returns |
|---|---|---|
| `refreshDevice()` | none | array of serial-number strings. It drops all previously held handles, enumerates, and opens every `3633:002e` into a global `Mutex<HashMap<serial, DeviceHandle>>`. |
| `closeDevice(serial)` | string | removes the handle from the map; dropping it releases interface 0 and calls `libusb_close` |
| `sendImageData(serial, bytes)` | string, byte container | `"success"`, `"device not found"`, `"send start command failed"`, `"send file command failed"`, `"send finish command failed"`, or `"Error getting active config descriptor: …"` |
| `sendGeneralCommand(serial, payload)` | string, byte container | `"success"`, `"failed"` or `"device not found"` |

(Whether `bytes` is a `Buffer` or a `number[]` isn't confirmed. It's read through neon's dynamic N-API table and copied into a `Vec<u8>`.)

## 13. Endpoint and interface selection (`src\usb.rs`, `0x1800013b0`)

For every endpoint on the active config: if the direction is OUT, its address is pushed to `out_eps`, otherwise to `in_eps`. The interface number is stored alongside. Before each send, the code calls `claim_interface(iface)`. With this device's descriptor order (`0x81, 0x02, 0x83, 0x04`):

- `out_eps[0]` = **`0x02` → image/file stream** (Start / trans / DCLdfinish), timeout **30 s**
- `out_eps[1]` = **`0x04` → general command** (`AA 2E` frames), timeout **1 s**
- interface **0**

## 14. General command frame (`0x180002960`), 46 bytes on EP `0x04`

```
off  len  value
0    2    AA 2E
2    N    payload from JS (N ≤ 42; a longer payload panics on the pad-length underflow)
2+N  42-N 00 … (zero pad)
44   2    checksum16 = (sum of bytes 0..43 as u32) & 0xFFFF, little-endian
```

This is the MYSTIQUE `AA 2E` family but **46 bytes, not 48**. `L136.node` doesn't add a `"HIDC"` footer. If the device expects one, the JS side must include it in the payload; that's still to be confirmed from `index.jsc`.

## 15. Image/file transfer (`0x180002c80`), 512-byte packets on EP `0x02`

`data` = the raw bytes from JS (no conversion is done in native code: no RGB565, compression or JPEG encoding). `CHUNK = 505` (`0x1f9`).

**1. Start (512 B)**
```
0   6  "Start" 01                    53 74 61 72 74 01
6   4  len(data), u32 LE
10  2  checksum16(data) = (Σ data bytes, u32) & 0xFFFF, LE
12  2  ceil(len / 505), u16 LE       (packet count)
14  2  00 00
16  496 00 …
```

**2. Data packets.** For `i = 0 ..= len/505`:
```
0   5    "trans"                     74 72 61 6e 73
5   2    seq = i+1, u16 LE           (1-based)
7   505  data[i*505 .. (i+1)*505], last chunk zero-padded to 505
```
Each packet is exactly 512 B (one HS bulk max-packet). The loop stops after the first partial chunk. Quirk: if `len % 505 == 0`, one extra all-zero-payload packet is sent, which is one more than the Start count says.

**3. Finish (512 B)**
```
"DCLdfinish" (44 43 4c 64 66 69 6e 69 73 68) + 502 × 00
```

Any failed write aborts with the matching error string. There is no ACK read between phases.

```python
# reference encoder (untested against hardware)
def frames(data: bytes):
    C = 505
    cks = sum(data) & 0xFFFF
    cnt = -(-len(data) // C)
    yield (b"Start\x01" + len(data).to_bytes(4, "little") + cks.to_bytes(2, "little")
           + cnt.to_bytes(2, "little") + b"\0" * 498)
    for i in range(len(data) // C + 1):
        chunk = data[i*C:(i+1)*C]
        yield b"trans" + (i+1).to_bytes(2, "little") + chunk.ljust(C, b"\0")
        if len(chunk) < C: break
    yield b"DCLdfinish".ljust(512, b"\0")

def general(payload: bytes):
    body = b"\xAA\x2E" + payload.ljust(42, b"\0")
    return body + (sum(body) & 0xFFFF).to_bytes(2, "little")
```

## 16. Other Phase 3 facts

- The leaked mock DB shows L136 media at **480×854** (portrait) and 854×480 (landscape), and the bundled `L136/media/*.jpg` presets have the same sizes. That's likely the panel resolution. The mock config keys include `brightnessControl`, `isMirror`, `displayModel`, `playMode`, `switchTime` and `playAnimation`.
- `report.xml` is an unrelated HWiNFO system dump. `docs/L140-full-chain-review.md` describes L140 (PID `0x36`) sending **JPEG** frames through `L140.node`, so JPEG payloads for L136 are plausible but not yet confirmed.
- Toolchain: Electron **23.3.13**, Chrome 110.0.5481.208, Node 18.12.1, V8 **11.0.226.20**. `.jsc` magic `0xC0DE05BC`, and the source-length field is 1,854,029 chars.

## 17. Still open (needs `index.jsc` or a capture)

- The actual `sendGeneralCommand` payloads: opcodes, init/handshake order, brightness/rotation/mode switching, and whether a `HIDC` footer is included.
- What `sendImageData` receives: JPEG vs raw frames, and how videos/animations and telemetry overlays are produced.

*(Both answered in Phase 3B below, §18–§24.)*

---

# Phase 3B — `index.jsc` decoded, full L136 host logic recovered (no device writes)

Status: **Every `sendGeneralCommand` / `sendImageData` call site for L136 has been recovered from V8 bytecode.** Still nothing has been sent to the device. The tooling is in `/tmp/deepcreative/re/dyn/` (see §24).

## 18. How `index.jsc` was cracked

I used Method A (a real Ignition disassembly), with the runtime doing the hard part:

1. The **Linux Electron 23.3.13** build (`process.versions.v8 == 11.0.226.20-electron.0`) accepts `index.jsc` as `cachedData` once the loader's flag-hash patch is applied. It reports `cachedDataRejected: false`, and the script is only compiled, **never run**.
2. Running with `ELECTRON_RUN_AS_NODE=1 electron --log-code --log-code-disassemble` makes V8's logger call `BytecodeArray::Disassemble()` on every deserialized function, even in this release build. That gives full Ignition listings for all **3,685** functions, with names and source offsets.
3. Release builds don't print constant pools, so `dumpcp.js` reads them **directly from the live heap via `/proc/self/mem`**, using the bytecode addresses from the log. The heap is pointer-compressed (4 GiB cage), and the BytecodeArray header is 34 bytes with the constant pool at +8. The instance types were calibrated in-process: FixedArray `0xaf`, one-byte string `0x08`, two-byte string `0x00`, HeapNumber `0x82`, Oddball `0x83`, Symbol `0x80`, ArrayBoilerplate `0x92`, ObjectBoilerplate `0xbc`, ScopeInfo `0x105`, SFI `0x106`, BytecodeArray `0xbf`. ScopeInfo context-local names are decoded too, including the 1,111-entry hashtable of the bundle's module scope.
4. `annotate.py` merges the two and models the context chain, so `LdaImmutableCurrentContextSlot [118]` resolves to `L136$1` (the `L136.node` binding). `decomp.py` folds the result into goto-style pseudo-JS. Every finding below was cross-checked against the raw bytecode.

I didn't need Method B (running it with mocks).

## 19. Where things live (bundle source offsets in `index.jsc`)

| Symbol | Offset | Role |
|---|---|---|
| `require("../../resources/L136/L136.node")` → `L136$1` | top level | native transport (§11–15) |
| enums `L136SecondaryDataBasicType`, `L136PlayAnimation`, `L136DisplyMode`, `L136TopicType` | 655268–657834 | §22 |
| `L136DBService` + default configs | 667129–669440 | persistence (LevelDB), keys `L136_<sn>_modelConfig`, `L136_<sn>_displayConfig_`, `L136_<sn>_playConfig_` |
| `L136UsbPlayModelService` | 713810 | decides *what* to draw (media, slideshow, DCast screen capture, region mapper) |
| `L136UsbService` | 769914–786000 | **the only code that calls `sendGeneralCommand` / `sendImageData`** |
| `WinUsbDevice.init` | 1214581 | `if (productId === 46) workerService.L136UsbService.init(serial)` |
| `L136AppService` / `L136Controller` | 1634543 / 1655844 | IPC handlers (§21) |

## 20. Complete command table (`sendGeneralCommand`, EP `0x04`)

**For L136 there are exactly two payloads in the whole program.** I swept every `CreateArrayLiteral` and every `sendGeneralCommand` reference in the L136 code, including the controller and worker. There is no `HIDC` footer and no ACK/read. Payloads are passed as `Buffer.from([...])`, and `L136.node` frames them as `AA 2E <payload zero-padded to 42> <sum16 LE>`.

| Payload | Full 46-byte frame | Sent by | Meaning |
|---|---|---|---|
| `05 01` | `AA 2E 05 01 00…00 DE 00` | first tick of `L136UsbService.updater` (guarded by `lastChange !== 1`, so **once per open**) | "start host image streaming". It's sent right before the first JPEG. |
| `04 MM BB UU` | e.g. brightness 30 °C: `AA 2E 04 00 1E 00 00…00 FA 00` | `L136UsbService.setSetting(sn, MM, BB, unit)` | device settings |

`04` field meanings (from `setSetting`, `modelConfigSet` and `updateSettingWithParam`):
- `MM` is display mode. `modelConfigSet` (brightness/rotate/orientation/theme changes in the UI) **always sends `0`**. `updateSettingWithParam` (global °C/°F change) sends `modelConfig.displayModel` (0 = horizontal, 1 = vertical). The device most likely ignores it for streamed frames, because rotation is baked into the JPEG (§23).
- `BB` is `brightnessControl`, an integer **0–100** (Element-Plus slider with its default min/max, shown as "%"; default **30**).
- `UU` is the temperature unit: `0` = °C (`temperatureDisplay === 0`), `1` = °F. The device only needs this if it has a standalone/built-in screen. The host renders all temperatures into the JPEG itself.

**Not present:** there's no separate rotation opcode (rotation is done host-side), no telemetry packet (sensors are drawn into the frame), no keepalive (the ~30 fps frame stream is the heartbeat), and no "display off" or shutdown command. `displayClose()` only logs `"L136 device display method not implemented."`, and `destroy()` only runs `clearInterval`. On a `sendImageData` failure the host calls `closeDevice(sn)` and stops the timer.

### Cold-boot / runtime sequence exactly as DeepCreative does it
```
WinUsbDevice sees 3633:002e  →  L136UsbService.init(sn):
  L136.refreshDevice()                      # open + claim iface 0 (§12)
  initcanvas()                              # canvas1 854x480, canvas2 480x854 (skia), fonts JZFSSans/Pixelnumsymbol
  playModelService.init(sn); updater(sn)    # setInterval(tick, 33 ms)
  l136ModelConfigInstance = db.getModelConfig(sn); initL136Player(sn)
tick (every 33 ms, async, not serialized):
  if first tick: sendGeneralCommand(sn, [05 01])
  pick canvas by displayModel; drawBackground; draw media/slideshow frame;
  if DataDisplay && !DCast: draw sensor block (theme = topicType)
  imageMode(sn): jpg = canvas.toBuffer("jpg", 100)
                 jpg = PlayerNode.rotateJPG(angle, canvas.w, canvas.h, jpg).data
                 r = sendImageData(sn, jpg); if r != "success": closeDevice(sn); clearInterval
on UI model-config change (brightness / isMirror / displayModel / topicType):
  db save → sendGeneralCommand(sn, [04 00 brightness unit])
```
`[04 …]` is **not** sent at boot, only when settings change. For our driver the sensible start is `[05 01]`, then `[04 mode bright unit]`, then frames.

## 21. Renderer ↔ main IPC for L136 (Task 1)

The renderer (`out/renderer/assets/index-01a108ad.js` `api.L136`, and the page `index-d89eaad0.js` for route `/devices/L136/:sn`) only calls `ipcRenderer.invoke(channel, ...toRaw(args))`. Responses are `{code?, message, data}`. **The renderer does no image encoding.** Cropper.js returns only crop-box numbers, and all decoding, cropping, JPEG encoding and rotation happens in the main process.

Handled by `L136Controller.registerEvents` (17 channels):

| Channel | Args | Main-side effect |
|---|---|---|
| `l136/modelConfigurationSet` | `(modelConfig, sn)` | DB save, then **`[04 00 brightness unit]`** unless unchanged (`checkConfigEqual`) |
| `l136/modelConfigurationSearch` | `(sn)` | returns modelConfig |
| `l136/displayConfigurationSet` | `(displayConfig, sn)` | `setElementDataCurrent`: changes what the canvas draws (no USB command) |
| `l136/displayConfigurationSearch` / `Default` / `Reset` | `(sn)` | read / reset displayConfig |
| `l136/playerConfigurationSet` / `Search` | `(playConfig, sn)` / `(sn)` | slideshow settings |
| `l136/uploadSelectedMedia` / `modifyMedia` | `(media[, id], sn)` | image/GIF import + crop (main process) |
| `l136/uploadSelectedVideo` / `modifyVideo` | `(video[, id], sn)` | video import + crop/trim |
| `l136/deleteOneMedia`, `l136/getAllMedia` | `(id, sn)`, `(sn)` | media library |
| `l136/image-transmission` | `(sn)` | returns the **last JPEG sent** as `data:image/jpg;base64,…` (UI preview) |
| `l136/checkFirstEnter`, `l136/completeGuide` | `(type)` | onboarding flags |

The renderer also calls 7 `l136/*Preset*` channels (`getPreset`, `setPreset`, `delPreset`, `renamePreset`, `listPreset`, `presetThumbnail` and `getPresetActiveStatus`), but **no main-side handler exists** for them (dead UI code). It also uses the shared `media/selectImg|selectGif|selectVideo|getSpecialMediaInfo`.

Payload shapes (defaults from `l136ModelConfigDefault` etc. and the renderer store):
```js
modelConfig   = { brightnessControl: 30 /*0..100*/, isMirror: false /*UI "rotate" button toggles it*/,
                  displayModel: 0 /*L136DisplyMode*/, topicType: 0 /*L136TopicType*/ }
playConfig    = { switchTime: 3000 /*ms*/, playMode: "sequential"|"random",
                  playAnimation: "static"|"panning"|"ease_in_out" }
displayConfig = { DCast: false /*mirror a virtual monitor*/, RegionMapper: false,
                  mapperRegion: {x:0,y:0,width:480,height:480}, DataDisplay: true,
                  dataDisplayType: "Time" /*L136SecondaryDataBasicType*/, dataSecondaryData:
                  {topLeft:"CpuFrequency",topRight:"GpuFrequency",bottomLeft:"FANSpeed",bottomRight:"GpuPower"},
                  SeeSee: false /*edge-magic overlay*/, seeSeeType: 0 /*GridEffect*/,
                  mediaInfo: {id:"-1", elementPath:"", mediaType:"" /*Jpg|Gif|Mp4*/},
                  ioFan: "", fontType: "rgba(255,255,255,1)" | "rgba(0,0,0,1)" }
media upload  = { id, path, originalPath, name, positionX, positionY, cutWidth, cutHeight,
                  startTime, endTime, sizeType: "3*2"|"2*3", mediaType, ratio: 854/480|480/854, … }
video upload  = { path, ratio, sizeType, offsetTime, startTime, endTime,
                  crop_info:{positionX,positionY,cutWidth,cutHeight}, display_info:{…}, positon_info:{…}, mediaType:"video" }
```

## 22. Enums (exact values from the enum IIFEs)

| Enum | Values |
|---|---|
| `L136DisplyMode` | `Horizonal = 0`, `Vertical = 1` |
| `L136TopicType` (theme of the sensor overlay) | `Boundary = 0` (color `RED$2`), `CodeZero = 1` (`BLUE$2`), `PixelWorld = 2` (matrix style, `rgb(255,255,11)`) |
| `L136PlayAnimation` | `static = "static"`, `panning = "panning"`, `blinds = "ease_in_out"` (default `panning`) |
| play mode | `"sequential"` (default), `"random"` |
| SeeSee / edge-magic | `GridEffect = 0`, `FlowerEffect = 1`, `GradientEffect = 2`, `ParticleEffect = 3` |
| `L136SecondaryDataBasicType` (data fields) | `Off, Time, CpuFrequency, GpuFrequency, CpuTemperature, CpuLoad, CpuPower, CpuVolage="CPUVolume", CpuRingVolage="CPURINGVolage", CpuSystemAgentVolage="CPUSystemAgentVolage", GpuTemperature="GPUTemperature", PumpSpeed, FANSpeed, GpuPower, NetworkSpeedUpload, NetworkSpeedDownload` |

These are strings the host uses to decide what to draw. **None of them reach the device as bytes.** Sensor values (`updateSensorsParams`: cpuTemperature, cpuUsage, cpuClock/1000, gpuTemperature, gpuUsage, gpuClock/1000, ramUsage, cpuPower, gpuPower, disk, network, mainboard fans…) are rendered as text/graphics into the canvas. `fanRpm`/`pumpRpm` are hard-coded to 0 for L136.

## 23. Image format (`sendImageData` payload)

**The payload is a single baseline JPEG file, always 480×854 (portrait), and a new one is sent every 33 ms (≈30 fps target).** There's no custom header: the JPEG bytes are exactly what `L136.node` wraps in `Start…`/`trans…`/`DCLdfinish` (§15).

Pipeline (`L136UsbService.imageMode`, then `rotate()`, then `PlayerNode.rotateJPG` in `resources/ffmpeg/ffmplayer.node`):
1. Draw on a skia canvas: **854×480** for horizontal, **480×854** for vertical.
2. `canvas.toBuffer("jpg", 100)` makes an intermediate JPEG at quality 100.
3. `rotateJPG(angle, canvas.width, canvas.height, jpg)` uses FFmpeg to decode, then a `buffer→transpose→buffersink` filter graph, then re-encode with the **MJPEG encoder, `q=2`, `pix_fmt=0` (YUV420P), full range**. The native code checks `angle ∈ {0,90,180,270}` and maps it to mode 0/1/2/3, swapping w/h for 90/270. Modes 1/2/3 correspond to the filter strings `transpose=1` (90° CW), `transpose=1,transpose=1` (180°) and `transpose=2` (90° CCW). *This mapping comes from string order plus FFmpeg semantics. I didn't find a direct xref.*

Clockwise angle chosen per frame:

| `displayModel` | `isMirror=false` | `isMirror=true` |
|---|---|---|
| 0 Horizonal (854×480 canvas) | **270** | 90 |
| 1 Vertical (480×854 canvas) | **180** | 0 |

So the device always receives 480×854, and all orientation handling is done by rotating the JPEG. The default vertical layout being rotated 180° suggests the panel's scan origin is the physical bottom-right, but that's an inference. `isMirror` is the UI's "rotate 180°" button, not a real mirror.

**Animations, GIFs and MP4s are never uploaded to the device.** `addMediaToPlayer(sn, path, w, h, 30 /*fps*/, 0, 0)` decodes them on the host in `ffmplayer.node`. On every tick, `getNextFrameFromPlayerId` (or `getImageFrameFromPlayerId` for stills and slideshows) gets a frame, it's drawn onto the canvas with the current overlay, and the whole composite is re-sent as a new JPEG. Slideshows switch every `switchTime` ms (default 3000) with a `panning`/`ease_in_out`/`static` transition that is also rendered on the host.

Quirk: the timer is `setInterval(async tick, 33)` with no back-pressure. If a transfer takes longer than 33 ms, ticks overlap. `L136.node` serializes the calls through its device mutex.

## 24. Updated reference encoder (untested against hardware)

```python
import io
from PIL import Image

W, H = 480, 854                     # native panel: portrait
HORIZONAL, VERTICAL = 0, 1          # L136DisplyMode

def checksum16(b: bytes) -> int:
    return sum(b) & 0xFFFF

def general(payload: bytes) -> bytes:
    """46-byte EP 0x04 frame built by L136.node sendGeneralCommand."""
    assert len(payload) <= 42
    body = b"\xAA\x2E" + payload.ljust(42, b"\0")
    return body + checksum16(body).to_bytes(2, "little")

def cmd_stream_start() -> bytes:          # aa 2e 05 01 00.. de 00
    """[05 01]: sent once, on the first frame tick after the device is opened."""
    return general(bytes([0x05, 0x01]))

def cmd_settings(brightness: int, celsius: bool = True, display_model: int = 0) -> bytes:
    """[04 mode bright unit]: sent when model config / temperature unit changes.
    DeepCreative sends mode=0 from modelConfigSet, displayModel from updateSettingWithParam."""
    assert 0 <= brightness <= 100 and display_model in (0, 1)
    return general(bytes([0x04, display_model, brightness, 0 if celsius else 1]))

def rotation_deg(display_model: int, is_mirror: bool) -> int:
    """Clockwise angle L136UsbService.imageMode passes to PlayerNode.rotateJPG."""
    if display_model == VERTICAL:
        return 0 if is_mirror else 180
    return 90 if is_mirror else 270

def prepare_frame(img: Image.Image, display_model: int = HORIZONAL, is_mirror: bool = False,
                  quality: int = 95) -> bytes:
    """Canvas image (854x480 horizontal / 480x854 vertical) -> JPEG bytes for sendImageData."""
    size = (854, 480) if display_model == HORIZONAL else (480, 854)
    img = img.convert("RGB").resize(size)
    cw = rotation_deg(display_model, is_mirror)
    if cw:  # PIL's ROTATE_n is counter-clockwise
        img = img.transpose({90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180,
                             270: Image.Transpose.ROTATE_90}[cw])
    assert img.size == (W, H)
    buf = io.BytesIO()   # baseline, 4:2:0 -- matches FFmpeg mjpeg q=2 yuv420p output
    img.save(buf, "JPEG", quality=quality, subsampling=2, progressive=False, optimize=False)
    return buf.getvalue()

def image_packets(data: bytes):
    """512-byte EP 0x02 packets built by L136.node sendImageData."""
    C = 505
    cnt = -(-len(data) // C)
    yield (b"Start\x01" + len(data).to_bytes(4, "little") + checksum16(data).to_bytes(2, "little")
           + cnt.to_bytes(2, "little") + b"\0" * 498)
    for i in range(len(data) // C + 1):
        chunk = data[i*C:(i+1)*C]
        yield b"trans" + (i + 1).to_bytes(2, "little") + chunk.ljust(C, b"\0")
        if len(chunk) < C:
            break
    yield b"DCLdfinish".ljust(512, b"\0")

# Host loop equivalent to DeepCreative (NOT yet run against the device):
#   ep4.write(cmd_stream_start()); ep4.write(cmd_settings(30))
#   every ~33 ms: for p in image_packets(prepare_frame(render())): ep2.write(p)
```
I checked it offline with no USB I/O. The frames come out as `aa 2e 05 01 … de 00` and `aa 2e 04 00 1e 00 … fa 00`. All four orientation cases produce a 480×854 JPEG, and every image packet is exactly 512 B.

Still unverified without hardware: whether `[05 01]` is actually required before the first frame, what `[04]` byte 1 does on the device, and the exact JPEG constraints the device's decoder imposes (for example, a maximum size or whether progressive JPEG is rejected).

**Tooling** (in `/tmp/deepcreative/re/dyn/`, not kept in this repo): `dumpcp.js` + `heap.js` do the constant-pool/ScopeInfo heap walker, `annotate.py` produces the annotated listing (`main.annotated.txt`, 10 MB), `decomp.py` is the pseudo-JS folder, and `fn.py` is a function grep/print helper. `../xref.py` and `../fnat.py` are PE string-xref and function disassembly helpers used on `ffmplayer.node`. The Electron runtime is in `/tmp/deepcreative/electron23/`.

---

# Phase 4 — First Hardware Bring-Up (`test_probe.py`)

Status: **USB transport confirmed working; visual/orientation correctness NOT yet confirmed by a human.** `test_probe.py` (repo root) opens `3633:002e`, claims interface 0, sends `cmd_stream_start()` + `cmd_settings()` on EP `0x04`, then streams a rendered test card (border edge labels, CPU temp/usage, frame counter) as baseline JPEG frames on EP `0x02` per the §24 reference encoder, unmodified.

Two independent runs against the real device (`--duration 10 --brightness 60` and a follow-up `--duration 5 --brightness 60`, both `--mode horizontal`) completed with **zero USB errors** across ~225 combined frames / ~20,000 packets, ~20-45 ms per frame. This confirms the `AA 2E` command framing (§14), the `Start`/`trans`/`DCLdfinish` image transfer (§15), and the two-command boot sequence (§20) are accepted by the device without error at the transport level.

**Not yet confirmed:** nobody has looked at the physical screen. Whether the image actually renders (vs. being silently rejected/ignored) and whether the `rotation_deg` mapping (270° CW for horizontal, §23) puts `TOP`/`BOTTOM`/`LEFT`/`RIGHT` on the correct physical edges is still open — a human needs to run `test_probe.py` and look at the panel before that claim can be made.

Supporting artifacts added in this phase:
- `udev/99-deepcool-lt360.rules` — installed to `/etc/udev/rules.d/`, grants `MODE="0666"` + `TAG+="uaccess"` for `idVendor==3633, idProduct==002e` so `pyusb` can open the device without root.
- `ref/re_scripts/` — the small `.py`/`.js` RE tooling scripts (not the 10 MB annotated listing or binaries).
- `assets/fonts/` — the bundled `JZFSSans-*`, `Pixel-numsymbol`, `L094-numsymbol`, `LSLDSeries-numsymbol`, `Assassin-numsymbol`, `dc-font` and `SourceHanSansCN` font files used by the official UI.

- `ref/l136_canvas_drawing.md` — extraction of the `Boundary`/`CodeZero`/`PixelWorld` sensor-theme canvas setup from the annotated bytecode. Confirms the theme names are just an ID→name enum, confirms the 854×480 native canvas and `JZFSSans`/`Pixelnumsymbol` font usage independently of §23/§24, but the actual per-theme coordinate/color literals were not locatable via static bytecode reading in this pass — see that file for what was and wasn't recoverable.

**Open for Phase 5**: (1) get a human to actually look at the screen while `test_probe.py` runs, to confirm image rendering and orientation before trusting §23's rotation math; (2) build out the real driver (continuous frame renderer, sensor polling, theme rendering) once that's confirmed — custom themes don't need to replicate DeepCool's exact layout, just the confirmed canvas size/fonts.
