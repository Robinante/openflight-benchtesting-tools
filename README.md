# OpenFlight bench capture

One interactive CLI for the configurable OpenFlight IWR6843 L3-dump firmware.
It generates and sends the RF configuration in memory, then archives the exact
CFG and profile metadata beside each capture. No hand-edited CFG files are
needed for the normal bench workflow.

## Install and start on Windows PowerShell

```powershell
cd path\to\openflight-bench
py -m pip install -e .
openflight-bench --port COM7 --out-dir .\captures
```

The default applied profile is 60.0 GHz, 128 samples at 4000 ksps, 3 TX, 12
loops, RX gain 24 dB, TX backoff 6 dB, HPF1/HPF2 0/0, IQ16, fixed bins 0–42
(about 0–2.01 m), 30 retained frames, stride 1. For 6 dB the generated packed backoff word is
`394758` / `0x060606`, not the human number `6`.

## Prompt examples

```text
bench> show
bench> raw empty_scene
bench> baseline bare_reflector
bench> material petg_gf_1mm
bench> set tx off
bench> apply
bench> raw tx_off
bench> set tx all
bench> set rxgain 30
bench> apply
bench> raw rx30
bench> run rxgain 24,30,36,42
bench> run txbackoff 0,6,12
bench> run tx off,tx0,tx1,tx2,tx02,all
bench> run hpf1 0,1,2,3
bench> run hpf2 0,1,2,3
bench> sweep elevation -10 angle_m10
bench> summary
```

`tx02` means TX0 + TX2, the pair used for OpenFlight’s fine elevation axis.
All TX selections retain three chirp indices so the firmware geometry guard
remains satisfied; disabled TXs have zero `chirpCfg` masks.

## Capture acceptance

The reader computes expected length from the returned ILD1 header and
descriptors. A short or overlong transfer is saved as `.rejected.l3dump`, with
`accepted_for_analysis=false` in its JSON sidecar. It is never presented as a
usable last-frame capture. Exact length is necessary, not a checksum; the wire
format itself has no payload CRC.

Each session gets a new timestamped directory. It contains generated profile
CFG snapshots, `session_manifest.json`, accepted `.l3dump` files, rejected
files for diagnosis, JSON sidecars, and an optional `session_summary.csv`.
Existing capture directories are not touched or mixed into the new baseline.

## Firmware assumptions

This package targets the uploaded configurable OpenFlight build: version 7,
timed format 4/5, three acquired chirp indices (0, 1, 2), four RX, 128 ADC
samples, and the `captureCfg` command. It cannot make the proposed `l3_satmon`
source active; that source is not listed in the supplied makefile or hooked
into `l3_dump.c`.

The package controls capture configuration and produces sidecars compatible
with the existing `analyze_material.py` and `analyze_sweep.py`. Continue using
those analyzers on accepted files until their math is deliberately folded into
a later package revision.
