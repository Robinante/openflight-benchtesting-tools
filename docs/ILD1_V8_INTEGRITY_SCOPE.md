# ILD1 End-to-End Integrity Extension Scope

**Status:** Backlog; ready for implementation planning
**Proposed contract:** ILD1 version 8
**Primary objective:** Make every accepted IWR6843 dump independently verifiable from frozen L3 memory through host parsing and file persistence.
**Target repositories:** OpenFlight and `openflight-benchtesting-tools`

> Before implementation, inspect the current OpenFlight `main` branch. If version 8 has already been assigned, use the next unused ILD1 version while preserving the design below.

## 1. Background

The current ILD1 version 7 record contains a fixed header, temperature report, per-frame timing/window descriptors, an optional IQ8 scale table, and the complex IQ payload. Host-side expected-length checks and SHA-256 hashes are useful but do not provide firmware-originated end-to-end integrity:

- Expected length detects truncation but not same-length corruption, duplication, or reordering.
- A host-generated SHA-256 proves that the saved file did not change after reception; it does not prove that the host received exactly what the IWR6843 emitted.
- The current record has no dump transaction identifier.
- Frame timing and complex-channel coherence can expose many structural faults, but they are inferential rather than definitive integrity checks.

September 2026 testing found repeated Windows-side losses in exact 256-byte multiples, while the same IWR6843, firmware, CP2105 interface, baud rate, and capture software produced 23/23 complete full-size dumps on a Raspberry Pi. No corruption was detected in those Pi captures, but a firmware-originated checksum would make future acceptance deterministic.

OpenFlight documents the firmware and Python decoder as a single binary contract that must remain synchronized:

- `firmware/iwr6843/dump_format.h`
- `firmware/iwr6843/l3_dump.c`
- `src/openflight/iwr6843/dump.py`

Reference: <https://github.com/open-flight/openflight/blob/main/docs/development/firmware.md#firmware-and-host-contract>

## 2. Goals

1. Detect any changed, inserted, deleted, duplicated, or reordered byte in a complete ILD1 record with CRC32 error-detection strength.
2. Identify skipped, repeated, or out-of-order dump transactions with a monotonic dump sequence number.
3. Preserve parsing support for existing ILD1 versions 1 through 7.
4. Preserve the existing v7 header, temperature, descriptor, scale, and payload layouts.
5. Reject unverifiable records before they enter analysis.
6. Preserve the original wire record and detailed failure metadata for diagnosis.
7. Add negligible storage and timing overhead.

## 3. Non-goals

- This is not authentication or tamper resistance. CRC32 is not cryptographic.
- This does not correct lost bytes; it detects and classifies them.
- This does not by itself prove that HWA/EDMA selected the intended frame or range window before data reached L3.
- This does not redesign UART framing, change baud rate, or fix the suspected Windows CP2105/VCP loss mechanism.
- This does not require per-frame CRCs in the first implementation.

## 4. Proposed ILD1 v8 Wire Contract

Keep the complete v7 record unchanged and append a fixed 12-byte little-endian integrity trailer:

```c
#define L3_DUMP_VERSION_INTEGRITY 8U
#define L3_DUMP_INTEGRITY_MAGIC   "ILC1"

typedef struct __attribute__((packed)) {
    char     magic[4];       /* "ILC1" */
    uint32_t dump_sequence;  /* Monotonic within one firmware boot */
    uint32_t record_crc32;   /* CRC-32/ISO-HDLC; field excluded */
} l3_integrity_trailer_t;    /* sizeof == 12 */
```

Serialized order:

1. Existing 20-byte ILD1 header
2. Existing 24-byte temperature report, when required by the version/format
3. Existing frame descriptors
4. Existing IQ8 frame-scale table, when present
5. Existing IQ16 or IQ8 wire-format payload
6. Trailer magic: `ILC1`
7. Little-endian `dump_sequence`
8. Little-endian `record_crc32`

### 4.1 CRC definition

Use standard **CRC-32/ISO-HDLC**, compatible with Python `zlib.crc32`:

| Parameter | Value |
|---|---:|
| Width | 32 bits |
| Polynomial | `0x04C11DB7` |
| Reflected polynomial | `0xEDB88320` |
| Initial value | `0xFFFFFFFF` |
| Input reflected | Yes |
| Output reflected | Yes |
| Final XOR | `0xFFFFFFFF` |
| Check value for ASCII `123456789` | `0xCBF43926` |

The CRC covers every byte from the initial `ILD1` magic through the trailer's `ILC1` magic and `dump_sequence`. The four-byte `record_crc32` field is excluded.

This choice protects header geometry, temperature, timing/window metadata, scale values, sample ordering, payload bytes, trailer identification, and sequence number with one calculation.

### 4.2 Sequence semantics

- Maintain a firmware-global `uint32_t gDumpSequence`.
- The first emitted record after boot has sequence 1.
- Allocate/increment the sequence after a successful capture freeze and before the first binary byte is transmitted.
- A cancellation or failed transmission consumes its allocated number. The resulting host-visible gap is diagnostic information.
- Wrap naturally modulo `2^32`.
- A radar reboot resets the sequence. The host must treat a reconnect or decreasing `device_time_ms` as a new sequence epoch rather than corruption.
- The first record observed in a host session establishes the starting value; it need not equal 1.

## 5. Firmware Work

### 5.1 Likely files

- `firmware/iwr6843/dump_format.h`
- `firmware/iwr6843/l3_dump.c`
- `firmware/iwr6843/makefile`, only if a new source file is introduced
- `docs/development/firmware.md`
- Release image and recorded SHA-256 after validation

### 5.2 Implementation tasks

1. Add the v8 version constant and packed trailer definition.
2. Add `gDumpSequence` and optionally `gLastDumpCrc` and `gUartShortWrites` diagnostic counters.
3. Implement a table-driven or otherwise bounded software CRC32 routine.
4. Do not casually reuse TI CRC channel 1: the current firmware assigns it to mmWaveLink. Use software CRC initially unless a separate hardware channel is proven safe.
5. Introduce a binary write wrapper that:
   - Updates the active CRC over the exact wire-format bytes.
   - Calls `UART_writePolling`.
   - Checks and records a short/error return when the UART API reports one.
6. Replace every integrity-covered binary write in the dump path with the wrapper:
   - Header
   - Temperature report
   - Frame descriptors
   - IQ8 scales
   - IQ16 frame blocks
   - IQ8 compressed output chunks
   - Trailer magic and sequence
7. Finalize the CRC and write the four-byte CRC field without including that field in the calculation.
8. Emit version 8 only when the trailer is present.
9. Add `dump_seq`, `last_crc`, and `uart_write_err` to `stats` if practical.
10. Confirm that cancellation leaves an unambiguous partial record and does not emit a misleading valid trailer.

### 5.3 Important implementation detail

CRC must be calculated over the **actual transmitted representation**:

- IQ16: bytes read from L3 and passed to UART.
- IQ8: compressed/scaled bytes in each temporary output buffer, not the original IQ16 scratch values.

This ensures the firmware and host hash the same byte stream.

## 6. OpenFlight Host Work

### 6.1 Likely files

- `src/openflight/iwr6843/dump.py`
- IWR6843 driver/capture code that reads the record
- Existing IWR6843 parser and driver tests
- Firmware developer documentation

### 6.2 Parser behavior

1. Continue decoding versions 1 through 7 exactly as before.
2. For v8, add 12 bytes to the calculated record length.
3. Require trailer magic `ILC1` at the calculated boundary.
4. Decode `dump_sequence` and transmitted CRC as little-endian unsigned integers.
5. Compute CRC32 over the defined covered bytes.
6. Apply validation in this order:
   1. ILD1 synchronization and minimally safe header validation
   2. Expected-length validation
   3. Trailer-magic validation
   4. CRC validation
   5. Sequence diagnostics
   6. IQ decoding and analysis admission
7. Never return a CRC-failed record as analysis-ready.
8. Preserve the raw wire bytes and structured reason on rejection.
9. Treat a sequence gap as a warning/diagnostic unless policy explicitly requires failure. A canceled dump or host reconnect may legitimately cause a gap.
10. Treat a repeated sequence within one uninterrupted device epoch as suspicious.

### 6.3 Suggested metadata

```json
{
  "ild_version": 8,
  "integrity_algorithm": "crc32-iso-hdlc",
  "dump_sequence": 42,
  "previous_dump_sequence": 41,
  "sequence_gap": 0,
  "firmware_crc32": "7a41cbe2",
  "calculated_crc32": "7a41cbe2",
  "crc_ok": true,
  "integrity_status": "verified"
}
```

Recommended status vocabulary:

- `verified`
- `legacy_unverified`
- `short_record`
- `missing_trailer`
- `bad_trailer_magic`
- `crc_mismatch`
- `sequence_duplicate`
- `sequence_gap`

## 7. Bench-Tool Work

Likely files:

- `openflight_bench/wire.py`
- `openflight_bench/capture.py`
- `openflight_bench/report.py`
- `tests/test_uart_reader.py`
- Parser/capture documentation

Required behavior:

- Mirror OpenFlight's v8 length and CRC handling.
- Print sequence and CRC result for each capture.
- Include all integrity fields in JSON sidecars and session summaries.
- Preserve `.wire.bin` for every CRC or framing failure.
- Keep `.rejected.l3dump` excluded from analysis.
- Report legacy v7 records as structurally accepted but `legacy_unverified`, not CRC-verified.

## 8. Backward Compatibility and Rollout

The trailer approach avoids moving any existing v7 field, but an old reader will calculate the v7 payload length and may leave the 12-byte trailer in its serial buffer. Therefore:

1. Implement and release readers that accept both v7 and v8.
2. Verify the updated readers against existing v1-v7 fixtures.
3. Build the v8 firmware in the same OpenFlight change set as its parser and tests.
4. Deploy the updated host reader before flashing v8 firmware on test hardware.
5. Run controlled hardware validation.
6. Only then update the checked-in release binary, SHA-256, and operator documentation.

The OpenFlight firmware and parser changes should normally land together because they define one contract. The bench-tool support lives in its own repository and should use a separate coordinated branch/PR.

Suggested branches:

- OpenFlight fork: `feature/iwr6843-ild1-integrity`
- Bench tools: `feature/ild1-v8-integrity`

## 9. Test Plan

### 9.1 Firmware CRC unit/golden-vector tests

- Empty byte string
- ASCII `123456789` -> `0xCBF43926`
- Fixed binary vector containing zeroes and `0x0A`
- Incremental chunk updates produce the same CRC as one complete update
- Chunk sizes crossing 256-byte and frame boundaries

### 9.2 Host parser tests

- Valid v8 IQ16 record accepted
- Valid v8 IQ8 record accepted
- Existing v1-v7 fixtures remain accepted under existing rules
- One flipped bit in each region is rejected:
  - Header
  - Temperature
  - Descriptor
  - IQ8 scale
  - Payload
  - Sequence
- One-, 256-, and 1,024-byte deletion rejected by length
- Same-length duplicated 256-byte block rejected by CRC
- Same-length swapped blocks rejected by CRC
- Corrupted CRC field rejected
- Missing or truncated trailer rejected
- False `ILC1` pattern inside payload does not terminate parsing
- Sequence increment accepted
- Sequence gap recorded
- Duplicate sequence recorded/rejected according to policy
- Sequence reset after recognized reboot accepted
- `2^32 - 1` to `0` wrap accepted

### 9.3 Hardware acceptance tests

Using the validated Pi baseline profile:

- RX gain 24 dB
- TX backoff 6 dB
- HPF 0/0
- All TX
- 12 loops
- Bins 0-42
- 10 ms frame period
- 5 post-trigger frames

Run:

1. **Raspberry Pi:** at least 100 consecutive full-size captures.
2. Require 100/100 expected-length records and 100/100 CRC matches.
3. Require `freeze_req == freeze_done`, `freeze_to == 0`, `hwa_rearm_err == 0`, and no regression in `hwa_missed` for this profile.
4. Corrupt copies after reception and prove every modified file fails CRC.
5. **Windows diagnostic:** at least 20 captures using the known CP2105/1,041,667-baud setup. Confirm that every short or corrupted record is rejected and classified; zero bad records may be admitted.
6. Confirm that CRC computation does not materially delay capture restart or change RF/HWA counters.

## 10. Acceptance Criteria

The feature is complete when:

- New firmware emits a documented v8 trailer for IQ16 and IQ8.
- OpenFlight and bench readers independently agree on CRC for golden fixtures and hardware captures.
- Every deliberate corruption test is rejected.
- Existing v1-v7 parser tests remain green.
- No unverified or CRC-failed v8 dump can become analysis-ready.
- Sequence gaps/duplicates and reboot resets are represented correctly in metadata.
- Raspberry Pi hardware testing completes at least 100/100 verified full-size dumps.
- Documentation, release image, and release SHA-256 are updated.

## 11. Optional Follow-up: Frame Provenance

Whole-record CRC proves that the host received the byte sequence calculated by firmware from the frozen record. It does not prove that L3 contains the intended acquisition frame order.

If stronger HWA/EDMA provenance is needed, add a monotonic acquisition sequence for each retained frame:

- Increment on each completed HWA frame.
- Store the value beside each L3 ring slot.
- Emit a `uint32_t frame_sequence[n_frames]` table or expand the timed descriptor in a later contract version.
- Cross-check sequence deltas against `frame_period_us`, measured `delta_us`, and intentional post-trigger stride.

This would identify duplicated slots, stale frames, unexpected gaps, and ordering errors inside the capture pipeline. It should be a separate follow-up unless implementation review shows it can be added without complicating the first v8 release.

## 12. Key Risks and Decisions

| Item | Recommended decision |
|---|---|
| CRC algorithm | CRC-32/ISO-HDLC for standard cross-language support |
| CRC location | Fixed trailer after payload |
| Sequence level | Dump-level in first release; frame-level later |
| CRC implementation | Software initially; do not conflict with mmWaveLink CRC channel 1 |
| Legacy files | Accept as `legacy_unverified` |
| Sequence gaps | Diagnose, do not automatically invalidate an otherwise verified dump |
| CRC failure | Always reject from analysis |
| Release strategy | Parser compatibility first; firmware flash second |

## 13. Handoff Checklist

- [ ] Confirm the next unused ILD1 version on current `main`.
- [ ] Open an issue/design discussion before changing the public binary contract.
- [ ] Confirm exact `UART_writePolling` return semantics in TI mmWave SDK 3.6.2.
- [ ] Implement and validate CRC golden vectors in C and Python.
- [ ] Update OpenFlight firmware and parser together.
- [ ] Add bench-tool support and rejection metadata.
- [ ] Run Pi and Windows hardware acceptance matrices.
- [ ] Update firmware documentation and release SHA-256.
- [ ] Decide separately whether per-frame acquisition sequence data is warranted.
