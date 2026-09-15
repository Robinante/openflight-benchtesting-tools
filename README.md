# OpenFlight bench capture

An interactive CLI for configurable OpenFlight IWR6843 L3-dump firmware.
It generates and sends the RF configuration in memory, captures binary ILD1
records, and archives the exact configuration and metadata beside every dump.
No hand-edited CFG files are needed for the normal bench workflow.

> [!IMPORTANT]
> Raspberry Pi/Linux is the validated host for analysis-ready captures. Under
> the tested Windows + CP2105 VCP path, sustained 1,041,667-baud transfers were
> intermittently truncated in exact 256-byte multiples. Windows remains useful
> for development and transport diagnostics, but not for material baselines.

## Validated status — 2026-09-14

The same IWR6843LEVM, firmware, CP2105 Enhanced interface, cable, baud rate,
IQ16 format, and Python reader were tested from Windows and a Raspberry Pi 5.

| Host and profile | Result | Firmware health |
| --- | ---: | --- |
| Raspberry Pi, 1-bin transport probe | 10/10 exact-length 37,164-byte records | 0 HWA misses; 10/10 freezes completed |
| Raspberry Pi, full capture at 3 ms / 30 post frames | 13/13 exact-length 767,976-byte records | 0 freeze timeouts; 14 accumulated HWA misses |
| Raspberry Pi, full capture at 10 ms / 5 post frames | 10/10 exact-length 767,976-byte records | 0 HWA misses; 10/10 freezes completed |
| Windows, equivalent diagnostic profiles | Intermittent truncation | Missing payload resolved into exact 256-byte multiples |

All 23 full Pi captures were copied back to Windows and matched SHA-256 hashes
created on the Pi. Structural and signal-level analysis found no evidence of
byte shifts, I/Q word shifts, channel/index shifts, duplicated 256-byte blocks,
duplicated frames, or output-rail clipping.

This does not mean Windows UART is generally broken. It isolates the observed
loss to the tested Windows host path: Windows, the CP2105 VCP/USB stack,
pySerial, the nonstandard baud rate, and sustained binary bursts. It also does
not provide firmware-originated proof against same-length corruption; ILD1 v7
has no payload CRC. A CRC and dump-sequence extension is scoped in the project
documentation.

## Install and start on Raspberry Pi/Linux

```bash
git clone https://github.com/Robinante/openflight-benchtesting-tools.git
cd openflight-benchtesting-tools

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

The capture user must have serial-port access. On Raspberry Pi OS/Debian, add
the user to `dialout` once, then log out and back in:

```bash
sudo usermod -aG dialout "$USER"
```

Find the CP2105 interfaces:

```bash
python -m serial.tools.list_ports -v
ls -l /dev/serial/by-id/
```

Use the CP2105 **Enhanced** interface (`if00`). For the validated board:

```bash
python -m openflight_bench \
  --port /dev/serial/by-id/usb-Silicon_Labs_CP2105_Dual_USB_to_UART_Bridge_Controller_0130F518-if00-port0 \
  --baud 1041667
```

The `/dev/serial/by-id/` path is preferred over `/dev/ttyUSB0` because it
remains tied to the physical CP2105 device across reconnects.

## Start on Windows for development or diagnosis

```powershell
Set-Location path\to\openflight-benchtesting-tools
py -m pip install -e .
py -m openflight_bench --port COM7 --baud 1041667 --out-dir .\captures
```

Confirm the actual Enhanced-interface COM number with:

```powershell
py -m serial.tools.list_ports -v
```

Do not admit Windows captures into analysis solely because a short batch
happened to complete. Nominally identical Windows runs varied from 10/10 to
1/10, and reducing a record to 6,444 bytes improved but did not eliminate the
loss.

## Validated material-test baseline

The default applied profile is:

| Setting | Value |
| --- | ---: |
| Start frequency | 60.0 GHz |
| ADC samples / sample rate | 128 / 4000 ksps |
| TX | TX0 + TX1 + TX2 |
| TDM loops | 12 |
| RX gain | 24 dB |
| TX backoff | 6 dB (`394758` / `0x060606`) |
| HPF1 / HPF2 | 0 / 0 |
| Sample format | IQ16 |
| Retained bins | 0–42, approximately 0–2.01 m |
| Frame period | 10 ms |
| Retained post-trigger frames | 5 |
| Capture plan | 26 pre-trigger + 5 post-trigger frames |
| ILD1 payload / record | 767,808 / 767,976 bytes |

The packed three-transmitter backoff word for 6 dB is `394758` / `0x060606`,
not the human-readable value `6`.

## Interactive workflow

The default profile is applied automatically at startup. Changes remain
pending until `apply` is entered.

```text
bench> show
bench> stats
bench> repeat 10 raw transport_check
bench> baseline empty_fixture
bench> material petg_gf_1mm

bench> set tx off
bench> apply
bench> raw tx_off

bench> set tx all
bench> set rxgain 30
bench> set period 10
bench> set frames 5
bench> set name rx30_p10_f5
bench> apply
bench> raw rx30

bench> run rxgain 24,30,36,42
bench> run txbackoff 0,6,12
bench> run tx off,tx0,tx1,tx2,tx02,all
bench> run hpf1 0,1,2,3
bench> run hpf2 0,1,2,3
bench> sweep elevation -10 angle_m10
bench> summary
bench> quit
```

`tx02` means TX0 + TX2, the pair used for OpenFlight's fine elevation axis.
All TX selections retain three chirp indices so the firmware geometry guard
remains satisfied; disabled transmitters receive zero `chirpCfg` masks.

## Capture acceptance and preservation

The reader computes the expected record length from the returned ILD1 header
and frame descriptors.

- Exact-length records are saved as `.l3dump` and marked
  `accepted_for_analysis=true`.
- Short or overlong records are saved as `.rejected.l3dump`, marked
  `accepted_for_analysis=false`, and excluded from analysis.
- The original UART transaction is preserved as `.wire.bin`, including on
  failures.
- JSON sidecars record the profile, firmware counters, expected and actual
  lengths, completion state, file SHA-256, and wire SHA-256.
- Generated CFG snapshots and `session_manifest.json` preserve the applied
  configuration and capture chronology.
- `summary` writes `session_summary.csv` for the current session.

Each invocation creates a new timestamped session directory. Existing capture
directories are never reused or mixed into a new baseline.

Exact length and host-generated hashes provide strong gating and persistence
checks, but not complete end-to-end proof. Until firmware CRC is implemented,
keep the sidecars and `.wire.bin` files with the accepted dumps.

## Firmware timing constraint

The current configurable firmware uses a fixed 250-tick HWA freeze wait. The
CLI rejects profiles whose requested post-trigger duration reaches 250 ms.
`30 frames x 10 ms` was verified to time out, while the validated
`5 frames x 10 ms` baseline completed without HWA misses.

If a freeze timeout leaves the sensor state unhealthy, stop and restart the
sensor or power-cycle the IWR before continuing. Do not treat the failed record
as a transport result.

## Evidence and investigation notes

- [Raspberry Pi UART baseline](docs/PI_UART_BASELINE_2026-09-14.md)
- [Windows vs. Raspberry Pi UART investigation](docs/WINDOWS_VS_PI_UART_REPORT_2026-09-14.md)
- [Interactive Pi capture analysis](docs/reports/pi-uart-baseline-2026-09-14/index.html)
- [UART reader diagnostics and wire preservation](docs/UART_READER_FIX.md)
- [Proposed ILD1 CRC and dump-sequence scope](docs/ILD1_V8_INTEGRITY_SCOPE.md)
- [Initial raw-dump follow-up](docs/OpenFlight_Raw_Dump_Followup.md)

The interactive HTML report must be downloaded or opened locally unless the
repository is being served through GitHub Pages.

## Firmware assumptions

This package targets the configurable OpenFlight build using ILD1 version 7,
timed format 4/5, three acquired chirp indices (0, 1, 2), four RX channels, 128
ADC samples, and the `captureCfg` command. It cannot make the proposed
`l3_satmon` source active; that source is not listed in the supplied makefile
or connected to `l3_dump.c`.

The package controls capture configuration and produces sidecars compatible
with the existing `analyze_material.py` and `analyze_sweep.py`. Continue using
those analyzers on accepted files until their calculations are deliberately
folded into this package.

## Development

Install pytest and run the suite before committing changes:

```bash
python -m pip install pytest
python -m pytest -q
```

This repository is an independent bench-testing workspace for OpenFlight radar
experiments. It is intentionally separate from the OpenFlight application
repository. Generated captures and local scratch artifacts remain outside Git;
commit only reusable tools, reproducible tests, profiles, reports, and design
notes.
