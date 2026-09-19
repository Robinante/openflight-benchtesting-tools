"""Honest definitions for the numbers this project reports.

Every function states what data it consumes, the formula, and the units. Where
an existing quantity turned out to be mislabelled, the corrected metric is added
under an accurate name and the old one is kept, unchanged, under a name that
says what it actually is. Nothing here silently redefines a number that has
already been written into a CSV.

Two averaging conventions appear throughout and must not be mixed casually:

    INCOHERENT   mean(|X|^2) over chirps/loops and RX -- magnitudes only.
                 This is what the range profile holds.
    COHERENT     |mean(X)|^2 where the mean is over loops, keeping phase.
                 Equal to the incoherent value only when the phase is perfectly
                 stable; otherwise strictly smaller. This is what
                 ``element_mean`` holds.

A ratio built from a coherent numerator and an incoherent denominator is not a
signal-to-noise ratio, because the two halves have different processing gain.
That is exactly the defect documented in ``legacy_snr_db`` below.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


# ---------------------------------------------------------------------------
# level / power
# ---------------------------------------------------------------------------

def power_db(linear_power) -> float:
    """10*log10(P). Input is mean |X|^2 in ADC counts squared.
    Units: dB relative to 1 ADC count squared. NOT dBm, NOT dBFS."""
    return float(10.0 * np.log10(np.maximum(np.asarray(linear_power, float), 1e-12)))


def level_db(amplitude) -> float:
    """20*log10(|X|). Units: dB relative to 1 ADC count.
    The int16 ceiling is 20*log10(32768) = 90.31 dB."""
    return float(20.0 * np.log10(np.maximum(np.abs(np.asarray(amplitude, float)), 1e-12)))


def peak_power_db(profile) -> float:
    """Power at the strongest bin of a RangeProfile.
    Data: the incoherent range profile. Formula: max(10*log10(power)). dB."""
    return float(profile.power_db.max())


def mean_power_db(profile, gate=None) -> float:
    """Mean power over a region, or the whole profile when gate is None.

    Averages in LINEAR power then converts, which is the correct order; taking
    the mean of dB values would be a geometric mean and would under-read.
    """
    p = profile.power if gate is None else profile.within(gate)
    return power_db(np.mean(p))


# ---------------------------------------------------------------------------
# noise floor
# ---------------------------------------------------------------------------

def noise_floor_db(profile, *, exclude_bins=(), guard_bins: int = 2,
                   peaks=()) -> float:
    """Median power of the bins that hold no detected return.

    Data: the incoherent range profile.
    Formula: median(10*log10(power[mask])) where mask clears +/- guard_bins
             around every bin in ``exclude_bins`` and around every peak in
             ``peaks``.
    Units: dB re 1 ADC count squared.

    This is the corrected floor. The Chunk 3 floor cleared only the single
    chosen peak, so any OTHER real return -- a stand, near-field leakage, a
    second target -- was counted as noise and inflated it. Pass the output of
    ``detect_peaks`` to exclude all of them.

    Note the median is taken over dB values here, deliberately: the median is
    order-preserving, so median(dB) == dB(median), and doing it in dB keeps the
    result robust against a few very large linear outliers.
    """
    n = profile.n_bins
    mask = np.ones(n, dtype=bool)
    targets = list(exclude_bins) + [pk.bin for pk in peaks]
    for gb in targets:
        i = profile.local(int(gb))
        mask[max(0, i - guard_bins):min(n, i + guard_bins + 1)] = False
    db = profile.power_db
    return float(np.median(db[mask])) if mask.any() else float(np.median(db))


def peak_to_floor_db(profile, *, peaks=(), guard_bins: int = 2,
                     at_bin: int | None = None) -> float:
    """Strongest (or specified) bin above the no-return floor.

    Data: the incoherent range profile, BOTH sides.
    Formula: power_db(at_bin or argmax) - noise_floor_db(profile, peaks=peaks).
    Units: dB.

    Both halves use the same estimator, which is what makes this quantity a
    meaningful ratio. This is the number to quote when asked "how far above the
    floor is that return".
    """
    floor = noise_floor_db(profile, peaks=peaks, guard_bins=guard_bins)
    pk = (profile.power_db[profile.local(at_bin)] if at_bin is not None
          else profile.power_db.max())
    return float(pk - floor)


# ---------------------------------------------------------------------------
# the legacy quantity, named for what it is
# ---------------------------------------------------------------------------

def legacy_snr_db(coherent_element_mean, profile, peak_bin: int, *,
                  guard_bins: int = 2, extra_exclude=()) -> float:
    """Reproduces the number Chunk 3 (and the original scripts) call ``snr_db``.

    Formula, exactly as implemented in ``extract_capture_elements``:

        numerator   = 10*log10( mean_over_elements( |coherent_mean_over_loops(X)|^2 ) )
        denominator = 10*log10( median_over_bins( mean_over_chirps,rx(|X|^2) ) )
                      with only peak_bin +/- guard_bins (and extra_exclude) removed
        value       = numerator - denominator

    **This is not a signal-to-noise ratio.** Two independent defects:

    1. The numerator is COHERENT and the denominator INCOHERENT. Coherent
       averaging over N loops suppresses phase-incoherent content by up to
       10*log10(N); the denominator gets no such suppression. The two sides are
       not measured the same way, so their difference is not a ratio of like
       quantities. On a stable target the numerator under-reads relative to the
       profile peak, which is why a clearly-visible return can report a negative
       "SNR" -- observed at -1.4 dB on a 6 Sep baseline whose profile peak sits
       ~7 dB above its own floor.
    2. The denominator is contaminated. Every bin except the chosen peak counts
       as noise, including other genuine returns.

    Kept verbatim so historical CSVs remain reproducible and comparable. For a
    real ratio use ``peak_to_floor_db``; for the coherent-vs-incoherent question
    use ``coherent_gain_db``.
    """
    n = profile.n_bins
    mask = np.ones(n, dtype=bool)
    i = profile.local(int(peak_bin))
    mask[max(0, i - guard_bins):min(n, i + guard_bins + 1)] = False
    for gb in extra_exclude:
        j = profile.local(int(gb))
        if 0 <= j < n:
            mask[j] = False
    samples = profile.power[mask] if mask.any() else profile.power
    floor = max(float(np.median(samples)), 1e-12)
    num = float(np.mean(np.abs(np.asarray(coherent_element_mean)) ** 2))
    return power_db(num) - power_db(floor)


def coherent_gain_db(coherent_element_mean, profile, peak_bin: int) -> float:
    """How much coherent averaging changed the estimate at the target bin.

    Formula: power_db(mean_over_elements(|coherent mean|^2))
             - power_db(profile power at peak_bin)
    Units: dB. Negative means phase varied across loops, so the coherent
    average is smaller than the incoherent one -- i.e. the return is not
    perfectly phase-stable, or the bin holds more than one scatterer.
    """
    num = float(np.mean(np.abs(np.asarray(coherent_element_mean)) ** 2))
    return power_db(num) - power_db(profile.at_bin(peak_bin))


# ---------------------------------------------------------------------------
# region comparison (material)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RegionComparison:
    """A vs B over one explicit region of the range profile."""

    gate_lo_bin: int
    gate_hi_bin: int
    gate_label: str
    reference_db: float
    measured_db: float
    delta_db: float                   # measured - reference; negative = loss
    n_bins: int
    method: str                       # "mean_power_in_gate" | "peak_in_gate"

    @property
    def attenuation_db(self) -> float:
        """Positive number describing loss, for readability. -delta_db."""
        return -self.delta_db


def compare_region(reference_profile, measured_profile, gate, *,
                   method: str = "mean_power_in_gate") -> RegionComparison:
    """Compare two profiles over one explicit gate.

    Data: linear mean power inside the gate, from both profiles. Nothing
    outside the gate contributes.
    Formula (``mean_power_in_gate``):
        delta_db = 10*log10(mean(measured[gate])) - 10*log10(mean(reference[gate]))
    Formula (``peak_in_gate``): the same with max() instead of mean().
    Units: dB. Negative delta = the measured profile is weaker = attenuation.

    The gate is required. There is deliberately no whole-profile default: a
    whole-range mean mixes the target with clutter, near-field leakage and
    noise, and calling that "material attenuation" would not be physically
    justified.
    """
    if gate is None:
        raise ValueError(
            "compare_region() requires an explicit gate. A whole-range mean is "
            "not material attenuation -- see docs/ANALYSIS_METRICS.md.")
    ref_vals = reference_profile.within(gate)
    meas_vals = measured_profile.within(gate)
    if ref_vals.size == 0 or meas_vals.size == 0:
        raise ValueError(f"{gate} does not overlap one of the profiles")
    agg = np.max if method == "peak_in_gate" else np.mean
    ref_db, meas_db = power_db(agg(ref_vals)), power_db(agg(meas_vals))
    lo, hi = gate.range_bounds_m(reference_profile)
    return RegionComparison(
        gate_lo_bin=gate.lo_bin, gate_hi_bin=gate.hi_bin,
        gate_label=gate.label or f"{lo:.3f}-{hi:.3f} m",
        reference_db=ref_db, measured_db=meas_db,
        delta_db=float(meas_db - ref_db),
        n_bins=int(min(ref_vals.size, meas_vals.size)), method=method,
    )


# ---------------------------------------------------------------------------
# element metrics
# ---------------------------------------------------------------------------

def element_level_db(element_mean) -> np.ndarray:
    """Per-(TX,RX) level from the coherent mean. 20*log10(|X|), dB re 1 count."""
    return 20.0 * np.log10(np.maximum(np.abs(element_mean), 1e-12))


def element_phase_deg(element_mean) -> np.ndarray:
    """Per-(TX,RX) phase of the coherent mean, degrees in (-180, 180]."""
    return np.degrees(np.angle(element_mean))


def element_coherence(samples) -> np.ndarray:
    """|mean(X)| / mean(|X|) over the averaged axis. Dimensionless, 0..1.

    1.0 means every averaged sample had identical phase. Low values mean the
    averaging is destroying signal, which invalidates any coherent level taken
    from the same data.
    """
    m = np.mean(samples, axis=0)
    mag = np.mean(np.abs(samples), axis=0)
    return np.divide(np.abs(m), mag, out=np.full_like(mag, np.nan), where=mag > 0)


def phase_shift_deg(measured_element_mean, reference_element_mean) -> np.ndarray:
    """Per-element phase change vs a reference, wrapped to (-180, 180].

    Formula: angle(measured * conj(reference)) in degrees. Computed on the
    complex product rather than by subtracting two angles, so wrapping is
    handled correctly.
    """
    return np.degrees(np.angle(np.asarray(measured_element_mean)
                               * np.conj(np.asarray(reference_element_mean))))
