"""OpenFlight Radar Workbench -- offline pages.

Run it:

    streamlit run workbench/app.py

Everything here is a thin shell over ``openflight_bench.analysis``. The
workbench holds no analysis logic of its own: it picks files, calls the same
functions the CLI calls, and renders what comes back. If a number here
disagrees with the CSV, that is a bug in this file, not in the analysis.

Offline only -- opening saved captures. Live acquisition is the Phase 1 chunk
that still has to touch the radar, so the Capture page is not here yet.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openflight_bench.analysis import (  # noqa: E402
    CollectingProgress, ConfigurationPolicy, Gate, MaterialOptions, SweepOptions,
    analyze_capture, analyze_material, analyze_sweep, load_capture, plots,
    read_session_manifest, result_metadata, verify_directory, write_json, write_rows,
)

st.set_page_config(page_title="OpenFlight Radar Workbench", layout="wide")

DUMP_GLOB = "*.l3dump"


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner=False)
def _scan(directory: str) -> list[str]:
    d = Path(directory)
    if not d.exists():
        return []
    found = sorted(str(p) for p in d.glob(DUMP_GLOB))
    if not found:  # a session folder with dated subdirectories
        found = sorted(str(p) for p in d.glob(f"*/{DUMP_GLOB}"))
    return found


@st.cache_resource(show_spinner=False)
def _capture(path: str, _rb: float | None):
    return load_capture(path, policy=ConfigurationPolicy(explicit_range_bin_m=_rb))


def _policy_controls():
    with st.sidebar:
        st.subheader("Configuration")
        override = st.checkbox("Override range_bin_m", value=False,
                               help="Off: the capture's own sidecar value wins, "
                                    "falling back to the chirp defaults.")
        rb = st.number_input("range_bin_m", value=0.0468425715625, format="%.13f",
                             disabled=not override)
        return rb if override else None


def _integrity_badge(cap) -> str:
    i = cap.integrity
    if i.tampered:
        return "🔴 SHA256 MISMATCH"
    if i.accepted_for_analysis is False:
        return "🟠 rejected by capture tool"
    if i.sha256_verified:
        return "🟢 sha256 verified"
    return "⚪ sha256 not recorded"


def _download_buttons(result, stem: str):
    cols = st.columns(len(result.table_names()) + 1)
    for col, name in zip(cols, result.table_names()):
        buf = io.StringIO()
        import csv as _csv
        rows = result.table(name)
        w = _csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
        col.download_button(f"{name}.csv", buf.getvalue(), f"{stem}_{name}.csv",
                            "text/csv", use_container_width=True)
    payload = {"metadata": result_metadata(result),
               "tables": {n: result.table(n) for n in result.table_names()}}
    cols[-1].download_button("result.json", json.dumps(payload, indent=1, default=str),
                             f"{stem}.json", "application/json", use_container_width=True)


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------

def page_session(directory: str, rb):
    st.header("Session")
    st.caption("What is in this folder, and can its bytes be trusted.")

    paths = _scan(directory)
    if not paths:
        st.info(f"No `{DUMP_GLOB}` files under `{directory}`.")
        return

    manifest = read_session_manifest(Path(paths[0]).parent)
    if manifest:
        c1, c2, c3 = st.columns(3)
        c1.metric("captures in manifest", len(manifest.get("captures", []) or []))
        c2.metric("baud", manifest.get("baud", "--"))
        prof = manifest.get("last_applied_profile") or {}
        c3.metric("last profile", prof.get("name", "--"))
        with st.expander("Applied profile"):
            st.json(prof)

    if st.button("Verify SHA256SUMS for this folder"):
        with st.spinner("hashing..."):
            res = verify_directory(Path(paths[0]).parent)
        a, b, c, d = st.columns(4)
        a.metric("ok", len(res["ok"]))
        b.metric("mismatch", len(res["mismatch"]))
        c.metric("missing", len(res["missing"]))
        d.metric("not listed", len(res["unlisted"]))
        if res["mismatch"]:
            st.error("MISMATCH: " + ", ".join(res["mismatch"]))
        if not any(res.values()):
            st.info("No SHA256SUMS file in this folder.")

    rows = []
    for p in paths:
        try:
            cap = _capture(p, rb)
        except Exception as exc:  # noqa: BLE001
            rows.append({"file": Path(p).name, "integrity": f"unreadable: {exc}"})
            continue
        i = cap.integrity
        rows.append({
            "file": cap.source.name,
            "integrity": _integrity_badge(cap),
            "frames": f"{i.n_frames_decoded}/{i.declared_frames}",
            "incomplete": i.n_frames_incomplete,
            "short_by": i.short_by_bytes,
            "kind": cap.metadata.kind,
            "swatch": cap.metadata.swatch,
            "range_bin_m": cap.configuration.range_bin_m,
            "from": cap.configuration.range_bin_source,
            "format": cap.source.sample_format_name,
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)


def page_analyze(directory: str, rb):
    st.header("Analyze")
    st.caption("The full captured range profile first. Detection is annotation.")

    paths = _scan(directory)
    if not paths:
        st.info(f"No `{DUMP_GLOB}` files under `{directory}`.")
        return
    chosen = st.selectbox("Capture", paths, format_func=lambda p: Path(p).name)
    guard = st.sidebar.number_input("guard bins", 0, 20, 2)

    cap = _capture(chosen, rb)
    base = analyze_capture(cap, guard_bins=guard)      # full profile, no gate
    prof = base.profile

    # ---- optional, explicit gate ----
    st.sidebar.subheader("Target region")
    use_gate = st.sidebar.checkbox("Apply a gate", value=False,
                                   help="Optional. A gate annotates a region "
                                        "and adds region metrics. It never "
                                        "removes data from the profile.")
    gate = None
    if use_gate:
        lo_m, hi_m = float(prof.range_m[0]), float(prof.range_m[-1])
        center = st.sidebar.slider("gate centre (m)", lo_m, hi_m,
                                   float(prof.range_m[prof.power.argmax()]),
                                   step=float(prof.range_bin_m))
        half = st.sidebar.number_input("half-width (bins)", 0, 20, 2)
        gate = Gate.around(prof, center, half_width_bins=int(half))
        zoom = st.sidebar.checkbox("Zoom to gate", value=False,
                                   help="Off by default: the rest of the range "
                                        "stays visible.")
    else:
        zoom = False
    annotate = st.sidebar.checkbox("Annotate detected returns", value=True)

    a = analyze_capture(cap, gate=gate, guard_bins=guard) if gate else base

    # ---- raw first ----
    st.pyplot(plots.raw_range_profile(a, gate=gate, annotate_peaks=annotate,
                                      zoom=zoom), use_container_width=True)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("captured bins", prof.n_bins)
    c2.metric("returns detected", len(a.peaks))
    c3.metric("no-return floor", f"{a.noise_floor_db:.1f} dB"
              if a.noise_floor_db is not None else "--")
    c4.metric("strongest above floor", f"{a.peak_to_floor_db:.1f} dB"
              if a.peak_to_floor_db is not None else "--")
    st.write(_integrity_badge(cap), "·", cap.integrity.describe_frames())
    for w in cap.warnings:
        st.warning(w)
    if a.annotation_error:
        st.warning(f"Annotation unavailable ({a.annotation_error}). "
                   "The profile above is unaffected.")

    if a.peaks:
        st.subheader("Detected returns")
        st.caption("Candidates, ranked by power. None of these is assumed to be "
                   "*the* target.")
        st.dataframe([{
            "rank": pk.rank, "bin": pk.bin,
            "range_in": round(pk.range_in, 1), "range_m": round(pk.range_m, 3),
            "power_db": round(pk.power_db, 2),
            "prominence_db": round(pk.prominence_db, 2),
            "in_gate": bool(gate and gate.contains_bin(pk.bin)),
        } for pk in a.peaks], use_container_width=True, hide_index=True)

    if gate is not None and a.gate_metrics:
        st.subheader("Gate metrics")
        st.caption("Region-only. The profile above still shows every bin.")
        st.dataframe([{k: (round(v, 3) if isinstance(v, float) else v)
                       for k, v in a.gate_metrics.items()}],
                     use_container_width=True, hide_index=True)

    with st.expander("Element detail at the auto-selected bin (legacy path)"):
        if a.element_mean is None:
            st.info("Element extraction unavailable for this capture.")
        else:
            m1, m2, m3 = st.columns(3)
            m1.metric("auto peak bin", a.peak_bin)
            m2.metric("legacy snr_db", f"{a.snr_db:.1f}")
            m3.metric("coherent gain", f"{a.coherent_gain_db:.1f} dB")
            st.caption("`legacy snr_db` is a coherent numerator over an "
                       "incoherent denominator -- see docs/ANALYSIS_METRICS.md. "
                       "Use *strongest above floor* above for a real ratio.")
            left, right = st.columns(2)
            left.pyplot(plots.element_heatmap(a), use_container_width=True)
            right.pyplot(plots.element_heatmap(a, quantity="phase"),
                         use_container_width=True)

    with st.expander("Capture detail"):
        st.json({
            "source": {"parser": cap.source.parser, "version": cap.source.format_version,
                       "format": cap.source.sample_format_name,
                       "config_path": cap.source.config_path},
            "configuration": {"range_bin_m": cap.configuration.range_bin_m,
                              "range_bin_source": cap.configuration.range_bin_source,
                              "window": [cap.configuration.window_start_bin,
                                         cap.configuration.window_bin_count],
                              "frame_period_us": cap.configuration.frame_period_us},
            "channels": {"n_tx": cap.channels.n_tx, "n_rx": cap.channels.n_rx,
                         "chirps_per_frame": cap.channels.chirps_per_frame,
                         "loops_per_frame": cap.channels.loops_per_frame},
            "integrity": {"sha256": cap.integrity.sha256,
                          "verified": cap.integrity.sha256_verified,
                          "source": cap.integrity.content.source,
                          "accepted_for_analysis": cap.integrity.accepted_for_analysis,
                          "status": cap.integrity.status},
            "timestamps": {"captured_at": cap.timestamps.captured_at,
                           "duration_us": cap.timestamps.duration_us,
                           "temperatures_c": cap.timestamps.temperatures_c},
        })


def page_compare(directory: str, rb):
    st.header("Compare")
    st.caption("Any A/B: baseline vs enclosure, swatch vs swatch, run vs run.")

    paths = _scan(directory)
    if not paths:
        st.info(f"No `{DUMP_GLOB}` files under `{directory}`.")
        return
    picked = st.multiselect("Captures", paths, default=paths[:3],
                            format_func=lambda p: Path(p).name)
    if not picked:
        st.info("Pick at least one capture.")
        return

    analyses = [analyze_capture(_capture(p, rb)) for p in picked]
    st.pyplot(plots.overlay_profiles(analyses), use_container_width=True)

    ref = st.selectbox("Reference", range(len(picked)),
                       format_func=lambda i: Path(picked[i]).name)
    base = analyses[ref]
    rows = []
    for p, a in zip(picked, analyses):
        rows.append({
            "capture": Path(p).name,
            "peak_bin": a.peak_bin,
            "peak_range_in": round(a.peak_range_m * plots.M_TO_IN, 2),
            "snr_db": round(a.snr_db, 2),
            "noise_db": round(a.noise_db, 2),
            "delta_snr_db": round(a.snr_db - base.snr_db, 2),
            "delta_peak_bin": a.peak_bin - base.peak_bin,
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)
    st.pyplot(plots.rows_bar(rows, x="capture", y="delta_snr_db",
                             title=f"SNR relative to {Path(picked[ref]).name}",
                             xlabel="delta SNR (dB)"), use_container_width=True)


def page_material(directory: str, rb):
    st.header("Material comparison")
    st.caption("Swatch attenuation and phase shift against a pooled baseline.")

    gate = st.sidebar.checkbox("Angle gating", value=False,
                               help="Off = --no-angle-gate (plain pooled argmax).")
    cal = st.sidebar.text_input("Calibration JSON", "") if gate else ""
    if not st.button("Run material analysis", type="primary"):
        return

    opts = MaterialOptions(capture_dir=directory, range_bin_m=rb,
                           no_angle_gate=not gate,
                           calibration=Path(cal) if cal else None)
    pg = CollectingProgress()
    try:
        with st.spinner("analyzing..."):
            result = analyze_material(opts, progress=pg)
    except Exception as exc:  # noqa: BLE001
        st.error(f"{type(exc).__name__}: {exc}")
        partial = getattr(exc, "result", None)
        cands = partial.table("angle_gate_candidates") if partial else []
        if cands:
            st.subheader("What the angle gate looked at")
            st.pyplot(plots.angle_gate_candidates(cands), use_container_width=True)
            st.dataframe(cands, use_container_width=True, hide_index=True)
        with st.expander("Log"):
            st.code(pg.text)
        return

    for w in result.warnings:
        st.warning(w)
    if result.baseline_timeline_rows:
        st.pyplot(plots.baseline_drift(result.baseline_timeline_rows),
                  use_container_width=True)
    if result.summary_rows:
        st.pyplot(plots.rows_bar(result.summary_rows, x="swatch",
                                 y="mean_delta_power_db_vs_baseline",
                                 title="attenuation vs pooled baseline",
                                 xlabel="mean delta power (dB)"),
                  use_container_width=True)
    for name in result.table_names():
        with st.expander(f"{name} ({len(result.table(name))} rows)"):
            st.dataframe(result.table(name), use_container_width=True, hide_index=True)
    _download_buttons(result, "material")
    with st.expander("Log"):
        st.code(pg.text)


def page_sweep(directory: str, rb):
    st.header("Sweep")
    st.caption("Per-(TX, RX) amplitude and phase versus mechanical angle.")
    if not st.button("Run sweep analysis", type="primary"):
        return
    pg = CollectingProgress()
    try:
        with st.spinner("analyzing..."):
            result = analyze_sweep(SweepOptions(capture_dir=directory, range_bin_m=rb),
                                   progress=pg)
    except Exception as exc:  # noqa: BLE001
        st.error(f"{type(exc).__name__}: {exc}")
        with st.expander("Log"):
            st.code(pg.text)
        return
    for w in result.warnings:
        st.warning(w)
    if result.summary_rows:
        st.pyplot(plots.rows_bar(result.summary_rows, x="angle_deg", y="snr_db",
                                 title="SNR by angle", xlabel="SNR (dB)",
                                 zero_line=False), use_container_width=True)
    for name in result.table_names():
        with st.expander(f"{name} ({len(result.table(name))} rows)"):
            st.dataframe(result.table(name), use_container_width=True, hide_index=True)
    _download_buttons(result, "sweep")
    with st.expander("Log"):
        st.code(pg.text)


# ---------------------------------------------------------------------------

PAGES = {
    "Session": page_session,
    "Analyze": page_analyze,
    "Compare": page_compare,
    "Material": page_material,
    "Sweep": page_sweep,
}


def main():
    st.sidebar.title("Radar Workbench")
    directory = st.sidebar.text_input(
        "Capture folder", value=os.environ.get("BENCH_CAPTURE_DIR", "captures"),
        help="Set BENCH_CAPTURE_DIR to change the default.")
    rb = _policy_controls()
    choice = st.sidebar.radio("Page", list(PAGES))
    st.sidebar.caption("Offline analysis of saved captures. Live capture is not "
                       "wired up yet -- that is the chunk that touches the radar.")
    PAGES[choice](directory, rb)


main()
