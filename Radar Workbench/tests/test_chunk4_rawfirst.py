"""Chunk 4: raw-first semantics, honest metrics, explicit target regions.

The synthetic tests build RangeProfiles directly with known values, so the
expected answers are exact arithmetic rather than "whatever the code produced".
The capture-backed tests use real dumps and skip cleanly when absent.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openflight_bench.analysis import (  # noqa: E402
    Gate, RangeProfile, analyze_capture, compare_region, detect_peaks,
    load_capture, metrics, range_profile,
)

FIXTURES = Path(os.environ.get("BENCH_FIXTURES", ROOT / "tests" / "fixtures"))
MATERIAL_DIR = Path(os.environ.get("BENCH_MATERIAL_DIR", FIXTURES / "material_subset"))
needs_material = pytest.mark.skipif(
    not list(MATERIAL_DIR.glob("*.l3dump")) if MATERIAL_DIR.exists() else True,
    reason=f"no captures in {MATERIAL_DIR}")

RB = 0.0468425715625          # meters per bin, the project's standard chirp


# ---------------------------------------------------------------------------
# synthetic scenes with exactly known answers
# ---------------------------------------------------------------------------

def make_profile(values, *, start_bin=0, name="synthetic") -> RangeProfile:
    return RangeProfile(start_bin=start_bin, power=np.asarray(values, float),
                        range_bin_m=RB, n_frames_used=1, n_frames_total=1,
                        source_name=name)


def two_target_scene(target_lin=100.0, clutter_lin=50.0, floor_lin=1.0,
                     n=41, target_bin=30, clutter_bin=9) -> RangeProfile:
    """Floor everywhere, a strong target, and a second real return elsewhere.

    Mirrors the case that motivated this chunk: a trihedral at ~1.4 m with a
    stand/near-field return at ~0.4 m that must stay visible.
    """
    p = np.full(n, floor_lin, float)
    p[target_bin] = target_lin
    p[clutter_bin] = clutter_lin
    return make_profile(p, name="two_target")


# --- 1. full-range preservation --------------------------------------------

def test_profile_keeps_every_captured_bin_synthetic():
    prof = two_target_scene()
    assert prof.n_bins == 41
    assert prof.window_bins == (0, 41)
    assert len(prof.power) == 41
    assert prof.power_db.shape == (41,)


@needs_material
def test_profile_keeps_every_captured_bin_real():
    path = sorted(MATERIAL_DIR.glob("*.l3dump"))[0]
    cap = load_capture(path)
    prof = range_profile(cap)
    assert prof.n_bins == cap.configuration.window_bin_count
    assert prof.start_bin == cap.configuration.window_start_bin
    a = analyze_capture(cap)
    assert a.profile is not None
    assert a.n_bins == prof.n_bins
    assert np.array_equal(a.profile.power, prof.power)


# --- 2. gate independence ---------------------------------------------------

def test_two_different_gates_do_not_alter_the_profile():
    prof = two_target_scene()
    before = prof.power.copy()

    g1 = Gate.around_bin(30, half_width_bins=2)
    g2 = Gate.around_bin(9, half_width_bins=4)
    s1, s2 = prof.within(g1), prof.within(g2)

    assert np.array_equal(prof.power, before), "the gate mutated the profile"
    assert s1.size == 5 and s2.size == 9
    # the slices are copies: writing to one must not touch the profile
    s1[:] = -999.0
    assert np.array_equal(prof.power, before)


@needs_material
def test_gate_choice_does_not_change_underlying_data_real():
    cap = load_capture(sorted(MATERIAL_DIR.glob("*.l3dump"))[0])
    base = analyze_capture(cap)
    g1 = Gate.around(base.profile, 1.4, half_width_bins=2)
    g2 = Gate.around(base.profile, 0.6, half_width_bins=6)
    a1 = analyze_capture(cap, gate=g1)
    a2 = analyze_capture(cap, gate=g2)

    assert np.array_equal(a1.profile.power, base.profile.power)
    assert np.array_equal(a2.profile.power, base.profile.power)
    assert a1.n_bins == a2.n_bins == base.n_bins
    # the gates DO change the gate metrics -- that is the only thing they change
    assert a1.gate_metrics["gate_lo_bin"] != a2.gate_metrics["gate_lo_bin"]
    assert a1.noise_floor_db == pytest.approx(a2.noise_floor_db)
    assert a1.peak_to_floor_db == pytest.approx(a2.peak_to_floor_db)


# --- 3. no-gate behavior ----------------------------------------------------

@needs_material
def test_full_analysis_works_with_no_gate_and_no_annotation():
    cap = load_capture(sorted(MATERIAL_DIR.glob("*.l3dump"))[0])
    a = analyze_capture(cap, annotate=False)
    assert a.profile is not None and a.n_bins > 0
    assert a.gate is None
    assert a.peaks == []
    assert a.gate_metrics == {}


@needs_material
def test_plot_renders_without_any_gate(tmp_path):
    from openflight_bench.analysis import plots
    cap = load_capture(sorted(MATERIAL_DIR.glob("*.l3dump"))[0])
    a = analyze_capture(cap)
    out = plots.save(plots.range_profile(a), tmp_path / "full.png")
    assert Path(out).stat().st_size > 5000


@needs_material
def test_annotation_failure_does_not_cost_the_profile():
    """A target-selection failure must degrade to annotation_error, not an
    exception that hides the data."""
    cap = load_capture(sorted(MATERIAL_DIR.glob("*.l3dump"))[0])
    prof = range_profile(cap)
    outside = prof.start_bin + prof.n_bins + 50          # nonsense forced bin
    a = analyze_capture(cap, forced_peak_bin=outside)
    assert a.profile is not None and a.n_bins == prof.n_bins
    assert a.annotation_error is not None
    assert a.peak_bin is None
    assert np.array_equal(a.profile.power, prof.power)


# --- 4. unexpected-return preservation --------------------------------------

def test_return_outside_the_gate_stays_visible():
    prof = two_target_scene(target_lin=100.0, clutter_lin=50.0)
    gate = Gate.around_bin(30, half_width_bins=2)          # only the target
    assert not gate.contains_bin(9)

    # the clutter bin is still in the profile, at its full value
    assert prof.at_bin(9) == pytest.approx(50.0)
    # and detect_peaks reports BOTH returns, not just the strongest
    peaks = detect_peaks(prof, min_prominence_db=3.0)
    bins = {pk.bin for pk in peaks}
    assert 30 in bins and 9 in bins, f"expected both returns, got {sorted(bins)}"
    strongest = min(peaks, key=lambda pk: pk.rank)
    assert strongest.bin == 30
    assert peaks[1].bin == 9


# --- 5. SNR / floor correctness on known data -------------------------------

def test_peak_to_floor_is_exact_on_synthetic_data():
    """floor 1.0 (0 dB), target 100.0 (20 dB) -> exactly 20 dB above floor."""
    prof = two_target_scene(target_lin=100.0, clutter_lin=50.0, floor_lin=1.0)
    peaks = detect_peaks(prof)
    floor = metrics.noise_floor_db(prof, peaks=peaks, guard_bins=2)
    assert floor == pytest.approx(0.0, abs=1e-9)
    assert metrics.peak_to_floor_db(prof, peaks=peaks) == pytest.approx(20.0, abs=1e-9)


def test_floor_excludes_other_real_returns():
    """A second return must not be counted as noise.

    Half the profile is raised to 10.0 (10 dB) as broad clutter. Excluding only
    the chosen peak leaves the median sitting on the clutter; excluding every
    detected return puts it back on the true floor.
    """
    n = 41
    p = np.full(n, 1.0)
    p[0:20] = 10.0                      # broad clutter, more than half the bins
    p[30] = 100.0                       # the target
    prof = make_profile(p)

    contaminated = metrics.noise_floor_db(prof, exclude_bins=[30], guard_bins=2)
    assert contaminated == pytest.approx(10.0, abs=1e-9), "clutter should dominate"

    clean = metrics.noise_floor_db(
        prof, exclude_bins=[30] + list(range(0, 20)), guard_bins=0)
    assert clean == pytest.approx(0.0, abs=1e-9)
    assert contaminated - clean == pytest.approx(10.0, abs=1e-9)


def test_legacy_snr_is_a_mixed_estimator_and_is_labelled_so():
    """Reproduce the defect exactly: a coherent numerator over an incoherent
    denominator understates by the coherent loss."""
    prof = two_target_scene(target_lin=100.0, clutter_lin=1.0, floor_lin=1.0)
    # element means whose coherent power is 10x BELOW the profile power at the bin
    em = np.full((3, 4), np.sqrt(10.0), dtype=complex)     # |.|^2 = 10 per element
    legacy = metrics.legacy_snr_db(em, prof, 30, guard_bins=2)
    honest = metrics.peak_to_floor_db(prof, peaks=detect_peaks(prof))
    gain = metrics.coherent_gain_db(em, prof, 30)

    assert honest == pytest.approx(20.0, abs=1e-9)
    assert legacy == pytest.approx(10.0, abs=1e-9)
    assert gain == pytest.approx(-10.0, abs=1e-9)
    # the identity that explains every observed discrepancy
    assert legacy == pytest.approx(honest + gain, abs=1e-9)


@needs_material
def test_legacy_snr_identity_holds_on_real_data():
    """legacy_snr == peak_to_floor + coherent_gain + (clean_floor - legacy_floor)."""
    cap = load_capture(sorted(MATERIAL_DIR.glob("*.l3dump"))[0])
    a = analyze_capture(cap)
    if a.peak_bin is None:
        pytest.skip("annotation unavailable on this capture")
    legacy_floor = a.noise_db
    identity = (a.peak_to_floor_db + a.coherent_gain_db
                + (a.noise_floor_db - legacy_floor))
    assert a.snr_db == pytest.approx(identity, abs=1e-6)
    assert a.snr_db != pytest.approx(a.peak_to_floor_db, abs=0.5), (
        "the two quantities should differ materially on real data")


# --- 6. material comparison correctness -------------------------------------

def test_region_comparison_is_exact_and_ignores_outside_clutter():
    """Target attenuated 3.0103 dB, clutter AMPLIFIED by the same factor.

    A correct target-region comparison reports the target's -3.0103 dB and is
    completely unaffected by what happened at the clutter bin.
    """
    ref = two_target_scene(target_lin=100.0, clutter_lin=50.0, floor_lin=1.0)
    meas = ref.power.copy()
    meas[30] = 50.0            # target halved   -> -3.0103 dB
    meas[9] = 100.0            # clutter doubled -> +3.0103 dB
    meas = make_profile(meas, name="measured")

    gate = Gate.around_bin(30, half_width_bins=0)
    c = compare_region(ref, meas, gate)
    assert c.delta_db == pytest.approx(-10.0 * np.log10(2.0), abs=1e-9)
    assert c.attenuation_db == pytest.approx(10.0 * np.log10(2.0), abs=1e-9)
    assert c.n_bins == 1
    assert c.method == "mean_power_in_gate"

    # the clutter gate sees the opposite change, proving the regions are independent
    c2 = compare_region(ref, meas, Gate.around_bin(9, half_width_bins=0))
    assert c2.delta_db == pytest.approx(+10.0 * np.log10(2.0), abs=1e-9)


def test_whole_range_mean_would_have_been_wrong():
    """Demonstrates why a whole-profile statistic must not be called attenuation."""
    ref = two_target_scene(target_lin=100.0, clutter_lin=50.0, floor_lin=1.0)
    meas_arr = ref.power.copy()
    meas_arr[30] = 50.0
    meas_arr[9] = 100.0
    meas = make_profile(meas_arr)

    target_delta = compare_region(ref, meas, Gate.around_bin(30, half_width_bins=0)).delta_db
    whole = metrics.mean_power_db(meas) - metrics.mean_power_db(ref)

    assert target_delta == pytest.approx(-3.0103, abs=1e-3)
    assert whole == pytest.approx(0.0, abs=1e-9), (
        "the equal-and-opposite clutter change cancels in a whole-range mean, "
        "hiding a real 3 dB target loss entirely")


def test_compare_region_refuses_to_run_without_a_gate():
    ref = two_target_scene()
    with pytest.raises(ValueError, match="requires an explicit gate"):
        compare_region(ref, ref, None)


def test_gate_partly_outside_the_window_clamps_rather_than_raising():
    prof = two_target_scene(n=41)
    g = Gate(-5, 3)
    lo, hi = g.local_slice(prof)
    assert (lo, hi) == (0, 3)
    assert prof.within(g).size == 3
    assert g.covers(prof)


@needs_material
def test_pooled_profile_matches_manual_mean():
    from openflight_bench.analysis import pooled_profile
    paths = sorted(MATERIAL_DIR.glob("*.l3dump"))[:3]
    profs = [range_profile(load_capture(p)) for p in paths]
    pooled = pooled_profile(paths)
    manual = np.mean([pr.power for pr in profs], axis=0)
    assert np.allclose(pooled.power, manual, rtol=0, atol=0)
    assert pooled.n_bins == profs[0].n_bins
