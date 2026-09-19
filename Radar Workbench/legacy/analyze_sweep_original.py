#!/usr/bin/env python3
"""
analyze_sweep.py -- Turn a directory of capture_benchtesting.py sweep
captures (.l3dump + .json sidecar pairs) into flat, spreadsheet-ready CSVs
of per-element power, phase, and SNR versus the logged ground-truth angle.

SCOPE -- READ BEFORE USING
--------------------------
This does the extraction step only: locate the static target's range bin
in each file, coherently average every chirp/loop/frame (and every repeat
capture at the same logged angle) at that bin, and report per-(TX, RX)
magnitude and phase against the angle you dialed on the gauge. It does
NOT run Bartlett or MUSIC to estimate an angle from the data -- Doug's own
call was to do that afterward with OpenFlight's actual doa.py/music.py
against this per-element output, not to have a second, standalone
reimplementation here that could silently disagree with production on
element ordering or sign convention. This script's raw per-(tx,rx)
complex output (see --complex-npz) is exactly what that later step would
need.

Low-level ILD1 parsing (parse_header, _read_frame_metadata, iter_frames,
range_bin_meters, the SAMPLE_FMT_* constants) is carried over UNCHANGED
from analyze_ild1.py -- that parsing is already validated against real
captures (fan11 etc.); nothing about a sweep changes how the binary format
itself is decoded, only what's done with the decoded complex samples
afterward.

WHY A SEPARATE EXTRACTION FROM analyze_ild1.py
------------------------------------------------
analyze_ild1.py deliberately collapses every chirp and every RX channel
into one averaged power number per bin (np.mean(np.abs(cplx)**2, axis=(0,1)))
-- exactly right for "is there a stable return here, what's the SNR", but
it throws away per-element amplitude AND all phase information before
anything is reported. A sweep aimed at antenna-pattern / AoA-phase-
distortion characterization needs both kept, split out per (TX, RX) pair,
which is what this script does instead.

TX/RX INDEXING ASSUMPTION -- VERIFY AGAINST YOUR ACTUAL .cfg
---------------------------------------------------------------
This assumes the firmware's chirp order within each frame is TX0, TX1,
... TX(n_tx-1), repeating every loop -- true for
iwr6843_benchtesting_static_sweep.cfg's chirpCfg/frameCfg (chirpCfg 0/1/2
map to TX enable bits 1/2/4 respectively, frameCfg cycles chirp index
0->2 each loop). If chirps_per_frame doesn't divide evenly by n_tx, this
script refuses to guess and raises instead of silently mis-assigning
channels. If you ever capture with a DIFFERENT chirp order, re-check this
assumption before trusting the tx/rx columns.

FIXED-WINDOW ASSUMPTION
------------------------
This also assumes every frame in a file shares the same (start_bin,
count) window -- true for captures made with
iwr6843_benchtesting_static_sweep.cfg, where lateStart == postStart so
there is no window-walk. A file whose window moves between frames raises
an error rather than being silently mishandled.

NOT YET VALIDATED
------------------
This has been exercised against a synthetic, hand-built ILD1 buffer with
known injected phase/amplitude per element (to check the reshape/indexing
math is self-consistent), NOT against a real capture_benchtesting.py
output from real hardware, because none exists yet. Bench-test it against
your first real sweep captures and sanity-check the numbers (peak range
lands where you expect, coherence is near 1.0 for a static target) before
trusting a whole sweep session's worth of data.

USAGE
-----
    python3 analyze_sweep.py captures\\bench_sweep1 \\
        --element-csv sweep_elements.csv --summary-csv sweep_summary.csv

Then open sweep_elements.csv (one row per angle x tx x rx) directly in
Excel/Sheets: pivot power_db or phase_deg against angle_deg, filtered/
grouped by tx and rx, for gain-vs-angle and phase-vs-angle curves per
element. sweep_summary.csv (one row per angle) gives a quick combined
roll-off curve across every element, for a first-pass sanity check.

Range-bin-to-meters conversion defaults to OpenFlight's current wide/dense
l3dump chirp profile, same as analyze_ild1.py. Pass --range-bin-m, or the
three --slope/--sample-rate/--num-adc-samples flags, if a different chirp
profile was used.
"""

from __future__ import annotations

import argparse
import csv as csv_module
import json
import struct
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Carried over UNCHANGED from analyze_ild1.py -- same binary format, same
# validated parsing. Do not fork this logic; if it needs a fix, fix it in
# both files (or better, factor it into a shared module) rather than let
# the two drift apart.
# ---------------------------------------------------------------------------

MAGIC = b"ILD1"
HEADER = struct.Struct("<4sHHHBBHBBHH")  # 20 bytes
TEMP_REPORT = struct.Struct("<Ihhhhhhhhhh")  # 24 bytes
TIMED_FRAME_DESCRIPTOR = struct.Struct("<BBH")  # 4 bytes: start_bin, bin_count, delta_us
MAX_SUPPORTED_DUMP_VERSION = 7

SAMPLE_INT16_IQ = 0
SAMPLE_RANGE_FFT_IQ16 = 1
SAMPLE_RANGE_FFT_IQ16_WINDOWED = 2
SAMPLE_RANGE_FFT_IQ16_VARIABLE = 3
SAMPLE_RANGE_FFT_IQ16_VARIABLE_TIMED = 4
SAMPLE_RANGE_FFT_IQ8_VARIABLE_TIMED = 5

SAMPLE_FMT_NAMES = {
    SAMPLE_INT16_IQ: "raw ADC IQ16 (needs range-FFT)",
    SAMPLE_RANGE_FFT_IQ16: "range-FFT IQ16, fixed window",
    SAMPLE_RANGE_FFT_IQ16_WINDOWED: "range-FFT IQ16, per-frame window",
    SAMPLE_RANGE_FFT_IQ16_VARIABLE: "range-FFT IQ16, variable window",
    SAMPLE_RANGE_FFT_IQ16_VARIABLE_TIMED: "range-FFT IQ16, variable window (timed)",
    SAMPLE_RANGE_FFT_IQ8_VARIABLE_TIMED: "range-FFT IQ8, variable window (timed, scaled)",
}

_VARIABLE_SAMPLE_FORMATS = (
    SAMPLE_RANGE_FFT_IQ16_VARIABLE,
    SAMPLE_RANGE_FFT_IQ16_VARIABLE_TIMED,
    SAMPLE_RANGE_FFT_IQ8_VARIABLE_TIMED,
)
_TIMED_SAMPLE_FORMATS = (
    SAMPLE_RANGE_FFT_IQ16_VARIABLE_TIMED,
    SAMPLE_RANGE_FFT_IQ8_VARIABLE_TIMED,
)
_RANGE_SNAPSHOT_FORMATS = (
    SAMPLE_RANGE_FFT_IQ16,
    SAMPLE_RANGE_FFT_IQ16_WINDOWED,
    SAMPLE_RANGE_FFT_IQ16_VARIABLE,
    SAMPLE_RANGE_FFT_IQ16_VARIABLE_TIMED,
    SAMPLE_RANGE_FFT_IQ8_VARIABLE_TIMED,
)

DEFAULT_NUM_ADC_SAMPLES = 128
DEFAULT_SLOPE_MHZ_PER_US = 100.0
DEFAULT_SAMPLE_RATE_KSPS = 4000.0
SPEED_OF_LIGHT_M_S = 299_792_458.0


def range_bin_meters(
    num_adc_samples: int = DEFAULT_NUM_ADC_SAMPLES,
    slope_mhz_per_us: float = DEFAULT_SLOPE_MHZ_PER_US,
    sample_rate_ksps: float = DEFAULT_SAMPLE_RATE_KSPS,
) -> float:
    """Meters per range-FFT bin for a given chirp profile."""
    slope_hz_per_s = slope_mhz_per_us * 1e12
    sample_rate_hz = sample_rate_ksps * 1e3
    adc_window_s = num_adc_samples / sample_rate_hz
    bandwidth_hz = slope_hz_per_s * adc_window_s
    return SPEED_OF_LIGHT_M_S / (2.0 * bandwidth_hz)


class ShortHeader(ValueError):
    """Raised when even the fixed header/metadata can't be read."""


def parse_header(raw: bytes) -> dict:
    """Unpack the fixed ILD1 header and optional temperature extension."""
    if len(raw) < HEADER.size:
        raise ShortHeader(f"file is only {len(raw)} bytes -- smaller than the 20-byte ILD1 header")
    magic, ver, nf, cpf, ntx, nrx, ns, fmt, pad, trig, period_us = HEADER.unpack_from(raw, 0)
    if magic != MAGIC:
        raise ShortHeader(
            f"bad magic {magic!r} (expected {MAGIC!r}) -- this isn't an OpenFlight ILD1 capture"
        )
    if ver > MAX_SUPPORTED_DUMP_VERSION:
        raise ShortHeader(f"unsupported dump version {ver} (this tool understands up to {MAX_SUPPORTED_DUMP_VERSION})")
    if fmt not in SAMPLE_FMT_NAMES:
        raise ShortHeader(f"unsupported sample_fmt {fmt}")

    header_nbytes = HEADER.size
    has_temp = ver == 7 or (ver == 5 and fmt not in _VARIABLE_SAMPLE_FORMATS)
    temperature_report = None
    if has_temp:
        if len(raw) < header_nbytes + TEMP_REPORT.size:
            raise ShortHeader("file is truncated inside the temperature-report header extension")
        temp = TEMP_REPORT.unpack_from(raw, header_nbytes)
        temperature_report = dict(
            zip(
                (
                    "device_time_ms", "rx0_c", "rx1_c", "rx2_c", "rx3_c",
                    "tx0_c", "tx1_c", "tx2_c", "pm_c", "dig0_c", "dig1_c",
                ),
                temp,
                strict=True,
            )
        )
        header_nbytes += TEMP_REPORT.size

    if fmt == SAMPLE_RANGE_FFT_IQ8_VARIABLE_TIMED:
        metadata_nbytes = TIMED_FRAME_DESCRIPTOR.size * nf + 2 * nf
    elif fmt == SAMPLE_RANGE_FFT_IQ16_VARIABLE_TIMED:
        metadata_nbytes = TIMED_FRAME_DESCRIPTOR.size * nf
    elif fmt == SAMPLE_RANGE_FFT_IQ16_VARIABLE:
        metadata_nbytes = 2 * nf
    elif fmt == SAMPLE_RANGE_FFT_IQ16_WINDOWED:
        metadata_nbytes = nf
    else:
        metadata_nbytes = 0

    return dict(
        version=ver, n_frames=nf, chirps_per_frame=cpf, n_tx=ntx, n_rx=nrx,
        n_samples=ns, sample_fmt=fmt, trigger_frame=trig, frame_period_us=period_us,
        header_nbytes=header_nbytes, metadata_nbytes=metadata_nbytes,
        temperature_report=temperature_report,
    )


def _read_frame_metadata(raw: bytes, meta: dict) -> None:
    """Populate per-frame range-window (and IQ8 scale) tables."""
    fmt = meta["sample_fmt"]
    nf = meta["n_frames"]
    start = meta["header_nbytes"]
    stop = start + meta["metadata_nbytes"]
    if len(raw) < stop:
        raise ShortHeader("file is truncated inside the per-frame range-window table (metadata), before any payload")

    if fmt == SAMPLE_RANGE_FFT_IQ16_WINDOWED:
        meta["range_bin_starts"] = tuple(raw[start:stop])
        meta["range_bin_counts"] = (meta["n_samples"],) * nf
    elif fmt == SAMPLE_RANGE_FFT_IQ16_VARIABLE:
        table = raw[start:stop]
        meta["range_bin_starts"] = tuple(table[0::2])
        meta["range_bin_counts"] = tuple(table[1::2])
    elif fmt in _TIMED_SAMPLE_FORMATS:
        desc_stop = start + TIMED_FRAME_DESCRIPTOR.size * nf
        descriptors = list(TIMED_FRAME_DESCRIPTOR.iter_unpack(raw[start:desc_stop]))
        meta["range_bin_starts"] = tuple(d[0] for d in descriptors)
        meta["range_bin_counts"] = tuple(d[1] for d in descriptors)
        deltas_us = tuple(d[2] for d in descriptors)
        elapsed = 0
        offsets = []
        for d in deltas_us:
            elapsed += d
            offsets.append(elapsed)
        meta["frame_time_offsets_us"] = tuple(offsets)
        if fmt == SAMPLE_RANGE_FFT_IQ8_VARIABLE_TIMED:
            scale_start = desc_stop
            scales = np.frombuffer(raw, dtype="<u2", offset=scale_start, count=nf)
            meta["iq8_scales"] = tuple(int(s) for s in scales)
    else:
        meta["range_bin_starts"] = (meta.get("range_bin_start", 0),) * nf
        meta["range_bin_counts"] = (meta["n_samples"],) * nf


def iter_frames(raw: bytes, meta: dict):
    """Yield (frame_index, start_bin, complex_array[cpf, n_rx, count], complete)
    for every frame that can be fully read."""
    cpf, nrx = meta["chirps_per_frame"], meta["n_rx"]
    fmt = meta["sample_fmt"]
    starts = meta["range_bin_starts"]
    counts = meta["range_bin_counts"]
    byte_offset = meta["header_nbytes"] + meta["metadata_nbytes"]
    bytes_per_complex = 2 if fmt == SAMPLE_RANGE_FFT_IQ8_VARIABLE_TIMED else 4

    for i in range(meta["n_frames"]):
        count = counts[i]
        n = cpf * nrx * count
        need_bytes = n * bytes_per_complex
        have_bytes = len(raw) - byte_offset

        if have_bytes <= 0:
            return
        if have_bytes < need_bytes:
            if fmt == SAMPLE_RANGE_FFT_IQ8_VARIABLE_TIMED:
                usable_samples = have_bytes // 2
                raw_i8 = np.frombuffer(raw, dtype=np.int8, offset=byte_offset, count=usable_samples * 2)
                scale = meta["iq8_scales"][i]
                iq = raw_i8.astype(np.float64) * scale
            else:
                usable_samples = have_bytes // 4
                raw_i16 = np.frombuffer(raw, dtype="<i2", offset=byte_offset, count=usable_samples * 2)
                iq = raw_i16.astype(np.float64)
            cplx_flat = np.zeros(n, dtype=complex)
            got = iq[1::2] + 1j * iq[0::2]
            cplx_flat[: got.size] = got
            cplx = cplx_flat.reshape(cpf, nrx, count)
            yield i, starts[i], cplx, False
            return

        if fmt == SAMPLE_RANGE_FFT_IQ8_VARIABLE_TIMED:
            raw_i8 = np.frombuffer(raw, dtype=np.int8, offset=byte_offset, count=2 * n)
            scale = meta["iq8_scales"][i]
            iq = raw_i8.astype(np.float64) * scale
        else:
            raw_i16 = np.frombuffer(raw, dtype="<i2", offset=byte_offset, count=2 * n)
            iq = raw_i16.astype(np.float64)
        cplx = (iq[1::2] + 1j * iq[0::2]).reshape(cpf, nrx, count)
        yield i, starts[i], cplx, True
        byte_offset += need_bytes


# ---------------------------------------------------------------------------
# NEW for this script.
# ---------------------------------------------------------------------------

def load_sweep_files(capture_dir: Path) -> list[dict]:
    """Find every *.json sidecar capture_benchtesting.py wrote in
    capture_dir, pair it with its .l3dump, and return one entry per
    capture. Skips (with a printed warning, not a crash) any sidecar
    missing its .l3dump, or any .json that isn't valid JSON."""
    entries = []
    for json_path in sorted(capture_dir.glob("*.json")):
        try:
            record = json.loads(json_path.read_text())
        except json.JSONDecodeError as exc:
            print(f"  !! Skipping {json_path.name}: not valid JSON ({exc})")
            continue
        l3dump_path = json_path.with_suffix(".l3dump")
        if not l3dump_path.exists():
            print(f"  !! Skipping {json_path.name}: no matching {l3dump_path.name}")
            continue
        if record.get("angle_deg") is None or record.get("axis") is None:
            print(f"  !! Skipping {json_path.name}: sidecar is missing angle_deg/axis")
            continue
        entries.append({
            "angle_deg": float(record["angle_deg"]),
            "axis": str(record["axis"]),
            "repeat_index": record.get("repeat_index"),
            "l3dump_path": l3dump_path,
            "sidecar_path": json_path,
        })
    if not entries:
        raise ValueError(f"no valid (.json + .l3dump) pairs found in {capture_dir}")
    return entries


def group_by_angle(entries: list[dict]) -> dict:
    groups = defaultdict(list)
    for e in entries:
        groups[(e["axis"], e["angle_deg"])].append(e)
    return groups


def combined_power_profile(raw: bytes, meta: dict):
    """Average power over every complete frame's (chirps x rx) at every
    bin -> one profile array for the file's single fixed window, plus its
    start_bin. Raises if the window isn't actually fixed across frames --
    this tool is built around iwr6843_benchtesting_static_sweep.cfg's
    lateStart == postStart (no window-walk); a file that doesn't hold that
    isn't something this extraction logic should guess about."""
    starts = meta["range_bin_starts"]
    counts = meta["range_bin_counts"]
    if len(set(starts)) != 1 or len(set(counts)) != 1:
        raise ValueError(
            "this file's capture window moves between frames (start_bin/count "
            "differ across frames) -- combined_power_profile() assumes a single "
            "fixed window for the whole file. Re-check the .cfg used for this "
            "capture; iwr6843_benchtesting_static_sweep.cfg's captureCfg sets "
            "lateStart == postStart specifically to avoid this."
        )
    start_bin = starts[0]
    count = counts[0]
    power_sum = np.zeros(count)
    n = 0
    for _idx, _start, cplx, complete in iter_frames(raw, meta):
        if not complete:
            continue
        power_sum += np.mean(np.abs(cplx) ** 2, axis=(0, 1))
        n += 1
    if n == 0:
        raise ValueError("no complete frames in this file")
    return start_bin, power_sum / n


def pooled_profile_peak(paths, *, guard_bins: int = 2) -> int:
    """Sum the power profiles of a group of repeat captures (same physical
    target, same fixed capture window -- e.g. every repeat logged at one
    angle) and return ONE consensus peak bin from the pooled result.

    This exists because letting each file pick its own argmax independently
    and only pooling AFTERWARD (the original bug here) can silently combine
    element_mean values from physically different range bins if one repeat's
    own single-file SNR is low enough that noise wins the argmax. Real data
    from this project's own material-testing session hit exactly this: two
    baseline captures picked bin 31 (1.4521 m) and a third independently
    picked bin 39 (1.8269 m) -- averaging all three's element_mean together
    produced a reference that doesn't correspond to any actual physical
    location. Pooling the profiles FIRST and picking one shared peak from
    the combined (higher-SNR) result avoids that failure mode.
    """
    start_bin = None
    pooled = None
    for p in paths:
        raw = Path(p).read_bytes()
        meta = parse_header(raw)
        _read_frame_metadata(raw, meta)
        if meta["sample_fmt"] not in _RANGE_SNAPSHOT_FORMATS:
            raise ValueError(f"{p}: sample_fmt={meta['sample_fmt']} is raw ADC data, not a range snapshot")
        b, profile = combined_power_profile(raw, meta)
        if start_bin is None:
            start_bin, pooled = b, profile.copy()
        elif b != start_bin or profile.shape != pooled.shape:
            raise ValueError(
                f"{p}: capture window (start_bin={b}, {profile.shape[0]} bins) differs from "
                f"the rest of this repeat group (start_bin={start_bin}, {pooled.shape[0]} bins) "
                "-- these captures can't be pooled together; check they all used the same .cfg."
            )
        else:
            pooled += profile
    if pooled is None:
        raise ValueError("no files given to pool")
    return start_bin + int(np.argmax(pooled))


def extract_file_elements(
    path: Path, *, range_bin_m: float, guard_bins: int = 2, forced_peak_bin: int | None = None,
) -> dict:
    """Parse one .l3dump, locate the static target's range bin, and
    coherently average the per-(TX, RX) complex value there across every
    loop and every complete frame in the file. This is the core departure
    from analyze_ild1.py: it keeps amplitude AND phase, split per element,
    instead of collapsing everything into one averaged power number.

    By default the peak bin is chosen independently from this file's own
    profile. Pass forced_peak_bin -- the output of pooled_profile_peak() over
    a whole repeat group -- when this file's result is going to be
    coherently combined with other repeats' via combine_repeats();
    combine_repeats() itself refuses to mix results whose peak_bin disagrees,
    specifically to catch a caller that forgot to do this.
    """
    raw = Path(path).read_bytes()
    meta = parse_header(raw)
    _read_frame_metadata(raw, meta)
    if meta["sample_fmt"] not in _RANGE_SNAPSHOT_FORMATS:
        raise ValueError(f"sample_fmt={meta['sample_fmt']} is raw ADC data, not a range snapshot")

    start_bin, profile = combined_power_profile(raw, meta)
    count = profile.shape[0]
    if forced_peak_bin is None:
        local_peak = int(np.argmax(profile))
    else:
        local_peak = forced_peak_bin - start_bin
        if not (0 <= local_peak < count):
            raise ValueError(
                f"forced_peak_bin={forced_peak_bin} falls outside this file's capture window "
                f"[{start_bin}, {start_bin + count}) -- check every repeat in this group used "
                "the same captureCfg window."
            )
    peak_bin = start_bin + local_peak

    guard_lo = max(0, local_peak - guard_bins)
    guard_hi = min(count, local_peak + guard_bins + 1)
    noise_mask = np.ones(count, dtype=bool)
    noise_mask[guard_lo:guard_hi] = False
    noise_samples = profile[noise_mask] if np.any(noise_mask) else profile
    noise_floor = max(
        float(np.median(noise_samples)) if noise_samples.size else float(np.median(profile)),
        1e-12,
    )
    noise_db = 10.0 * np.log10(noise_floor)

    n_tx = meta["n_tx"]
    n_rx = meta["n_rx"]
    cpf = meta["chirps_per_frame"]
    if cpf % n_tx != 0:
        raise ValueError(
            f"chirps_per_frame={cpf} doesn't divide evenly by n_tx={n_tx} -- can't "
            "resolve which chirp belongs to which TX antenna. This tool assumes the "
            "TDM order is TX0,TX1,...,TX(n_tx-1) repeating every loop, matching "
            "iwr6843_benchtesting_static_sweep.cfg's chirpCfg/frameCfg. Re-check the "
            ".cfg if this file used a different chirp order."
        )
    loops_per_frame = cpf // n_tx

    # One pass over the file: collect every loop's complex sample at the
    # target bin, per (tx, rx), across every complete frame.
    per_loop_samples = []
    n_frames_used = 0
    for _idx, f_start, cplx, complete in iter_frames(raw, meta):
        if not complete or f_start != start_bin:
            continue
        reshaped = cplx.reshape(loops_per_frame, n_tx, n_rx, count)
        for loop in range(loops_per_frame):
            per_loop_samples.append(reshaped[loop, :, :, local_peak])
        n_frames_used += 1

    if not per_loop_samples:
        raise ValueError("no usable frames to average for element extraction")

    samples = np.stack(per_loop_samples, axis=0)  # (total_loops, n_tx, n_rx)
    element_mean = samples.mean(axis=0)           # coherent (complex) average
    mag_mean = np.abs(samples).mean(axis=0)
    # Coherence: |mean(z)| / mean(|z|) -- 1.0 when every averaged sample has
    # identical phase+amplitude, lower with more spread. Cheap sanity check:
    # a static target should sit very close to 1.0 within one file.
    coherence = np.divide(
        np.abs(element_mean), mag_mean,
        out=np.full_like(mag_mean, np.nan), where=mag_mean > 0,
    )

    peak_power = float(np.mean(np.abs(element_mean) ** 2))
    peak_db = 10.0 * np.log10(max(peak_power, 1e-12))
    snr_db = peak_db - noise_db

    return {
        "path": Path(path),
        "peak_bin": peak_bin,
        "peak_range_m": peak_bin * range_bin_m,
        "n_frames_used": n_frames_used,
        "n_loops_total": samples.shape[0],
        "n_tx": n_tx,
        "n_rx": n_rx,
        "element_mean": element_mean,   # complex, shape (n_tx, n_rx)
        "coherence": coherence,         # shape (n_tx, n_rx)
        "noise_db": noise_db,
        "snr_db": snr_db,
    }


def combine_repeats(results: list[dict]) -> dict:
    """Coherently combine several extract_file_elements() results (repeat
    captures logged at the same angle) into one. This averages each
    repeat's already-averaged per-element MEAN with equal weight, and
    reports coherence across those per-file means -- coarser than pooling
    every underlying loop across every repeat, but enough to flag a repeat
    that disagrees with the others (e.g. mount slop between repeats)."""
    n_tx, n_rx = results[0]["n_tx"], results[0]["n_rx"]
    peak_bins = {r["peak_bin"] for r in results}
    if len(peak_bins) > 1:
        raise ValueError(
            f"combine_repeats() was given results from different range bins {sorted(peak_bins)} -- "
            "these can't be coherently averaged together (this is exactly the bug pooled_profile_peak() "
            "exists to prevent). Extract every repeat with the same forced_peak_bin before combining."
        )
    stacked = np.stack([r["element_mean"] for r in results], axis=0)  # (n_repeats, n_tx, n_rx)
    combined_mean = stacked.mean(axis=0)
    mag_mean = np.abs(stacked).mean(axis=0)
    coherence = np.divide(
        np.abs(combined_mean), mag_mean,
        out=np.full_like(mag_mean, np.nan), where=mag_mean > 0,
    )
    return {
        "n_repeats": len(results),
        "n_tx": n_tx,
        "n_rx": n_rx,
        "peak_range_m": float(np.mean([r["peak_range_m"] for r in results])),
        "snr_db": float(np.mean([r["snr_db"] for r in results])),
        "element_mean": combined_mean,
        "coherence_across_repeats": coherence,
        "n_loops_total": sum(r["n_loops_total"] for r in results),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("capture_dir", type=Path,
                     help="Directory of .l3dump + .json pairs written by capture_benchtesting.py")
    ap.add_argument("--range-bin-m", type=float, default=None, help="Override meters-per-bin directly")
    ap.add_argument("--num-adc-samples", type=int, default=DEFAULT_NUM_ADC_SAMPLES)
    ap.add_argument("--slope-mhz-per-us", type=float, default=DEFAULT_SLOPE_MHZ_PER_US)
    ap.add_argument("--sample-rate-ksps", type=float, default=DEFAULT_SAMPLE_RATE_KSPS)
    ap.add_argument("--guard-bins", type=int, default=2,
                     help="Bins excluded around the peak when estimating the noise floor")
    ap.add_argument("--reference-tx", type=int, default=0,
                     help="TX index used as the phase-0 reference for phase_rel_ref_deg")
    ap.add_argument("--reference-rx", type=int, default=0,
                     help="RX index used as the phase-0 reference for phase_rel_ref_deg")
    ap.add_argument("--element-csv", type=Path, default=Path("capture_csv/sweep_elements.csv"),
                     help="One row per (angle, tx, rx)")
    ap.add_argument("--summary-csv", type=Path, default=Path("capture_csv/sweep_summary.csv"),
                     help="One row per angle, averaged across every element")
    args = ap.parse_args()

    range_bin_m = args.range_bin_m or range_bin_meters(
        args.num_adc_samples, args.slope_mhz_per_us, args.sample_rate_ksps
    )

    entries = load_sweep_files(args.capture_dir)
    groups = group_by_angle(entries)

    element_rows = []
    summary_rows = []

    for (axis, angle_deg), members in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        paths = [m["l3dump_path"] for m in members]
        try:
            consensus_bin = pooled_profile_peak(paths, guard_bins=args.guard_bins)
        except ValueError as exc:
            print(f"  !! axis={axis} angle={angle_deg}: {exc} -- skipped")
            continue

        per_file_results = []
        for m in members:
            try:
                r = extract_file_elements(
                    m["l3dump_path"], range_bin_m=range_bin_m, guard_bins=args.guard_bins,
                    forced_peak_bin=consensus_bin,
                )
            except (ShortHeader, ValueError) as exc:
                print(f"  !! {m['l3dump_path'].name}: {exc}")
                continue
            per_file_results.append(r)

        if not per_file_results:
            print(f"  !! axis={axis} angle={angle_deg}: no usable captures -- skipped")
            continue

        combined = combine_repeats(per_file_results)
        n_tx, n_rx = combined["n_tx"], combined["n_rx"]

        if not (0 <= args.reference_tx < n_tx and 0 <= args.reference_rx < n_rx):
            print(f"  !! --reference-tx/--reference-rx ({args.reference_tx},{args.reference_rx}) "
                  f"out of range for this file's {n_tx}TX x {n_rx}RX -- using (0, 0) instead")
            ref_tx, ref_rx = 0, 0
        else:
            ref_tx, ref_rx = args.reference_tx, args.reference_rx
        ref_phase_deg = float(np.degrees(np.angle(combined["element_mean"][ref_tx, ref_rx])))

        row_power_dbs = []
        for tx in range(n_tx):
            for rx in range(n_rx):
                z = combined["element_mean"][tx, rx]
                power_db = 10.0 * np.log10(max(abs(z) ** 2, 1e-12))
                phase_deg = float(np.degrees(np.angle(z)))
                phase_rel_deg = ((phase_deg - ref_phase_deg + 180.0) % 360.0) - 180.0
                within_file_coh = float(np.nanmean([r["coherence"][tx, rx] for r in per_file_results]))
                row_power_dbs.append(power_db)
                element_rows.append({
                    "axis": axis,
                    "angle_deg": angle_deg,
                    "tx": tx,
                    "rx": rx,
                    "range_m": round(combined["peak_range_m"], 4),
                    "power_db": round(power_db, 2),
                    "phase_deg": round(phase_deg, 2),
                    "phase_rel_ref_deg": round(phase_rel_deg, 2),
                    "snr_db": round(combined["snr_db"], 2),
                    "n_repeats": combined["n_repeats"],
                    "n_loops_total": combined["n_loops_total"],
                    "coherence_within_file": round(within_file_coh, 4),
                    "coherence_across_repeats": round(float(combined["coherence_across_repeats"][tx, rx]), 4),
                })

        summary_rows.append({
            "axis": axis,
            "angle_deg": angle_deg,
            "range_m": round(combined["peak_range_m"], 4),
            "power_db_mean_all_elements": round(float(np.mean(row_power_dbs)), 2),
            "snr_db": round(combined["snr_db"], 2),
            "n_repeats": combined["n_repeats"],
        })
        print(f"  axis={axis:9s} angle={angle_deg:+7.2f} deg  "
              f"range={combined['peak_range_m']:.3f} m  SNR={combined['snr_db']:.1f} dB  "
              f"({combined['n_repeats']} repeat(s), {combined['n_loops_total']} loops total)")

    if not element_rows:
        print("No data to write.")
        return 1

    args.element_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(args.element_csv, "w", newline="", encoding="utf-8") as fh:
        writer = csv_module.DictWriter(fh, fieldnames=list(element_rows[0].keys()))
        writer.writeheader()
        writer.writerows(element_rows)
    print(f"\nWrote {args.element_csv} ({len(element_rows)} rows)")

    args.summary_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(args.summary_csv, "w", newline="", encoding="utf-8") as fh:
        writer = csv_module.DictWriter(fh, fieldnames=list(summary_rows[0].keys()))
        writer.writeheader()
        writer.writerows(summary_rows)
    print(f"Wrote {args.summary_csv} ({len(summary_rows)} rows)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
