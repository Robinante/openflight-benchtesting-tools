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
    """What one capture, on its own, looks like.

    This is the single-capture view Phase 2's Analyze page needs: where the peak
    is, how far above the floor it sits, the full range profile, and the
    per-element complex means at the peak. It is a thin, typed wrapper over
    ``extract_capture_elements`` plus the profile, not a new computation.
    """

    kind: str = "capture"
    capture: object = None
    peak_bin: int = 0
    peak_range_m: float = 0.0
    start_bin: int = 0
    power_profile: object = None        # np.ndarray, linear power per bin
    noise_db: float = 0.0
    snr_db: float = 0.0
    element_mean: object = None         # complex (n_tx, n_rx)
    coherence: object = None            # (n_tx, n_rx)
    n_frames_used: int = 0
    n_loops_total: int = 0

    @property
    def profile_db(self):
        if self.power_profile is None:
            return None
        return 10.0 * np.log10(np.maximum(self.power_profile, 1e-12))

    @property
    def range_axis_m(self):
        if self.power_profile is None or self.capture is None:
            return None
        rb = self.capture.configuration.range_bin_m
        return (self.start_bin + np.arange(self.power_profile.shape[0])) * rb


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
