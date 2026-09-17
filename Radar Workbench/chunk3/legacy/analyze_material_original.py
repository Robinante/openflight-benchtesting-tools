#!/usr/bin/env python3
"""
analyze_material.py -- Turn a directory of capture_benchtesting.py
Material Testing captures (.l3dump + .json sidecar pairs, test_type
"material") into spreadsheet-ready CSVs comparing each swatch against a
pooled baseline: attenuation (power delta) and phase shift per element,
plus a baseline-drift timeline.

SCOPE
-----
This answers the two things bench material testing actually needs: how
much did this material attenuate the signal (delta_power_db, negative =
loss), and did it introduce any phase shift (phase_shift_vs_baseline_deg),
both per (TX, RX) element and pooled into a summary row per swatch.

This is deliberately NOT the same comparison as analyze_sweep.py's
phase_rel_ref_deg. That column compares one element's phase against
ANOTHER element in the SAME capture (useful for AoA questions). Material
distortion is a different question -- did THIS element's phase change
when the material went in front of it, compared to nothing in front of
it -- which needs a BASELINE capture as the reference, not another
channel. That is what this script computes instead.

Because you may (as intended) shoot baseline reference captures
interleaved between material groups rather than just once at the start,
every baseline capture in the directory is pooled -- per angle, see below
-- into a combined reference (more baseline data -> a better reference),
and ALSO reported individually, sorted by timestamp, in
baseline_timeline.csv so you can visually check whether the baseline
itself drifted over the session (temperature, mechanical settling, etc.)
before trusting the material comparisons that rely on it.

OFF-AXIS TESTING
----------------
Most material shots are at boresight (0 az, 0 el), where one pooled
baseline covers everything. But an off-axis material shot (e.g. checking
whether a swatch's attenuation/phase-shift changes 20 deg off axis, the
way TI's own radome testing checks "off-axis distortion" as a material
property) can NOT be validly compared against the boresight baseline --
the antenna itself already behaves differently off axis, baseline or not,
and that antenna-only effect would get misattributed to the material.

So baseline and material captures are each grouped by (axis, angle_deg)
first (read from the sidecar's angle_deg/axis fields -- see
capture_benchtesting.py's run_material_mode()), and a material group is
only compared against the baseline group at that SAME angle. A swatch
shot off axis with no baseline taken at that same angle is reported as
skipped, with a warning telling you to go take that baseline, rather than
silently falling back to the boresight baseline and quietly producing a
wrong number. Old sidecars with no angle_deg/axis fields default to
boresight, so existing boresight-only sessions load and pool exactly as
before.

Low-level ILD1 parsing and the per-(TX,RX) coherent-averaging extraction
(parse_header, iter_frames, extract_file_elements, combine_repeats) are
the same logic as analyze_sweep.py -- copied here rather than imported so
this stays a standalone file with no dependency between the two analysis
scripts, at the cost of the usual duplication risk: a fix to the parsing
logic needs to be made in both files (or factored into a shared module)
to avoid the two drifting apart.

ANGLE-OF-ARRIVAL GATING (default, since 2026-09-07)
----------------------------------------------------
Bin selection (which range bin is "the trihedral") now defaults to
estimate_target_bin_by_angle() instead of plain pooled-power argmax: it
shortlists the top --angle-gate-top-k highest-power bins in a group's
pooled profile, computes calibrated elevation + coarse axis + bias-
corrected range PER REPEAT at each candidate, and only accepts a
candidate whose median angles sit near boresight AND stay tightly
clustered across independent repeats (a real static point target does;
board-level leakage, a tripod/stand, or environmental clutter does not)
AND, if --expected-range-m is given, whose range matches the tape
measurement. This directly targets the failure mode plain argmax has no
defense against: a session where clutter is louder than the trihedral,
which silently produces SNR/phase numbers for the wrong physical thing.

Pass --calibration <path to iwr6843_calibration_reference.json> for a
calibrated elevation estimate (recommended -- an uncalibrated array still
runs but is noisier) and --expected-range-m <tape-measured distance,
radar face to trihedral apex> to also gate on range (strongly
recommended -- without it, a same-axis reflector at the wrong range can
still pass). --el-max-deg/--axis-max-deg/--range-tol-m/--spread-max-deg
tune how strict the gate is if the defaults are wrong for your geometry.
--no-angle-gate disables all of this and restores the old plain-argmax
behavior, kept only for comparison/debugging.

If a baseline round turns out not to be a true, settled baseline (shot
while still finishing setup, say) but another round at the same angle is
clean, --exclude-baseline <filename substring> (repeatable) drops it from
POOLING only -- it stays on disk, untouched, and still shows up in
--baseline-timeline-csv (flagged excluded_from_pooling=True) for the
record. This is the mechanism for "don't compare against it, but don't
delete/move it either."

Hardware-validated 2026-09-06/07: also caught and fixed a real bug this
same evening -- the swatch-vs-baseline own-bin sanity check (which only
ever exists to print a mismatch warning; extraction always uses the
baseline's bin regardless) used to propagate a ValueError from a failed
angle-gate straight into skipping the ENTIRE swatch. A lossy material
sample routinely drops a capture's own independent SNR too low for ITS
OWN angle-gate to confidently re-derive the trihedral's bin on its own --
that's an expected, non-fatal side effect of attenuation, not evidence
the swatch's data is bad, so it must never skip extraction. Fixed to
degrade to an informational note instead.

SNR SCOPE (fixed 2026-09-07): snr_db used to compare the chosen peak
against a noise floor built from every OTHER bin in the whole capture
window (minus a small guard band) -- which quietly treated every other
real, static, competing return (a tripod/stand, near-field leakage,
sidelobe smear) as "noise" too, inflating the floor and understating the
trihedral's true SNR. extract_file_elements() now also excludes every bin
the angle-gate itself flagged as a candidate (extra_noise_exclude_bins,
threaded through from estimate_target_bin_by_angle(return_candidates=
True) via select_consensus_bin()), for both the baseline pool AND every
swatch compared against it, using the SAME clutter-bin set established
once from the baseline's own scan -- not a per-swatch-redefined one --
so "what counts as noise here" stays fixed for a whole baseline-vs-swatch
comparison. This is comparing the trihedral bin's own SNR, not the whole
window's; delta_power_db/phase_shift_vs_baseline were never affected
either way (both already compared baseline vs. material at the one same
physical bin).

Bench-tested against a real 2026-09-06 session: the fix moved this
session's pooled baseline SNR from -6.8 dB to -5.2 dB -- real, but
modest. Don't expect this fix alone to turn a low SNR into a high one:
if the window's elevated floor comes from a broad sidelobe skirt (this
firmware's rectangular, unwindowed range-FFT rolls off slowly) rather
than from a handful of isolated competing returns, most of that floor
sits on bins the top-k_bins candidate list never flags as loud enough to
exclude. A persistently low snr_db after this fix is telling you
something real about this bench window's noise floor relative to the
trihedral's return strength -- not a sign the metric is still broken.

Hardware-validated 2026-09-06/07 against real bench captures (garage and
driveway, 60in trihedral range, 4 independent repeat groups: c8, c9, c10,
c11): correctly rejected every clutter candidate -- near-field leakage at
bin ~0-2, and a real, static, but wrong-range return at bins 14-15 (very
likely the trihedral's own tripod/stand) -- in all four groups. The true
trihedral return itself showed up smeared across two adjacent bins
(~1.57 m and ~1.62 m raw, i.e. ~1.51-1.55 m once the calibration's range
bias is subtracted -- within 2-3 cm of the 1.524 m/60in tape measurement
either way), consistent with this firmware's disabled range-FFT window
spreading a strong return's energy into its neighbor. Bin 35 passed with
a very tight, highly repeatable elevation (+1.0 to +1.8 deg, <1 deg
spread) in 3 of 4 groups (c8, c9, c11); bin 36 usually had marginally
more raw power but a wider, less consistent axis reading, and won the
highest-passing-power tiebreak in 1 of 4 groups (c10) by a fraction of a
dB. Net effect either way: the gate correctly narrows a 0-2 m window down
to a 1-bin-wide neighborhood around the real target and never once
selected a clutter bin, which plain argmax cannot promise. If your own
sessions show this same adjacent-bin coin-flip and it matters for your
comparison, tightening --axis-max-deg/--spread-max-deg a bit further
should resolve it in favor of the more repeatable bin.

Also run end-to-end through this script's own main() CLI (not just the
standalone angle_at_bin()/estimate_target_bin_by_angle() functions) against
a real (older, non-trihedral) material_baseline session: --calibration
loads correctly, the baseline-pooling call site angle-gates and re-
extracts at the consensus bin exactly like the pre-existing pooled_profile
_peak() call it replaced, --no-angle-gate correctly restores the old
behavior, and CSVs write out with no material captures present. That
older session had no known tape-measured range on file, so it was run
without --expected-range-m -- always pass --expected-range-m for a real
material-testing session; without it the range criterion is skipped
entirely and angle-gating degrades to "highest-power boresight-ish bin,"
which is safer than plain argmax but not as strong a guarantee.

NOT YET VALIDATED
------------------
Verified with a synthetic ILD1 buffer (known injected per-element values,
a fake baseline vs. a fake "material" with a deliberately injected
power/phase offset) to confirm the delta-vs-baseline math is correct.
NOT yet run against a real capture_benchtesting.py session, because none
exists yet. Bench-test it against your first real material session and
sanity-check that a "material" capture identical in every respect to
baseline (e.g. an accidental duplicate baseline logged as "material")
reports ~0 dB delta and ~0 deg phase shift, before trusting a full
session's comparisons.

USAGE
-----
    python3 analyze_material.py captures\\material_session1 \\
        --calibration iwr6843_calibration_reference.json \\
        --expected-range-m 1.524 \\
        --element-csv material_elements.csv \\
        --summary-csv material_summary.csv \\
        --baseline-timeline-csv baseline_timeline.csv

    # old plain-argmax behavior, for comparison only:
    python3 analyze_material.py captures\\material_session1 --no-angle-gate
"""

from __future__ import annotations

from analyzer_validation import (
    ShortHeader, DataValidationError, parse_header, _read_frame_metadata, iter_frames,
    read_capture_bytes, load_entries, require_coherence, add_validation_arguments,
    prepare_analysis, run_checked,
)
import argparse
import csv as csv_module
import json
import struct
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Carried over from analyze_ild1.py / analyze_sweep.py -- same binary
# format, same validated parsing. See analyze_sweep.py's header comment
# for why this is duplicated rather than imported.
# ---------------------------------------------------------------------------

MAGIC = b"ILD1"
HEADER = struct.Struct("<4sHHHBBHBBHH")
TEMP_REPORT = struct.Struct("<Ihhhhhhhhhh")
TIMED_FRAME_DESCRIPTOR = struct.Struct("<BBH")
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
    slope_hz_per_s = slope_mhz_per_us * 1e12
    sample_rate_hz = sample_rate_ksps * 1e3
    adc_window_s = num_adc_samples / sample_rate_hz
    bandwidth_hz = slope_hz_per_s * adc_window_s
    return SPEED_OF_LIGHT_M_S / (2.0 * bandwidth_hz)










def combined_power_profile(raw: bytes, meta: dict):
    starts = meta["range_bin_starts"]
    counts = meta["range_bin_counts"]
    if len(set(starts)) != 1 or len(set(counts)) != 1:
        raise ValueError(
            "this file's capture window moves between frames -- this tool assumes a "
            "single fixed window for the whole file, as produced by "
            "iwr6843_benchtesting_static_sweep.cfg's captureCfg (lateStart == postStart)."
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
    target, same fixed capture window -- e.g. every repeat of one swatch, or
    every baseline at one angle) and return ONE consensus peak bin from the
    pooled result.

    This exists because letting each file pick its own argmax independently
    and only pooling AFTERWARD (the original bug here) can silently combine
    element_mean values from physically different range bins if one repeat's
    own single-file SNR is low enough that noise wins the argmax. Real data
    from this project's own baseline session hit exactly this: two captures
    picked bin 31 (1.4521 m) and a third independently picked bin 39
    (1.8269 m) -- averaging all three's element_mean together produced a
    baseline that doesn't correspond to any actual physical location, which
    is why every material's phase_shift_vs_baseline came out near the +/-180
    wrap boundary. Pooling the profiles FIRST and picking one shared peak
    from the combined (higher-SNR) result avoids that failure mode.
    """
    start_bin = None
    pooled = None
    for p in paths:
        raw = read_capture_bytes(p)
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


# ---------------------------------------------------------------------------
# NEW: angle-of-arrival gating -- filter "the trihedral" out of clutter.
#
# pooled_profile_peak() above picks the single highest-power bin in the
# pooled profile. That's fine only when the trihedral dominates its whole
# window, which real bench sessions (06-07sep26) showed is NOT reliable: a
# bare-board setup picks up other static returns in the same 0-2m window
# (TX/RX near-field coupling near bin 0, a mount/stand a few bins in,
# driveway/garage clutter) that can be just as strong as -- or stronger
# than -- the trihedral itself, especially once FFT sidelobe smearing
# (this firmware runs its range-FFT with windowEn disabled) spreads a
# strong return's energy into several neighboring bins.
#
# The trihedral is the only thing in the window guaranteed to sit at (or
# near) boresight -- 0 deg azimuth, 0 deg elevation -- AND at the known
# tape-measured range. Everything else -- board-level leakage, a tripod
# stand, a car, a garage wall -- fails at least one of those. This section
# adds an angle-of-arrival check, matching OpenFlight's own production DOA
# math (openflight/src/openflight/iwr6843/{doa,music,calibration}.py), so
# estimate_target_bin_by_angle() can gate candidate bins on az/el/range
# instead of blind argmax, and reject anything that doesn't look like a
# real point target sitting where the trihedral actually is.
#
# Hardware-validated 2026-09-06/07 against real bench captures: a bare
# tripod/stand at ~0.7 m repeatably showed near-zero elevation but a
# clearly off-axis coarse azimuth reading and the wrong range; the actual
# trihedral (60in captures, garage and driveway both) repeatably showed
# tight (roughly 1-2 deg spread across independent repeats) elevation AND
# axis readings within a few degrees of 0, at a range matching the tape
# measurement once the calibration's own range bias is applied.
# ---------------------------------------------------------------------------

F_C_HZ = 62.0e9  # 60-64 GHz chirp center (IWR6843) -- matches openflight's music.py
LAM_M = SPEED_OF_LIGHT_M_S / F_C_HZ  # ~4.84 mm
_ANGLE_GRID_RAD = np.radians(np.arange(-40.0, 40.001, 0.1))


class Calibration:
    """Minimal standalone re-implementation of openflight's
    iwr6843/calibration.py -- just the element phase/gain correction and
    range bias, loaded from the same JSON format. Deliberately NOT
    importing the real openflight package (this script stays dependency-
    free, see module docstring) and deliberately NOT applying
    tilt_deg/radar_height_m -- those correct to ground-referenced launch
    angle for the INSTALLED enclosure mount, which a bare-board bench rig
    isn't in. Bench angle checks want boresight-relative angles instead.
    """

    def __init__(self, elem_correction: np.ndarray, range_bias_m: float, source: str):
        self.elem_correction = elem_correction  # complex, len 8
        self.range_bias_m = range_bias_m
        self.source = source

    @classmethod
    def load(cls, path) -> "Calibration":
        raw = json.loads(Path(path).read_text())
        corr = np.exp(-1j * np.asarray(raw["elem_phase_rad"])) / np.asarray(raw["elem_gain"])
        return cls(corr, float(raw.get("range_bias_const_m", 0.0)), str(path))

    @classmethod
    def identity(cls) -> "Calibration":
        return cls(np.ones(8, dtype=complex), 0.0, "identity (uncalibrated)")

    def true_range_m(self, measured_m: float) -> float:
        return measured_m - self.range_bias_m


def load_calibration(path) -> Calibration:
    """Load a calibration JSON, or fall back to an uncalibrated identity
    correction (with a warning) if path is None or doesn't exist. An
    uncalibrated angle check still runs -- it just trusts raw per-element
    phase/gain instead of a real corner-reflector calibration, so this
    degrades gracefully rather than refusing to run without one."""
    if path is None:
        print("  !! No --calibration given -- angle gating will use an UNCALIBRATED "
              "array (raw phase/gain, no per-element correction). Elevation/axis "
              "numbers will be noisier; pass --calibration <path to "
              "iwr6843_calibration_reference.json> if you have one for this board.")
        return Calibration.identity()
    p = Path(path)
    if not p.exists():
        raise DataValidationError("MISSING_CALIBRATION", str(p))
        print(f"  !! --calibration {path} not found -- falling back to an "
              f"UNCALIBRATED array (see above).")
        return Calibration.identity()
    cal = Calibration.load(p)
    print(f"  Loaded calibration from {cal.source}")
    return cal


def _bartlett_elevation_deg(snapshot8: np.ndarray) -> float:
    """Bartlett beamforming on a calibrated 8-element snapshot -- matches
    openflight/src/openflight/iwr6843/music.py's est_bartlett() exactly
    (lambda/2 ULA, steer(theta)=exp(j*pi*sin(theta)*m), -40..40 deg grid).
    MUSIC needs a noise-variance/source-count estimate that's fragile on
    low-SNR bench captures, so this deliberately uses Bartlett only."""
    m = np.arange(8)
    steering = np.exp(1j * np.pi * np.sin(_ANGLE_GRID_RAD)[:, None] * m[None, :])  # (grid, 8)
    power = np.abs(steering.conj() @ snapshot8) ** 2
    return float(np.degrees(_ANGLE_GRID_RAD[int(np.argmax(power))]))


def _circular_median(values: np.ndarray) -> float:
    wrapped = np.abs(np.angle(np.exp(1j * (values[:, None] - values[None, :]))))
    return float(values[int(np.argmin(np.median(wrapped, axis=0)))])


def _orthogonal_axis_deg(tx0_4: np.ndarray, tx1_4: np.ndarray, tx2_4: np.ndarray) -> float:
    """Coarse single-baseline (lambda/2) angle from physical TX1 vs the
    midpoint of TX0/TX2, matching openflight's doa.py tx2_phase_at() /
    tx2_phase_to_axis_angle_rad(). This is NOT calibrated the way
    elevation is (production doesn't apply per-element correction to this
    baseline either) -- treat it as indicative, not precise, and give it
    a looser tolerance than elevation when gating."""
    reference = 0.5 * (tx0_4 + tx2_4)
    phases = [
        float(np.angle(np.conj(reference[rx]) * tx1_4[rx]))
        for rx in range(4) if abs(reference[rx]) * abs(tx1_4[rx]) > 0
    ]
    if not phases:
        return float("nan")
    phase = _circular_median(np.asarray(phases))
    axis_sine = phase / np.pi  # baseline = lambda/2 -> 2*pi*0.5 = pi
    return float(np.degrees(np.arcsin(np.clip(axis_sine, -1.0, 1.0))))


def _repeat_snapshot_at_bin(path, *, local_bin: int, n_tx: int, n_rx: int, chirps_per_frame: int):
    """Coherently sum every loop of every complete frame in one file at a
    given LOCAL bin index. Returns (tx0_4, tx1_4, tx2_4) raw complex 4-RX
    vectors (physical TX order, uncalibrated) plus the number of frames
    actually summed. Static-target assumption -- no Doppler/TDM
    correction -- since a bench trihedral (or clutter) isn't moving."""
    if n_tx != 3:
        raise ValueError(
            f"angle gating assumes this firmware's 3TX geometry (TX0/TX2 -> elevation "
            f"array, TX1 -> coarse axis), but this file has n_tx={n_tx}"
        )
    raw = read_capture_bytes(path)
    meta = parse_header(raw)
    _read_frame_metadata(raw, meta)
    loops_per_frame = chirps_per_frame // n_tx
    tx_acc = np.zeros((3, n_rx), dtype=complex)
    n_frames = 0
    for _idx, _start, cplx, complete in iter_frames(raw, meta):
        if not complete:
            continue
        reshaped = cplx[:, :, local_bin].reshape(loops_per_frame, n_tx, n_rx)
        tx_acc += reshaped.sum(axis=0)
        n_frames += 1
    if n_frames == 0:
        raise ValueError(f"{path}: no complete frames to build an angle snapshot from")
    return tx_acc[0], tx_acc[1], tx_acc[2], n_frames


def angle_at_bin(paths, global_bin: int, cal: Calibration, *, range_bin_m: float):
    """Per-repeat elevation/axis/range at one GLOBAL bin, across a group of
    repeat captures. Returns a list of dicts (one per path) with
    elevation_deg, axis_deg, range_m (bias-corrected), and power_db -- the
    raw material for estimate_target_bin_by_angle()'s gating, also handy
    on its own for a quick 'is this bin real' spot check from a REPL."""
    out = []
    for p in paths:
        raw = read_capture_bytes(p)
        meta = parse_header(raw)
        _read_frame_metadata(raw, meta)
        starts, counts = meta["range_bin_starts"], meta["range_bin_counts"]
        if len(set(starts)) != 1 or len(set(counts)) != 1:
            raise ValueError(f"{p}: capture window moves between frames -- can't angle-gate this file")
        start_bin = starts[0]
        local_bin = global_bin - start_bin
        if not (0 <= local_bin < counts[0]):
            raise ValueError(
                f"{p}: bin {global_bin} falls outside this file's window "
                f"[{start_bin}, {start_bin + counts[0]})"
            )
        tx0, tx1, tx2, n_frames = _repeat_snapshot_at_bin(
            p, local_bin=local_bin, n_tx=meta["n_tx"], n_rx=meta["n_rx"],
            chirps_per_frame=meta["chirps_per_frame"],
        )
        snap8 = np.concatenate([tx0, tx2])[::-1] * cal.elem_correction
        el_deg = _bartlett_elevation_deg(snap8)
        axis_deg = _orthogonal_axis_deg(tx0, tx1, tx2)
        power_db = 10.0 * np.log10(np.mean(np.abs(np.concatenate([tx0, tx2])) ** 2) / (n_frames * (meta["chirps_per_frame"] // meta["n_tx"]))**2 + 1e-12)
        out.append({
            "path": Path(p), "elevation_deg": el_deg, "axis_deg": axis_deg,
            "range_m": cal.true_range_m(global_bin * range_bin_m), "power_db": power_db,
        })
    return out


def estimate_target_bin_by_angle(
    paths, cal: Calibration, *, range_bin_m: float,
    expected_range_m: float | None = None, range_tol_m: float = 0.15,
    el_max_deg: float = 10.0, axis_max_deg: float = 20.0,
    spread_max_deg: float = 6.0, top_k_bins: int = 12, verbose: bool = True,
    return_candidates: bool = False,
):
    """Find the ONE bin in this repeat group's pooled power profile that
    looks like a real, static point target sitting at (or near) boresight
    and, if expected_range_m is given, at the known tape-measured range --
    as opposed to just the highest-power bin in the whole window, which
    board-level leakage, a mount/stand, or environmental clutter can win
    just as easily as the trihedral (see this section's module comment).

    Candidate bins are the top_k_bins highest-power bins in the group's
    pooled profile (angle estimation is too expensive, and noise bins too
    meaningless, to run on all of them). For each candidate, elevation and
    axis are estimated PER REPEAT (coherently summed within each repeat,
    not across repeats), so repeat-to-repeat SPREAD -- not just the
    median -- is part of the gate: a real static target gives tightly
    clustered angles across independent repeats; clutter or noise does
    not. A candidate passes only if:
      - |median elevation| <= el_max_deg AND elevation spread <= spread_max_deg
      - |median axis| <= axis_max_deg AND axis spread <= 3*spread_max_deg
        (axis is the coarser, uncalibrated single-baseline estimate --
        see _orthogonal_axis_deg -- so it gets a looser tolerance)
      - if expected_range_m is given: |bias-corrected range - expected_range_m| <= range_tol_m

    Among passing candidates, the one with the highest pooled power wins
    (once clutter is excluded by angle/range, the strongest survivor is
    the trihedral). Raises ValueError -- loudly, rather than silently
    falling back to plain argmax -- if nothing passes, since that means
    either the gate is too strict for this session's actual geometry, the
    expected range is wrong, or something is physically off with the setup.
    """
    start_bin = None
    pooled = None
    for p in paths:
        raw = read_capture_bytes(p)
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
                f"the rest of this repeat group (start_bin={start_bin}, {pooled.shape[0]} bins)."
            )
        else:
            pooled += profile
    if pooled is None:
        raise ValueError("no files given to angle-gate")

    pooled_db = 10.0 * np.log10(pooled + 1e-12)
    order = np.argsort(pooled_db)[::-1]
    candidate_locals = order[: min(top_k_bins, len(order))]

    report = []
    passing = []
    for local_bin in candidate_locals:
        global_bin = start_bin + int(local_bin)
        per_repeat = angle_at_bin(paths, global_bin, cal, range_bin_m=range_bin_m)
        els = [r["elevation_deg"] for r in per_repeat]
        axs = [r["axis_deg"] for r in per_repeat]
        rngs = [r["range_m"] for r in per_repeat]
        el_med, el_spread = float(np.median(els)), float(max(els) - min(els))
        ax_med, ax_spread = float(np.median(axs)), float(max(axs) - min(axs))
        rng_med = float(np.median(rngs))
        ok_el = abs(el_med) <= el_max_deg and el_spread <= spread_max_deg
        ok_axis = abs(ax_med) <= axis_max_deg and ax_spread <= 3 * spread_max_deg
        ok_range = expected_range_m is None or abs(rng_med - expected_range_m) <= range_tol_m
        passed = ok_el and ok_axis and ok_range
        entry = {
            "bin": global_bin, "range_m": global_bin * range_bin_m, "true_range_m": rng_med,
            "power_db": float(pooled_db[local_bin]),
            "elevation_median_deg": el_med, "elevation_spread_deg": el_spread,
            "axis_median_deg": ax_med, "axis_spread_deg": ax_spread,
            "passed": passed, "ok_el": ok_el, "ok_axis": ok_axis, "ok_range": ok_range,
        }
        report.append(entry)
        if passed:
            passing.append(entry)

    if verbose:
        print(f"    angle-gate candidates (top {len(candidate_locals)} bins by power):")
        for e in sorted(report, key=lambda r: -r["power_db"]):
            mark = "PASS" if e["passed"] else "fail"
            reasons = [n for n, ok in (("el", e["ok_el"]), ("axis", e["ok_axis"]), ("range", e["ok_range"])) if not ok]
            reason_note = f" (failed: {','.join(reasons)})" if reasons else ""
            print(f"      bin {e['bin']:2d}  {e['true_range_m']:.3f} m  {e['power_db']:6.2f} dB  "
                  f"el={e['elevation_median_deg']:+5.1f}+/-{e['elevation_spread_deg']:.1f}  "
                  f"axis={e['axis_median_deg']:+6.1f}+/-{e['axis_spread_deg']:.1f}  [{mark}]{reason_note}")

    if not passing:
        raise ValueError(
            f"no candidate bin (out of the top {top_k_bins} by power) passed the angle/range gate "
            f"(el<={el_max_deg} deg, axis<={axis_max_deg} deg, spread<={spread_max_deg} deg"
            + (f", range within {range_tol_m} m of {expected_range_m} m)" if expected_range_m else ")")
            + " -- either loosen the gate (--el-max-deg/--axis-max-deg/--range-tol-m), double-check "
              "--expected-range-m, or the trihedral genuinely isn't a clean detection in this session."
        )
    best = max(passing, key=lambda e: e["power_db"])
    if verbose:
        print(f"    -> selected bin {best['bin']} ({best['true_range_m']:.3f} m, "
              f"el={best['elevation_median_deg']:+.1f} deg, axis={best['axis_median_deg']:+.1f} deg)")
    if return_candidates:
        # Every bin this function looked at (pass OR fail) was loud enough to
        # be a top_k_bins candidate in the first place -- i.e. either the
        # real target or some other real, static, competing return (a
        # tripod/stand, near-field leakage, sidelobe smear). None of that is
        # "noise" in the SNR sense, so extract_file_elements()'s noise floor
        # should exclude all of it, not just a small guard band around the
        # one bin that won. See extra_noise_exclude_bins there.
        return best["bin"], {int(e["bin"]) for e in report}
    return best["bin"]


def extract_file_elements(
    path: Path, *, range_bin_m: float, guard_bins: int = 2, forced_peak_bin: int | None = None,
    extra_noise_exclude_bins: set[int] | None = None,
) -> dict:
    """Extract per-(TX, RX) coherent means at the target's range bin.

    By default the peak bin is chosen independently from this file's own
    profile (the right choice for a single diagnostic capture, e.g. each row
    of baseline_timeline.csv, where an independently-wandering peak IS the
    drift signal that timeline exists to surface). Pass forced_peak_bin --
    the output of pooled_profile_peak() over a whole repeat group -- when
    this file's result is going to be coherently combined with other
    repeats' via combine_repeats(); combine_repeats() itself refuses to mix
    results whose peak_bin disagrees, specifically to catch a caller that
    forgot to do this.

    snr_db is computed against a noise floor taken from every bin in this
    capture's window EXCEPT a small guard band around the chosen peak --
    which is fine only if every other bin is genuinely quiet. In a real
    bench window that's often false: a tripod/stand, near-field leakage, or
    FFT sidelobe smear are real, static, competing returns, not noise, and
    folding them into the "noise" floor inflates it and understates the
    trihedral's true SNR (this is why a whole-window SNR can read far worse
    than the trihedral bin's own local contrast actually is). Pass
    extra_noise_exclude_bins -- the angle-gate's own candidate bin set, see
    estimate_target_bin_by_angle(return_candidates=True) -- to exclude every
    other real detected return from the noise floor too, so snr_db reflects
    the trihedral against genuine background noise, not against a mix of
    real clutter and noise.
    """
    raw = read_capture_bytes(path)
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
    if extra_noise_exclude_bins:
        for gbin in extra_noise_exclude_bins:
            lbin = gbin - start_bin
            if 0 <= lbin < count:
                noise_mask[lbin] = False
    if not np.any(noise_mask):
        raise DataValidationError("INSUFFICIENT_NOISE_BINS", "No bins remain outside target guard; SNR is undefined")
    noise_samples = profile[noise_mask]
    noise_floor = max(
        float(np.median(noise_samples)) if noise_samples.size else float(np.median(profile)), 1e-12
    )
    noise_db = 10.0 * np.log10(noise_floor)

    n_tx = meta["n_tx"]
    n_rx = meta["n_rx"]
    cpf = meta["chirps_per_frame"]
    if cpf % n_tx != 0:
        raise ValueError(
            f"chirps_per_frame={cpf} doesn't divide evenly by n_tx={n_tx} -- can't resolve "
            "which chirp belongs to which TX antenna. Re-check the .cfg used for this capture."
        )
    loops_per_frame = cpf // n_tx

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

    samples = np.stack(per_loop_samples, axis=0)
    element_mean = samples.mean(axis=0)
    mag_mean = np.abs(samples).mean(axis=0)
    coherence = np.divide(
        np.abs(element_mean), mag_mean, out=np.full_like(mag_mean, np.nan), where=mag_mean > 0,
    )
    require_coherence(coherence, "extract_file_elements")

    peak_power = float(np.mean(np.abs(element_mean) ** 2))
    peak_db = 10.0 * np.log10(max(peak_power, 1e-12))
    snr_db = peak_db - noise_db

    return {
        "path": Path(path), "peak_bin": peak_bin, "peak_range_m": peak_bin * range_bin_m,
        "n_frames_used": n_frames_used, "n_loops_total": samples.shape[0],
        "n_tx": n_tx, "n_rx": n_rx,
        "element_mean": element_mean, "coherence": coherence,
        "noise_db": noise_db, "snr_db": snr_db,
    }


def combine_repeats(results: list[dict]) -> dict:
    n_tx, n_rx = results[0]["n_tx"], results[0]["n_rx"]
    peak_bins = {r["peak_bin"] for r in results}
    if len(peak_bins) > 1:
        raise ValueError(
            f"combine_repeats() was given results from different range bins {sorted(peak_bins)} -- "
            "these can't be coherently averaged together (this is exactly the bug pooled_profile_peak() "
            "exists to prevent). Extract every repeat with the same forced_peak_bin before combining."
        )
    stacked = np.stack([r["element_mean"] for r in results], axis=0)
    combined_mean = stacked.mean(axis=0)
    mag_mean = np.abs(stacked).mean(axis=0)
    coherence = np.divide(
        np.abs(combined_mean), mag_mean, out=np.full_like(mag_mean, np.nan), where=mag_mean > 0,
    )
    require_coherence(coherence, "combine_repeats")
    return {
        "n_repeats": len(results), "n_tx": n_tx, "n_rx": n_rx,
        "peak_bin": next(iter(peak_bins)),
        "peak_range_m": float(np.mean([r["peak_range_m"] for r in results])),
        "snr_db": float(np.mean([r["snr_db"] for r in results])),
        "element_mean": combined_mean,
        "coherence_across_repeats": coherence,
        "n_loops_total": sum(r["n_loops_total"] for r in results),
    }


# ---------------------------------------------------------------------------
# NEW for this script: material-specific grouping and baseline comparison.
# ---------------------------------------------------------------------------

def load_material_files(capture_dir):
    return load_entries(capture_dir, "material")


def _wrap_deg(x: float) -> float:
    """Wrap an angle difference in degrees to (-180, 180]."""
    return ((x + 180.0) % 360.0) - 180.0


def _angle_key(axis: str, angle_deg: float) -> tuple:
    """Group key for 'same physical angle' -- rounded so float noise in a
    typed-in angle (5.5 vs 5.50000001) doesn't split one angle into two
    groups. Boresight is its own axis regardless of what axis field a
    capture happened to carry (capture_benchtesting.py always logs
    axis='boresight' for angle 0, but be defensive about older data)."""
    if angle_deg == 0.0:
        return ("boresight", 0.0)
    return (axis, round(float(angle_deg), 2))


def _angle_label(key: tuple) -> str:
    axis, angle_deg = key
    if axis == "boresight":
        return "boresight (0)"
    return f"{axis} {angle_deg:+.2f} deg"


def _analysis_main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("capture_dir", type=Path,
                     help="Directory of .l3dump + .json pairs written by capture_benchtesting.py "
                          "in Material Testing mode")
    ap.add_argument("--range-bin-m", type=float, default=None)
    ap.add_argument("--num-adc-samples", type=int, default=DEFAULT_NUM_ADC_SAMPLES)
    ap.add_argument("--slope-mhz-per-us", type=float, default=DEFAULT_SLOPE_MHZ_PER_US)
    ap.add_argument("--sample-rate-ksps", type=float, default=DEFAULT_SAMPLE_RATE_KSPS)
    ap.add_argument("--guard-bins", type=int, default=2)
    ap.add_argument("--calibration", type=Path, default=None,
                     help="Path to iwr6843_calibration_reference.json (or an equivalent "
                          "elem_phase_rad/elem_gain/range_bias_const_m file) used to correct "
                          "the elevation/range estimate during angle-gating. Omit to angle-gate "
                          "uncalibrated (noisier, but still usable).")
    ap.add_argument("--expected-range-m", type=float, default=None,
                     help="Tape-measured trihedral range (meters, radar face to trihedral apex). "
                          "Strongly recommended -- without it, angle-gating can only reject "
                          "off-boresight clutter, not a same-axis reflector at the wrong range.")
    ap.add_argument("--el-max-deg", type=float, default=10.0,
                     help="Max |median elevation| (deg) for a candidate bin to pass angle-gating.")
    ap.add_argument("--axis-max-deg", type=float, default=20.0,
                     help="Max |median axis| (deg, coarse/uncalibrated) for a candidate to pass.")
    ap.add_argument("--range-tol-m", type=float, default=0.15,
                     help="Max |detected range - --expected-range-m| (meters) for a candidate to pass. "
                          "Ignored if --expected-range-m isn't given.")
    ap.add_argument("--spread-max-deg", type=float, default=6.0,
                     help="Max repeat-to-repeat elevation spread (deg) for a candidate to pass "
                          "(axis gets 3x this). A real static target is tightly clustered across "
                          "independent repeats; clutter/noise is not.")
    ap.add_argument("--angle-gate-top-k", type=int, default=12,
                     help="How many highest-power bins to angle-check per group before giving up.")
    ap.add_argument("--no-angle-gate", action="store_true",
                     help="Disable angle-of-arrival gating and fall back to plain pooled-power "
                          "argmax (pooled_profile_peak) -- the old behavior, kept for comparison/"
                          "debugging. Not recommended for real material comparisons: argmax can "
                          "silently lock onto clutter instead of the trihedral (see this module's "
                          "angle-gating section docstring).")
    ap.add_argument("--exclude-baseline", action="append", default=[], metavar="SUBSTRING",
                     help="Exclude baseline captures whose .l3dump filename contains SUBSTRING from "
                          "pooling/comparison (repeatable -- pass it once per capture, or once per "
                          "shared substring like a timestamp prefix). Excluded files are NOT deleted "
                          "or moved and still show up in --baseline-timeline-csv (flagged "
                          "excluded_from_pooling=True) -- this only keeps them out of the consensus-"
                          "bin pooling that material comparisons get computed against. Use this when "
                          "a baseline round turns out not to be a true, settled baseline (e.g. shot "
                          "while you were still finishing setup) but you want to keep the data on "
                          "disk and in the record rather than move/delete files.")
    ap.add_argument("--element-csv", type=Path, default=Path("capture_csv/material_elements.csv"),
                     help="One row per (swatch, tx, rx)")
    ap.add_argument("--summary-csv", type=Path, default=Path("capture_csv/material_summary.csv"),
                     help="One row per swatch, averaged across every element")
    ap.add_argument("--baseline-timeline-csv", type=Path, default=Path("capture_csv/baseline_timeline.csv"),
                     help="One row per individual baseline capture, sorted by time -- "
                          "plot this to check for baseline drift across the session")
    add_validation_arguments(ap)
    args = ap.parse_args()
    if prepare_analysis(args, "material", extract_file_elements):
        return 0

    range_bin_m = args.range_bin_m or range_bin_meters(
        args.num_adc_samples, args.slope_mhz_per_us, args.sample_rate_ksps
    )

    if args.no_angle_gate:
        cal = None
        print("!! --no-angle-gate: falling back to plain pooled-power argmax. This will happily "
              "lock onto clutter (a stand, near-field leakage, a wall) instead of the trihedral if "
              "clutter is louder in a given session -- see this module's angle-gating docstring.\n")
    else:
        cal = load_calibration(args.calibration)
        if args.expected_range_m is None:
            print("  !! No --expected-range-m given -- angle-gating will accept any bin that "
                  "looks like a real boresight point target, even at the wrong range. Pass "
                  "--expected-range-m <tape-measured distance> to also gate on range.")
        print()

    def select_consensus_bin(paths, *, label: str) -> tuple[int, set[int]]:
        """Dispatch to angle-gated bin selection (default) or plain pooled-power
        argmax (--no-angle-gate). Raises ValueError on failure either way, so
        every existing call site's try/except ValueError keeps working.

        Returns (bin, clutter_bins) -- clutter_bins is every OTHER real,
        static, competing return the angle-gate saw while looking for the
        trihedral (a stand, near-field leakage, sidelobe smear -- anything
        loud enough to be a top_k_bins candidate but that didn't get chosen).
        Pass it straight through to extract_file_elements()'s
        extra_noise_exclude_bins so the reported SNR is the trihedral against
        genuine noise, not against a mix of noise and other real targets.
        --no-angle-gate has no such list (plain argmax never looks at other
        bins), so it always returns an empty set -- snr_db there is exactly
        the old whole-window-minus-guard-band behavior."""
        if args.no_angle_gate:
            return pooled_profile_peak(paths, guard_bins=args.guard_bins), set()
        print(f"  angle-gating {label}:")
        return estimate_target_bin_by_angle(
            paths, cal, range_bin_m=range_bin_m,
            expected_range_m=args.expected_range_m, range_tol_m=args.range_tol_m,
            el_max_deg=args.el_max_deg, axis_max_deg=args.axis_max_deg,
            spread_max_deg=args.spread_max_deg, top_k_bins=args.angle_gate_top_k,
            return_candidates=True,
        )

    entries = load_material_files(args.capture_dir)
    baseline_entries = [e for e in entries if e["kind"] == "baseline"]
    material_entries = [e for e in entries if e["kind"] == "material"]
    if not args.no_angle_gate and any(e["angle_deg"] != 0 for e in entries):
        raise DataValidationError("UNSUPPORTED_OFF_AXIS_GATE", "Boresight gate cannot validate off-axis targets. Use --no-angle-gate only with independently verified target selection.")

    if not baseline_entries:
        print("!! No baseline captures found -- can't compute attenuation or phase shift "
              "without a reference. (kind='baseline' sidecars required.)")
        return 1

    print(f"Found {len(baseline_entries)} baseline capture(s) and "
          f"{len(material_entries)} material capture(s) across "
          f"{len({e['swatch'] for e in material_entries})} swatch(es).\n")

    # --- extract every baseline file individually, and group by (axis,
    # angle_deg) -- an off-axis material shot must be compared against a
    # baseline taken at that SAME angle, not the boresight baseline, or the
    # antenna's own inherent off-axis behavior gets misattributed to the
    # material. See module docstring / capture_benchtesting.py's
    # run_material_mode() docstring for why. ---
    baseline_results = []
    baseline_by_angle = defaultdict(list)
    excluded_baseline_names = set()
    for e in baseline_entries:
        try:
            r = extract_file_elements(e["l3dump_path"], range_bin_m=range_bin_m, guard_bins=args.guard_bins)
        except (ShortHeader, ValueError) as exc:
            raise
            print(f"  !! {e['l3dump_path'].name}: {exc}")
            continue
        r["timestamp"] = e["timestamp"]
        r["angle_deg"] = e["angle_deg"]
        r["axis"] = e["axis"]
        # Every baseline capture goes into baseline_results (and therefore
        # --baseline-timeline-csv) regardless of --exclude-baseline -- this
        # is a record of what you shot, not just what got trusted. Only
        # baseline_by_angle (pooling -> what material captures actually get
        # compared against) skips an excluded file, so a baseline round that
        # wasn't a true settled baseline (still adjusting the setup, etc.)
        # stays on disk and in the CSV, flagged, instead of silently
        # corrupting -- or requiring you to move/delete -- real data.
        baseline_results.append(r)
        fname = e["l3dump_path"].name
        if args.exclude_baseline and any(pat in fname for pat in args.exclude_baseline):
            excluded_baseline_names.add(fname)
            print(f"  Excluding from baseline pooling (matched --exclude-baseline): {fname}")
            continue
        # Keep the sidecar entry alongside its diagnostic result -- pooling
        # below needs the l3dump_path again to re-extract at the group's
        # consensus peak bin (see pooled_profile_peak()'s docstring).
        baseline_by_angle[_angle_key(e["axis"], e["angle_deg"])].append((e, r))

    if args.exclude_baseline and not excluded_baseline_names:
        print(f"  !! --exclude-baseline {args.exclude_baseline} matched no baseline filenames -- "
              f"double check the substring(s) against your actual filenames.")

    if not baseline_results:
        print("!! Every baseline capture failed to parse -- nothing to compare against.")
        return 1

    # Pool each angle's baselines onto ONE consensus peak bin before
    # coherently averaging them -- NOT by averaging each file's independently
    # -chosen element_mean, which can silently mix data from different range
    # bins if one repeat's own SNR was too low for its argmax to agree with
    # the others (see pooled_profile_peak()'s docstring; this is exactly the
    # bug behind the near-180-degree phase_shift_vs_baseline values seen in
    # earlier sessions).
    pooled_by_angle = {}
    clutter_bins_by_angle = {}
    for key, pairs in sorted(baseline_by_angle.items()):
        paths = [e["l3dump_path"] for e, r in pairs]
        try:
            consensus_bin, clutter_bins = select_consensus_bin(paths, label=f"baseline @ {_angle_label(key)}")
        except ValueError as exc:
            raise
            print(f"  !! Can't pool baseline @ {_angle_label(key)}: {exc}")
            continue
        # clutter_bins excludes the consensus_bin itself already (it never
        # passed as its own candidate), but subtract it explicitly anyway in
        # case a future gate change ever includes the winner in its report --
        # extract_file_elements's own guard band already covers the winner,
        # this is just belt-and-suspenders against ever excluding OUR target
        # from its own noise floor... no wait, the target's own bin is never
        # "noise" regardless, so this is purely defensive.
        clutter_bins = clutter_bins - {consensus_bin}
        clutter_bins_by_angle[key] = clutter_bins
        forced_results = []
        for e, r in pairs:
            if r["peak_bin"] != consensus_bin:
                print(f"  !! note: {e['l3dump_path'].name} independently picked bin {r['peak_bin']} "
                      f"but this baseline group's pooled consensus is bin {consensus_bin} -- "
                      f"re-extracting it at the consensus bin for pooling.")
            try:
                forced_results.append(extract_file_elements(
                    e["l3dump_path"], range_bin_m=range_bin_m, guard_bins=args.guard_bins,
                    forced_peak_bin=consensus_bin, extra_noise_exclude_bins=clutter_bins,
                ))
            except (ShortHeader, ValueError) as exc:
                raise
                print(f"  !! {e['l3dump_path'].name}: {exc}")
        if forced_results:
            pooled_by_angle[key] = combine_repeats(forced_results)

    if not pooled_by_angle:
        print("!! No baseline group could be pooled -- nothing to compare materials against. If "
              "one baseline round looks unstable in the angle-gate diagnostics above (e.g. shot "
              "before the setup had settled) but another round at the same angle is clean, try "
              "--exclude-baseline <substring of the bad round's filename> to drop it from pooling "
              "without touching the files on disk.")
        return 1

    n_tx, n_rx = baseline_results[0]["n_tx"], baseline_results[0]["n_rx"]
    for key, pooled in sorted(pooled_by_angle.items()):
        print(f"Pooled baseline @ {_angle_label(key)}: {pooled['n_repeats']} capture(s), "
              f"{pooled['n_loops_total']} loops total, range={pooled['peak_range_m']:.3f} m, "
              f"SNR={pooled['snr_db']:.1f} dB")
    print()

    # --- baseline drift timeline: each baseline file individually, sorted by time ---
    baseline_timeline_rows = []
    sorted_baseline = sorted(
        (r for r in baseline_results if r.get("timestamp")),
        key=lambda r: r["timestamp"],
    )
    for r in sorted_baseline:
        power_db = 10.0 * np.log10(np.clip(np.abs(r["element_mean"]) ** 2, 1e-12, None))
        key = _angle_key(r["axis"], r["angle_deg"])
        baseline_timeline_rows.append({
            "timestamp": r["timestamp"],
            "file": r["path"].name,
            "axis": key[0],
            "angle_deg": key[1],
            "range_m": round(r["peak_range_m"], 4),
            "power_db_mean_all_elements": round(float(np.mean(power_db)), 2),
            "snr_db": round(r["snr_db"], 2),
            "excluded_from_pooling": r["path"].name in excluded_baseline_names,
        })

    # --- per-(swatch, axis, angle_deg) comparison against the baseline
    # pooled at that SAME angle ---
    by_swatch_angle = defaultdict(list)
    for e in material_entries:
        by_swatch_angle[(e["swatch"], _angle_key(e["axis"], e["angle_deg"]))].append(e)

    element_rows = []
    summary_rows = []
    for (swatch, angle_key), members in sorted(by_swatch_angle.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        pooled_baseline = pooled_by_angle.get(angle_key)
        if pooled_baseline is None:
            raise DataValidationError("MISSING_BASELINE", f"No matching baseline for {angle_key}")
            print(f"  !! swatch={swatch} @ {_angle_label(angle_key)}: no baseline capture at this "
                  f"angle -- skipped (take a baseline at the same angle to compare against; "
                  f"see run_material_mode()'s docstring).")
            continue

        # Anchor every material extraction to the BASELINE's consensus bin at
        # this angle, not this swatch's own argmax. The trihedral is static
        # and does not move between the baseline and a material shot -- the
        # true target range is whatever the baseline established, full stop.
        # Letting each swatch pick its own "loudest" bin (the previous fix
        # here) only guaranteed a swatch's 3 repeats agreed WITH EACH OTHER;
        # it never guaranteed they agreed with the baseline's location, and
        # at this session's SNR (~-1 to 1 dB at the true bin) argmax noise
        # routinely wins by a wide margin -- this data alone shows the
        # "detected" range wandering from 1.08 m to 1.64 m across supposedly
        # identical, immobile-target captures, which is not physically
        # possible and is a pure artifact of comparing power at whatever bin
        # each capture group's own noise happened to peak at. Comparing
        # delta_power_db/phase_shift between two DIFFERENT physical bins is
        # meaningless regardless of how internally consistent each group is.
        # This is a SANITY CHECK ONLY -- extraction below always uses the
        # baseline's consensus_bin, never own_consensus. A material swatch
        # (especially a lossy one) can easily drop this capture's own SNR
        # low enough that ITS OWN independent angle-gate can't confidently
        # re-derive the trihedral's bin on its own -- that's an expected,
        # non-fatal outcome of putting lossy material in the beam, not a
        # sign the underlying data is bad, so a failure here must never skip
        # the swatch. It only means this cross-check can't run for this
        # swatch; the extraction proceeds against the baseline's bin
        # regardless, exactly like when the check *can* run and agrees.
        paths = [m["l3dump_path"] for m in members]
        consensus_bin = pooled_baseline["peak_bin"]
        # The clutter set (other real, static, competing returns) is the
        # BASELINE's -- established once from the clean bare-trihedral scan,
        # then reused for every swatch at this angle. This is deliberate,
        # not just convenient: a swatch's own top_k_bins scan can rank
        # differently once a lossy material changes the local power profile,
        # and we want a stable, consistent definition of "what's clutter
        # here" across a whole baseline-vs-swatch comparison, not a
        # per-swatch-redefined one that could quietly change what counts as
        # noise between the bare and materialed shots.
        clutter_bins = clutter_bins_by_angle.get(angle_key, set())
        try:
            own_consensus, _own_clutter_bins = select_consensus_bin(paths, label=f"swatch={swatch} @ {_angle_label(angle_key)}")
        except ValueError as exc:
            print(f"  (swatch={swatch} @ {_angle_label(angle_key)}: couldn't independently verify this "
                  f"swatch's own bin ({exc.__class__.__name__.lower()}: {exc}) -- proceeding with the "
                  f"baseline's bin {consensus_bin} anyway, since that's what's used either way.)")
        else:
            if own_consensus != consensus_bin:
                print(f"  !! note: swatch={swatch} @ {_angle_label(angle_key)}: this swatch's own "
                      f"captures alone would independently select bin {own_consensus}, but the "
                      f"baseline's bin is {consensus_bin} -- using the baseline's bin (the static "
                      f"target hasn't moved).")

        per_file_results = []
        for m in members:
            try:
                r = extract_file_elements(
                    m["l3dump_path"], range_bin_m=range_bin_m, guard_bins=args.guard_bins,
                    forced_peak_bin=consensus_bin, extra_noise_exclude_bins=clutter_bins,
                )
            except (ShortHeader, ValueError) as exc:
                raise
                print(f"  !! {m['l3dump_path'].name}: {exc}")
                continue
            per_file_results.append(r)
        if not per_file_results:
            print(f"  !! swatch={swatch} @ {_angle_label(angle_key)}: no usable captures -- skipped")
            continue

        ref_power_db = 10.0 * np.log10(np.clip(np.abs(pooled_baseline["element_mean"]) ** 2, 1e-12, None))
        ref_phase_deg = np.degrees(np.angle(pooled_baseline["element_mean"]))

        combined = combine_repeats(per_file_results)
        power_db = 10.0 * np.log10(np.clip(np.abs(combined["element_mean"]) ** 2, 1e-12, None))
        phase_deg = np.degrees(np.angle(combined["element_mean"]))

        delta_power_db = power_db - ref_power_db          # negative = attenuation vs. baseline
        phase_shift_deg = np.vectorize(_wrap_deg)(phase_deg - ref_phase_deg)

        axis_out, angle_out = angle_key
        for tx in range(n_tx):
            for rx in range(n_rx):
                within_coh = float(np.nanmean([r["coherence"][tx, rx] for r in per_file_results]))
                element_rows.append({
                    "swatch": swatch,
                    "axis": axis_out,
                    "angle_deg": angle_out,
                    "tx": tx,
                    "rx": rx,
                    "range_m": round(combined["peak_range_m"], 4),
                    "power_db": round(float(power_db[tx, rx]), 2),
                    "delta_power_db_vs_baseline": round(float(delta_power_db[tx, rx]), 2),
                    "phase_deg": round(float(phase_deg[tx, rx]), 2),
                    "phase_shift_vs_baseline_deg": round(float(phase_shift_deg[tx, rx]), 2),
                    "snr_db": round(combined["snr_db"], 2),
                    "n_repeats": combined["n_repeats"],
                    "n_loops_total": combined["n_loops_total"],
                    "coherence_within_file": round(within_coh, 4),
                    "coherence_across_repeats": round(float(combined["coherence_across_repeats"][tx, rx]), 4),
                })

        summary_rows.append({
            "swatch": swatch,
            "axis": axis_out,
            "angle_deg": angle_out,
            "range_m": round(combined["peak_range_m"], 4),
            "mean_delta_power_db_vs_baseline": round(float(np.mean(delta_power_db)), 2),
            "worst_delta_power_db_vs_baseline": round(float(np.min(delta_power_db)), 2),
            "mean_abs_phase_shift_deg": round(float(np.mean(np.abs(phase_shift_deg))), 2),
            "max_abs_phase_shift_deg": round(float(np.max(np.abs(phase_shift_deg))), 2),
            "snr_db": round(combined["snr_db"], 2),
            "n_repeats": combined["n_repeats"],
        })
        print(f"  swatch={swatch:20s} @ {_angle_label(angle_key):16s}  mean delta={np.mean(delta_power_db):+6.2f} dB  "
              f"worst={np.min(delta_power_db):+6.2f} dB  "
              f"max |phase shift|={np.max(np.abs(phase_shift_deg)):5.2f} deg  "
              f"SNR={combined['snr_db']:.1f} dB  ({combined['n_repeats']} repeat(s))")

    # Write whatever's actually available. A baseline-only session (no
    # material swatches shot yet -- e.g. a rig sanity check) is a real,
    # expected use case, not a failure: it should still get its
    # baseline_timeline.csv written so you can check SNR/drift before
    # moving on to swatches. Only the material-specific CSVs are
    # conditional on having material data.
    if baseline_timeline_rows:
        args.baseline_timeline_csv.parent.mkdir(parents=True, exist_ok=True)
        with open(args.baseline_timeline_csv, "w", newline="", encoding="utf-8") as fh:
            writer = csv_module.DictWriter(fh, fieldnames=list(baseline_timeline_rows[0].keys()))
            writer.writeheader()
            writer.writerows(baseline_timeline_rows)
        print(f"\nWrote {args.baseline_timeline_csv} ({len(baseline_timeline_rows)} rows) "
              f"-- plot power_db_mean_all_elements over timestamp to check for baseline drift")

    if not element_rows:
        print("\nNo material captures found yet -- nothing to write to "
              f"{args.element_csv} or {args.summary_csv}. That's expected for a "
              "baseline-only session; check the pooled-baseline line above "
              "(SNR, range) before moving on to swatches.")
        return 0

    args.element_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(args.element_csv, "w", newline="", encoding="utf-8") as fh:
        writer = csv_module.DictWriter(fh, fieldnames=list(element_rows[0].keys()))
        writer.writeheader()
        writer.writerows(element_rows)
    print(f"Wrote {args.element_csv} ({len(element_rows)} rows)")

    args.summary_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(args.summary_csv, "w", newline="", encoding="utf-8") as fh:
        writer = csv_module.DictWriter(fh, fieldnames=list(summary_rows[0].keys()))
        writer.writeheader()
        writer.writerows(summary_rows)
    print(f"Wrote {args.summary_csv} ({len(summary_rows)} rows)")

    return 0


def main():
    return run_checked(_analysis_main)


if __name__ == "__main__":
    sys.exit(main())
