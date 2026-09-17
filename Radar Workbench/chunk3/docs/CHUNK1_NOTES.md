# Chunk 1 -- analysis consolidation

**Revision 2 (2026-09-17)** -- re-homed `bench_analysis/` to
`openflight_bench/analysis/` and renamed `MaterialResult` to
`MaterialComparisonResult` to match the roadmap's Phase 1 result naming
(`CaptureAnalysis`, `SweepResult`, `MaterialComparisonResult`,
`RadarHealthResult`). No logic changed; all CSV output re-verified
byte-identical after the move. `CaptureAnalysis` and `RadarHealthResult` are
deliberately NOT stubbed -- they belong with the normalized `Capture` model in
Chunk 2, and inventing empty shells now would just be something else to migrate.

Refactor of the analysis half of `openflight-benchtesting-tools`. Capture,
serial, UART and radar-control code was not touched. This is a **separate
copy**; nothing was written back to the original project.

---

## 1. Blocker found before any code was changed

`analyze_material.py` **could not run**. Line 195:

```python
from analyzer_validation import (
    ShortHeader, DataValidationError, parse_header, _read_frame_metadata, iter_frames,
    read_capture_bytes, load_entries, require_coherence, add_validation_arguments,
    prepare_analysis, run_checked,
)
```

`analyzer_validation.py` does not exist in the repository (13 `.py` files, none
of them it), nor in `BenchTesting\`. `import analyze_material` raised
`ModuleNotFoundError`. `analyze_sweep.py` was unaffected -- it still carried its
own `parse_header` / `iter_frames`.

This looks like a half-finished version of exactly this refactor: the imports
were switched to a shared module that never landed.

Seven of the eleven names were recoverable with certainty:

| name | recovered from |
|---|---|
| `ShortHeader`, `parse_header`, `_read_frame_metadata`, `iter_frames` | `analyze_sweep.py`, verbatim -- `analyze_material.py`'s own docstring says they are "the same logic as analyze_sweep.py" |
| `read_capture_bytes` | only sane meaning: `Path(p).read_bytes()`, which is literally what `analyze_sweep.py` calls at the same call sites |
| `load_entries(dir, "material")` | `analyze_sweep.py`'s `load_sweep_files`, plus a `test_type` filter |
| `DataValidationError(code, message)` | inferred from its five two-argument call sites |

Four were **not** recoverable and are reconstructed conservatively so the
default command line behaves exactly as before:

| name | reconstruction |
|---|---|
| `require_coherence(coherence, where)` | no-op unless `--min-coherence` is given |
| `add_validation_arguments(ap)` | adds `--min-coherence` and `--self-test` only |
| `prepare_analysis(args, kind, extractor)` | returns `False` (do not short-circuit) unless `--self-test` |
| `run_checked(fn)` | runs `fn`, turning `DataValidationError` into a message + exit code 2 |

**If the real module enforced stricter validation, that strictness is not
reproduced.** It lives in `openflight_bench/analysis/validation.py`, isolated and clearly
marked, so dropping in the real file later is a one-file swap.

A root-level `analyzer_validation.py` shim re-exports all eleven names from the
package, so anything else that imported the old module keeps working.

---

## 2. Architecture

```
            .l3dump + .json sidecar
                      |
              loaders.load_entries          <- discovery, pairing, sidecar fields
                      |
     ild1.parse_header / _read_frame_metadata / iter_frames
                      |
   elements.combined_power_profile / pooled_profile_peak
            elements.extract_file_elements
            elements.combine_repeats
                      |
         +------------+------------+
         |                         |
  material.analyze_material   sweep.analyze_sweep
         |                         |
         +------------+------------+
                      |
              export.write_rows          <- CSV, unchanged column order
```

Placed inside the existing `openflight_bench` package rather than as a sibling
top-level package, so there is one import root for the CLI / workbench / tests
triangle and an obvious home for `openflight_bench/backends/` in Phase 4.
`openflight_bench/__init__.py` is deliberately NOT modified: `import
openflight_bench` stays numpy-free, and the capture CLI gains no new dependency.
Analysis is opt-in via `import openflight_bench.analysis`.

```
openflight_bench/
    __init__.py  capture.py  cli.py  config.py  report.py  wire.py   <- untouched
    analysis/
        __init__.py      public API re-exports
    ild1.py          ILD1 format constants, ShortHeader, parse_header,
                     _read_frame_metadata, iter_frames, range_bin_meters
    loaders.py       read_capture_bytes, load_entries (+ sweep/material aliases),
                     group_by_angle
    validation.py    reconstruction of the missing analyzer_validation layer
    elements.py      combined_power_profile, pooled_profile_peak,
                     extract_file_elements, combine_repeats
    export.py        write_rows
    material.py      calibration + angle gating + analyze_material() + CLI
    sweep.py         analyze_sweep() + CLI
```

Each analysis module is split three ways, which is what makes it GUI-callable:

```python
build_arg_parser()      # the original argparse block, verbatim
analyze_material(args)  # the original computation, verbatim, now RETURNING rows
main()                  # parse -> analyze -> write CSVs
```

The computation bodies were moved **without a single internal edit**. That was
possible because the parameter is still named `args`, and `MaterialOptions` /
`SweepOptions` are dataclasses whose attribute names and defaults mirror the
argparse Namespace exactly. So:

```python
from pathlib import Path
from openflight_bench.analysis import MaterialOptions, analyze_material

result = analyze_material(MaterialOptions(capture_dir=Path("captures/mat_session")))
result.element_rows            # list[dict], the material_elements.csv rows
result.summary_rows            # material_summary.csv rows
result.baseline_timeline_rows  # baseline_timeline.csv rows
```

Nothing is written to disk unless you call `write_rows` yourself.

---

## 3. What was genuinely duplicated, and what was not

Comparing the two scripts function by function:

**Byte-identical in both (16 definitions)** -- every ILD1 sample-format
constant, `MAGIC`, `MAX_SUPPORTED_DUMP_VERSION`, the `DEFAULT_*` chirp
parameters, `SPEED_OF_LIGHT_M_S`. Moved to `ild1.py` unchanged.

**Identical logic, cosmetic differences only** -- `HEADER` / `TEMP_REPORT` /
`TIMED_FRAME_DESCRIPTOR` (trailing comments), `range_bin_meters` (docstring),
`combined_power_profile` (docstring + error wording), `pooled_profile_peak`
(docstring + `Path(p).read_bytes()` vs `read_capture_bytes(p)`). The
`analyze_sweep.py` copies were kept as canonical.

**Genuinely different -- handled carefully, not flattened:**

- `extract_file_elements` -- material's copy added `extra_noise_exclude_bins`,
  and where sweep fell back to the whole profile when the guard band consumed
  every bin, material raised `INSUFFICIENT_NOISE_BINS`. Material's version is
  now shared, with that one divergence behind `strict_noise` (default `False`
  = sweep's fallback; material passes `True`). Both behaviors preserved.
- `combine_repeats` -- material's added a `peak_bin` key and a
  `require_coherence` call. Material's version is shared. The extra key cannot
  leak into sweep's CSVs: both scripts build rows field by field and never
  splat this dict (verified).
- `main` -- entirely different, correctly so. Kept separate.

**Deliberately left separate** -- everything material-specific (the
`Calibration` class, `load_calibration`, `_bartlett_elevation_deg`,
`_orthogonal_axis_deg`, `angle_at_bin`, `estimate_target_bin_by_angle`, the
angle-key helpers) and everything sweep-specific (`group_by_angle`, the
reference-element phase logic). No shared "do everything" function was created.

---

## 4. Questionable existing behavior -- observed, NOT changed

1. **The missing `analyzer_validation` module.** Section 1. Biggest one.
2. **`pooled_profile_peak` is called with `guard_bins` from the CLI in sweep but
   the default in some material paths.** Left as-is; changing it would move
   numbers.
3. **`combined_power_profile` hard-fails on a moving capture window** with a
   message naming `iwr6843_benchtesting_static_sweep.cfg`, but both tools are
   routinely pointed at captures taken with other `.cfg` files. The check is
   correct; the message is misleading.
4. **`extract_file_elements` computes `snr_db` against a whole-window median.**
   The material docstring already explains at length that stands, near-field
   leakage and sidelobe smear inflate that floor. Unchanged, but it means
   `snr_db` is pessimistic on a cluttered bench.
5. **The angle gate can reject every candidate bin and raise**, aborting the
   whole run rather than falling back or reporting per-group. On the
   `material_session_20260906` fixture with the real calibration, that is
   exactly what happens. Existing behavior, preserved, and the regression test
   asserts old and new raise identically.
6. **`load_entries` skips sidecars missing `angle_deg`/`axis` with a printed
   warning**, so a capture directory mixing `test_type: "raw"` and
   `"material"` silently drops the raw ones. Preserved.
7. **Material's noise-floor `max(..., 1e-12)` clamp** differs subtly in
   formatting between the two originals but not in value.

---

## 5. Chunk 2 recommendations -- NOT implemented

Re-pointed at the roadmap's Phase 1 completion criterion ("a capture can be
acquired, parsed, analyzed, exported, and plotted using reusable backend calls
while the existing CLI continues to work"). Chunk 1 delivered *analyzed* and
*exported*. Still open: *acquired* (capture control is still inside the CLI
handlers), *parsed* (reusable but not normalized), *plotted* (no plotting layer
exists at all).

0. **The normalized `Capture` model is the keystone.** Analysis still reaches
   into the raw ILD1 header dict (`meta["sample_fmt"]`, `meta["chirps_per_frame"]`,
   `meta["range_bin_starts"]`), so "downstream analysis should not need to know
   which parser produced the capture" is not yet true and an OPS or replay
   backend would have to fake ILD1 fields to get through `extract_file_elements`.
   Concrete symptom: every modern sidecar records `capture_info.range_bin_m`
   (0.0468425715625) and the analysis ignores it, recomputing from the
   `--num-adc-samples 128 / --slope 100.0 / --sample-rate 4000.0` defaults. They
   agree today only because the chirp has not changed. Change the slope or sample
   count and every range in every CSV goes quietly wrong next to a sidecar
   holding the right value. That is `configuration` in the Capture model.

1. **Get the real `analyzer_validation.py` back, or bless the reconstruction.**
   Everything else is downstream of knowing what validation was intended. Note
   the Capture model has an `integrity` field and `check_l3dump.py` / `wire.py`
   already do integrity work -- that is where this layer belongs, rather than as
   a standalone module.
2. **Separate presentation from analysis properly.** The compute bodies still
   `print()` progress as they go. A GUI wants a callback or a structured log,
   not stdout. Suggest a `progress=` callable defaulting to `print`.
3. **Return typed records, not `list[dict]`.** The rows are CSV-shaped
   (stringly typed, column order load-bearing). A dataclass per row with an
   explicit `to_csv_row()` would let a GUI bind fields without guessing.
4. **`capture_dir` should accept an iterable of files.** A GUI will want to
   analyze a selection, not a whole directory.
5. **Lift the angle gate out of `analyze_material`** into its own callable that
   returns candidates and verdicts, so a GUI can show the candidate table (it
   is currently only printed) and let the user override the chosen bin.
6. **`openflight_bench/report.py` overlaps `analysis/export.py`, and there is no
   JSON exporter for analysis results** (the JSON in this repo is capture
   sidecars). Phase 1 lists "CSV and JSON output"; one export layer emitting both
   from the normalized result structures closes that item.

7. **Do not start Phase 2 / Streamlit before the Capture model exists.** A GUI
   built against IWR6843-shaped dicts makes Phase 4 a rewrite of the GUI rather
   than the addition of a backend.

8. **Curate a committed reference-capture set (Phase 6 groundwork).** The
   regression harness here already has the shape -- frozen originals in
   `legacy/`, golden CSVs in `docs/golden/`, fixtures resolved through
   `BENCH_MATERIAL_DIR` / `BENCH_SWEEP_DIR` / `BENCH_CALIBRATION`. What is
   missing is a small committed set covering each sample format and each known
   pathology: truncated last frame, the trailing-`Done` case, a clipped capture,
   a window-walk capture.
