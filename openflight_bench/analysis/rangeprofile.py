"""Raw-first range data: the full captured profile, with no target decision.

The ordering principle for this module is deliberate:

    show what the radar measured first,
    and what the software thinks it means second.

``RangeProfile`` is produced by averaging every complete frame across the whole
captured window. It never selects a bin, never applies a gate, and nothing in
this module can shrink it. Peak detection and gating live here too, but strictly
as *annotation over* a profile that already exists in full.

    profile = range_profile(capture)      # every captured bin, always
    peaks   = detect_peaks(profile)       # candidates, ranked -- not a verdict
    gate    = Gate.around(profile, 1.5)   # explicit, optional, non-destructive

A ``Gate`` marks a region. It does not filter, mask or replace ``profile.power``
-- ask it for ``profile.within(gate)`` if you want the slice, and the original
stays intact either way.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

M_TO_IN = 39.3700787


# ---------------------------------------------------------------------------
# the profile
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RangeProfile:
    """Mean power per range bin over every complete frame of one capture.

    ``power`` is linear mean power, ``start_bin`` is the global bin index of
    ``power[0]``. Both cover the entire captured window; there is no code path
    that produces a partial RangeProfile.
    """

    start_bin: int
    power: np.ndarray                 # linear, shape (n_bins,)
    range_bin_m: float
    n_frames_used: int = 0
    n_frames_total: int = 0
    source_name: str = ""

    # -- geometry ----------------------------------------------------------
    @property
    def n_bins(self) -> int:
        return int(self.power.shape[0])

    @property
    def bins(self) -> np.ndarray:
        """Global bin indices covered by this profile."""
        return self.start_bin + np.arange(self.n_bins)

    @property
    def range_m(self) -> np.ndarray:
        return self.bins * self.range_bin_m

    @property
    def range_in(self) -> np.ndarray:
        return self.range_m * M_TO_IN

    @property
    def window_bins(self) -> tuple:
        return (self.start_bin, self.start_bin + self.n_bins)

    # -- values ------------------------------------------------------------
    @property
    def power_db(self) -> np.ndarray:
        """10*log10(mean |X|^2) per bin. Incoherent across chirps and RX."""
        return 10.0 * np.log10(np.maximum(self.power, 1e-12))

    def at_bin(self, global_bin: int) -> float:
        i = int(global_bin) - self.start_bin
        if not 0 <= i < self.n_bins:
            raise IndexError(
                f"bin {global_bin} is outside this capture's window "
                f"[{self.start_bin}, {self.start_bin + self.n_bins})")
        return float(self.power[i])

    def local(self, global_bin: int) -> int:
        return int(global_bin) - self.start_bin

    def within(self, gate) -> np.ndarray:
        """A COPY of the power values inside a gate. The profile is untouched."""
        lo, hi = gate.local_slice(self)
        return self.power[lo:hi].copy()

    def nearest_bin(self, range_m: float) -> int:
        return int(round(float(range_m) / self.range_bin_m))

    def __len__(self) -> int:
        return self.n_bins

    def __str__(self) -> str:
        return (f"RangeProfile({self.source_name or 'capture'}: bins "
                f"{self.start_bin}..{self.start_bin + self.n_bins - 1}, "
                f"{self.range_m[0]:.3f}-{self.range_m[-1]:.3f} m, "
                f"{self.n_frames_used}/{self.n_frames_total} frames)")


def range_profile(capture) -> RangeProfile:
    """The full captured range profile. No selection, no gate, no exceptions
    about targets -- if the capture parses, you get every bin it holds."""
    from .elements import capture_power_profile      # local: avoids a cycle

    capture.require_range_snapshot()
    start_bin, power = capture_power_profile(capture)
    integ = capture.integrity
    return RangeProfile(
        start_bin=int(start_bin),
        power=np.asarray(power, dtype=float),
        range_bin_m=capture.configuration.range_bin_m,
        n_frames_used=integ.n_frames_decoded - integ.n_frames_incomplete,
        n_frames_total=integ.declared_frames,
        source_name=capture.source.name,
    )


# ---------------------------------------------------------------------------
# peaks -- candidates, not verdicts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Peak:
    """One local maximum in a profile. An observation, not a target claim."""

    bin: int
    range_m: float
    power_db: float
    prominence_db: float              # above the local floor around it
    rank: int = 0                     # 0 = strongest in this profile

    @property
    def range_in(self) -> float:
        return self.range_m * M_TO_IN

    def __str__(self) -> str:
        return (f"bin {self.bin} @ {self.range_in:.1f} in  {self.power_db:.1f} dB "
                f"(+{self.prominence_db:.1f} dB prominence)")


def detect_peaks(profile: RangeProfile, *, min_prominence_db: float = 3.0,
                 max_peaks: int = 8, min_separation_bins: int = 2) -> list:
    """Every local maximum worth mentioning, strongest first.

    Returns candidates. It does not decide which one is "the" target, and
    nothing downstream is required to use the result. A scene with a trihedral
    at 1.5 m and a stand at 0.9 m yields BOTH.

    ``prominence_db`` is measured against the median of the profile excluding
    all candidate regions, so a second strong return does not inflate the floor
    used to judge the first.
    """
    p = profile.power
    n = p.shape[0]
    if n < 3:
        return []
    db = profile.power_db

    local_max = []
    for i in range(n):
        lo, hi = max(0, i - 1), min(n, i + 2)
        if p[i] == p[lo:hi].max() and (i == 0 or p[i] >= p[i - 1]) and \
           (i == n - 1 or p[i] >= p[i + 1]):
            local_max.append(i)

    floor_db = _floor_excluding(profile, local_max, min_separation_bins)

    cands = []
    for i in local_max:
        prom = float(db[i] - floor_db)
        if prom >= min_prominence_db:
            cands.append((i, prom))
    cands.sort(key=lambda t: -db[t[0]])

    kept, out = [], []
    for i, prom in cands:
        if any(abs(i - j) < min_separation_bins for j in kept):
            continue
        kept.append(i)
        out.append(Peak(bin=profile.start_bin + i,
                        range_m=(profile.start_bin + i) * profile.range_bin_m,
                        power_db=float(db[i]), prominence_db=prom,
                        rank=len(out)))
        if len(out) >= max_peaks:
            break
    return out


def _floor_excluding(profile: RangeProfile, peak_locals, guard: int) -> float:
    """Median power_db of the bins that are not near any candidate peak."""
    n = profile.n_bins
    mask = np.ones(n, dtype=bool)
    for i in peak_locals:
        mask[max(0, i - guard):min(n, i + guard + 1)] = False
    db = profile.power_db
    return float(np.median(db[mask])) if mask.any() else float(np.median(db))


# ---------------------------------------------------------------------------
# gates -- explicit, optional, non-destructive
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Gate:
    """An explicit region of interest, in global bin indices, inclusive-exclusive.

    A Gate is a *label on* a profile. It never modifies, filters or replaces the
    profile it is applied to: ``profile.power`` is identical before and after,
    and two different gates over the same profile see the same underlying data.
    """

    lo_bin: int
    hi_bin: int                       # exclusive
    label: str = ""
    origin: str = "explicit"          # "explicit" | "around" | "peak"

    def __post_init__(self):
        if self.hi_bin <= self.lo_bin:
            raise ValueError(f"empty gate: [{self.lo_bin}, {self.hi_bin})")

    # -- constructors ------------------------------------------------------
    @classmethod
    def around(cls, profile: RangeProfile, range_m: float, *, half_width_bins: int = 2,
               label: str = "") -> "Gate":
        """A gate centred on a physical range, e.g. where you put the trihedral."""
        c = profile.nearest_bin(range_m)
        return cls(c - half_width_bins, c + half_width_bins + 1,
                   label or f"{range_m:.3f} m", "around")

    @classmethod
    def around_bin(cls, center_bin: int, *, half_width_bins: int = 2,
                   label: str = "") -> "Gate":
        return cls(int(center_bin) - half_width_bins,
                   int(center_bin) + half_width_bins + 1,
                   label or f"bin {center_bin}", "around")

    @classmethod
    def from_peak(cls, peak: Peak, *, half_width_bins: int = 2) -> "Gate":
        return cls(peak.bin - half_width_bins, peak.bin + half_width_bins + 1,
                   f"peak bin {peak.bin}", "peak")

    # -- geometry ----------------------------------------------------------
    @property
    def bins(self) -> np.ndarray:
        return np.arange(self.lo_bin, self.hi_bin)

    @property
    def center_bin(self) -> int:
        return (self.lo_bin + self.hi_bin - 1) // 2

    @property
    def n_bins(self) -> int:
        return self.hi_bin - self.lo_bin

    def range_bounds_m(self, profile: RangeProfile) -> tuple:
        return (self.lo_bin * profile.range_bin_m,
                (self.hi_bin - 1) * profile.range_bin_m)

    def local_slice(self, profile: RangeProfile) -> tuple:
        """Clamped (lo, hi) indices into profile.power. Never raises; a gate
        partly outside the window simply covers less of it."""
        lo = max(0, self.lo_bin - profile.start_bin)
        hi = min(profile.n_bins, self.hi_bin - profile.start_bin)
        return (lo, max(lo, hi))

    def covers(self, profile: RangeProfile) -> bool:
        lo, hi = self.local_slice(profile)
        return hi > lo

    def contains_bin(self, global_bin: int) -> bool:
        return self.lo_bin <= int(global_bin) < self.hi_bin

    def __str__(self) -> str:
        return f"Gate[{self.lo_bin}, {self.hi_bin}) {self.label}".rstrip()


def pooled_profile(captures, *, label: str = "") -> RangeProfile:
    """Sum the profiles of several captures of the same scene into one.

    Pooling raises the effective SNR of the whole profile without choosing a
    target, which is what makes a group comparison meaningful. Every capture
    must share the same window; mixing windows is an error, not something to
    silently reconcile.
    """
    from .capture import as_capture

    caps = [as_capture(c) for c in captures]
    if not caps:
        raise ValueError("no captures given to pool")
    profiles = [range_profile(c) for c in caps]
    first = profiles[0]
    for pr in profiles[1:]:
        if pr.start_bin != first.start_bin or pr.n_bins != first.n_bins:
            raise ValueError(
                f"{pr.source_name}: window {pr.window_bins} differs from "
                f"{first.source_name}'s {first.window_bins} -- these captures "
                "cannot be pooled; check they used the same .cfg")
    stacked = np.stack([pr.power for pr in profiles], axis=0)
    return RangeProfile(
        start_bin=first.start_bin,
        power=stacked.mean(axis=0),
        range_bin_m=first.range_bin_m,
        n_frames_used=sum(pr.n_frames_used for pr in profiles),
        n_frames_total=sum(pr.n_frames_total for pr in profiles),
        source_name=label or f"pooled({len(profiles)} captures)",
    )
