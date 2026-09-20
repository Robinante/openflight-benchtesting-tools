# Analysis metrics -- what each number actually is

Audited in Chunk 4. For each metric: the data it consumes, the formula, the
units, and whether its label was accurate.

Two averaging conventions run through everything and must not be mixed:

| convention | formula | where it lives |
|---|---|---|
| **incoherent** | `mean(|X|^2)` over chirps/loops and RX | the range profile |
| **coherent** | `|mean(X)|^2`, mean over loops, phase kept | `element_mean` |

Coherent averaging suppresses phase-unstable content by up to `10*log10(N)` for
N loops. Incoherent averaging does not. **A ratio with a coherent numerator and
an incoherent denominator is not a signal-to-noise ratio.**

---

## Metrics

| metric | data | formula | units | label accurate? |
|---|---|---|---|---|
| `power_db` (profile) | incoherent profile | `10*log10(mean|X|^2)` | dB re 1 ADC count² | yes |
| `level_db` (element) | coherent `element_mean` | `20*log10(|X|)` | dB re 1 ADC count | yes |
| `peak_power_db` | incoherent profile | `max(power_db)` | dB | yes |
| `mean_power_db` | incoherent profile, optional gate | `10*log10(mean(power))` — linear mean, then log | dB | yes |
| `noise_floor_db` **(new)** | incoherent profile | `median(power_db[mask])`, mask clears ±guard around **every detected peak** | dB | yes |
| `peak_to_floor_db` **(new)** | incoherent profile, both halves | `peak_power_db - noise_floor_db` | dB | yes |
| `coherent_gain_db` **(new)** | both | `power_db(mean_elem(|coherent|²)) - power_db(profile[peak])` | dB | yes |
| `noise_db` (legacy) | incoherent profile | `10*log10(median(power[mask]))`, mask clears **only the chosen peak** ±guard | dB | **no** — it is a floor contaminated by every other real return |
| `snr_db` (legacy) | mixed | coherent numerator − contaminated incoherent denominator | dB | **no** — see below |
| `delta_power_db_vs_baseline` | coherent `element_mean` at one consensus bin | `power_db(measured) - power_db(reference)` per element | dB | yes (target-region already) |
| `phase_shift_vs_baseline_deg` | coherent `element_mean` | `angle(measured * conj(reference))` | degrees, (−180, 180] | yes |
| `coherence` / `coherence_across_repeats` | pre-average samples | `|mean(X)| / mean(|X|)` | dimensionless 0–1 | yes |

---

## The SNR problem, stated precisely

`snr_db` as implemented in `extract_capture_elements` (unchanged since the
original scripts) is:

```
numerator   = 10*log10( mean_over_elements( |coherent_mean_over_loops(X)|² ) )
denominator = 10*log10( median_over_bins( mean_over_chirps,rx(|X|²) ) )
              with only peak_bin ± guard_bins removed
snr_db      = numerator - denominator
```

**Two independent defects.**

1. **Mismatched estimators.** Numerator coherent, denominator incoherent. On a
   target whose phase is not perfectly stable across loops the numerator
   under-reads by the coherent loss, while the denominator does not. The
   difference is therefore not a ratio of like quantities.
2. **Contaminated denominator.** Every bin that is not the chosen peak counts as
   noise — including a stand, near-field leakage, or a second genuine target.

### Measured on real data

`material_baseline_20260906_215848_r1_g0001.l3dump`:

```
legacy snr_db        -1.386 dB     <- a clearly visible peak reported below "noise"
legacy noise_db      77.847 dB     <- only bin 30 ±2 excluded
noise_floor_db       76.918 dB     <- all 7 detected returns excluded
peak_to_floor_db      8.050 dB     <- same estimator both sides
coherent_gain_db     -8.507 dB     <- coherent average is 8.5 dB below incoherent
```

The identity closes exactly:

```
snr_db = peak_to_floor_db + coherent_gain_db + (noise_floor_db - noise_db)
-1.386 =        8.050     +     (-8.507)     +   (76.918 - 77.847)
```

`tests/test_chunk4_rawfirst.py::test_legacy_snr_identity_holds_on_real_data`
asserts this to 1e-6 on a real capture, and
`test_legacy_snr_is_a_mixed_estimator_and_is_labelled_so` reproduces it on
synthetic data where every term is known exactly (honest 20 dB, coherent gain
−10 dB, legacy 10 dB).

**So a reported "SNR" of −1.4 dB on that capture never meant the target was
below the noise.** It meant the two halves of the fraction were measured
differently.

### Resolution

- The legacy quantity is **kept, unchanged**, so historical CSVs stay
  reproducible and byte-identical. It is now reachable under the honest name
  `metrics.legacy_snr_db()`, whose docstring states both defects.
- `peak_to_floor_db` is the corrected ratio and is what the workbench shows as
  **"strongest above floor"**.
- `coherent_gain_db` exposes the coherent/incoherent difference directly, so a
  low value tells you the bin is not phase-stable rather than hiding inside a
  confusing SNR number.

**Not renamed in the CSVs.** Changing the `snr_db` column header would break
every existing sheet and script. The column keeps its name and its number;
the documentation and the new API carry the correction.

---

## Material comparison

**The audit did not find a whole-range mean.** `delta_power_db_vs_baseline` was
already computed from `element_mean` at a single consensus bin — a target-region
quantity. The concern that unrelated clutter could dominate it does not apply to
that column.

What *was* missing was an explicit, inspectable way to say which region is being
compared. Chunk 4 adds `metrics.compare_region(reference, measured, gate)`:

```
delta_db = 10*log10(mean(measured.power[gate])) - 10*log10(mean(reference.power[gate]))
```

- The gate is **required**. Passing `None` raises, with a message explaining
  that a whole-range mean is not material attenuation.
- `method="peak_in_gate"` uses `max` instead of `mean`.
- Nothing outside the gate contributes.

`test_whole_range_mean_would_have_been_wrong` makes the point concretely: a
scene where the target loses 3.01 dB while unrelated clutter gains 3.01 dB
yields a whole-range mean delta of **exactly 0.0 dB** — the real target loss
completely hidden — while the gated comparison reports **−3.0103 dB**.

---

## Not changed

- `rxGain` is preserved verbatim as capture metadata. No dB interpretation is
  applied anywhere in the analysis layer, and no historical capture's recorded
  value is rewritten.
- The reconstructed `analyzer_validation` helpers (`require_coherence`,
  `add_validation_arguments`, `prepare_analysis`, `run_checked`) were checked
  against the Chunk 4 metric paths. `require_coherence` is a no-op unless
  `--min-coherence` is set, and none of the new metrics call it. They do not
  affect any number in this document. Deeper cleanup stays in Chunk 5.
