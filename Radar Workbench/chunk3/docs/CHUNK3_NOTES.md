# Chunk 3 -- integrity, presentation split, JSON export, plots, workbench

Builds on Chunk 2. Capture/serial/UART/radar-control code is still untouched.
Separate copy; nothing written back to the original project.

Scope agreed was presentation split + JSON export. Two things were added on top:
**SHA-256 verification**, because the capture tool now records it and the
Capture model was the obvious home; and **plots + a Streamlit workbench**
(previously scoped as chunk 5), because the analysis layer was ready and the
goal was a working GUI.

---

## 1. Integrity (`analysis/integrity.py`, new)

ILD1 v7 carries no checksum, so verification has to come from outside the dump.
The capture tool already writes it in three places and this reads all three
rather than inventing a fourth:

| source | fields |
|---|---|
| `<capture>.json` | `file_sha256`, `wire_sha256`, `wire_file`, `wire_bytes`, `config_sha256`, `accepted_for_analysis`, `capture_info.status` / `expected_bytes` / `actual_bytes` / `extra_bytes` / `completion_seen` |
| `SHA256SUMS` | standard `sha256sum -c` format, per directory, cached |
| `session_manifest.json` | per-capture `status`, applied profile, per-profile `config_sha256` |

`load_capture` hashes the bytes it read and compares. Precedence for the
expected digest is sidecar `file_sha256` first (same process, same moment as the
dump), then `SHA256SUMS`.

`CaptureIntegrity` gained:

```
sha256              computed from the bytes actually read
sha256_verified     True / False / None   (None = no digest was recorded)
tampered            a digest exists and does not match
usable              not rejected by the capture tool, and not corrupt
accepted_for_analysis, status, expected_bytes, actual_bytes, extra_bytes,
completion_seen, manifest_status
content.wire_sha256 / wire_file / wire_bytes / config_sha256
```

A mismatch, or `accepted_for_analysis: false`, raises a warning onto the capture
and thence onto the result. **Nothing is rejected automatically** -- the model
records what it found and the caller decides. Captures predating `file_sha256`
report `sha256_verified is None`, not a failure.

`verify_directory(path)` gives the same answer as `sha256sum -c`, plus files on
disk the sums file does not cover.

Verified against the real `bench_20260914_191529_683136` session: digest matches
the sidecar, `wire_sha256` and `config_sha256` are carried, `accepted_for_analysis`
is `True`, manifest status `complete`. A test flips one bit deep in the payload
and asserts detection.

**On a v8:** a payload hash inside the dump would let a reader detect corruption
with no sidecar, and would cover the case where dump and sidecar are separated.
Until then this is strictly external, which is why `sha256_verified` is
three-valued rather than a boolean -- "not recorded" is a real state and must
not be confused with "failed".

## 2. Presentation split (`analysis/progress.py`, new)

`analyze_material` and `analyze_sweep` take `progress=`. Every line they used to
print goes there instead. `as_progress` wraps any one-argument sink into
something `print`-compatible (same `*args`, `sep`, `end`), so the analysis
bodies needed no edits and `progress=None` returns the real builtin -- which is
why CLI console output is still byte-identical.

`CollectingProgress` keeps the lines and can mirror them live.

The angle gate's narration routes through the same sink, so a GUI run produces
nothing on stdout at all (there is a test asserting exactly that).

## 3. The angle-gate candidate table escapes

`estimate_target_bin_by_angle` gained `candidate_sink`, filled **before** the
pass/fail check, so a gate that rejects everything still hands back what it
looked at. `analyze_material` collects it into `tables["angle_gate_candidates"]`
and, when selection fails, attaches the partial result to the exception as
`exc.result`. The exception type and message are unchanged, which the Chunk 1
regression test still asserts.

That table was previously printed text only, and it is the most useful output
the gate produces when it fails.

## 4. JSON export

`write_json(path, result)` writes `{"metadata": ..., "tables": ...}`. The tables
are the same row dicts as the CSVs; a test asserts field-by-field agreement
between the two exports. The metadata is what a CSV cannot carry:

```
kind, generated_utc, row_counts, warnings, range_bin_sources,
n_captures, n_sha256_verified / _unrecorded / _mismatch,
captures[]: file, sha256, sha256_verified, sha256_source,
            accepted_for_analysis, frames_declared/decoded/incomplete, status,
            range_bin_m, range_bin_source, sample_format, config_path,
            n_tx, n_rx, window_start_bin, window_bin_count
```

So a result file now states where its ranges came from and whether every input's
bytes verified. `result_metadata(result)` is exported separately for callers
building their own envelope.

## 5. Plots (`analysis/plots.py`, new)

Computes nothing -- every number comes from a Capture or an AnalysisResult.
matplotlib is imported lazily, so the analysis layer still works without it.

`range_profile`, `overlay_profiles`, `element_heatmap` (level or phase),
`rows_bar` (generic over any result table), `angle_gate_candidates`,
`baseline_drift`, `save`.

Terminology is pinned in the module docstring: level = `20*log10(mean |X|)` in
ADC counts, power = `10*log10(mean |X|^2)`, ranges in inches by default.

## 6. Workbench (`workbench/app.py`, new)

```
pip install streamlit matplotlib
streamlit run workbench/app.py          # or workbench\run_workbench.bat
```

Five pages -- Session, Analyze, Compare, Material, Sweep -- over saved captures.
It holds no analysis logic; every page calls the same functions the CLI calls.
Session verifies a folder against `SHA256SUMS` on demand and shows each
capture's integrity state. Material shows the angle-gate candidate table when
the gate rejects everything, instead of only an error. Every result page offers
CSV per table plus the JSON envelope.

**Offline only.** Live capture is the remaining Phase 1 chunk, the one that has
to touch the radar.

## 7. Test results

```
48 passed, 3 skipped, 31 subtests passed
```

`tests/test_chunk3.py` is new (12 tests): sha256 against the real sidecar
format, `SHA256SUMS` fallback, bit-flip detection, "not recorded" handling,
`verify_directory`, stdout silence under `progress=`, unchanged CLI printing,
`as_progress` print-compatibility, the candidate table surviving a failed gate,
JSON/CSV agreement, and every plot rendering.

Chunks 1 and 2 test files are unmodified and still pass.

End-to-end on the full real sessions, old vs new: `material_elements` (252),
`material_summary` (21), `baseline_timeline` (6), `sweep_elements` (12),
`sweep_summary` (1) all **byte-identical**, console output identical.

Capture-side files, `pyproject.toml`, the profile cfg and your two existing test
suites are md5-unchanged; `legacy/` still md5-matches your originals.

## 8. Not done, deliberately

- **`openflight_bench/report.py` is NOT reconciled with `analysis/export.py`.**
  They do overlap. But `report.py` is capture-side, it imports `wire.inspect_bytes`,
  and importing the analysis package from it would drag numpy into
  `import openflight_bench` and break the property that the capture CLI has no
  new dependency. It is a small change and it belongs in the capture-side chunk,
  with the rest of that module's neighbours. Flagged, not forced.
- **`RadarHealthResult`** -- still nothing to put in it.
- **The bare `raise`** in `analyze_material`'s baseline loop (Chunk 2 §4 item 8)
  is still there, still making two lines dead code.

## 9. Phase 1 status

| criterion | state |
|---|---|
| acquired | **not started** -- capture control still lives in the CLI handlers |
| parsed | done (Chunk 2) |
| analyzed | done (Chunk 1) |
| exported | done -- CSV and JSON |
| plotted | done |
| existing CLI still works | verified byte-identical every chunk |

One chunk left: capture control out of the CLI handlers. It is the only one that
needs the radar in front of you.
