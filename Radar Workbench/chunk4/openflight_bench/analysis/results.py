"""Normalized analysis results.

Before Chunk 2 each analysis returned its own ad-hoc container: one had
``element_rows``/``summary_rows``, the other added ``baseline_timeline_rows``,
and nothing else could be written against them generically. A GUI page, a JSON
exporter or a test would have had to special-case each analysis.

Everything now derives from :class:`AnalysisResult`, which carries the same
three things regardless of which analysis produced it:

    result.kind        "capture" | "sweep" | "material_comparison"
    result.tables      {table_name: [row dict, ...]}  -- the CSV-shaped output
    result.warnings    [str, ...]                     -- anything worth surfacing

so a caller can do::

    for name, rows in result.tables.items():
        write_rows(out_dir / f"{name}.csv", rows)

without knowing what ran. The old attribute names survive as properties over
``tables``, so existing code and the regression tests are unaffected and the row
dicts themselves are untouched -- same keys, same order, same values.

``RadarHealthResult`` from the roadmap is deliberately absent: there are no
health checks to put in it yet, and an empty shell would just be something else
to migrate later.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class AnalysisResult:
    """Base for every analysis output."""

    kind: str = "analysis"
    tables: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    captures: list = field(default_factory=list, repr=False)

    # -- uniform access ----------------------------------------------------
    def table(self, name: str) -> list:
        """Rows for one named table, or [] if this analysis produced none."""
        return self.tables.get(name, [])

    def table_names(self) -> list:
        return [n for n, rows in self.tables.items() if rows]

    def row_counts(self) -> dict:
        return {n: len(rows) for n, rows in self.tables.items()}

    def add_warning(self, message: str) -> None:
        if message and message not in self.warnings:
            self.warnings.append(message)

    def collect_capture_warnings(self) -> None:
        """Pull any per-capture warnings (e.g. a range_bin_m disagreement) up
        to the result, so one place shows everything worth telling the user."""
        for cap in self.captures:
            for w in getattr(cap, "warnings", ()):
                self.add_warning(w)

    def __bool__(self) -> bool:
        """True when ANY table has rows -- including angle_gate_candidates from
        a run whose bin selection failed. Callers wanting "did the primary
        analysis produce output" should test the specific rows they need, which
        is what both CLIs do."""
        return any(self.tables.values())

    def __str__(self) -> str:
        counts = ", ".join(f"{n}={c}" for n, c in self.row_counts().items())
        warn = f", {len(self.warnings)} warning(s)" if self.warnings else ""
        return f"<{self.kind} {counts}{warn}>"


@dataclass
class CaptureAnalysis(AnalysisResult):
    """What one capture looks like -- raw first, interpretation second.

    ``profile`` is the full captured range profile and is ALWAYS present when
    the capture parsed. Everything below it is annotation: peaks are candidates,
    the gate is whatever the caller explicitly asked for, and the target-derived
    metrics are ``None`` when annotation could not be computed. A failure to
    pick a target never costs you the profile -- see ``annotation_error``.
    """

    kind: str = "capture"
    capture: object = None

    # ---- raw, always populated -------------------------------------------
    profile: object = None            # RangeProfile over every captured bin

    # ---- annotation, may be absent ---------------------------------------
    peaks: list = field(default_factory=list)      # detect_peaks() candidates
    gate: object = None                            # explicit Gate, or None
    annotation_error: str | None = None            # why the target metrics are None

    peak_bin: int | None = 0
    peak_range_m: float | None = 0.0
    start_bin: int = 0
    power_profile: object = None      # kept: alias of profile.power
    noise_db: float | None = 0.0      # legacy contaminated floor
    snr_db: float | None = 0.0        # legacy mixed-estimator quantity
    element_mean: object = None
    coherence: object = None
    n_frames_used: int = 0
    n_loops_total: int = 0

    # ---- corrected metrics (Chunk 4) --------------------------------------
    noise_floor_db: float | None = None     # floor with ALL detected peaks excluded
    peak_to_floor_db: float | None = None   # same estimator both sides
    coherent_gain_db: float | None = None   # coherent vs incoherent at the peak
    gate_metrics: dict = field(default_factory=dict)

    # ---- convenience ------------------------------------------------------
    @property
    def profile_db(self):
        if self.profile is not None:
            return self.profile.power_db
        if self.power_profile is None:
            return None
        return 10.0 * np.log10(np.maximum(self.power_profile, 1e-12))

    @property
    def range_axis_m(self):
        if self.profile is not None:
            return self.profile.range_m
        if self.power_profile is None or self.capture is None:
            return None
        rb = self.capture.configuration.range_bin_m
        return (self.start_bin + np.arange(self.power_profile.shape[0])) * rb

    @property
    def has_annotation(self) -> bool:
        return self.annotation_error is None and self.peak_bin is not None

    @property
    def n_bins(self) -> int:
        return 0 if self.profile is None else self.profile.n_bins

    def describe_peaks(self) -> str:
        if not self.peaks:
            return "no peaks above the prominence threshold"
        return "; ".join(str(pk) for pk in self.peaks)


@dataclass
class SweepResult(AnalysisResult):
    """Per-(TX, RX) amplitude and phase versus mechanical angle."""

    kind: str = "sweep"

    @property
    def element_rows(self) -> list:
        return self.tables.setdefault("sweep_elements", [])

    @property
    def summary_rows(self) -> list:
        return self.tables.setdefault("sweep_summary", [])


@dataclass
class MaterialComparisonResult(AnalysisResult):
    """Attenuation and phase shift of a swatch against a pooled baseline."""

    kind: str = "material_comparison"

    @property
    def element_rows(self) -> list:
        return self.tables.setdefault("material_elements", [])

    @property
    def summary_rows(self) -> list:
        return self.tables.setdefault("material_summary", [])

    @property
    def baseline_timeline_rows(self) -> list:
        return self.tables.setdefault("baseline_timeline", [])
