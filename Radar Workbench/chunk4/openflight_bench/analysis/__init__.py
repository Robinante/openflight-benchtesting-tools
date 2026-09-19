"""Reusable analysis layer for OpenFlight IWR6843 bench captures.

    input .l3dump + .json
            |
    loaders.load_entries
            |
    ild1.parse_header / iter_frames        <- format-specific, swappable
            |
    capture.load_capture  ->  Capture      <- the normalized model everything
            |                                 downstream consumes
    elements.extract_capture_elements / combine_repeats
            |
      +-----+-----+
      |           |
  material    sweep
      |           |
      +-----+-----+
            |
    results.AnalysisResult subclasses  ->  export.write_rows
"""

from .rangeprofile import (
    Gate, Peak, RangeProfile, detect_peaks, pooled_profile, range_profile,
)
from .metrics import (
    coherent_gain_db, compare_region, legacy_snr_db, level_db, mean_power_db,
    noise_floor_db, peak_power_db, peak_to_floor_db, power_db,
)
from .capture import (
    Capture, CaptureConfiguration, CaptureIntegrity, CaptureMetadata, CaptureSource,
    CaptureTimestamps, ChannelLayout, ConfigurationPolicy, FrameSet,
    as_capture, load_capture, load_captures,
)
from .ild1 import (
    MAGIC, MAX_SUPPORTED_DUMP_VERSION, SAMPLE_FMT_NAMES, ShortHeader,
    is_range_snapshot, iter_frames, parse_header, range_bin_meters,
)
from .loaders import (
    group_by_angle, load_entries, load_material_files, load_sweep_files, read_capture_bytes,
)
from .elements import (
    analyze_capture, capture_power_profile, combine_repeats, combined_power_profile,
    extract_capture_elements, extract_file_elements, pooled_profile_peak,
)
from .export import result_metadata, write_json, write_rows
from . import metrics, plots  # noqa: F401  (lazy matplotlib inside)
from .progress import CollectingProgress, as_progress
from .results import (
    AnalysisResult, CaptureAnalysis, MaterialComparisonResult, SweepResult,
)
from .integrity import (
    ContentVerification, read_sha256sums, read_session_manifest, sha256_bytes,
    sha256_file, verify_bytes, verify_directory,
)
from .validation import DataValidationError
from .material import MaterialOptions, analyze_material
from .sweep import SweepOptions, analyze_sweep

__all__ = [
    # normalized capture model
    "Capture", "CaptureConfiguration", "CaptureIntegrity", "CaptureMetadata",
    "CaptureSource", "CaptureTimestamps", "ChannelLayout", "ConfigurationPolicy",
    "FrameSet", "as_capture", "load_capture", "load_captures",
    # ILD1 format layer
    "MAGIC", "MAX_SUPPORTED_DUMP_VERSION", "SAMPLE_FMT_NAMES", "ShortHeader",
    "is_range_snapshot", "iter_frames", "parse_header", "range_bin_meters",
    # discovery
    "group_by_angle", "load_entries", "load_material_files", "load_sweep_files",
    "read_capture_bytes",
    # extraction
    "analyze_capture", "capture_power_profile", "combine_repeats",
    "combined_power_profile", "extract_capture_elements", "extract_file_elements",
    "pooled_profile_peak",
    # results and export
    "AnalysisResult", "CaptureAnalysis", "MaterialComparisonResult", "SweepResult",
    "CollectingProgress", "as_progress", "plots", "metrics",
    # raw-first range data (Chunk 4)
    "RangeProfile", "range_profile", "pooled_profile", "Peak", "detect_peaks", "Gate",
    # honest metrics (Chunk 4)
    "power_db", "level_db", "peak_power_db", "mean_power_db", "noise_floor_db",
    "peak_to_floor_db", "legacy_snr_db", "coherent_gain_db", "compare_region",
    "write_rows", "write_json", "result_metadata", "DataValidationError",
    # integrity
    "ContentVerification", "read_sha256sums", "read_session_manifest",
    "sha256_bytes", "sha256_file", "verify_bytes", "verify_directory",
    # analyses
    "MaterialOptions", "analyze_material", "SweepOptions", "analyze_sweep",
]
