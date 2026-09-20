"""Regression: the refactored openflight_bench.analysis package must reproduce the
pre-refactor scripts exactly.

Old side  = legacy/analyze_{material,sweep}_original.py, frozen copies of the
            scripts as they were before this refactor.
New side  = openflight_bench.analysis.material.analyze_material / .sweep.analyze_sweep

Both sides are driven from the same argparse Namespace over the same captures,
and every produced row is compared field by field (floats to 1e-9 relative).

Capture fixtures default to tests/fixtures; override with

    BENCH_FIXTURES=/path/to/captures python3 -m pytest tests/test_analysis_regression.py

pointing at a directory that holds material_subset/ and sweep_subset/, or set
BENCH_MATERIAL_DIR / BENCH_SWEEP_DIR individually to any real capture session.
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES = Path(os.environ.get("BENCH_FIXTURES", ROOT / "tests" / "fixtures"))
MATERIAL_DIR = Path(os.environ.get("BENCH_MATERIAL_DIR", FIXTURES / "material_subset"))
SWEEP_DIR = Path(os.environ.get("BENCH_SWEEP_DIR", FIXTURES / "sweep_subset"))

needs_material = pytest.mark.skipif(
    not list(MATERIAL_DIR.glob("*.l3dump")) if MATERIAL_DIR.exists() else True,
    reason=f"no .l3dump captures in {MATERIAL_DIR}",
)
needs_sweep = pytest.mark.skipif(
    not list(SWEEP_DIR.glob("*.l3dump")) if SWEEP_DIR.exists() else True,
    reason=f"no .l3dump captures in {SWEEP_DIR}",
)


def assert_rows_equal(old: list[dict], new: list[dict], label: str) -> None:
    assert len(old) == len(new), f"{label}: row count {len(old)} -> {len(new)}"
    assert old and new, f"{label}: produced no rows, nothing was compared"
    for i, (a, b) in enumerate(zip(old, new)):
        assert list(a.keys()) == list(b.keys()), (
            f"{label} row {i}: column order or names changed\n  old={list(a.keys())}\n  new={list(b.keys())}"
        )
        for k in a:
            av, bv = a[k], b[k]
            if isinstance(av, float) and isinstance(bv, float):
                if math.isnan(av) and math.isnan(bv):
                    continue
                assert av == pytest.approx(bv, rel=1e-9, abs=1e-12), (
                    f"{label} row {i} field {k!r}: {av!r} -> {bv!r}"
                )
            else:
                assert av == bv, f"{label} row {i} field {k!r}: {av!r} -> {bv!r}"


# --------------------------------------------------------------------------
# shared parsing layer: identical bytes in, identical structures out
# --------------------------------------------------------------------------

@needs_material
def test_parse_header_matches_legacy():
    from openflight_bench.analysis import ild1
    from legacy import analyze_sweep_original as legacy

    for path in sorted(MATERIAL_DIR.glob("*.l3dump")):
        raw = path.read_bytes()
        a, b = legacy.parse_header(raw), ild1.parse_header(raw)
        legacy._read_frame_metadata(raw, a)
        ild1._read_frame_metadata(raw, b)
        assert a == b, path.name


@needs_material
def test_iter_frames_matches_legacy():
    import numpy as np
    from openflight_bench.analysis import ild1
    from legacy import analyze_sweep_original as legacy

    path = sorted(MATERIAL_DIR.glob("*.l3dump"))[0]
    raw = path.read_bytes()
    ma = legacy.parse_header(raw); legacy._read_frame_metadata(raw, ma)
    mb = ild1.parse_header(raw); ild1._read_frame_metadata(raw, mb)
    fa = list(legacy.iter_frames(raw, ma))
    fb = list(ild1.iter_frames(raw, mb))
    assert len(fa) == len(fb) and fa, "no frames decoded"
    for (ia, sa, xa, oa), (ib, sb, xb, ob) in zip(fa, fb):
        assert (ia, sa, oa) == (ib, sb, ob)
        assert np.array_equal(xa, xb)


@needs_material
def test_extract_file_elements_matches_legacy():
    import numpy as np
    from openflight_bench.analysis.elements import extract_file_elements as new_extract
    from legacy import analyze_sweep_original as legacy

    rb = legacy.range_bin_meters()
    for path in sorted(MATERIAL_DIR.glob("*.l3dump"))[:6]:
        a = legacy.extract_file_elements(path, range_bin_m=rb)
        b = new_extract(path, range_bin_m=rb)
        assert a["peak_bin"] == b["peak_bin"] == b["peak_bin"]
        for k in ("peak_range_m", "n_frames_used", "n_loops_total", "n_tx", "n_rx",
                  "noise_db", "snr_db"):
            assert a[k] == pytest.approx(b[k], rel=1e-12), (path.name, k)
        assert np.allclose(a["element_mean"], b["element_mean"], rtol=0, atol=0)
        assert np.allclose(a["coherence"], b["coherence"], rtol=0, atol=0, equal_nan=True)


@needs_material
def test_pooled_profile_peak_matches_legacy():
    from openflight_bench.analysis.elements import pooled_profile_peak as new_peak
    from legacy import analyze_sweep_original as legacy

    paths = sorted(MATERIAL_DIR.glob("material_baseline_*.l3dump"))
    assert paths, "baseline captures missing from fixture"
    assert legacy.pooled_profile_peak(paths) == new_peak(paths)


# --------------------------------------------------------------------------
# end-to-end: same CSV rows
# --------------------------------------------------------------------------

@needs_sweep
def test_sweep_rows_match_legacy(tmp_path, monkeypatch, capsys):
    from openflight_bench.analysis.sweep import SweepOptions, analyze_sweep
    from legacy import analyze_sweep_original as legacy

    opts = SweepOptions(capture_dir=SWEEP_DIR,
                        element_csv=tmp_path / "e.csv",
                        summary_csv=tmp_path / "s.csv")
    new = analyze_sweep(opts)

    monkeypatch.setattr(sys, "argv", ["analyze_sweep.py", str(SWEEP_DIR),
                                      "--element-csv", str(tmp_path / "le.csv"),
                                      "--summary-csv", str(tmp_path / "ls.csv")])
    assert legacy.main() == 0
    capsys.readouterr()
    old_e = _read_csv(tmp_path / "le.csv")
    old_s = _read_csv(tmp_path / "ls.csv")
    assert_rows_equal(old_e, _stringify(new.element_rows), "sweep elements")
    assert_rows_equal(old_s, _stringify(new.summary_rows), "sweep summary")


@needs_material
def test_material_rows_match_legacy(tmp_path, monkeypatch, capsys):
    from openflight_bench.analysis.material import MaterialOptions, analyze_material
    from legacy import analyze_material_original as legacy

    opts = MaterialOptions(capture_dir=MATERIAL_DIR,
                           element_csv=tmp_path / "e.csv",
                           summary_csv=tmp_path / "s.csv",
                           baseline_timeline_csv=tmp_path / "b.csv",
                           no_angle_gate=True)
    new = analyze_material(opts)

    monkeypatch.setattr(sys, "argv", ["analyze_material.py", str(MATERIAL_DIR),
                                      "--no-angle-gate",
                                      "--element-csv", str(tmp_path / "le.csv"),
                                      "--summary-csv", str(tmp_path / "ls.csv"),
                                      "--baseline-timeline-csv", str(tmp_path / "lb.csv")])
    assert legacy._analysis_main() == 0
    capsys.readouterr()
    assert_rows_equal(_read_csv(tmp_path / "lb.csv"),
                      _stringify(new.baseline_timeline_rows), "baseline timeline")
    assert_rows_equal(_read_csv(tmp_path / "le.csv"),
                      _stringify(new.element_rows), "material elements")
    assert_rows_equal(_read_csv(tmp_path / "ls.csv"),
                      _stringify(new.summary_rows), "material summary")


# --------------------------------------------------------------------------
# the wrappers still behave like scripts
# --------------------------------------------------------------------------

@needs_sweep
def test_sweep_cli_wrapper_writes_csv(tmp_path, monkeypatch):
    from openflight_bench.analysis.sweep import main
    monkeypatch.setattr(sys, "argv", ["analyze_sweep.py", str(SWEEP_DIR),
                                      "--element-csv", str(tmp_path / "e.csv"),
                                      "--summary-csv", str(tmp_path / "s.csv")])
    assert main() == 0
    assert (tmp_path / "e.csv").exists() and (tmp_path / "s.csv").exists()


def _read_csv(path):
    import csv
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _stringify(rows):
    """Render row values the way csv.DictWriter would, so a dict-vs-CSV
    comparison compares like with like."""
    out = []
    for r in rows:
        out.append({k: ("" if v is None else str(v)) for k, v in r.items()})
    return out


CALIBRATION = Path(os.environ.get(
    "BENCH_CALIBRATION", FIXTURES / "bench_calibration_reference.json"))

needs_calibration = pytest.mark.skipif(
    not CALIBRATION.exists(), reason=f"no calibration file at {CALIBRATION}")


@needs_material
@needs_calibration
def test_material_angle_gated_path_matches_legacy(tmp_path, monkeypatch, capsys):
    """Same comparison on the DEFAULT code path -- angle gating on, real
    calibration loaded -- which is what the bench workflow actually runs.

    On some sessions the angle gate legitimately rejects every candidate bin
    and the analysis raises. That IS the existing behavior, so this compares
    outcomes: both sides must either raise the same exception with the same
    message, or both succeed with identical rows.
    """
    from openflight_bench.analysis.material import MaterialOptions, analyze_material
    from legacy import analyze_material_original as legacy

    def run_new():
        opts = MaterialOptions(capture_dir=MATERIAL_DIR,
                               calibration=CALIBRATION,
                               element_csv=tmp_path / "e.csv",
                               summary_csv=tmp_path / "s.csv",
                               baseline_timeline_csv=tmp_path / "b.csv")
        return analyze_material(opts)

    def run_old():
        monkeypatch.setattr(sys, "argv",
                            ["analyze_material.py", str(MATERIAL_DIR),
                             "--calibration", str(CALIBRATION),
                             "--element-csv", str(tmp_path / "le.csv"),
                             "--summary-csv", str(tmp_path / "ls.csv"),
                             "--baseline-timeline-csv", str(tmp_path / "lb.csv")])
        return legacy._analysis_main()

    new_exc = old_exc = None
    try:
        new = run_new()
    except Exception as exc:                      # noqa: BLE001 -- comparing behavior
        new_exc, new = exc, None
    try:
        old_rc = run_old()
    except Exception as exc:                      # noqa: BLE001
        old_exc, old_rc = exc, None
    capsys.readouterr()

    assert (new_exc is None) == (old_exc is None), (
        f"one side raised and the other did not:\n  old={old_exc!r}\n  new={new_exc!r}")
    if new_exc is not None:
        assert type(new_exc) is type(old_exc), (type(old_exc), type(new_exc))
        assert str(new_exc) == str(old_exc), (str(old_exc), str(new_exc))
        return

    assert old_rc == 0
    assert_rows_equal(_read_csv(tmp_path / "lb.csv"),
                      _stringify(new.baseline_timeline_rows), "baseline timeline (gated)")
    if new.element_rows:
        assert_rows_equal(_read_csv(tmp_path / "le.csv"),
                          _stringify(new.element_rows), "material elements (gated)")
        assert_rows_equal(_read_csv(tmp_path / "ls.csv"),
                          _stringify(new.summary_rows), "material summary (gated)")


@needs_material
def test_material_cli_wrapper_writes_csv(tmp_path, monkeypatch):
    from openflight_bench.analysis.material import main
    monkeypatch.setattr(sys, "argv", ["analyze_material.py", str(MATERIAL_DIR),
                                      "--no-angle-gate",
                                      "--element-csv", str(tmp_path / "e.csv"),
                                      "--summary-csv", str(tmp_path / "s.csv"),
                                      "--baseline-timeline-csv", str(tmp_path / "b.csv")])
    assert main() == 0
    assert (tmp_path / "e.csv").exists()
    assert (tmp_path / "s.csv").exists()
    assert (tmp_path / "b.csv").exists()
