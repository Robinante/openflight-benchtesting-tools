# Range-bin selection and gating audit (Chunk 3 code, pre-Chunk-4)

> Historical snapshot. For current setup and layout, see the [repository README](../../README.md).

Traced every place a range bin is chosen, and what downstream depends on it.

## 1. Where bins are selected

| site | file:line | how |
|---|---|---|
| per-capture peak | `analysis/elements.py:165-175` | `local_peak = int(np.argmax(profile))`, or `forced_peak_bin` when the caller supplies one |
| group consensus peak | `analysis/elements.py:123` | `start_bin + int(np.argmax(pooled))` over the summed profiles of a repeat group |
| angle gate | `analysis/material.py:262-320` | top-K bins by pooled power, each scored on elevation/axis/range, **raises `ValueError` if none pass** |
| material dispatch | `analysis/material.py:526-546` | `select_consensus_bin()` picks angle gate or plain argmax |

## 2. What depends on the selected bin

| consumer | affected? | detail |
|---|---|---|
| **power profile** | **no** | `capture_power_profile()` averages every complete frame over the whole captured window and returns `(start_bin, profile[count])`. It never looks at a peak. The full profile is computed before any selection. |
| **plotting** | **indirectly, yes** | `plots.range_profile()` draws the whole array, so the picture is full-range. But it is fed by `CaptureAnalysis`, which is built by `analyze_capture()` -> `extract_capture_elements()`, and that call **raises** on several conditions (no usable frames, forced bin outside window, TDM mismatch, `INSUFFICIENT_NOISE_BINS`). So a failure in *peak/element extraction* prevents you from seeing the *profile*, even though the profile itself was computed fine. |
| **normalization** | **no** | Nothing is normalized by the peak. Profile values are absolute mean power per bin. |
| **noise floor** | **yes, strongly** | `noise_mask` clears only `peak +/- guard_bins` (plus optional `extra_noise_exclude_bins`). Every other bin, **including other real returns**, is treated as noise. |
| **SNR** | **yes** | `snr_db = peak_db - noise_db`, both derived from the selected bin. See `ANALYSIS_METRICS.md` -- the two halves use different estimators. |
| **material comparison** | **yes, and correctly so** | `delta_power_db` compares `element_mean` at one consensus bin between swatch and baseline. This is a target-region quantity already; it is *not* a whole-range mean. |
| **element metrics** (`element_mean`, `coherence`, phase) | **yes, by definition** | These are per-(TX,RX) values *at* one bin. That is what they are for. |

## 3. Is out-of-gate data ignored or discarded?

**Never discarded.** The full profile is always computed and kept. But it was
**under-exposed**: the only way to obtain it was through `analyze_capture()`,
which could raise before returning it, and the only structure carrying it was
`CaptureAnalysis`, which requires a successful peak extraction to construct.

Out-of-gate data is also **silently re-purposed**: bins holding other genuine
returns are folded into the noise-floor median, so a second real target at, say,
0.9 m raises the "noise" floor and depresses the reported SNR of the 1.5 m
trihedral. The Chunk 3 docstring for `extract_capture_elements` already admitted
this and offered `extra_noise_exclude_bins` as a manual workaround.

## 4. Verdict

The data was preserved; the **framing** was target-first. Three concrete
problems:

1. **Availability coupling.** Profile access required successful peak
   extraction. A failed angle gate meant no plot at all.
2. **Automatic selection was implicit.** `argmax` ran by default with no way to
   say "do not pick a target, just show me the range profile."
3. **Noise floor absorbs real returns.** Any bin that is not the chosen peak is
   noise by default, which is wrong whenever the scene has more than one return.

## 5. What Chunk 4 changes

- New `analysis/rangeprofile.py`: `RangeProfile` is computed with **no peak
  selection at all** and is the primary product of opening a capture.
  `range_profile(capture)` cannot be affected by any gate.
- `analyze_capture()` now **always** returns the full profile. Peak/SNR
  annotation is attempted and, if it fails, recorded as `annotation_error`
  instead of raising.
- Peak detection becomes `detect_peaks()`, returning a *list of candidates* with
  prominence, not a single decision.
- Gating is an explicit `Gate` object the caller opts into. `Gate` never filters
  the stored profile -- it only marks a region and drives region metrics.
- Noise floor is estimated with **all detected returns excluded**, not just the
  one chosen peak (`noise_floor_db`), and the old contaminated quantity is kept
  under an honest name.

The legacy `extract_capture_elements()` return dict is **unchanged**, so every
existing CSV stays byte-identical. All new quantities are additive.
