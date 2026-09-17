# Chunk 2 -- normalized capture model and result types

Builds on Chunk 1 rev 2. Scope as agreed: **the Capture model plus normalized
results**. Capture/serial/UART/radar-control code is still untouched, there is
still no plotting layer, and Streamlit is still not started.

This is a **separate copy**. Nothing was written back to the original project.

---

## 1. What changed

### The normalized Capture (`analysis/capture.py`, new)

Analysis used to pass around the raw ILD1 header dict and reach into it by key
(`meta["sample_fmt"]`, `meta["chirps_per_frame"]`, `meta["range_bin_starts"]`,
and nine others). A second parser would have had to fabricate ILD1 header fields
to get through extraction. It now goes through:

```
Capture
 |- source          CaptureSource        path, sidecar path, parser, format
 |                                       version, sample format + name, cfg used
 |- metadata        CaptureMetadata      test_type, kind, swatch, angle_deg,
 |                                       axis, repeat_index, label, raw sidecar
 |- configuration   CaptureConfiguration range_bin_m (+ where it came from),
 |                                       frame period, capture window, chirp
 |                                       params when derived
 |- frames          FrameSet             lazy iteration, range-bin tables,
 |                                       window_is_fixed, complete_only()
 |- channels        ChannelLayout        n_tx, n_rx, chirps_per_frame,
 |                                       loops_per_frame, n_virtual,
 |                                       tdm_divides_evenly
 |- timestamps      CaptureTimestamps    sidecar wall-clock, per-frame offsets,
 |                                       device_time_ms, per-die temperatures
 |- integrity       CaptureIntegrity     declared vs decoded frames, incomplete
 |                                       count, sidecar short_by, file bytes,
 |                                       complete, describe()
 |- header          the raw ILD1 meta dict, still there for unmigrated code
 |- warnings        anything worth surfacing about this capture
```

`FrameSet` is deliberately lazy. A session is hundreds of files; materializing
every frame of every one would be gigabytes. Iterating yields exactly what
`ild1.iter_frames` always yielded, so extraction code is unchanged.

Nothing is invented. Every field comes from data that already existed -- the
ILD1 header, the per-frame descriptor table, or the sidecar. A capture with no
sidecar still loads; it just has less context (`metadata` mostly `None`,
`range_bin_source == "derived"`).

### Normalized results (`analysis/results.py`, new)

```
AnalysisResult
 |- kind        "capture" | "sweep" | "material_comparison"
 |- tables      {table_name: [row dict, ...]}
 |- warnings    [str, ...]
 |- captures    [Capture, ...]
 +- table(name) / table_names() / row_counts() / collect_capture_warnings()

CaptureAnalysis          one capture: peak, profile, floor, SNR, per-element means
SweepResult              .element_rows / .summary_rows
MaterialComparisonResult .element_rows / .summary_rows / .baseline_timeline_rows
```

The old attribute names are now properties over `tables` and return the *same
list objects*, so existing code is unaffected and the row dicts are untouched --
same keys, same order, same values. What is new is that a caller can do

```python
for name, rows in result.tables.items():
    write_rows(out_dir / f"{name}.csv", rows)
```

without knowing which analysis produced it. That is what a Phase 2 page, a JSON
exporter and a generic test all need.

`RadarHealthResult` is deliberately absent -- there are no health checks to put
in it, and an empty shell would just be something else to migrate.

### `analyze_capture()` -- the single-capture entry point

`CaptureAnalysis` needed a producer, so `elements.analyze_capture(capture)`
returns one: peak bin, peak range, the full power profile (plus `profile_db`
and `range_axis_m` conveniences), noise floor, SNR, per-element complex means
and coherence. Same numbers as `extract_capture_elements` -- it wraps it and
adds the profile. This is what Phase 2's Analyze page calls.

### Extraction now consumes Captures

| before | now | old name kept? |
|---|---|---|
| `combined_power_profile(raw, meta)` | `capture_power_profile(capture)` | yes, wrapper |
| `extract_file_elements(path, range_bin_m=...)` | `extract_capture_elements(capture, ...)` | yes, wrapper |
| `pooled_profile_peak(paths)` | `pooled_profile_peak(captures)` | accepts Captures, entry dicts, or paths |
| `angle_at_bin(paths, ...)` | `angle_at_bin(captures, ...)` | material-internal |
| `estimate_target_bin_by_angle(paths, ...)` | `(captures, ...)` | material-internal |
| `_repeat_snapshot_at_bin(path, ...)` | `(capture, ...)` | material-internal |

The path-taking wrappers keep their old signatures and semantics, which is why
the Chunk 1 regression tests still pass unmodified.

`analyze_material` now loads one Capture per entry **once**, up front, and
reuses it everywhere. Previously the same file was re-parsed up to three times
(individual extraction, angle gating, then re-extraction at the consensus bin).

---

## 2. The one intentional behavior change

`range_bin_m` precedence, highest first:

1. `--range-bin-m` -- explicit user override. Always wins.
2. `capture_info.range_bin_m` from the capture's own sidecar. **(new)**
3. `range_bin_meters(num_adc_samples, slope_mhz_per_us, sample_rate_ksps)`.

Step 2 is new; the scripts went straight from 1 to 3. Every modern sidecar
records `0.0468425715625` and the CLI defaults derive the same number, so **on
every capture in the project's current sessions this changes nothing** -- which
is why the full byte-for-byte comparison below still passes.

It matters the moment a capture is taken with a different chirp. Previously the
analysis would have used the CLI defaults and produced ranges that silently
disagreed with the sidecar sitting next to the dump.

When the resolved value differs from what the old precedence would have given,
a warning is emitted per capture, collected onto the result, and printed:

```
!! foo.l3dump: using range_bin_m=0.0999 from the capture's own sidecar; the
   chirp settings in play here would have derived 0.0468425715625. Ranges follow
   the capture. Pass --range-bin-m to force the other way.
```

Older sidecars with no recorded value (e.g. `boresight_test1`, 3 Sep format)
fall through to step 3 and behave exactly as before, silently.

`tests/test_capture_model.py` covers all four branches, including a test that
asserts a changed `range_bin_m` actually moves `peak_range_m` rather than just
sitting in a metadata field.

---

## 3. Test results

```
36 passed, 3 skipped, 31 subtests passed
```

- `tests/test_analysis_regression.py` (Chunk 1, **unmodified**) -- 10 tests
  comparing against the frozen pre-refactor scripts in `legacy/`.
- `tests/test_capture_model.py` (new) -- 11 tests on the Capture model,
  the range_bin_m precedence, and the result interface.
- `tests/test_openflight_bench.py`, `tests/test_uart_reader.py` (yours,
  untouched) -- 15 passed, 3 skipped, 31 subtests.

End-to-end on the full real sessions, old vs new:

| output | rows | result |
|---|---|---|
| `material_elements.csv` | 252 | **byte-identical** |
| `material_summary.csv` | 21 | **byte-identical** |
| `baseline_timeline.csv` | 6 | **byte-identical** |
| `sweep_elements.csv` | 12 | **byte-identical** |
| `sweep_summary.csv` | 1 | **byte-identical** |

Console output is identical too, not just the CSVs.

Integrity: all seven `openflight_bench/*.py` capture-side modules,
`check_l3dump.py`, `pyproject.toml`, the profile cfg and both of your existing
test files are byte-identical to your originals by md5, and the frozen `legacy/`
copies still md5-match your `analyze_material.py` / `analyze_sweep.py`.
`openflight_bench/__init__.py` is still untouched, so `import openflight_bench`
remains numpy-free and the capture CLI is unaffected.

---

## 4. Questionable existing behavior -- observed, NOT changed

Carried forward from Chunk 1 §4, plus one found while rewiring:

8. **`analyze_material`'s baseline loop has an unconditional bare `raise`.**

   ```python
   for e in baseline_entries:
       try:
           r = extract_file_elements(...)
       except (ShortHeader, ValueError) as exc:
           raise
           print(f"  !! {e['l3dump_path'].name}: {exc}")   # unreachable
           continue                                         # unreachable
   ```

   The `print` and `continue` are dead code. One unreadable baseline file aborts
   the entire run rather than being skipped with a warning, which is what the
   dead lines intended. Left exactly as-is -- removing the `raise` would change
   behavior on real broken captures. Worth a decision.

9. **`n_frames_decoded` counts frames `iter_frames` yields, including
   incomplete ones.** `CaptureIntegrity.complete` therefore requires
   `n_frames_incomplete == 0` separately. This mirrors what the analysis already
   did (it filters on the `complete` flag); the model just makes it visible. Most
   captures in the current sessions report one incomplete trailing frame.

---

## 5. Chunk 3 recommendations -- NOT implemented

Unchanged from Chunk 1 §5 except that items 0 (Capture model) and the result
normalization are now done. Remaining, in the order I would take them:

1. **Presentation split.** The compute bodies still `print()` progress. A
   `progress=` callback defaulting to `print` keeps the CLI byte-identical and
   lets a GUI show status. The angle gate's candidate table is still only
   printed -- it should be returned so the Analyze page can render it and let
   the user override the chosen bin. Warnings already flow to
   `AnalysisResult.warnings`, so that half is done.
2. **JSON export + reconcile `openflight_bench/report.py` with
   `analysis/export.py`.** Now cheap: `AnalysisResult.tables` is already the
   serializable shape, so a JSON exporter is a few lines over the same
   structure. Phase 1 lists "CSV and JSON output".
3. **Capture control out of the CLI handlers.** The other half of Phase 1 and
   the only remaining blocker on its completion criterion ("a capture can be
   **acquired**, parsed, analyzed, exported, and plotted using reusable backend
   calls"). Larger and riskier than anything so far -- it touches hardware.
4. **A plotting layer.** Nothing exists. `CaptureAnalysis.profile_db` and
   `.range_axis_m` were added with this in mind.
5. **Get the real `analyzer_validation.py` back, or bless the reconstruction.**
   Still open. `CaptureIntegrity` is now the natural home for what it did.
6. **Curate a committed reference-capture set (Phase 6 groundwork):** one small
   capture per sample format, plus the known pathologies -- truncated last
   frame, trailing-`Done`, a clipped capture, a window-walk capture. The
   window-walk one matters more now: `capture_power_profile` still hard-fails on
   it, and `Capture.configuration.window_fixed` is the flag a caller would
   branch on.

Phase 2 / Streamlit is now unblocked as far as the analysis layer goes, but
item 1 should land first or the GUI will end up scraping stdout.
