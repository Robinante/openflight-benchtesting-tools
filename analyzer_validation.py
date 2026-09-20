"""Compatibility shim for the module analyze_material.py imports.

The original ``analyzer_validation.py`` is missing from this repository (see
docs/CHUNK1_NOTES.md). This shim re-exports the same eleven names from the new
``openflight_bench.analysis`` package so the legacy script imports and runs again, and so
anything else that referenced the module keeps working.
"""

from openflight_bench.analysis.ild1 import (  # noqa: F401
    ShortHeader, parse_header, _read_frame_metadata, iter_frames,
)
from openflight_bench.analysis.loaders import read_capture_bytes, load_entries  # noqa: F401
from openflight_bench.analysis.validation import (  # noqa: F401
    DataValidationError, require_coherence, add_validation_arguments,
    prepare_analysis, run_checked, set_min_coherence,
)

__all__ = [
    "ShortHeader", "DataValidationError", "parse_header", "_read_frame_metadata",
    "iter_frames", "read_capture_bytes", "load_entries", "require_coherence",
    "add_validation_arguments", "prepare_analysis", "run_checked", "set_min_coherence",
]
