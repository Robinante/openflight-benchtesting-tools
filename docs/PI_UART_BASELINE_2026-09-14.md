# Raspberry Pi UART baseline — 2026-09-14

## Result

The Raspberry Pi received all 23 full-size IWR6843 dumps at their exact
expected length. Each file was 767976 bytes, including a 767808-byte payload.
All 23 files were copied back to Windows and verified against SHA-256 hashes
created on the Pi; no hashes failed.

## Controlled host comparison

The IWR6843LEVM, flashed firmware, CP2105 Enhanced UART, cable, 1041667 baud
rate, IQ16 format, 43-bin window, 12 loops, and Python reader were held
constant. Windows had previously returned intermittent short transfers in
256-byte multiples. Raspberry Pi OS completed every full-size transfer.

This result strongly implicates the Windows CP2105/VCP/USB host path. It also
demonstrates that the IWR L3 snapshot, dump construction, and UART transmit
path can repeatedly produce a complete full-size dump.

## Profiles tested

| Profile | Capture plan | Result | Final counters |
| --- | --- | --- | --- |
| 3 ms period, 30 post frames | 1 pre / 30 post | 13/13 exact-length dumps | 13 freezes complete, 0 timeouts, 14 HWA missed |
| 10 ms period, 5 post frames | 26 pre / 5 post | 10/10 exact-length dumps | 10 freezes complete, 0 timeouts, 0 HWA missed |

The 10 ms / 5-post-frame profile is the new material-testing baseline.

## Firmware timeout finding

A 10 ms / 30-post-frame profile failed with `HWA post-trigger frame freeze
timed out`. It requests 300 ms of post-trigger data, but the current firmware
uses a fixed `Semaphore_pend(..., 250U)` wait. The timeout left the HWA capture
state requiring a sensor restart or IWR power cycle.

The bench CLI now rejects post-trigger plans that reach this firmware limit.
