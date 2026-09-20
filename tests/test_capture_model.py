"""Chunk 2: the normalized Capture model and the result types.

Complements test_analysis_regression.py, which proves the refactor did not move
any numbers. This file covers the new surface: that Capture is populated from
real data, that the range_bin_m precedence behaves as specified, and that the
result types expose the uniform interface.

Fixtures resolve the same way as the regression tests -- see tests/FIXTURES.md.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES = Path(os.environ.get("BENCH_FIXTURES", ROOT / "tests" / "fixtures"))
MATERIAL_DIR = Path(os.environ.get("BENCH_MATERIAL_DIR", FIXTURES / "material_subset"))

needs_material = pytest.mark.skipif(
    not list(MATERIAL_DIR.glob("*.l3dump")) if MATERIAL_DIR.exists() else True,
    reason=f"no .l3dump captures in {MATERIAL_DIR}",
)


def a_capture_path() -> Path:
    return sorted(MATERIAL_DIR.glob("*.l3dump"))[0]


# ---------------------------------------------------------------------------
# the model is populated from real data
# ---------------------------------------------------------------------------

@needs_material
def test_capture_facets_populated():
    from openflight_bench.analysis import load_capture

    cap = load_capture(a_capture_path())

    assert cap.source.parser == "ild1"
    assert cap.source.format_version is not None
    assert cap.source.sample_format_name and "unknown" not in cap.source.sample_format_name
    assert cap.is_range_snapshot

    assert cap.channels.n_tx > 0 and cap.channels.n_rx > 0
    assert cap.channels.tdm_divides_evenly
    assert cap.channels.loops_per_frame * cap.channels.n_tx == cap.channels.chirps_per_frame
    assert cap.channels.n_virtual == cap.channels.n_tx * cap.channels.n_rx

    assert cap.configuration.range_bin_m > 0
    assert cap.configuration.range_bin_source in {"sidecar", "derived", "override"}
    assert cap.configuration.window_bin_count == len(next(iter(cap.frames))[2][0][0])

    assert cap.integrity.declared_frames > 0
    assert cap.integrity.file_bytes == len(cap.raw)
    assert cap.integrity.n_frames_decoded >= 1
    assert isinstance(cap.integrity.describe(), str)

    assert cap.header, "the raw ILD1 meta dict is still available for unmigrated code"
    assert str(cap)


@needs_material
def test_capture_reads_sidecar_metadata():
    from openflight_bench.analysis import load_capture

    path = a_capture_path()
    cap = load_capture(path)
    sidecar = json.loads(path.with_suffix(".json").read_text())

    assert cap.metadata.test_type == sidecar.get("test_type")
    assert cap.metadata.kind == sidecar.get("kind")
    assert cap.metadata.swatch == sidecar.get("swatch")
    assert cap.metadata.angle_deg == sidecar.get("angle_deg")
    assert cap.metadata.record == sidecar
    assert cap.timestamps.captured_at == sidecar.get("timestamp")
    assert cap.source.config_path == sidecar.get("config_path")


@needs_material
def test_frames_iterate_like_the_parser():
    import numpy as np
    from openflight_bench.analysis import ild1, load_capture

    cap = load_capture(a_capture_path())
    direct = list(ild1.iter_frames(cap.raw, cap.header))
    viaset = list(cap.frames)
    assert len(direct) == len(viaset) > 0
    for (ia, sa, xa, oa), (ib, sb, xb, ob) in zip(direct, viaset):
        assert (ia, sa, oa) == (ib, sb, ob)
        assert np.array_equal(xa, xb)
    assert len(list(cap.frames.complete_only())) == sum(1 for f in direct if f[3])


# ---------------------------------------------------------------------------
# range_bin_m precedence -- the one behavior change in Chunk 2
# ---------------------------------------------------------------------------

@needs_material
def test_range_bin_m_prefers_the_sidecar_and_warns(tmp_path):
    """A capture whose sidecar disagrees with the chirp defaults must follow the
    sidecar, say so, and move its ranges accordingly."""
    from openflight_bench.analysis import ConfigurationPolicy, load_capture

    src = a_capture_path()
    dst = tmp_path / src.name
    shutil.copy(src, dst)
    sidecar = json.loads(src.with_suffix(".json").read_text())
    bogus = 0.0999
    sidecar.setdefault("capture_info", {})["range_bin_m"] = bogus
    dst.with_suffix(".json").write_text(json.dumps(sidecar))

    cap = load_capture(dst)
    assert cap.configuration.range_bin_m == bogus
    assert cap.configuration.range_bin_source == "sidecar"
    assert cap.warnings and "sidecar" in cap.warnings[0]

    policy = ConfigurationPolicy()
    assert not _close(bogus, policy.derived_range_bin_m), "fixture no longer exercises the branch"


@needs_material
def test_explicit_flag_overrides_the_sidecar_and_warns(tmp_path):
    from openflight_bench.analysis import ConfigurationPolicy, load_capture

    src = a_capture_path()
    dst = tmp_path / src.name
    shutil.copy(src, dst)
    sidecar = json.loads(src.with_suffix(".json").read_text())
    sidecar.setdefault("capture_info", {})["range_bin_m"] = 0.0999
    dst.with_suffix(".json").write_text(json.dumps(sidecar))

    cap = load_capture(dst, policy=ConfigurationPolicy(explicit_range_bin_m=0.05))
    assert cap.configuration.range_bin_m == 0.05
    assert cap.configuration.range_bin_source == "override"
    assert cap.warnings and "overrides" in cap.warnings[0]


@needs_material
def test_no_sidecar_value_falls_back_to_derived(tmp_path):
    """Older sidecars have no capture_info.range_bin_m -- those must behave
    exactly as before, deriving from the chirp parameters, with no warning."""
    from openflight_bench.analysis import ConfigurationPolicy, load_capture, range_bin_meters

    src = a_capture_path()
    dst = tmp_path / src.name
    shutil.copy(src, dst)
    sidecar = json.loads(src.with_suffix(".json").read_text())
    sidecar.get("capture_info", {}).pop("range_bin_m", None)
    dst.with_suffix(".json").write_text(json.dumps(sidecar))

    cap = load_capture(dst, policy=ConfigurationPolicy())
    assert cap.configuration.range_bin_source == "derived"
    assert cap.configuration.range_bin_m == range_bin_meters()
    assert not cap.warnings


@needs_material
def test_matching_sidecar_produces_no_warning():
    """Every capture in the project's current sessions is this case, which is
    why the whole regression suite still comes out byte-identical."""
    from openflight_bench.analysis import load_capture

    for path in sorted(MATERIAL_DIR.glob("*.l3dump"))[:6]:
        cap = load_capture(path)
        assert not cap.warnings, (path.name, cap.warnings)


@needs_material
def test_peak_range_follows_the_resolved_range_bin(tmp_path):
    """The point of the whole exercise: a different range_bin_m must actually
    change the reported ranges, not just a metadata field."""
    from openflight_bench.analysis import extract_capture_elements, load_capture

    src = a_capture_path()
    dst = tmp_path / src.name
    shutil.copy(src, dst)
    sidecar = json.loads(src.with_suffix(".json").read_text())
    sidecar.setdefault("capture_info", {})["range_bin_m"] = 0.0999
    dst.with_suffix(".json").write_text(json.dumps(sidecar))

    normal = extract_capture_elements(load_capture(src))
    shifted = extract_capture_elements(load_capture(dst))
    assert normal["peak_bin"] == shifted["peak_bin"]
    assert shifted["peak_range_m"] == pytest.approx(shifted["peak_bin"] * 0.0999)
    assert shifted["peak_range_m"] != pytest.approx(normal["peak_range_m"])


# ---------------------------------------------------------------------------
# normalized results
# ---------------------------------------------------------------------------

@needs_material
def test_capture_analysis_is_typed_and_matches_the_dict():
    import numpy as np
    from openflight_bench.analysis import analyze_capture, extract_capture_elements, load_capture

    cap = load_capture(a_capture_path())
    a = analyze_capture(cap)
    r = extract_capture_elements(cap)

    assert a.kind == "capture"
    assert a.peak_bin == r["peak_bin"]
    assert a.snr_db == pytest.approx(r["snr_db"])
    assert a.noise_db == pytest.approx(r["noise_db"])
    assert np.array_equal(a.element_mean, r["element_mean"])
    assert a.power_profile is not None and a.profile_db.shape == a.power_profile.shape
    assert a.range_axis_m.shape == a.power_profile.shape


@needs_material
def test_results_expose_a_uniform_tables_interface(tmp_path):
    from openflight_bench.analysis import MaterialOptions, analyze_material

    result = analyze_material(MaterialOptions(capture_dir=MATERIAL_DIR, no_angle_gate=True))

    assert result.kind == "material_comparison"
    assert set(result.tables) >= {"material_elements", "material_summary", "baseline_timeline"}
    # the legacy attribute names still work and are the same objects
    assert result.element_rows is result.tables["material_elements"]
    assert result.summary_rows is result.tables["material_summary"]
    assert result.baseline_timeline_rows is result.tables["baseline_timeline"]
    assert result.row_counts()["material_elements"] == len(result.element_rows)
    assert result.captures and all(c.source.parser == "ild1" for c in result.captures)

    # a caller can export every table without knowing which analysis ran
    from openflight_bench.analysis import write_rows
    for name in result.table_names():
        n = write_rows(tmp_path / f"{name}.csv", result.table(name))
        assert n == len(result.table(name))


@needs_material
def test_sweep_result_shares_the_same_interface():
    from openflight_bench.analysis import AnalysisResult, SweepOptions, analyze_sweep

    result = analyze_sweep(SweepOptions(capture_dir=MATERIAL_DIR))
    assert isinstance(result, AnalysisResult)
    assert result.kind == "sweep"
    assert result.element_rows is result.tables["sweep_elements"]
    assert result.summary_rows is result.tables["sweep_summary"]


def _close(a, b, rel=1e-9):
    return abs(a - b) <= rel * max(abs(a), abs(b))
