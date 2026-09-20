# Test fixtures

The repository includes one real capture in `tests/fixtures/integrity_session`
for hashing and integrity tests, plus a reference calibration JSON. The
integrity fixture is deliberately partial: its original `SHA256SUMS` lists
other captures that are not bundled, and a test checks that they are reported
as missing.

The external material and sweep datasets used for full regression comparisons
are not bundled. In PowerShell, point the tests at your matching datasets:

```powershell
$env:BENCH_MATERIAL_DIR = 'C:\path\to\material_session_20260906\2026-09-06'
$env:BENCH_SWEEP_DIR = 'C:\path\to\boresight_test1'
$env:BENCH_CALIBRATION = 'C:\path\to\bench_calibration_reference.json'
python -m pytest -q -rs
```

On Linux, set these with `export BENCH_MATERIAL_DIR=/path/to/session` and the
corresponding variables. Run from an environment with `.[analysis,dev]` (or
`.[workbench,dev]`) installed.

Without overrides, tests look in `tests/fixtures/material_subset` and
`tests/fixtures/sweep_subset` and skip if no dumps are present. `BENCH_FIXTURES`
can override the fixture root, including the integrity fixture location.
Some tests assume the original material session's baseline/angle/calibration
properties; an arbitrary capture directory is not an equivalent fixture.
Three older transport tests additionally use optional files under `upload/`.

| Test file | Coverage |
| --- | --- |
| `test_analysis_regression.py` | Shared analysis versus frozen scripts in `legacy/` |
| `test_capture_model.py` | Normalized capture model and range-bin precedence |
| `test_integrity_exports.py` | Integrity checks, progress, exports, and plots |
| `test_range_profiles.py` | Full range profiles, gates, and metric arithmetic |
| `test_openflight_bench.py`, `test_uart_reader.py` | Capture configuration and transport behavior |

Tests that modify captures or sidecars use temporary directories. Keep the
original capture files and frozen legacy scripts for repeatable comparisons.
Use `-rs` to inspect every skip; a clean-clone pass does not mean the external
dataset regression tests ran.
