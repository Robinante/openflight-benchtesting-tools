# Windows vs. Raspberry Pi IWR6843 UART Capture Investigation

**Investigation dates:** 2026-09-10 through 2026-09-14  
**Report date:** 2026-09-15  
**Radar:** TI IWR6843LEVM  
**USB-UART bridge:** Silicon Labs CP2105 Dual UART Bridge, Enhanced interface  
**Wire rate:** 1,041,667 baud, 8N1, no flow control  
**Result:** Raspberry Pi selected as the supported bench-capture host; Windows captures remain diagnostic-only.

## 1. Executive Conclusion

The intermittent `short_by` problem was reproduced repeatedly on the Windows laptop and eliminated by moving the same IWR6843/CP2105 capture path to a Raspberry Pi.

The strongest controlled comparison is:

- **Windows:** frequent truncated ILD1 payloads, usually missing exact multiples of 256 bytes, across two cables, the laptop's USB-A path, a USB-C adapter/path, power-management changes, the custom bench reader, and an upstream OpenFlight driver probe.
- **Raspberry Pi:** 10/10 complete small diagnostic dumps, followed by **23/23 complete 767,976-byte full captures** using the same baud rate and ILD1 firmware.
- All 23 full Pi dumps passed structural parsing and SHA-256 verification after transfer back to Windows.
- Deep analysis found no evidence of byte shifts, I/Q word shifts, channel/index shifts, duplicated 256-byte blocks, duplicated frames, or output rail clipping.

This does **not** establish that “Windows UART is broken” generally. It isolates the fault to the tested Windows host path: the interaction among Windows, the CP2105 VCP/USB stack, pySerial, the nonstandard 1,041,667-baud configuration, and sustained binary bursts. The exact failing layer inside that host path was not identified.

The evidence strongly argues against the following as primary causes of the short records:

- IWR6843 L3 ring storage
- HWA/EDMA payload production
- OpenFlight's binary header-size calculation
- The custom bench reader alone
- One defective USB cable
- Micro-USB bandwidth
- USB selective suspend alone

The high-rate HWA cadence issue (`hwa_missed`) is real but separate from UART byte loss.

## 2. Operational Decision

For material and enclosure testing:

1. Capture on the Raspberry Pi.
2. Use the validated 10 ms / 5-post-frame profile.
3. Keep strict expected-length rejection and SHA-256 manifests.
4. Preserve `.wire.bin` for transport failures.
5. Treat Windows as a development/diagnostic host until the CP2105/VCP behavior is independently explained or firmware-originated CRC is added.

Validated material-test starting profile:

| Setting | Value |
|---|---:|
| RX gain | 24 dB |
| TX backoff | 6 dB |
| HPF1 / HPF2 | 0 / 0 |
| TX | All: TX0, TX1, TX2 |
| TDM loops | 12 |
| Retained bins | 0-42 |
| Frame period | 10 ms |
| Post-trigger frames | 5 |
| Sample format | IQ16 |

## 3. Test System

### 3.1 Common hardware and firmware

- TI IWR6843LEVM
- Integrated Silicon Labs CP2105 Dual USB-to-UART Bridge
- USB VID:PID `10C4:EA70`
- CP2105 serial `0130F518`
- Enhanced interface:
  - Windows: `COM7`
  - Linux: `/dev/ttyUSB0`, interface `if00`
- Standard CP2105 interface was not used for the dumps.
- ILD1 timed variable-width IQ16 firmware
- UART opened at 1,041,667 baud
- Command echo, binary ILD1 record, and completion prompt shared the Enhanced interface

OpenFlight's current developer guide describes the same on-chip path: ADCBUF -> HWA range FFT -> EDMA -> L3 circular frame ring -> freeze -> stream to Raspberry Pi. It also defines `dump_format.h` and the Python decoder as one synchronized wire contract:

<https://github.com/open-flight/openflight/blob/main/docs/development/firmware.md>

### 3.2 Windows host

- Windows laptop
- PowerShell 7.6.6
- Python 3.14
- pySerial
- CP2105 Enhanced VCP on `COM7`
- Device Manager receive and transmit FIFO sliders at their maximum settings
- USB power-management permission disabled during later tests

The 9,600-baud value visible in Device Manager was not the active capture baud. pySerial configured 1,041,667 baud when it opened the port.

### 3.3 Raspberry Pi host

- Raspberry Pi 5
- Debian 13 (trixie), aarch64
- Linux 6.18.34 Raspberry Pi kernel
- Python 3.13 virtual environment
- Linux `cp210x` driver presenting `/dev/ttyUSB0`
- Stable by-id path:

```text
/dev/serial/by-id/usb-Silicon_Labs_CP2105_Dual_USB_to_UART_Bridge_Controller_0130F518-if00-port0
```

- User in `dialout`
- `vcgencmd get_throttled` returned `0x0`

## 4. Windows Test Results

The table preserves individual experiments rather than presenting one aggregate failure rate because frame plans, payload sizes, reader versions, and USB paths varied.

| Session / condition | Geometry | Result | Important observation |
|---|---|---:|---|
| Original supplied RX-gain sweep | Legacy captures | 2/5 complete | Established the initial short-record problem |
| 17:32 diagnostic batches | 1 bin, 5 post, 3 ms, 12 loops; 37,164-byte record | 13/20 complete | Failures initially reported as 242/498/1,010-byte shortfalls |
| 17:43 spaced captures | Same small profile, manually spaced | 3/5 complete | Waiting between commands did not eliminate failures |
| 18:37 three back-to-back batches | 1 bin, 30 post, 3 ms, 12 loops; 37,164-byte record | 28/30 complete | A good run did not remain consistently good; final `hwa_missed=29` |
| 19:44 P3 batch | 1 bin, 30 post, 3 ms | 7/10 complete | Mixed short and complete records remained |
| 20:08 P10/F5 single probe | 1 bin, 5 post, 10 ms | 0/1 complete | 512-byte-class loss persisted at slower cadence |
| 20:22 corrected reader | 1 bin, 5 post, 10 ms | 0/3 complete | Exact logical shortfalls: 256, 256, and 1,024 bytes |
| 20:43 cable B, USB-A, power saving off | 1 bin, 5 post, 10 ms | 4/10 complete | New cable and power setting did not solve it |
| 20:53 cable B through USB-C adapter, power saving off | Same | 1/10 complete | Alternate laptop USB path was worse, not better |
| 21:28 upstream OpenFlight driver, clone commit `8668dfc6` | 1 bin, 5 post, 10 ms, 12 loops | 2/10 complete | Reproduced outside the custom bench reader |
| 21:43 upstream driver, reduced loops | 1 bin, 5 post, 10 ms, 2 loops; 6,444-byte record | 8/10 complete | Smaller record improved yield but still lost 256 bytes twice |

Success rates changed substantially between batches, including 10/10 runs followed by failures under nominally identical settings. Therefore a successful short run on Windows was not adequate evidence of transport reliability.

### 4.1 Shortfall signature

Before the diagnostic reader fix, the 14-byte ASCII completion footer could remain mixed into the count displayed as `returned`, producing confusing values:

| Earlier displayed `short_by` | Actual missing binary payload |
|---:|---:|
| 242 | 256 bytes |
| 498 | 512 bytes |
| 1,010 | 1,024 bytes |
| 1,266 | 1,280 bytes |

After the reader separated the binary record from the 14-byte `Done\nl3dump:/>` footer, observed losses resolved into exact multiples of 256 bytes:

```text
256, 512, 768, 1,024, 1,280, and 1,536 bytes
```

This was a major diagnostic result:

- The original reader made the reported shortfall look irregular by 14 bytes.
- The reader did not cause the underlying loss.
- The firmware was returning its completion footer after a truncated binary payload.
- Exact 256-byte multiples point toward buffering or transfer-block behavior somewhere in the Windows host path, although they do not identify the exact driver or layer.

### 4.2 Failure timing

With the diagnostic reader, complete 37-kB records commonly completed in about 0.13-0.44 seconds depending on geometry. Short records often took approximately 4.4-4.7 seconds because the footer had arrived but the reader correctly continued waiting for the declared binary length before timing out and rejecting the record.

Earlier reader behavior could wait roughly 45 seconds after some failures. Fixing the state machine made diagnosis faster and preserved the actual wire bytes; it did not repair the transport.

## 5. Changes and Hypotheses Tested on Windows

### 5.1 Correct port and UART configuration

- Confirmed `COM7` was the CP2105 Enhanced interface.
- Confirmed `COM8` was the Standard interface and was not selected.
- Used explicit `--baud 1041667` on every relevant run.
- Used binary UART reading and writing in firmware/host paths.

**Result:** Correct interface and explicit baud did not prevent short records.

### 5.2 Reader/parser behavior

- Replaced footer-oriented parsing with expected-length-driven binary capture.
- Preserved the complete original `.wire.bin` record.
- Added failure metadata and strict rejection.
- Exercised 11 UART-reader unit tests, including all footer splits, single-byte reads, embedded marker bytes, missing footer, partial header, extra bytes, and firmware errors.
- Reproduced failures using the upstream OpenFlight Python driver rather than only `openflight_bench`.

**Result:** Parser accounting improved and false interpretations were removed. Physical byte loss continued.

### 5.3 Capture size

- Reduced retained range data to one bin.
- Reduced loops from 12 to 2, shrinking the record from 37,164 to 6,444 bytes.

**Result:** Smaller transfers improved success from 2/10 to 8/10 in the upstream probe, but did not eliminate exact 256-byte losses. The problem was size-sensitive, not dependent on full L3 capacity.

### 5.4 Frame timing and HWA load

- Added configurable frame period to the bench CLI.
- Tested 3 ms and 10 ms periods.
- Tested 30 and 5 post-trigger frames.

**Result:** Slower 10 ms cadence prevented HWA misses in the chosen baseline, but Windows UART truncation still occurred with `hwa_missed=0`, successful freeze completion, and no RF faults.

### 5.5 Cable, physical port path, and USB power

- Replaced the USB-A to Micro-USB cable.
- Disabled the CP2105 Enhanced port's Windows power-management permission.
- Confirmed FIFO buffers were enabled and set high.
- Tested the laptop's only native USB-A port.
- Tested a USB-C port through a USB-A-to-C adapter.

**Result:** No combination eliminated the losses. The USB-C-adapter run completed only 1/10 records.

### 5.6 Micro-USB bandwidth

The connector was not operating near its bandwidth limit. A 1,041,667-baud 8N1 UART carries roughly 104 kB/s of payload before protocol overhead, far below USB 2.0 capacity.

**Result:** Connector bandwidth is not a plausible explanation. A defective physical path was also weakened by the second cable and successful Pi testing.

## 6. Raspberry Pi Test Results

The IWR6843 was moved to the Raspberry Pi and detected as the same CP2105 Enhanced interface on `/dev/ttyUSB0`.

| Pi test | Geometry | Result | Firmware health |
|---|---|---:|---|
| Small transport probe | 1 bin, 5 post, 10 ms, 12 loops; 37,164 bytes | **10/10 complete** | `hwa_missed=0`, `freeze_req=10`, `freeze_done=10`, `freeze_to=0` |
| Full P3 capture group | 43 bins, 30 post, 3 ms, 12 loops; 767,976 bytes | **13/13 complete** | Payloads clean; cadence accumulated approximately one `hwa_missed` per dump |
| Full P10 capture group | 43 bins, 5 post, 10 ms, 12 loops; 767,976 bytes | **10/10 complete** | `hwa_missed=0`, `freeze_req=10`, `freeze_done=10`, `freeze_to=0` |

Combined emitted-record result on Pi:

- **33/33 complete transfers** across the small probe and two full-size groups.
- **23/23 full-size transfers** at 767,976 bytes each.

One attempted 10 ms / 30-post configuration failed inside the firmware's post-trigger freeze operation and emitted no ILD1 record. Its counters showed `freeze_done=0` and `freeze_to=1`. It is classified as a firmware capture-state/configuration failure, not a UART-short-record failure. After reapplying a valid profile, captures resumed normally.

## 7. Full Pi Dataset Integrity Analysis

The 23 full captures were copied from the Pi to Windows with their sidecars, generated configurations, wire records, manifest, and `SHA256SUMS`. The analysis archive contained the 23 ILD1 files, sidecars, profiles, manifest, and checksums; wire files were intentionally omitted from that reduced archive.

### 7.1 File and format verification

- 23/23 ILD1 files were exactly 767,976 bytes.
- 23/23 matched the SHA-256 recorded in their JSON sidecars.
- 23/23 matched the session `SHA256SUMS` file after Pi-to-Windows transfer.
- 23/23 referenced the correct saved configuration SHA-256.
- All 23 dump SHA-256 hashes were unique.
- Every record decoded as ILD1 version 7 timed variable-width IQ16.
- Every record contained:
  - 31 retained frames
  - 36 chirps per frame
  - 3 TX
  - 4 RX
  - 43 bins
  - 767,808 payload bytes
  - 168 bytes of header, temperature, and descriptors
- Frame timing descriptors were exactly 3,000 or 10,000 microseconds as configured.

### 7.2 Missing/shifted data checks

Across 8,829,792 decoded signed int16 I/Q components:

- Maximum absolute component: 17,330
- Output rail hits at -32,768 or +32,767: 0
- Components above absolute 20,000: 0
- Output headroom: 5.53 dB
- Exact duplicate non-overlapping 256-byte payload blocks: 0 across 68,977 blocks
- Exact adjacent duplicate frames: 0
- Long all-zero byte runs: none; maximum was only 3-4 bytes

A forced one-byte-offset decode produced the expected corruption signature:

- 38.7% of components exceeded absolute 20,000
- 8.2% exceeded absolute 30,000

The correctly aligned data had zero components above 20,000. Any sustained odd-byte shift would therefore have been obvious.

### 7.3 Structural coherence

At dominant range bin 2:

- Minimum per-loop 12-channel complex-fingerprint correlation:
  - P3: 0.999531
  - P10: 0.999582
- Minimum frame-averaged fingerprint correlation:
  - P3: 0.999935
  - P10: 0.999979
- P10 pre/post memory-boundary change:
  - Mean amplitude: -0.0090 dB
  - Common phase: +0.0556 degrees
  - Mean complex correlation: approximately 0.999998

These checks strongly reject persistent shifts in I/Q byte alignment, word alignment, bin indexing, chirp indexing, RX order, TX order, or the pre/post L3 memory boundary.

### 7.4 Signal repeatability

- P10 median per-bin capture standard deviation: 0.066 dB
- P10 maximum per-bin capture standard deviation: 0.282 dB at bin 11
- P3 median per-bin capture standard deviation: 0.087 dB
- P10 dominant-bin element standard deviation: 0.055-0.113 dB
- Relative phase standard deviation to TX0/RX0: no more than approximately 0.15 degrees
- P10 background range-profile floor: approximately -34.22 dBFS
- P3 background range-profile floor: approximately -34.80 dBFS

P10 dominant-bin power drifted smoothly by approximately +0.325 dB while mean RX temperature fell by 3.5 degrees C. This is physical/thermal behavior, not corruption, and supports temperature stabilization plus interleaved references for precise material-loss work.

## 8. HWA Cadence vs. UART Transport

Two independent problems appeared during testing.

### 8.1 HWA cadence behavior

- At 3 ms, `hwa_missed` could increase while the radar continued running.
- On Windows, a 30-dump P3 diagnostic session ended at `hwa_missed=29`.
- On Pi, the 13 full P3 captures were all byte-perfect even though `hwa_missed` accumulated around the dump/rearm events.
- The 10 ms / 5-post Pi profile held `hwa_missed=0` throughout its ten full captures.
- A 10 ms / 30-post attempt produced an explicit firmware freeze timeout and no ILD1 record.

### 8.2 UART transport behavior

- Windows produced short records even when `hwa_missed=0`, `freeze_req == freeze_done`, `freeze_to=0`, and `rf_faults=0`.
- Pi produced complete records even during the P3 group where `hwa_missed` was nonzero.

Therefore:

- `hwa_missed` diagnoses frame-processing cadence and rearm timing.
- `short_by` diagnoses incomplete host receipt of an already-declared ILD1 record.
- They can coexist, but one does not explain the other.

## 9. What the Evidence Supports

### High confidence

- The Pi/Linux capture path is reliable for the tested profiles.
- The 10 ms / 5-post profile is the preferred baseline cadence.
- Windows short records represent actual missing binary bytes, not merely incorrect footer parsing.
- The custom bench reader is not the root cause because the upstream OpenFlight driver reproduced the losses.
- The full Pi dataset contains no detectable structural corruption.

### Moderate confidence

- The loss occurs within the Windows-side CP2105 VCP/USB/serial-receive path or its interaction with host scheduling and buffering.
- Exact 256-byte losses are consistent with dropped or discarded buffered transfer blocks.

### Not established

- The exact Windows driver component or call responsible.
- Whether a different CP2105 driver release, lower baud, different Windows machine, or different USB controller would eliminate the issue.
- Cryptographic certainty that no isolated low-order bit changed on the wire; ILD1 v7 has no firmware-generated CRC.

## 10. Ruled Out or Substantially Weakened

| Candidate cause | Status | Reason |
|---|---|---|
| Wrong COM port/interface | Ruled out | Enhanced CP2105 interface positively identified on both hosts |
| Device Manager 9,600 setting | Ruled out | pySerial explicitly configured 1,041,667 at open |
| One bad cable | Substantially weakened | Two cables tested; Pi succeeded through same board/bridge path |
| USB selective suspend alone | Ruled out as sole cause | Disabling device power control did not resolve failures |
| USB-A controller alone | Substantially weakened | USB-C adapter/path also failed |
| Micro-USB bandwidth | Ruled out | UART throughput is far below USB 2.0 capacity |
| Custom bench parser alone | Ruled out | Upstream OpenFlight driver reproduced; raw wire preservation confirmed loss |
| Footer-marker collision | Ruled out | Expected-length reader ignores marker-like payload bytes and has unit coverage |
| Full L3 size only | Ruled out | Loss remained with 6,444-byte records |
| HWA misses causing short UART records | Ruled out | Windows shorts occurred at `hwa_missed=0`; Pi full records occurred with nonzero P3 misses |
| IWR L3 save path as primary cause | Strongly weakened | Same firmware/L3 data produced 23/23 coherent full records on Pi |

## 11. Remaining Limitations

ILD1 v7 provides no firmware-generated record checksum or acquisition sequence number. Current assurance combines:

- Expected length
- Header/descriptor consistency
- Host SHA-256
- Wire-envelope metadata
- Statistical and complex-channel coherence
- Cross-host reproduction

This is strong engineering evidence, but the cleanest end-to-end proof would add a firmware-originated CRC32 and dump sequence. See `ILD1_V8_INTEGRITY_SCOPE.md`.

## 12. Recommended Next Steps

### Immediate material/enclosure work

1. Use Raspberry Pi capture only.
2. Keep the validated P10/F5 profile.
3. Thermally stabilize before baseline acquisition.
4. Record temperatures with every dump.
5. Use a measured trihedral range outside the coupling-dominated first bins.
6. Keep the scene still and interleave no-material references during material tests.
7. Continue rejecting every short transfer rather than padding or clipping FFT data.

### Deferred transport work

1. Add firmware-originated CRC32 and a dump sequence number.
2. Optionally capture USB traffic on Windows to identify where 256-byte blocks disappear.
3. Record CP2105 Windows driver version and compare another Windows PC/controller.
4. Test lower exact-divisor baud rates only if Windows support becomes operationally necessary.
5. Add per-frame acquisition sequence values later if L3/HWA provenance needs direct validation.

## 13. Final Disposition

**Windows:** unsuitable for analysis-ready IWR6843 captures under the tested configuration. Useful for reproducing and diagnosing the host transport defect.  
**Raspberry Pi:** validated for ongoing bench, material, and enclosure testing.  
**Data integrity:** no detectable corruption in the 23 full Pi captures; verifiable by strict structure, length, and host hashes, with firmware CRC reserved as a future improvement.  
**Preferred profile:** RX24 / TX backoff 6 / HPF 0,0 / all TX / 12 loops / bins 0-42 / 10 ms / 5 post.

