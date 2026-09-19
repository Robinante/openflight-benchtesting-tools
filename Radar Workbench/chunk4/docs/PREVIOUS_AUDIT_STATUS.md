# Previous audit -- recovered status

The prior session started a seven-item audit of the Chunk 3 deliverable and ran
out of budget partway. This records only what is recoverable from existing
evidence (the Chunk 3 archive, its docs, and re-running its tests). It was not
restarted.

## Completed

**Item 1 — the three skipped tests.** Identified exactly. All three are in
`tests/test_openflight_bench.py`, which is the operator's own capture-side
suite, untouched by any chunk:

| test | line | skip reason |
|---|---|---|
| `test_supplied_dumps_are_gated_by_length` | 58 | `optional uploaded raw-dump fixtures are not present` |
| `test_reader_strips_completion_text_and_rejects_short_payload` | 98 | `optional uploaded raw-dump fixture is not present` |
| `test_capture_triggers_firmware_dump_before_reading` | 119 | `optional uploaded raw-dump fixture is not present` |

All three look for `<repo>/upload/raw_couch_rxgain_sweep_1_*.l3dump`, which is
deliberately excluded from clean clones. They skip for one structural reason:
**the fixture directory is not in the repository.** Nothing is wrong with the
tests or the code under them.

Incidental finding: each guard is written twice — a `pytest.skip(...)` followed
by a now-unreachable `if not paths: return`. Harmless, but the same dead-code
pattern as the bare `raise` noted in Chunk 2 §4.

**Item 4 (partially) — clean-extract verification.** The Chunk 3 archive was
extracted to a fresh directory and the suite run. Two results worth carrying
forward:

- From a clean extract **with no capture fixtures**, far more than three tests
  skip — 19 of them, because `BENCH_MATERIAL_DIR` / `BENCH_SWEEP_DIR` are unset
  and `tests/fixtures/` is excluded from the archive. The "48 passed, 3 skipped"
  figure is only reproducible when those variables point at real capture
  directories.
- With them set, the clean extract reproduced **48 passed, 3 skipped, 31
  subtests** exactly.

## Partially completed

**Item 2 — remaining-risk list.** The Chunk 1–3 notes already carry most of it
(`docs/CHUNK1_NOTES.md` §4, `CHUNK2_NOTES.md` §4, `CHUNK3_NOTES.md` §8). It was
never consolidated into one document. The reconstructed-code risk is recorded in
`CHUNK1_NOTES.md` §1 and is restated in `CHUNK4_NOTES.md`.

**Item 5 — dependency list.** Not written up, but recoverable by inspection and
now recorded in `CHUNK4_NOTES.md`.

## Not completed

- **Item 3 — the five-value legacy-CLI-vs-workbench comparison.** A comparison
  script was written but never ran to completion; the session's tool backend
  began failing on long inline commands. **No numeric comparison table was ever
  produced.** Chunk 4 does not reproduce it either — but note that the
  byte-identical CSV check (below) is a strictly stronger statement about the
  same code path, since the workbench calls the same functions the CLI calls.
- **Item 6 — file-level adoption plan.** Never written.
- **Item 7** was an instruction not to make further changes, not a work item.

## Important findings that survive

1. **48 passed / 3 skipped / 31 subtests** reproduced on a clean extract of the
   Chunk 3 archive with fixtures pointed at real captures.
2. **Material and sweep CSV outputs byte-identical to the frozen legacy scripts**
   on the full 6 Sep material session and `boresight_test1` — 252 / 21 / 6 / 12 /
   1 rows — with console output identical too. Re-verified in Chunk 4.
3. **Capture-side files md5-unchanged** across every chunk, and `legacy/` still
   md5-matches the operator's original `analyze_material.py` / `analyze_sweep.py`.
4. The archive alone cannot run the capture-backed tests; it needs either
   `tests/fixtures/` populated or the three `BENCH_*` environment variables.
   `tests/FIXTURES.md` documents this.

## Carried into Chunk 4

The adoption plan (old item 6) is written up in `CHUNK4_NOTES.md`. The
five-value comparison (old item 3) is **not** attempted here; the byte-identical
CSV verification covers the same ground more strongly, and re-running a
superseded audit was not a good use of this chunk's budget.
