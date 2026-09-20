# UART reader correction and next bench test

The 2026-09-12 20:09:30 probe used a 10 ms frame period and five retained
post-trigger frames. Its header reports 10,000 us, 64 frames, 36 chirps per
frame, four RX channels, and one IQ16 range bin. The expected dump is 37,164
bytes. Firmware counters show one successful freeze, no freeze timeouts,
no HWA rearm errors, and no missed frame starts.

The saved rejected file has 36,666 bytes, including the final 14 ASCII bytes
`Done\nl3dump:/>`. Its binary portion is 36,652 bytes: 512 bytes short.
This demonstrates a short transfer despite clean HWA and freeze counters.
It does not identify whether the missing bytes originated in firmware output,
UART hardware, the USB bridge, the driver, or host reception.

## Changes in this patch

- Recognize the complete Done/prompt suffix across serial read boundaries,
  after a quiet tail, and exclude it from binary byte counts.
- Never stop merely because the four bytes `Done` occur within a read.
- Preserve every byte returned to the dump reader in a matching `.wire.bin`,
  including any command echo, received dump, and received completion text.
  This is a record at the Python read boundary, not a hardware UART trace.
- Record wire filename, byte count, SHA-256, and completion text in the JSON.
- Show firmware responses that return to the CLI without an ILD1 header.
  Also save received bytes and failure metadata for malformed or missing headers.
- End a stalled short transfer instead of repeatedly restarting its idle timer.

The patch does not recover missing samples, change baud or firmware, or alter
RF settings. Old files are not rewritten. Original rejected dumps remain
rejected. Extra diagnostic files use additional disk space, approximately one
extra received-stream copy per capture.

There is no device-provided payload checksum in this protocol. `complete`
continues to mean complete by structural/length checks, not proof of bit-exact
delivery. A full footer at the end of a stalled binary stream is necessarily
a protocol heuristic; the unmodified wire copy preserves evidence for review.

## Apply and verify in PowerShell

Quit the bench CLI. Save `uart-reader-fix.patch` in the repository root and
stay on the existing `feature/frame-period` branch. This patch is applied on
top of the already-installed frame-period change; no Git pull is involved.

```powershell
git apply --check .\uart-reader-fix.patch
git apply .\uart-reader-fix.patch
git diff --check
python -m unittest discover -s tests -p test_uart_reader.py -v
```

Stop if patch validation or tests fail. These offline tests use Python's
standard library; pytest and a connected radar are not required. They cover
footer fragmentation, byte-at-a-time reads, embedded markers, complete/short/
extra data, absent completion text, firmware errors, incomplete headers, and
saving both the binary file and unmodified received stream.

## Short hardware check

```powershell
python -m openflight_bench --port COM7 --baud 1041667
```

At the bench prompt:

```text
set window 0 1
set frames 5
set period 10
set name uart_p10_f5_readerfix
apply
```

Wait two seconds to fill the pre-trigger ring, then:

```text
stats
repeat 3 raw readerfix_probe
stats
```

Keep the capture size and RF profile fixed for this test. The default startup
profile is still applied automatically, so re-enter the commands above after
restarting the tool. Do not use 30 post frames at 10 ms with the existing
firmware's fixed freeze timeout.

For one rejected capture, provide its `.wire.bin`, `.rejected.l3dump`, and
`.json`, plus the terminal output. If all three pass, provide the terminal
output and one capture's `.wire.bin` and `.json`. A few passing captures do
not establish transport reliability.

## Save the code checkpoint after validation

```powershell
git add openflight_bench/wire.py openflight_bench/capture.py tests/test_uart_reader.py docs/UART_READER_FIX.md
git commit -m "Fix UART footer parsing and preserve receive diagnostics"
git push
```

These commands update your current feature branch. Review hardware results
before merging into your repository's main branch. The downloaded patch is
only a delivery file and need not be committed.
