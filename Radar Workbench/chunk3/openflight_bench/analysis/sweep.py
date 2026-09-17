"""Sweep analysis: per-(TX, RX) amplitude and phase versus mechanical angle.

Everything below the option plumbing is the body of the original
analyze_sweep.py ``main()``, unchanged. It was split in three:

    build_arg_parser()  -- the argparse block, verbatim
    analyze_sweep(args) -- the computation, verbatim, now RETURNING its rows
    main()              -- CLI glue: parse, analyze, write CSVs

``analyze_sweep`` takes anything with the same attributes as the argparse
Namespace, so ``SweepOptions`` (a plain dataclass) works and the body needed no
edits at all. That is what makes the analysis callable from a future GUI.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .capture import ConfigurationPolicy, load_captures
from .elements import (
    combine_repeats, capture_power_profile, extract_capture_elements, pooled_profile_peak,
)
from .export import write_rows
from .ild1 import (
    DEFAULT_NUM_ADC_SAMPLES, DEFAULT_SAMPLE_RATE_KSPS, DEFAULT_SLOPE_MHZ_PER_US,
    ShortHeader, range_bin_meters,
)
from .loaders import group_by_angle, load_sweep_files
from .progress import as_progress
from .results import SweepResult


@dataclass
class SweepOptions:
    """Attribute-compatible stand-in for the sweep argparse Namespace."""
    capture_dir: Path
    range_bin_m: float | None = None
    num_adc_samples: int = DEFAULT_NUM_ADC_SAMPLES
    slope_mhz_per_us: float = DEFAULT_SLOPE_MHZ_PER_US
    sample_rate_ksps: float = DEFAULT_SAMPLE_RATE_KSPS
    guard_bins: int = 2
    reference_tx: int = 0
    reference_rx: int = 0
    element_csv: Path = field(default_factory=lambda: Path("capture_csv/sweep_elements.csv"))
    summary_csv: Path = field(default_factory=lambda: Path("capture_csv/sweep_summary.csv"))

    def __post_init__(self):
        for f in ("capture_dir", "element_csv", "summary_csv"):
            v = getattr(self, f, None)
            if isinstance(v, str):
                setattr(self, f, Path(v))


def build_arg_parser(description: str | None = None) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=description or __doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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
    return ap


def analyze_sweep(args, *, progress=None) -> SweepResult:
    """Run the sweep analysis. ``args`` may be a SweepOptions or an argparse
    Namespace. Returns the rows instead of writing them.

    ``progress`` receives every line the analysis would have printed. It
    defaults to the builtin ``print``, so the CLI stays byte-identical; a GUI
    passes its own sink and nothing reaches stdout.
    """
    print = as_progress(progress)

    policy = ConfigurationPolicy(
        explicit_range_bin_m=args.range_bin_m,
        num_adc_samples=args.num_adc_samples,
        slope_mhz_per_us=args.slope_mhz_per_us,
        sample_rate_ksps=args.sample_rate_ksps,
    )

    entries = load_sweep_files(args.capture_dir)
    groups = group_by_angle(entries)

    result = SweepResult()
    element_rows = result.element_rows
    summary_rows = result.summary_rows

    for (axis, angle_deg), members in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        captures = load_captures(members, policy=policy)
        result.captures.extend(captures)
        try:
            consensus_bin = pooled_profile_peak(captures, guard_bins=args.guard_bins)
        except ValueError as exc:
            print(f"  !! axis={axis} angle={angle_deg}: {exc} -- skipped")
            continue

        per_file_results = []
        for cap in captures:
            try:
                r = extract_capture_elements(
                    cap, guard_bins=args.guard_bins, forced_peak_bin=consensus_bin,
                )
            except (ShortHeader, ValueError) as exc:
                print(f"  !! {cap.source.name}: {exc}")
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
    result.collect_capture_warnings()
    for w in result.warnings:
        print(f"  !! {w}")
    return result


def main(argv: list[str] | None = None) -> int:
    ap = build_arg_parser()
    args = ap.parse_args(argv)
    result = analyze_sweep(args)

    if not result.element_rows:
        print("No data to write.")
        return 1

    n = write_rows(args.element_csv, result.element_rows)
    print(f"\nWrote {args.element_csv} ({n} rows)")
    n = write_rows(args.summary_csv, result.summary_rows)
    print(f"Wrote {args.summary_csv} ({n} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
