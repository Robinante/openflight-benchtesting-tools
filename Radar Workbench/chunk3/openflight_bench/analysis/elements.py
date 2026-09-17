"""Shared element extraction: power profiles, peak selection, coherent averaging.

``combined_power_profile`` and ``pooled_profile_peak`` were byte-identical in
both original scripts apart from comment text, so the analyze_sweep.py copies
are used verbatim.

Chunk 2: these now consume a normalized :class:`~.capture.Capture` instead of
the raw ILD1 header dict, so a second parser (OPS, replay) only has to produce a
Capture. The path-taking entry points are kept as wrappers -- same signature,
same numbers -- so existing callers and the regression tests are unaffected.

``extract_file_elements`` and ``combine_repeats`` genuinely differed. The
analyze_material.py versions are used, because they are supersets:

  * extract_file_elements gained ``extra_noise_exclude_bins``. Material's copy
    also RAISED where sweep's fell back to the whole profile when the guard
    band consumed every bin -- that single divergence is preserved behind
    ``strict_noise`` (default False = sweep's original fallback; material
    passes True).
  * combine_repeats gained a ``peak_bin`` key in its result and a
    ``require_coherence`` call. The extra key cannot leak into sweep's CSVs:
    both scripts build their rows field by field, never by splatting this dict.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .capture import Capture, ConfigurationPolicy, as_capture, load_capture
from .ild1 import (
    _RANGE_SNAPSHOT_FORMATS, _TIMED_SAMPLE_FORMATS, _VARIABLE_SAMPLE_FORMATS,
    SAMPLE_FMT_NAMES, iter_frames, parse_header, _read_frame_metadata,
)
from .loaders import read_capture_bytes
from .validation import DataValidationError, require_coherence

# NOTE: bodies marked "verbatim" below are copied unchanged from the
# original analyze_sweep.py / analyze_material.py so behavior is preserved
# exactly. Do not reformat them casually -- the regression tests in
# tests/test_analysis_regression.py compare against the originals.


def capture_power_profile(capture: Capture):
    """Average power over every complete frame's (chirps x rx) at every
    bin -> one profile array for the file's single fixed window, plus its
    start_bin. Raises if the window isn't actually fixed across frames --
    this tool is built around iwr6843_benchtesting_static_sweep.cfg's
    lateStart == postStart (no window-walk); a file that doesn't hold that
    isn't something this extraction logic should guess about."""
    starts = capture.frames.range_bin_starts
    counts = capture.frames.range_bin_counts
    if not capture.frames.window_is_fixed:
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
    for _idx, _start, cplx in capture.frames.complete_only():
        power_sum += np.mean(np.abs(cplx) ** 2, axis=(0, 1))
        n += 1
    if n == 0:
        raise ValueError("no complete frames in this file")
    return start_bin, power_sum / n


def combined_power_profile(raw: bytes, meta: dict):
    """Back-compatible entry point taking raw bytes + the ILD1 meta dict."""
    from .capture import FrameSet  # local import: avoids a cycle at module load
    shim = Capture.__new__(Capture)
    shim.frames = FrameSet(raw=raw, header=meta)
    return capture_power_profile(shim)


def pooled_profile_peak(captures, *, guard_bins: int = 2,
                        policy: ConfigurationPolicy | None = None) -> int:
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

    ``captures`` accepts Capture objects, loader entry dicts, or bare paths.
    """
    start_bin = None
    pooled = None
    for item in captures:
        cap = as_capture(item, policy=policy)
        p = cap.source.path if cap.source.path is not None else item
        try:
            cap.require_range_snapshot()
        except ValueError as exc:
            raise ValueError(f"{p}: {exc}") from None
        b, profile = capture_power_profile(cap)
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


def extract_capture_elements(
    capture: Capture, *, range_bin_m: float | None = None, guard_bins: int = 2,
    forced_peak_bin: int | None = None,
    extra_noise_exclude_bins: set[int] | None = None,
    strict_noise: bool = False,
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
    capture.require_range_snapshot()
    if range_bin_m is None:
        range_bin_m = capture.configuration.range_bin_m
    path = capture.source.path

    start_bin, profile = capture_power_profile(capture)
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
        if strict_noise:
            raise DataValidationError(
                "INSUFFICIENT_NOISE_BINS",
                "No bins remain outside target guard; SNR is undefined",
            )
        noise_samples = profile
    else:
        noise_samples = profile[noise_mask]
    noise_floor = max(
        float(np.median(noise_samples)) if noise_samples.size else float(np.median(profile)), 1e-12
    )
    noise_db = 10.0 * np.log10(noise_floor)

    n_tx = capture.channels.n_tx
    n_rx = capture.channels.n_rx
    cpf = capture.channels.chirps_per_frame
    if not capture.channels.tdm_divides_evenly:
        raise ValueError(
            f"chirps_per_frame={cpf} doesn't divide evenly by n_tx={n_tx} -- can't resolve "
            "which chirp belongs to which TX antenna. Re-check the .cfg used for this capture."
        )
    loops_per_frame = capture.channels.loops_per_frame

    per_loop_samples = []
    n_frames_used = 0
    for _idx, f_start, cplx in capture.frames.complete_only():
        if f_start != start_bin:
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
    require_coherence(coherence, "extract_capture_elements")

    peak_power = float(np.mean(np.abs(element_mean) ** 2))
    peak_db = 10.0 * np.log10(max(peak_power, 1e-12))
    snr_db = peak_db - noise_db

    return {
        "path": Path(path) if path is not None else None,
        "peak_bin": peak_bin, "peak_range_m": peak_bin * range_bin_m,
        "n_frames_used": n_frames_used, "n_loops_total": samples.shape[0],
        "n_tx": n_tx, "n_rx": n_rx,
        "element_mean": element_mean, "coherence": coherence,
        "noise_db": noise_db, "snr_db": snr_db,
        "capture": capture,
        "range_bin_m": range_bin_m,
    }


def extract_file_elements(
    path, *, range_bin_m: float | None = None, guard_bins: int = 2,
    forced_peak_bin: int | None = None,
    extra_noise_exclude_bins: set[int] | None = None,
    strict_noise: bool = False,
    policy: ConfigurationPolicy | None = None,
) -> dict:
    """Back-compatible entry point taking a path.

    Passing ``range_bin_m`` explicitly keeps the pre-Chunk-2 meaning: use
    exactly this value. Omit it and the Capture resolves it, which prefers the
    sidecar's recorded value over one derived from chirp defaults.
    """
    capture = as_capture(path, policy=policy)
    return extract_capture_elements(
        capture, range_bin_m=range_bin_m, guard_bins=guard_bins,
        forced_peak_bin=forced_peak_bin,
        extra_noise_exclude_bins=extra_noise_exclude_bins,
        strict_noise=strict_noise,
    )


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


def analyze_capture(
    capture, *, guard_bins: int = 2, forced_peak_bin: int | None = None,
    extra_noise_exclude_bins: set[int] | None = None, strict_noise: bool = False,
    policy: ConfigurationPolicy | None = None,
):
    """Single-capture analysis, returned as a typed :class:`CaptureAnalysis`.

    Same numbers as ``extract_capture_elements`` -- this adds the power profile
    and wraps the result in the normalized type so a caller (Phase 2's Analyze
    page, a JSON exporter, a test) gets named attributes instead of a bare dict.
    """
    from .results import CaptureAnalysis   # local import: results imports nothing here

    cap = as_capture(capture, policy=policy)
    start_bin, profile = capture_power_profile(cap)
    r = extract_capture_elements(
        cap, guard_bins=guard_bins, forced_peak_bin=forced_peak_bin,
        extra_noise_exclude_bins=extra_noise_exclude_bins, strict_noise=strict_noise,
    )
    analysis = CaptureAnalysis(
        captures=[cap],
        capture=cap,
        peak_bin=r["peak_bin"],
        peak_range_m=r["peak_range_m"],
        start_bin=start_bin,
        power_profile=profile,
        noise_db=r["noise_db"],
        snr_db=r["snr_db"],
        element_mean=r["element_mean"],
        coherence=r["coherence"],
        n_frames_used=r["n_frames_used"],
        n_loops_total=r["n_loops_total"],
    )
    analysis.collect_capture_warnings()
    return analysis
