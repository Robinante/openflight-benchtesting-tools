"""Chunk 3: integrity verification, the progress split, JSON export, plots."""

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
INTEGRITY_DIR = FIXTURES / "integrity_session"
CALIBRATION = Path(os.environ.get("BENCH_CALIBRATION",
                                  FIXTURES / "bench_calibration_reference.json"))

needs_material = pytest.mark.skipif(
    not list(MATERIAL_DIR.glob("*.l3dump")) if MATERIAL_DIR.exists() else True,
    reason=f"no captures in {MATERIAL_DIR}")
needs_integrity = pytest.mark.skipif(
    not list(INTEGRITY_DIR.glob("*.l3dump")) if INTEGRITY_DIR.exists() else True,
    reason=f"no SHA256SUMS fixture in {INTEGRITY_DIR}")


# --------------------------------------------------------------- integrity --

@needs_integrity
def test_sha256_verifies_against_the_real_sidecar_format():
    from openflight_bench.analysis import load_capture
    path = sorted(INTEGRITY_DIR.glob("*.l3dump"))[0]
    cap = load_capture(path)
    i = cap.integrity
    assert i.sha256_verified is True
    assert i.content.source == "sidecar"
    assert i.sha256 == json.loads(path.with_suffix(".json").read_text())["file_sha256"]
    assert i.content.wire_sha256 and i.content.config_sha256
    assert i.accepted_for_analysis is True
    assert i.usable and not i.tampered
    assert not cap.warnings


@needs_integrity
def test_sha256sums_file_is_honoured_when_the_sidecar_has_no_digest(tmp_path):
    from openflight_bench.analysis import load_capture, read_sha256sums
    src = sorted(INTEGRITY_DIR.glob("*.l3dump"))[0]
    shutil.copy(src, tmp_path / src.name)
    shutil.copy(INTEGRITY_DIR / "SHA256SUMS", tmp_path / "SHA256SUMS")
    sidecar = json.loads(src.with_suffix(".json").read_text())
    sidecar.pop("file_sha256")
    (tmp_path / src.with_suffix(".json").name).write_text(json.dumps(sidecar))

    assert src.name in read_sha256sums(tmp_path)
    cap = load_capture(tmp_path / src.name)
    assert cap.integrity.sha256_verified is True
    assert cap.integrity.content.source == "SHA256SUMS"


@needs_integrity
def test_corrupted_bytes_are_detected_and_warned(tmp_path):
    from openflight_bench.analysis import load_capture
    src = sorted(INTEGRITY_DIR.glob("*.l3dump"))[0]
    dst = tmp_path / src.name
    data = bytearray(src.read_bytes())
    data[-64] ^= 0xFF                      # flip a bit deep in the payload
    dst.write_bytes(bytes(data))
    shutil.copy(src.with_suffix(".json"), dst.with_suffix(".json"))

    cap = load_capture(dst)
    assert cap.integrity.sha256_verified is False
    assert cap.integrity.tampered and not cap.integrity.usable
    assert not cap.integrity.complete
    assert any("MISMATCH" in w for w in cap.warnings)


@needs_material
def test_captures_with_no_recorded_digest_report_none_not_failure():
    from openflight_bench.analysis import load_capture
    cap = load_capture(sorted(MATERIAL_DIR.glob("*.l3dump"))[0])
    assert cap.integrity.sha256_verified is None
    assert cap.integrity.sha256, "the hash is still computed"
    assert cap.integrity.usable and not cap.warnings


@needs_integrity
def test_verify_directory_matches_sha256sum_c():
    from openflight_bench.analysis import verify_directory
    res = verify_directory(INTEGRITY_DIR)
    assert len(res["ok"]) == 1                 # only one dump staged
    assert not res["mismatch"]
    assert res["missing"], "the fixture intentionally holds one of many listed files"


# ---------------------------------------------------------------- progress --

@needs_material
def test_progress_diverts_everything_off_stdout(capsys):
    from openflight_bench.analysis import (CollectingProgress, MaterialOptions,
                                           analyze_material)
    pg = CollectingProgress()
    analyze_material(MaterialOptions(capture_dir=MATERIAL_DIR, no_angle_gate=True),
                     progress=pg)
    assert len(pg) > 5
    assert capsys.readouterr().out == "", "nothing should reach stdout"


@needs_material
def test_default_progress_still_prints(capsys):
    from openflight_bench.analysis import MaterialOptions, analyze_material
    analyze_material(MaterialOptions(capture_dir=MATERIAL_DIR, no_angle_gate=True))
    assert capsys.readouterr().out, "the CLI path must be unchanged"


def test_as_progress_is_print_compatible():
    from openflight_bench.analysis import as_progress
    got = []
    p = as_progress(got.append)
    p("a", 1, 2.5)
    p()                       # bare print() must not explode
    p("x", sep="-", end="")
    assert got == ["a 1 2.5", "", "x"]


# ------------------------------------------------------- angle-gate table --

@needs_material
def test_failing_angle_gate_still_yields_its_candidate_table():
    from openflight_bench.analysis import (CollectingProgress, MaterialOptions,
                                           analyze_material)
    if not CALIBRATION.exists():
        pytest.skip("no calibration fixture")
    pg = CollectingProgress()
    with pytest.raises(ValueError) as excinfo:
        analyze_material(MaterialOptions(capture_dir=MATERIAL_DIR,
                                         calibration=CALIBRATION), progress=pg)
    result = getattr(excinfo.value, "result", None)
    assert result is not None
    rows = result.table("angle_gate_candidates")
    assert rows, "the gate's candidates must survive the failure"
    assert {"bin", "power_db", "passed", "ok_el", "ok_axis", "group"} <= set(rows[0])
    assert rows == sorted(rows, key=lambda r: -r["power_db"])


# ------------------------------------------------------------ JSON export --

@needs_material
def test_json_export_carries_tables_and_provenance(tmp_path):
    from openflight_bench.analysis import MaterialOptions, analyze_material, write_json
    r = analyze_material(MaterialOptions(capture_dir=MATERIAL_DIR, no_angle_gate=True))
    n = write_json(tmp_path / "r.json", r)
    doc = json.loads((tmp_path / "r.json").read_text())

    assert n == sum(len(v) for v in doc["tables"].values())
    assert doc["tables"]["material_elements"] == r.element_rows
    m = doc["metadata"]
    assert m["kind"] == "material_comparison"
    assert m["n_captures"] == len(r.captures)
    assert m["range_bin_sources"] == ["sidecar"]
    assert m["n_sha256_mismatch"] == 0
    assert set(m["captures"][0]) >= {"file", "sha256", "sha256_verified",
                                     "range_bin_m", "range_bin_source"}


@needs_material
def test_json_and_csv_agree(tmp_path):
    import csv
    from openflight_bench.analysis import (MaterialOptions, analyze_material,
                                           write_json, write_rows)
    r = analyze_material(MaterialOptions(capture_dir=MATERIAL_DIR, no_angle_gate=True))
    write_rows(tmp_path / "e.csv", r.element_rows)
    write_json(tmp_path / "r.json", r)
    with open(tmp_path / "e.csv", newline="") as fh:
        csv_rows = list(csv.DictReader(fh))
    json_rows = json.loads((tmp_path / "r.json").read_text())["tables"]["material_elements"]
    assert len(csv_rows) == len(json_rows)
    for a, b in zip(csv_rows, json_rows):
        assert list(a) == list(b)
        for k in a:
            assert a[k] == ("" if b[k] is None else str(b[k]))


# ----------------------------------------------------------------- plots --

@needs_material
def test_every_plot_renders(tmp_path):
    from openflight_bench.analysis import (MaterialOptions, analyze_capture,
                                           analyze_material, load_capture, plots)
    paths = sorted(MATERIAL_DIR.glob("*.l3dump"))[:3]
    analyses = [analyze_capture(load_capture(p)) for p in paths]
    figs = [
        plots.range_profile(analyses[0]),
        plots.overlay_profiles(analyses),
        plots.element_heatmap(analyses[0]),
        plots.element_heatmap(analyses[0], quantity="phase"),
    ]
    r = analyze_material(MaterialOptions(capture_dir=MATERIAL_DIR, no_angle_gate=True))
    figs.append(plots.rows_bar(r.summary_rows, x="swatch",
                               y="mean_delta_power_db_vs_baseline"))
    figs.append(plots.baseline_drift(r.baseline_timeline_rows))
    for i, f in enumerate(figs):
        out = plots.save(f, tmp_path / f"f{i}.png")
        assert Path(out).stat().st_size > 5000
