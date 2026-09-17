"""Reconstruction of the missing ``analyzer_validation`` module.

analyze_material.py imports eleven names from ``analyzer_validation``:

    ShortHeader, DataValidationError, parse_header, _read_frame_metadata,
    iter_frames, read_capture_bytes, load_entries, require_coherence,
    add_validation_arguments, prepare_analysis, run_checked

That module does not exist anywhere in the repository, so analyze_material.py
currently fails at import with ModuleNotFoundError. The five parsing names are
recovered verbatim from analyze_sweep.py (openflight_bench.analysis.ild1) and the two
loader names from openflight_bench.analysis.loaders.

The four genuinely unknown names are reconstructed HERE as the most conservative
implementation that keeps analyze_material.py's DEFAULT code path byte-for-byte
identical to what it would have done:

  * require_coherence      -- no-op unless a threshold is set
  * add_validation_arguments -- adds --min-coherence / --self-test only
  * prepare_analysis       -- returns False (do not short-circuit) unless
                              --self-test was passed
  * run_checked            -- calls the function, turning DataValidationError
                              into a printed message and exit code 2

If the real module enforced stricter validation, THAT STRICTNESS IS NOT
REPRODUCED HERE. See docs/CHUNK1_NOTES.md, open question 1.
"""

from __future__ import annotations

import sys

import numpy as np

from .ild1 import (  # noqa: F401  (re-exported for import compatibility)
    ShortHeader, parse_header, _read_frame_metadata, iter_frames,
)
from .loaders import read_capture_bytes, load_entries  # noqa: F401


class DataValidationError(Exception):
    """Raised when input data cannot support the requested analysis.

    Carries a short machine-readable code plus a human message, matching the
    two-argument call sites in analyze_material.py, e.g.
    ``DataValidationError("MISSING_BASELINE", "No matching baseline for ...")``.
    """

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}" if message else code)


#: Set by add_validation_arguments()/prepare_analysis() when the caller asks
#: for a coherence floor. ``None`` means "do not check", which is the default
#: and therefore the behavior of every existing command line.
_MIN_COHERENCE: float | None = None


def set_min_coherence(value: float | None) -> None:
    global _MIN_COHERENCE
    _MIN_COHERENCE = value


def require_coherence(coherence, where: str) -> None:
    """Optional guard on per-element coherence. No-op by default."""
    if _MIN_COHERENCE is None:
        return
    arr = np.asarray(coherence, dtype=float)
    finite = arr[np.isfinite(arr)]
    if finite.size and float(finite.min()) < _MIN_COHERENCE:
        raise DataValidationError(
            "LOW_COHERENCE",
            f"{where}: min coherence {float(finite.min()):.3f} < {_MIN_COHERENCE:.3f}",
        )


def add_validation_arguments(ap) -> None:
    """Register the validation flags on an argparse parser."""
    ap.add_argument("--min-coherence", type=float, default=None,
                    help="Fail if any per-element coherence falls below this (default: no check)")
    ap.add_argument("--self-test", action="store_true",
                    help="Run the extractor's internal self-check and exit without analyzing")


def prepare_analysis(args, kind: str, extractor) -> bool:
    """Apply validation options. Return True to short-circuit main()."""
    set_min_coherence(getattr(args, "min_coherence", None))
    if getattr(args, "self_test", False):
        print(f"self-test: {kind} extractor is {extractor.__name__}; "
              f"min_coherence={_MIN_COHERENCE}")
        return True
    return False


def run_checked(fn):
    """Run fn(), converting DataValidationError into a message + exit code 2."""
    try:
        return fn()
    except DataValidationError as exc:
        print(f"!! {exc.code}: {exc.message}", file=sys.stderr)
        return 2
