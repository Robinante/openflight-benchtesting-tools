# Running the regression tests

The `.l3dump` capture fixtures are NOT in this archive (they are ~16 MB of
binary that you already have). Point the tests at your own captures:

```bat
set BENCH_MATERIAL_DIR=C:\Users\lazer\Documents\OpenFlight\BenchTesting\captures\material_session_20260906\2026-09-06
set BENCH_SWEEP_DIR=C:\Users\lazer\Documents\OpenFlight\BenchTesting\captures\boresight_test1
set BENCH_CALIBRATION=C:\Users\lazer\Documents\OpenFlight\BenchTesting\bench_calibration_reference.json
python -m pytest tests\test_analysis_regression.py tests\test_capture_model.py -q
```

Any directory of `.l3dump` + `.json` pairs works. Without those variables the
tests look in `tests/fixtures/{material_subset,sweep_subset}` and skip cleanly
if nothing is there -- they will not fail just because the captures are absent.

`test_capture_model.py` (Chunk 2) covers the normalized Capture model and the
range_bin_m precedence; it synthesizes its own modified sidecars in a tmpdir and
never touches the fixture files.

What the regression tests compare: `legacy/analyze_{material,sweep}_original.py` (frozen,
md5-identical copies of your scripts as they were) against the new
`openflight_bench.analysis` package, over the same captures, field by field.
