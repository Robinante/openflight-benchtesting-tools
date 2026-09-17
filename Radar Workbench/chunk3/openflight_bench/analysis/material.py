"""Material analysis: attenuation and phase shift of a swatch versus a pooled baseline.

Structure mirrors openflight_bench.analysis.sweep:

    build_arg_parser()     -- the argparse block, verbatim
    analyze_material(args) -- the computation, verbatim, now RETURNING its rows
    main()                 -- CLI glue: parse, analyze, write CSVs

The calibration/angle-gating helpers below are copied unchanged from
analyze_material.py. The original imported its parsing and validation helpers
from ``analyzer_validation``, a module missing from the repository; those now
come from openflight_bench.analysis.ild1 / .loaders / .validation. See
docs/CHUNK1_NOTES.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .capture import Capture, ConfigurationPolicy, as_capture, load_capture
from .elements import (
    combine_repeats, capture_power_profile, extract_capture_elements, pooled_profile_peak,
)
from .export import write_rows
from .ild1 import (
    DEFAULT_NUM_ADC_SAMPLES, DEFAULT_SAMPLE_RATE_KSPS, DEFAULT_SLOPE_MHZ_PER_US,
    SPEED_OF_LIGHT_M_S, _RANGE_SNAPSHOT_FORMATS, ShortHeader, iter_frames,
    parse_header, _read_frame_metadata, range_bin_meters,
)
from .loaders import load_entries, load_material_files, read_capture_bytes
from .progress import as_progress
from .results import MaterialComparisonResult
from .validation import (
    DataValidationError, add_validation_arguments, prepare_analysis,
    require_coherence, run_checked,
)

# NOTE: everything from here down is verbatim from analyze_material.py.

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


def _repeat_snapshot_at_bin(capture, *, local_bin: int, n_tx: int, n_rx: int, chirps_per_frame: int):
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
    capture = as_capture(capture)
    path = capture.source.path
    loops_per_frame = chirps_per_frame // n_tx
    tx_acc = np.zeros((3, n_rx), dtype=complex)
    n_frames = 0
    for _idx, _start, cplx in capture.frames.complete_only():
        reshaped = cplx[:, :, local_bin].reshape(loops_per_frame, n_tx, n_rx)
        tx_acc += reshaped.sum(axis=0)
        n_frames += 1
    if n_frames == 0:
        raise ValueError(f"{path}: no complete frames to build an angle snapshot from")
    return tx_acc[0], tx_acc[1], tx_acc[2], n_frames


def angle_at_bin(captures, global_bin: int, cal: Calibration, *, range_bin_m: float):
    """Per-repeat elevation/axis/range at one GLOBAL bin, across a group of
    repeat captures. Returns a list of dicts (one per path) with
    elevation_deg, axis_deg, range_m (bias-corrected), and power_db -- the
    raw material for estimate_target_bin_by_angle()'s gating, also handy
    on its own for a quick 'is this bin real' spot check from a REPL."""
    out = []
    for item in captures:
        cap = as_capture(item)
        p = cap.source.path
        meta = cap.header
        starts, counts = cap.frames.range_bin_starts, cap.frames.range_bin_counts
        if not cap.frames.window_is_fixed:
            raise ValueError(f"{p}: capture window moves between frames -- can't angle-gate this file")
        start_bin = starts[0]
        local_bin = global_bin - start_bin
        if not (0 <= local_bin < counts[0]):
            raise ValueError(
                f"{p}: bin {global_bin} falls outside this file's window "
                f"[{start_bin}, {start_bin + counts[0]})"
            )
        tx0, tx1, tx2, n_frames = _repeat_snapshot_at_bin(
            cap, local_bin=local_bin, n_tx=cap.channels.n_tx, n_rx=cap.channels.n_rx,
            chirps_per_frame=cap.channels.chirps_per_frame,
        )
        snap8 = np.concatenate([tx0, tx2])[::-1] * cal.elem_correction
        el_deg = _bartlett_elevation_deg(snap8)
        axis_deg = _orthogonal_axis_deg(tx0, tx1, tx2)
        power_db = 10.0 * np.log10(np.mean(np.abs(np.concatenate([tx0, tx2])) ** 2) / (n_frames * cap.channels.loops_per_frame)**2 + 1e-12)
        out.append({
            "path": Path(p), "elevation_deg": el_deg, "axis_deg": axis_deg,
            "range_m": cal.true_range_m(global_bin * range_bin_m), "power_db": power_db,
        })
    return out


def estimate_target_bin_by_angle(
    captures, cal: Calibration, *, range_bin_m: float,
    expected_range_m: float | None = None, range_tol_m: float = 0.15,
    el_max_deg: float = 10.0, axis_max_deg: float = 20.0,
    spread_max_deg: float = 6.0, top_k_bins: int = 12, verbose: bool = True,
    return_candidates: bool = False, candidate_sink: list | None = None,
    progress=None,
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
    print = as_progress(progress)
    start_bin = None
    pooled = None
    captures = [as_capture(c) for c in captures]
    for cap in captures:
        p = cap.source.path
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
        per_repeat = angle_at_bin(captures, global_bin, cal, range_bin_m=range_bin_m)
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

    # Hand the full candidate table to the caller BEFORE the pass/fail check,
    # so a gate that rejects everything still shows what it looked at. That
    # table is the most useful thing this function produces when it fails, and
    # it used to exist only as printed text.
    if candidate_sink is not None:
        candidate_sink.extend(report)

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


@dataclass
class MaterialOptions:
    """Attribute-compatible stand-in for the material argparse Namespace.

    Every field mirrors a flag on build_arg_parser() with the same name and the
    same default, so analyze_material() reads identical values whether it is
    handed one of these or a real parsed Namespace.
    """
    capture_dir: Path
    range_bin_m: float | None = None
    num_adc_samples: int = DEFAULT_NUM_ADC_SAMPLES
    slope_mhz_per_us: float = DEFAULT_SLOPE_MHZ_PER_US
    sample_rate_ksps: float = DEFAULT_SAMPLE_RATE_KSPS
    guard_bins: int = 2
    # angle-gating
    calibration: Path | None = None
    expected_range_m: float | None = None
    el_max_deg: float = 10.0
    axis_max_deg: float = 20.0
    range_tol_m: float = 0.15
    spread_max_deg: float = 6.0
    angle_gate_top_k: int = 12
    no_angle_gate: bool = False
    exclude_baseline: list = field(default_factory=list)
    # outputs
    element_csv: Path = field(default_factory=lambda: Path("capture_csv/material_elements.csv"))
    summary_csv: Path = field(default_factory=lambda: Path("capture_csv/material_summary.csv"))
    baseline_timeline_csv: Path = field(
        default_factory=lambda: Path("capture_csv/baseline_timeline.csv"))
    # validation layer
    min_coherence: float | None = None
    self_test: bool = False

    def __post_init__(self):
        for f in ("capture_dir", "calibration", "element_csv", "summary_csv",
                  "baseline_timeline_csv"):
            v = getattr(self, f, None)
            if isinstance(v, str):
                setattr(self, f, Path(v))
        # Belt and braces: if the parser ever grows a flag this dataclass has
        # not been taught, fall back to the parser's own default rather than
        # letting the analysis body hit AttributeError.
        for action in build_arg_parser()._actions:
            if action.dest in ("help", "capture_dir"):
                continue
            if not hasattr(self, action.dest):
                setattr(self, action.dest, action.default)


def build_arg_parser(description: str | None = None) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=description or __doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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
    return ap


def analyze_material(args, *, progress=None) -> MaterialComparisonResult:
    """Run the material analysis. ``args`` may be a MaterialOptions or an
    argparse Namespace. Returns the rows instead of writing them.

    ``progress`` receives every line the analysis would have printed. It
    defaults to the builtin ``print``, so the CLI stays byte-identical; a GUI
    passes its own sink and nothing reaches stdout.
    """
    print = as_progress(progress)

    try:
        return _analyze_material_body(args, print)
    except Exception as exc:                       # noqa: BLE001
        # The angle-gate candidate table is the most useful thing produced when
        # selection fails; attach it so a caller (GUI) can still show it.
        r = getattr(exc, "_partial_result", None)
        if r is not None:
            exc.result = r
        raise


def _analyze_material_body(args, print) -> MaterialComparisonResult:
    policy = ConfigurationPolicy(
        explicit_range_bin_m=args.range_bin_m,
        num_adc_samples=args.num_adc_samples,
        slope_mhz_per_us=args.slope_mhz_per_us,
        sample_rate_ksps=args.sample_rate_ksps,
    )
    result = MaterialComparisonResult()
    try:
        range_bin_m = policy.derived_range_bin_m

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

        def select_consensus_bin(captures, *, label: str) -> tuple[int, set[int]]:
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
                return pooled_profile_peak(captures, guard_bins=args.guard_bins), set()
            print(f"  angle-gating {label}:")
            sink: list = []
            try:
                return estimate_target_bin_by_angle(
                    captures, cal, range_bin_m=range_bin_m, candidate_sink=sink,
                    progress=print,
                    expected_range_m=args.expected_range_m, range_tol_m=args.range_tol_m,
                    el_max_deg=args.el_max_deg, axis_max_deg=args.axis_max_deg,
                    spread_max_deg=args.spread_max_deg, top_k_bins=args.angle_gate_top_k,
                    return_candidates=True,
                )
            finally:
                # Recorded even when the gate raises -- that is when you most want
                # to see the table.
                rows = result.tables.setdefault("angle_gate_candidates", [])
                for e in sorted(sink, key=lambda r: -r["power_db"]):
                    rows.append({"group": label, **e})

        entries = load_material_files(args.capture_dir)
        # One Capture per entry, loaded once here and reused everywhere below, so
        # the file is parsed a single time and every consumer sees the same
        # normalized configuration (notably range_bin_m).
        for e in entries:
            e["capture"] = load_capture(e["l3dump_path"], sidecar=e.get("record"),
                                        sidecar_path=e.get("sidecar_path"), policy=policy)
            result.captures.append(e["capture"])
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
                r = extract_capture_elements(e["capture"], guard_bins=args.guard_bins)
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
            captures = [e["capture"] for e, r in pairs]
            try:
                consensus_bin, clutter_bins = select_consensus_bin(captures, label=f"baseline @ {_angle_label(key)}")
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
                    forced_results.append(extract_capture_elements(
                        e["capture"], guard_bins=args.guard_bins,
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
        baseline_timeline_rows = result.baseline_timeline_rows
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

        element_rows = result.element_rows
        summary_rows = result.summary_rows
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
            captures = [m["capture"] for m in members]
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
                own_consensus, _own_clutter_bins = select_consensus_bin(captures, label=f"swatch={swatch} @ {_angle_label(angle_key)}")
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
                    r = extract_capture_elements(
                        m["capture"], guard_bins=args.guard_bins,
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
    except Exception as exc:                       # noqa: BLE001
        exc._partial_result = result
        raise
    result.collect_capture_warnings()
    for w in result.warnings:
        print(f"  !! {w}")
    return result


def _analysis_main() -> int:
    ap = build_arg_parser()
    args = ap.parse_args()
    if prepare_analysis(args, "material", extract_capture_elements):
        return 0

    try:
        result = analyze_material(args)
    except Exception as exc:                       # noqa: BLE001
        partial = getattr(exc, "result", None)
        if partial is not None and partial.table("angle_gate_candidates"):
            print(f"  (angle-gate candidate table available: "
                  f"{len(partial.table('angle_gate_candidates'))} rows)")
        raise

    # Write whatever's actually available. A baseline-only session (no
    # material swatches shot yet -- e.g. a rig sanity check) is a real,
    # expected use case, not a failure: it should still get its
    # baseline_timeline.csv written so you can check SNR/drift before
    # moving on to swatches. Only the material-specific CSVs are
    # conditional on having material data.
    if result.baseline_timeline_rows:
        n = write_rows(args.baseline_timeline_csv, result.baseline_timeline_rows)
        print(f"\nWrote {args.baseline_timeline_csv} ({n} rows) "
              f"-- plot power_db_mean_all_elements over timestamp to check for baseline drift")

    if not result.element_rows:
        print("\nNo material captures found yet -- nothing to write to "
              f"{args.element_csv} or {args.summary_csv}. That's expected for a "
              "baseline-only session; check the pooled-baseline line above "
              "(SNR, range) before moving on to swatches.")
        return 0

    n = write_rows(args.element_csv, result.element_rows)
    print(f"Wrote {args.element_csv} ({n} rows)")
    n = write_rows(args.summary_csv, result.summary_rows)
    print(f"Wrote {args.summary_csv} ({n} rows)")
    return 0


def main() -> int:
    return run_checked(_analysis_main)


if __name__ == "__main__":
    sys.exit(main())
