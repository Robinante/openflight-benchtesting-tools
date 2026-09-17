"""Plotting for bench analysis results.

Nothing here computes anything -- every number comes from a Capture or an
AnalysisResult that was produced elsewhere. That separation is the point: the
same figure functions serve the CLI, the workbench and a notebook, and a plot
can never disagree with the CSV beside it.

matplotlib is imported lazily so the analysis layer still works without it.
Each function returns a Figure; the caller decides whether to show it, save it,
or hand it to Streamlit.

Terminology is fixed deliberately, matching the bench write-ups:
  level (dB)   20*log10(mean |X|) in ADC counts -- NOT dBm, NOT dBFS
  power (dB)   10*log10(mean |X|^2), what the range profile carries
  range        inches by default, since the bench is measured with a tape
"""

from __future__ import annotations

import numpy as np

M_TO_IN = 39.3700787

# A small, colour-blind-safe set. Blue is the subject, orange the comparison,
# grey the reference/floor. Consistent across every figure here.
BLUE, ORANGE, GREEN, INK, MUTED, SURF = (
    "#2a78d6", "#eb6834", "#1baf7a", "#0b0b0b", "#9a9992", "#fcfcfb")
INT16_CEILING_DB = 20 * np.log10(32768)


def _plt():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "plotting needs matplotlib -- pip install matplotlib, or use the "
            "CSV/JSON exports instead"
        ) from exc
    plt.rcParams.update({
        "font.size": 10, "axes.edgecolor": MUTED, "axes.labelcolor": "#52514e",
        "xtick.color": "#52514e", "ytick.color": "#52514e",
        "figure.facecolor": SURF, "axes.facecolor": SURF, "axes.linewidth": 0.8,
    })
    return plt


def _tidy(ax, *, grid_axis="y"):
    ax.grid(axis=grid_axis, color=MUTED, alpha=0.25, lw=0.7)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    return ax


def range_profile(analysis, *, units="in", mark_peak=True, ax=None, label=None,
                  color=BLUE, title=None):
    """One capture's range profile from a CaptureAnalysis."""
    plt = _plt()
    if ax is None:
        _, ax = plt.subplots(figsize=(9, 4.2))
    scale = M_TO_IN if units == "in" else 1.0
    x = analysis.range_axis_m * scale
    ax.plot(x, analysis.profile_db, lw=1.6, color=color,
            label=label or (analysis.capture.source.name if analysis.capture else None))
    if mark_peak:
        px = analysis.peak_range_m * scale
        ax.axvline(px, color=MUTED, ls=":", lw=1.1)
        ax.plot([px], [analysis.profile_db[analysis.peak_bin - analysis.start_bin]],
                "o", ms=8, color=color, mec=SURF, mew=1.6, zorder=5)
    ax.axhline(analysis.noise_db, color=MUTED, ls="--", lw=1.0)
    ax.set_xlabel(f"range ({'inches' if units == 'in' else 'meters'})")
    ax.set_ylabel("power (dB)")
    ax.set_title(title or (f"bin {analysis.peak_bin} @ {analysis.peak_range_m * scale:.1f} "
                           f"{'in' if units == 'in' else 'm'}  ·  "
                           f"SNR {analysis.snr_db:.1f} dB  ·  floor {analysis.noise_db:.1f} dB"),
                 loc="left", color=INK, fontsize=11.5, pad=8)
    return _tidy(ax).figure


def overlay_profiles(analyses, *, units="in", labels=None, ax=None, title=None):
    """Several captures' profiles on one axis -- the A/B workhorse."""
    plt = _plt()
    if ax is None:
        _, ax = plt.subplots(figsize=(9.5, 4.6))
    scale = M_TO_IN if units == "in" else 1.0
    cycle = [BLUE, ORANGE, GREEN, "#7d5ba6", "#c2185b", "#00838f"]
    for i, a in enumerate(analyses):
        lab = (labels[i] if labels else
               (a.capture.source.name if a.capture else f"capture {i + 1}"))
        ax.plot(a.range_axis_m * scale, a.profile_db, lw=1.5,
                color=cycle[i % len(cycle)], alpha=0.9, label=lab)
    ax.set_xlabel(f"range ({'inches' if units == 'in' else 'meters'})")
    ax.set_ylabel("power (dB)")
    if title:
        ax.set_title(title, loc="left", color=INK, fontsize=11.5, pad=8)
    ax.legend(frameon=False, fontsize=8.5, ncol=2)
    return _tidy(ax).figure


def element_heatmap(analysis, *, quantity="power", ax=None):
    """Per-(TX, RX) amplitude or phase at the peak bin.

    12 virtual channels is small enough that a labelled grid beats a colourbar
    for actually reading values, so every cell carries its number.
    """
    plt = _plt()
    em = analysis.element_mean
    if quantity == "phase":
        data, fmt, cmap, title = np.degrees(np.angle(em)), "{:+.0f}", "twilight", "phase (deg)"
    else:
        data = 20 * np.log10(np.maximum(np.abs(em), 1e-12))
        fmt, cmap, title = "{:.1f}", "viridis", "level (dB re 1 ADC count)"
    if ax is None:
        _, ax = plt.subplots(figsize=(1.5 + 1.1 * data.shape[1], 1.4 + 0.9 * data.shape[0]))
    im = ax.imshow(data, cmap=cmap, aspect="auto")
    for t in range(data.shape[0]):
        for r in range(data.shape[1]):
            ax.text(r, t, fmt.format(data[t, r]), ha="center", va="center",
                    fontsize=9, color="white")
    ax.set_xticks(range(data.shape[1]), [f"RX{r}" for r in range(data.shape[1])])
    ax.set_yticks(range(data.shape[0]), [f"TX{t}" for t in range(data.shape[0])])
    ax.set_title(title, loc="left", color=INK, fontsize=11, pad=8)
    for side in ("top", "right", "bottom", "left"):
        ax.spines[side].set_visible(False)
    return ax.figure


def rows_bar(rows, *, x, y, ax=None, title=None, xlabel=None, color=BLUE,
             err=None, zero_line=True):
    """Horizontal bar/dot plot straight off a result table's rows.

    Generic on purpose: the same call draws material attenuation, a sweep
    summary, or a gain sweep, because they are all "one number per named row".
    """
    plt = _plt()
    names = [str(r[x]) for r in rows]
    vals = [float(r[y]) for r in rows]
    if ax is None:
        _, ax = plt.subplots(figsize=(9, 1.1 + 0.32 * max(len(rows), 4)))
    ypos = np.arange(len(rows))[::-1]
    if err:
        for yy, v, r in zip(ypos, vals, rows):
            e = float(r[err])
            ax.plot([v - e, v + e], [yy, yy], color=color, lw=2.0, alpha=0.45,
                    solid_capstyle="round")
    ax.plot(vals, ypos, "o", ms=8, color=color, mec=SURF, mew=1.5, ls="none")
    if zero_line:
        ax.axvline(0, color="#52514e", lw=1.0)
    ax.set_yticks(ypos, names, fontsize=9)
    ax.tick_params(axis="y", length=0, pad=6)
    ax.set_xlabel(xlabel or y)
    if title:
        ax.set_title(title, loc="left", color=INK, fontsize=11.5, pad=8)
    ax.set_ylim(-1, len(rows))
    _tidy(ax, grid_axis="x")
    ax.spines["left"].set_visible(False)
    return ax.figure


def angle_gate_candidates(rows, *, ax=None, title=None):
    """The angle gate's candidate table: power vs range, pass/fail coloured.

    This used to exist only as printed text, and it is the most useful thing
    the gate produces when it rejects everything.
    """
    plt = _plt()
    if ax is None:
        _, ax = plt.subplots(figsize=(9, 4.4))
    for r in rows:
        ok = bool(r.get("passed"))
        ax.plot(float(r["true_range_m"]) * M_TO_IN, float(r["power_db"]), "o",
                ms=11 if ok else 8, color=GREEN if ok else ORANGE,
                mec=SURF, mew=1.5, zorder=4 if ok else 3)
        ax.annotate(f"  {int(r['bin'])}", (float(r["true_range_m"]) * M_TO_IN,
                                           float(r["power_db"])),
                    fontsize=8, color="#52514e", va="center")
    ax.plot([], [], "o", color=GREEN, mec=SURF, label="passed gate")
    ax.plot([], [], "o", color=ORANGE, mec=SURF, label="rejected")
    ax.set_xlabel("bias-corrected range (inches)")
    ax.set_ylabel("pooled power (dB)")
    ax.set_title(title or "angle-gate candidates (label = range bin)",
                 loc="left", color=INK, fontsize=11.5, pad=8)
    ax.legend(frameon=False, fontsize=9)
    return _tidy(ax).figure


def baseline_drift(rows, *, ax=None, title=None):
    """Baseline level over a session -- the drift check."""
    plt = _plt()
    if ax is None:
        _, ax = plt.subplots(figsize=(9, 3.8))
    key = next((k for k in ("power_db_mean_all_elements", "power_db", "snr_db")
                if rows and k in rows[0]), None)
    if key is None:
        raise ValueError(f"no plottable level column in these rows: {sorted(rows[0]) if rows else []}")
    vals = [float(r[key]) for r in rows]
    ax.plot(range(1, len(vals) + 1), vals, "-o", color=BLUE, ms=7,
            mec=SURF, mew=1.4, lw=1.4)
    if len(vals) > 1:
        sd = float(np.std(vals, ddof=1))
        ax.axhline(float(np.mean(vals)), color=MUTED, ls="--", lw=1.0)
        ax.set_title(title or f"baseline drift: sd {sd:.2f} dB, spread {max(vals) - min(vals):.2f} dB",
                     loc="left", color=INK, fontsize=11.5, pad=8)
    ax.set_xlabel("baseline capture, in time order")
    ax.set_ylabel(key)
    return _tidy(ax).figure


def save(fig, path, *, dpi=160):
    fig.savefig(path, dpi=dpi, facecolor=SURF, bbox_inches="tight")
    return path
