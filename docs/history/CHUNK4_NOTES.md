# Chunk 4 -- raw-first analysis

> Historical snapshot. For current setup and layout, see the [repository README](../../README.md).

Built on Chunk 3. Capture/serial/UART/radar-control code untouched. No
acquisition work. Separate copy; nothing written back to the original project.

**Principle:** show what the radar measured first, and what the software thinks
it means second.

---

## 1. What changed

### New: `analysis/rangeprofile.py`

```
RangeProfile     every captured bin, produced with NO peak selection
range_profile()  the primary product of opening a capture
pooled_profile() several captures of one scene, summed, still no selection
Peak             one local maximum: bin, range, power, prominence, rank
detect_peaks()   ALL candidates, ranked -- not a verdict
Gate             explicit region of interest; annotation only
```

`Gate` cannot shrink a profile. `profile.within(gate)` returns a **copy**, and
a test writes into that copy to prove the original is untouched.

### New: `analysis/metrics.py`

Every metric carries its data source, formula and units in the docstring. See
`ANALYSIS_METRICS.md` for the table.

### Changed: `analyze_capture()`

Now computes the full profile **first and unconditionally**, then layers
annotation. A target-selection failure is recorded in `annotation_error`
instead of raising, so you never lose the data because the software could not
pick a target.

```python
a = analyze_capture(cap)                    # full profile + peaks
a = analyze_capture(cap, annotate=False)    # pure raw, no detection at all
a = analyze_capture(cap, gate=Gate.around(prof, 1.5))   # + region metrics
```

`CaptureAnalysis` gained `profile`, `peaks`, `gate`, `annotation_error`,
`noise_floor_db`, `peak_to_floor_db`, `coherent_gain_db`, `gate_metrics`.
Existing fields kept.

### New plot: `plots.raw_range_profile()`

Draws every captured bin. Peaks are markers (green inside the gate, orange
outside), a gate is a shaded span with dashed bounds, and the axis is **not**
restricted unless `zoom=True` is explicitly passed.

### Workbench Analyze page

Full profile is the first thing rendered, before any metric. Headline numbers
are now **captured bins**, **returns detected**, **no-return floor**,
**strongest above floor**. Gate is an opt-in sidebar control with an explicit
"Zoom to gate" checkbox that defaults to off. The legacy element view and
`snr_db` are demoted into a collapsed expander with a caption pointing at
`ANALYSIS_METRICS.md`.

---

## 2. Intentional behavior changes

| # | change | effect on existing outputs |
|---|---|---|
| 1 | `analyze_capture` no longer raises when target selection fails | **none on CSVs.** Callers that relied on the exception now see `annotation_error` and `peak_bin is None`. The material/sweep CLIs do not use `analyze_capture`. |
| 2 | `CaptureAnalysis.peak_bin` / `snr_db` / `noise_db` may be `None` | only when annotation failed; previously you got an exception and no object at all |
| 3 | Noise floor for the **new** metric excludes all detected returns | new metric; the legacy `noise_db` column is unchanged |
| 4 | `compare_region()` refuses to run without a gate | new API; nothing previously called it |
| 5 | Workbench Analyze headline metrics changed | UI only |

**Not changed:** every CSV column, name, order and value. `snr_db` keeps its
name and its number — see `ANALYSIS_METRICS.md` for why renaming the column was
rejected.

Re-verified this chunk on the full 6 Sep material session and `boresight_test1`:

```
material_elements.csv   252 rows  IDENTICAL
material_summary.csv     21 rows  IDENTICAL
baseline_timeline.csv     6 rows  IDENTICAL
sweep_elements.csv       12 rows  IDENTICAL
sweep_summary.csv         1 row   IDENTICAL
```

**No golden file was updated.** Nothing needed updating.

---

## 3. Test summary

```
65 passed, 3 skipped, 31 subtests passed
```

- `tests/test_range_profiles.py` — **17 new**, covering all six requested
  categories. 10 run without any fixture (synthetic, exact arithmetic).
- `tests/test_integrity_exports.py` (12), `test_capture_model.py` (11),
  `test_analysis_regression.py` (10) — unmodified, all pass.
- `tests/test_openflight_bench.py`, `test_uart_reader.py` — the operator's own,
  untouched: 15 passed, 3 skipped, 31 subtests.

The 3 skips are unchanged and are documented in `PREVIOUS_AUDIT_STATUS.md`.

### The six requested proofs

| requirement | test |
|---|---|
| full-range preservation | `test_profile_keeps_every_captured_bin_synthetic` / `_real` |
| gate independence | `test_two_different_gates_do_not_alter_the_profile`, `test_gate_choice_does_not_change_underlying_data_real` |
| no-gate behavior | `test_full_analysis_works_with_no_gate_and_no_annotation`, `test_plot_renders_without_any_gate`, `test_annotation_failure_does_not_cost_the_profile` |
| unexpected-return preservation | `test_return_outside_the_gate_stays_visible` |
| SNR correctness | `test_peak_to_floor_is_exact_on_synthetic_data`, `test_floor_excludes_other_real_returns`, `test_legacy_snr_is_a_mixed_estimator_and_is_labelled_so`, `test_legacy_snr_identity_holds_on_real_data` |
| material comparison correctness | `test_region_comparison_is_exact_and_ignores_outside_clutter`, `test_whole_range_mean_would_have_been_wrong`, `test_compare_region_refuses_to_run_without_a_gate` |

Synthetic scenes use a floor of 1.0 (0 dB) with a target at 100.0 (20 dB) and a
second return at 50.0, so expected values are exact to 1e-9 rather than
"whatever the code produced".

---

## 4. Dependencies

| package | CLI | analysis | plotting | GUI | notes |
|---|---|---|---|---|---|
| `numpy` | yes | **required** | yes | yes | already used by the original scripts |
| `pyserial` | capture only | no | no | no | pre-existing, declared in `pyproject.toml` |
| `matplotlib` | no | no | **required** | required | **new in Chunk 3.** Imported lazily inside `plots._plt()`; the analysis layer works without it |
| `streamlit` | no | no | no | **required** | **new in Chunk 3.** Only imported by `workbench/app.py` |
| `pytest` | no | no | no | no | tests only |

`import openflight_bench` remains numpy-free — verified — so the capture CLI
gains nothing. Analysis is opt-in via `import openflight_bench.analysis`.

Minimums: analysis `pip install numpy`; plots `+ matplotlib`; GUI
`+ streamlit`. `pyproject.toml` is **unchanged** (it declares only `pyserial`);
the new dependencies are documented here and in `workbench/README.md` rather
than added, since adopting them is the operator's decision.

---

## 5. File-level adoption plan

Carried over from the unfinished prior audit. Nothing here is required; adopt in
stages and stop wherever it stops being useful.

### Stage 1 — additive only, zero risk

Copy in; nothing existing changes behavior.

```
openflight_bench/analysis/          entire new subpackage
analyzer_validation.py              NEW at repo root -- REQUIRED for analyze_material.py to import at all
docs/CHUNK{1,2,3,4}_NOTES.md
docs/history/RANGE_ANALYSIS_AUDIT.md
docs/ANALYSIS_METRICS.md
docs/history/PREVIOUS_AUDIT_STATUS.md
docs/golden/                        reference CSVs
tests/test_analysis_regression.py
tests/test_capture_model.py
tests/test_integrity_exports.py
tests/test_range_profiles.py
tests/FIXTURES.md
legacy/                             frozen pre-refactor scripts (test-only)
```

`openflight_bench/__init__.py` is **not** modified, so the capture CLI is
unaffected by Stage 1.

### Stage 2 — replaces two files

```
analyze_material.py    REPLACED by a 12-line wrapper
analyze_sweep.py       REPLACED by a 12-line wrapper
```

Same command lines, same flags, same CSV bytes. Keep your originals — the
copies in `legacy/` are md5-identical to them and are what the regression tests
compare against. **Do this only after Stage 1's tests pass on your machine.**

### Stage 3 — optional GUI

```
workbench/app.py
workbench/README.md
workbench/run_workbench.bat
```

Needs `pip install streamlit matplotlib`.

### Not for adoption

```
README.md              contains chunk-specific sections; merge by hand if wanted
pyproject.toml         unchanged from yours -- do not copy over
tests/fixtures/        excluded; point BENCH_MATERIAL_DIR etc. at your captures
```

### Verification after each stage

```bat
set BENCH_MATERIAL_DIR=...\captures\material_session_20260906\2026-09-06
set BENCH_SWEEP_DIR=...\captures\boresight_test1
set BENCH_CALIBRATION=...\bench_calibration_reference.json
python -m pytest tests -q
```

Expect `65 passed, 3 skipped, 31 subtests`.

---

## 6. Unresolved -- Chunk 5 candidates

1. **`analyzer_validation` is still reconstructed.** `require_coherence`,
   `add_validation_arguments`, `prepare_analysis`, `run_checked` were inferred.
   Confirmed this chunk that they do not affect any Chunk 4 metric —
   `require_coherence` is a no-op unless `--min-coherence` is passed and none of
   the new metrics call it. Either recover the original or bless the
   reconstruction and delete the caveat.
2. **Material/sweep CLIs still use automatic bin selection.** Chunk 4 made the
   *inspection* path raw-first; `analyze_material` still picks one consensus bin
   internally. Wiring `compare_region` + an explicit `--gate` into the material
   CLI is the natural next step, and would change CSV numbers, so it needs its
   own chunk and its own decision.
3. **`peak_to_floor_db` is not in any CSV.** Only `CaptureAnalysis` and the
   workbench carry it. Adding it as a new column is safe; changing `snr_db` is
   not.
4. **`openflight_bench/report.py` still overlaps `analysis/export.py`**, still
   deferred for the same reason (capture-side, would drag numpy into
   `import openflight_bench`).
5. **The bare `raise`** in `analyze_material`'s baseline loop still makes two
   lines dead code.
6. **Acquisition.** Capture control still lives in the CLI handlers. This is the
   only remaining Phase 1 criterion and the only one that needs the radar.
7. **Sweep/Compare pages** were left as they were — this chunk's workbench scope
   was Analyze only.
