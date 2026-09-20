# OpenFlight bench audit: findings from the five raw dumps

Reviewed September 12, 2026. This follows the earlier bench-tool audit and adds direct evidence from the five September 10 couch captures, the uploaded firmware/build files, and the proposed saturation monitor.

**The immediate blocker is capture integrity. Three dumps contain internal byte loss that moves samples between range bins and RX channels. The other two have consistent alignment and no IQ16 rail hits. These five files do not establish TX-on analog linearity or validate RX24/TX6.**

The supplied `l3_dump.c` and `dump_format.h` match the previously retrieved OpenFlight sources after newline normalization. Independently decoding the raw payload as little-endian `(int16 Im, int16 Re)`, in frame/chirp/RX/bin order, reproduces the original parser's sample values exactly. The fundamental byte order is correct. Acceptance of damaged captures is the problem.

**1. Three files must be excluded in their entirety.**

Every header declares version 7, format 4, 25 frames, 36 chirps per frame, 4 RX, 3 TDM TX slots, and 53 bins beginning at bin 0. The descriptors show 3,000 µs spacing. The byte budget is:

- Fixed header: 20 bytes.
- Temperature extension: 24 bytes.
- 25 frame descriptors: 100 bytes.
- Payload: 25 × 36 × 4 × 53 × 4 = 763,200 bytes.
- Expected file length: **763,344 bytes**.

The firmware retains one pre-trigger frame plus 24 post-trigger frames. The CFG comments count only the 24 post-trigger frames; that discrepancy does not explain these short files.

| Filename suffix | Actual bytes | Missing bytes | Result |
|---|---:|---:|---|
| `200109_r1_g0001` | 762,320 | 1,024 | Reject: internal sample-alignment changes |
| `200123_r2_g0002` | 761,040 | 2,304 | Reject: internal sample-alignment changes |
| `200136_r3_g0003` | 762,320 | 1,024 | Reject: internal sample-alignment changes |
| `200150_r4_g0004` | 763,344 | 0 | Exact length; consistent alignment |
| `200158_r5_g0005` | 763,344 | 0 | Exact length; consistent alignment |

All filenames begin `raw_couch_rxgain_sweep_1_20260910_` and end `.l3dump`.

To distinguish internal deletion from simple tail truncation, I compared each four-chirp magnitude block with all circular shifts of r4's average RX-major profile. One nominal chirp spans 4 × 53 = 212 complex samples. The early portions of all files align at shift zero. The damaged files then change alignment and stay displaced. Their final shifts agree exactly with the total missing byte counts:

| File | Final circular shift, complex samples | Predicted shift from missing bytes |
|---|---:|---:|
| r1 | 168 | −(1,024 / 4) modulo 212 = 168 |
| r2 | 60 | −(2,304 / 4) modulo 212 = 60 |
| r3 | 168 | −(1,024 / 4) modulo 212 = 168 |
| r4, r5 | 0 | 0 |

The final four-chirp blocks correlate approximately 0.991–0.992 with the displaced reference. The first strongly matched nonzero shifts appear near nominal frame positions 6.61, 2.28, and 3.28 respectively, using zero-based indexing. These are diagnostic block positions, not exact loss timestamps. Intermediate stable shifts are also compatible with losses in multiples of 256 bytes.

This is strong evidence of internal deletion, and explains artificial peaks in naive range profiles. It does **not** identify whether the loss originates in firmware UART output, the USB/serial path, a driver, or host capture handling. There is no end-to-end checksum to localize it. I did not reconstruct missing samples or accept realigned data for metrology.

*The original sample-alignment figure was not included in the repository.*

`capture_benchtesting.py` describes a short payload as an otherwise complete capture with a short last frame. That statement is contradicted by these bytes. `analyze_material.py` then calls the first 24 nominal frames complete, even when many have already shifted. **Reject the entire file whenever actual and declared lengths differ.** An exact length remains a necessary check, not a checksum-backed guarantee.

**2. What r4 and r5 actually establish.**

The two aligned captures have zero components equal to either signed IQ16 rail, −32,768 or +32,767. Maximum absolute components are 3,416 and 3,280 respectively, equivalent to approximately 19.64 and 19.99 dB of stored-component headroom. These numbers apply to the retained range-FFT output, not ADC headroom or usable target-to-noise dynamic range.

Their uncalibrated bin-0 mean-magnitude levels differ by only 0.12 dB; their median levels across bins 20–52 differ by 0.015 dB. Here the diagnostic estimator is `20 log10(mean(abs(I+jQ)))`, averaged across nominal chirps, RX channels, and complete frames. It is neither calibrated power nor a noise-floor measurement. The retained TX-off spectrum is structured, so a median of it should not automatically be called receiver noise.

All five sidecars name `iwr6843_satmon_notx.cfg`; that uploaded file disables every chirp's TX mask. This establishes the recorded test intent. It is not RF readback. No TX-on raw file or working ADC/CQ monitor result was supplied in this batch.

r4 also has a failed/missing stats transaction: its raw stats field contains only `Done` and prompts. Treat its RF-health fields as unknown, rather than zero. The other sidecars report no RF faults or HWA rearm errors, but these counters are not sample-integrity or analog-saturation checks. `iq8_clipped=0` is irrelevant to an IQ16 capture.

**3. Manual CFG edits need to be separated from settings actually sent.**

The capture program calls `send_config_file()` once during startup. The repeat loop sends `l3dump` and queries `stats`; it does not reread the CFG. Saving an RX-gain change in VS Code while the loop runs leaves the radar's applied profile unchanged. Restarting the program and successfully resending the configuration between groups addresses that issue.

The filenames and repeat indices indicate these five captures are one automatic repeat group. Available freeze counters progress from 1 to 5, consistent with that reading. They should not be assigned five different RX gains without another record of reconfiguration.

The currently uploaded TX-on satmon CFG ends its `profileCfg` in RX28; the TX-off CFG ends in RX30. Both have a zero TX-backoff word. Given your manual edits, those are current file contents, not historical proof of each capture's gain. None of the sidecars archives the sent CFG contents, a hash, or parsed RF settings.

For today's runs, use a separate named CFG per setting and a separate capture directory per setting. Archive the exact text sent at configuration time and its CLI responses; hashing the current file later is insufficient. Keep a known-supported RX30 comparison when investigating RX24, because the earlier audit found conflicting support information for the lower gain code.

**4. The supplied saturation monitor is not integrated into the supplied build.**

The makefile lists only `l3_dump.c` as a source. That C file has no satmon include, initialization, configuration, sampling, stats, or `cqdump` hooks. None of these sidecars contains `hwa_clip`, `clip_reads`, `cq_max`, or `cq_cfg`. A CFG named `satmon` does not activate that C module. These files describe a proposed addition; they do not demonstrate it ran on the flashed device.

There are substantive review items before relying on the addition:

- **CQ header handling:** TI's published `rlRfRxSaturationCqData_t` puts a `numSlices` byte before `satCqVal[]`. The proposed loop starts with `cq[0]`, treating every byte as a count. With that documented layout and 97 slices, it would count the header value 97 as saturation and omit the last count. Confirm the exact SDK 3.6.2 CQ packing and active ping/pong buffer, then decode and validate the header explicitly. The reference inspected is TI-authored SDK documentation hosted in an archive, not the installed 3.6.2 headers. [TI saturation CQ structure, archived SDK documentation](https://astroa.net/fmcw-RADAR/mmwave_sdk/packages/ti/control/mmwavelink/docs/doxygen/html/structrl_rf_rx_saturation_cq_data__t.html).
- **Coverage:** sampling the latest CQ buffer once per frame does not observe all 36 chirps. With three alternating TXs, static coupling can still be TX-dependent. A zero reading cannot clear unobserved chirps. The monitor also combines ADC and IFA1 events; nonzero counts do not isolate the converter alone.
- **Failure handling:** `gCqSatBufAddr` becomes nonzero before the RF monitor configuration succeeds. A later configuration failure leaves the sampling path enabled against that address. Publish monitor results only when configuration succeeded and valid data was observed.
- **Disable flag:** `-UL3_SATMON_CQ` does not disable CQ, because the header defines it as 1 whenever it is undefined. Set `L3_SATMON_CQ=0` explicitly for an HWA-only build.
- **Profile guard:** the slice-duration test compares compiled constants, not the applied RF profile. It cannot detect a changed ADC sample count or sample rate. Its integer arithmetic also truncates 31.36 µs to 31 µs.
- **Integration claims:** enabling only bit 24 through `rlRfAnaMonConfig()` replaces the requested analog-monitor mask; it is not inherently additive to other monitors. The claimed execution time and timing neutrality have not been measured here.

I have not compiled or hardware-qualified this proposed monitor. The above code findings do not depend on assuming it was already flashed.

**5. HWA output formatting is a separate saturation boundary.**

For the IQ16 path, `l3_configHwaProcessParam()` sets `srcScale=0`, `dstScale=0`, and `butterflyScaling=0x7F` for the 128-point FFT. TI documents an eight-bit input left shift, seven divide-by-two butterfly stages, and output saturation when converting back to 16 bits. Thus an ideal bin-centered input tone of amplitude A produces approximately 256A before output clipping. A=200 input counts would demand 51,200 output counts. This is a scaling calculation, not a measurement of these captures.

The FFT clip register covers butterfly-stage clipping. A zero value does not certify the 16-bit output conversion. [TI HWA guide, §§1.3.1.3, 1.4.1.3 and Table 6](https://www.ti.com/lit/pdf/swru526).

A controlled firmware experiment would increase the IQ16 destination shift by one bit, hold the RF settings fixed, and compare a stable scene. Unsaturated output should fall about 6.02 dB. Record the shift and use a fresh baseline; changing numerical scale cannot recover clipping upstream. Count output rails as well as checking the applicable hardware monitors.

**6. A practical order for today's bench session.**

1. Keep HPF settings fixed at 0/0. Establish repeatable, exact-length transfers first. Apply the included checker before analysis. Capture ten identical repeats as a practical initial gate; ten passes are not proof of a zero error rate. If losses recur, isolate transfer behavior with a shorter capture and a simple buffered reader before changing RF settings. Changing UART baud requires a matching device-side change.
2. Explicitly apply and archive one profile. If using your intended RX24/TX6 candidate, the standard packed backoff word for all three transmitters is decimal **394758**, hexadecimal **0x060606**, rather than decimal 6. The uploaded files do not show that setting. This encoding was verified in the earlier audit against [TI's backoff example](https://e2e.ti.com/support/sensors-group/sensors/f/sensors-forum/1012970/iwr6843-question-about-the-total-transmit-power-when-beamforming).
3. Capture matched TX-off and TX-on empty-scene data, then a fixed reflector. Check retained IQ16 rails and whether the reflector return follows the commanded gain/backoff changes. Do not interpret TX-off headroom as a pass for the TX-on condition.
4. Resolve HWA output scaling and validate any saturation instrumentation before treating a strong return as linear. Distinguish butterfly clipping, output-format clipping, and analog saturation in the results.
5. Only then compare no-window / window / no-window measurements with fixed geometry and identical settings. Use power averaging for echo-loss comparisons; assess relative channel phase separately. Establish baseline repeatability before interpreting changes as window-induced angle error. Keep the rotated board's physical-axis convention explicit and use a same-angle baseline for each angle.

The existing coherent-averaging, off-axis gating, angle-error reporting, and old-calibration findings from the earlier audit still apply. This follow-up identifies an additional concrete reason why some previous bench plots were misleading; it does not invalidate every earlier session.

**Checker and reproduction notes.**

`check_l3dump.py` needs only standard Python. It checks v6/v7 timed IQ16/IQ8 metadata and exact expected length, without touching the input files:

```text
python check_l3dump.py path/to/capture_folder
```

`LENGTH_OK` is intentionally a limited result. Any rejected file makes the process exit with code 1. Unsupported formats are rejected for manual review. The checker does not automatically intercept the existing analyzers; use only accepted files as analysis inputs.

Verified against all five supplied binaries, plus synthetic tail truncation, internal deletion, extra trailer, truncated header/metadata, zero-width descriptor, and variable-width IQ8 captures with v6/v7 headers. Independent decoding matched the existing parser sample-for-sample. The figure was generated with NumPy/Matplotlib and visually checked. `make_raw_audit.py` and `raw_dump_audit_results.json` reproduce and record the numeric evidence; the alignment diagnostic is specific to this stationary fixed-window dataset and must not be used as a generic data-repair procedure.
