"""The normalized capture representation.

Phase 1 of the roadmap asks for one capture model that downstream analysis can
consume without knowing which parser produced it:

    Capture
     |- source          where it came from and in what format
     |- metadata        experiment context from the .json sidecar
     |- configuration   radar settings this capture was taken under
     |- frames          the decoded per-frame IQ
     |- channels        TX/RX layout
     |- timestamps      when, per frame and per session
     |- integrity       is this capture whole, and if not, how much is missing

Every field is populated from data that already existed -- the ILD1 header, the
per-frame descriptor table, and the sidecar. Nothing is invented and nothing is
recomputed differently, with one deliberate exception documented under
``ConfigurationPolicy``: ``range_bin_m`` now prefers the value the capture tool
recorded in the sidecar over one re-derived from chirp defaults.

``Capture.header`` still exposes the raw ILD1 meta dict. Anything not yet
migrated can keep reading it, and the ILD1-specific bits of the parser stay in
``ild1.py`` where a second parser can sit beside them.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .ild1 import (
    DEFAULT_NUM_ADC_SAMPLES, DEFAULT_SAMPLE_RATE_KSPS, DEFAULT_SLOPE_MHZ_PER_US,
    SAMPLE_FMT_NAMES, _RANGE_SNAPSHOT_FORMATS, iter_frames, parse_header,
    _read_frame_metadata, range_bin_meters,
)
from .integrity import ContentVerification, manifest_entry_for, verify_bytes
from .loaders import read_capture_bytes

#: Relative tolerance for calling two range-bin sizes "the same". Sidecars store
#: the value as a full-precision float written by the capture tool from the same
#: formula, so a real mismatch means a genuinely different chirp, not rounding.
RANGE_BIN_RTOL = 1e-9


# ---------------------------------------------------------------------------
# configuration resolution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConfigurationPolicy:
    """How to resolve the radar configuration for a capture.

    Precedence for ``range_bin_m``, highest first:

    1. ``explicit_range_bin_m`` -- the user passed ``--range-bin-m``. Always wins.
    2. the value the capture tool recorded in ``capture_info.range_bin_m``.
    3. ``range_bin_meters(num_adc_samples, slope_mhz_per_us, sample_rate_ksps)``.

    Step 2 is new. The scripts used to go straight from 1 to 3, so a capture
    taken with a different chirp than the CLI defaults produced ranges that
    silently disagreed with the sidecar sitting next to it. Where the sidecar
    and the derived value agree -- which is every capture in the project's
    current sessions -- output is unchanged.

    ``warnings`` collects one line per capture whose resolved value disagrees
    with what the old precedence would have produced, so a run that does change
    says so out loud rather than quietly.
    """

    explicit_range_bin_m: float | None = None
    num_adc_samples: int = DEFAULT_NUM_ADC_SAMPLES
    slope_mhz_per_us: float = DEFAULT_SLOPE_MHZ_PER_US
    sample_rate_ksps: float = DEFAULT_SAMPLE_RATE_KSPS

    @property
    def derived_range_bin_m(self) -> float:
        return range_bin_meters(
            self.num_adc_samples, self.slope_mhz_per_us, self.sample_rate_ksps
        )

    def resolve_range_bin_m(self, sidecar_value: float | None, label: str = ""):
        """Return (value, source, warning_or_None)."""
        derived = self.derived_range_bin_m

        if self.explicit_range_bin_m is not None:
            value, source = float(self.explicit_range_bin_m), "override"
            if sidecar_value is not None and not _close(value, sidecar_value):
                return value, source, (
                    f"{label}: --range-bin-m {value!r} overrides the value this capture "
                    f"recorded ({sidecar_value!r}); ranges follow the flag"
                )
            return value, source, None

        if sidecar_value is not None:
            value, source = float(sidecar_value), "sidecar"
            if not _close(value, derived):
                return value, source, (
                    f"{label}: using range_bin_m={value!r} from the capture's own sidecar; "
                    f"the chirp settings in play here would have derived {derived!r}. "
                    f"Ranges follow the capture. Pass --range-bin-m to force the other way."
                )
            return value, source, None

        return derived, "derived", None


def _close(a: float, b: float) -> bool:
    return math.isclose(float(a), float(b), rel_tol=RANGE_BIN_RTOL, abs_tol=0.0)


# ---------------------------------------------------------------------------
# facets
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CaptureSource:
    """Where this capture came from and in what wire format."""
    path: Path | None = None
    sidecar_path: Path | None = None
    parser: str = "ild1"
    format_version: int | None = None
    sample_format: int | None = None
    sample_format_name: str = ""
    config_path: str | None = None          # the .cfg the capture tool applied

    @property
    def name(self) -> str:
        return self.path.name if self.path is not None else "<in-memory>"


@dataclass(frozen=True)
class CaptureMetadata:
    """Experiment context, read from the .json sidecar. All optional -- a
    capture with no sidecar is still a valid Capture, just with less context."""
    test_type: str | None = None
    kind: str | None = None
    swatch: str | None = None
    angle_deg: float | None = None
    axis: str | None = None
    repeat_index: int | None = None
    label: str | None = None
    record: dict = field(default_factory=dict)   # the whole sidecar, verbatim


@dataclass(frozen=True)
class CaptureConfiguration:
    """Radar settings this capture was taken under."""
    range_bin_m: float
    range_bin_source: str                  # "override" | "sidecar" | "derived"
    frame_period_us: int | None = None
    window_start_bin: int | None = None    # None when the window walks
    window_bin_count: int | None = None
    window_fixed: bool = True
    num_adc_samples: int | None = None
    slope_mhz_per_us: float | None = None
    sample_rate_ksps: float | None = None

    @property
    def window_range_m(self) -> tuple[float, float] | None:
        if self.window_start_bin is None or self.window_bin_count is None:
            return None
        lo = self.window_start_bin * self.range_bin_m
        return (lo, lo + self.window_bin_count * self.range_bin_m)


@dataclass(frozen=True)
class ChannelLayout:
    """TX/RX layout and the TDM chirp ordering the extraction assumes."""
    n_tx: int
    n_rx: int
    chirps_per_frame: int

    @property
    def loops_per_frame(self) -> int:
        return self.chirps_per_frame // self.n_tx

    @property
    def n_virtual(self) -> int:
        return self.n_tx * self.n_rx

    @property
    def tdm_divides_evenly(self) -> bool:
        return self.n_tx > 0 and self.chirps_per_frame % self.n_tx == 0


@dataclass(frozen=True)
class CaptureTimestamps:
    captured_at: str | None = None                 # sidecar wall-clock
    frame_time_offsets_us: tuple | None = None     # cumulative, timed formats only
    device_time_ms: int | None = None
    temperatures_c: dict = field(default_factory=dict)

    @property
    def duration_us(self) -> int | None:
        if not self.frame_time_offsets_us:
            return None
        return self.frame_time_offsets_us[-1]


@dataclass(frozen=True)
class CaptureIntegrity:
    """Is this capture whole, are its bytes the bytes that were written, and did
    the capture tool consider it usable."""
    declared_frames: int
    file_bytes: int
    short_by_bytes: int | None = None      # from the sidecar, if it recorded one
    n_frames_decoded: int = 0
    n_frames_incomplete: int = 0
    # --- newer sidecar schema / SHA256SUMS / session_manifest ---
    content: ContentVerification = field(default_factory=ContentVerification)
    status: str | None = None                   # capture_info.status
    expected_bytes: int | None = None
    actual_bytes: int | None = None
    extra_bytes: int | None = None
    completion_seen: bool | None = None
    accepted_for_analysis: bool | None = None   # the capture tool's own verdict
    manifest_status: str | None = None          # session_manifest captures[].status

    # -- content -----------------------------------------------------------
    @property
    def sha256(self) -> str:
        return self.content.sha256

    @property
    def sha256_verified(self):
        """True / False when a digest was recorded, None when none was."""
        return self.content.verified

    @property
    def tampered(self) -> bool:
        """A digest was recorded and the bytes do not match it."""
        return self.content.verified is False

    # -- structure ---------------------------------------------------------
    @property
    def frames_whole(self) -> bool:
        return (self.n_frames_incomplete == 0
                and self.n_frames_decoded == self.declared_frames
                and not self.short_by_bytes)

    @property
    def complete(self) -> bool:
        """Whole frames AND, if a digest was recorded, matching bytes."""
        return self.frames_whole and self.content.verified is not False

    @property
    def usable(self) -> bool:
        """What analysis should gate on: the capture tool did not reject it and
        the bytes are not known-corrupt. An incomplete trailing frame does NOT
        make a capture unusable -- extraction has always skipped those."""
        return self.accepted_for_analysis is not False and not self.tampered

    def describe_frames(self) -> str:
        """Frame/byte completeness only -- no content-hash verdict."""
        bits = []
        if self.frames_whole:
            return f"complete ({self.n_frames_decoded} frames)"
        bits.append(f"{self.n_frames_decoded}/{self.declared_frames} frames decoded")
        if self.n_frames_incomplete:
            bits.append(f"{self.n_frames_incomplete} incomplete")
        if self.short_by_bytes:
            bits.append(f"short by {self.short_by_bytes} B")
        return ", ".join(bits)

    def describe(self) -> str:
        bits = []
        if self.frames_whole:
            bits.append(f"complete ({self.n_frames_decoded} frames)")
        else:
            bits.append(f"{self.n_frames_decoded}/{self.declared_frames} frames decoded")
            if self.n_frames_incomplete:
                bits.append(f"{self.n_frames_incomplete} incomplete")
            if self.short_by_bytes:
                bits.append(f"short by {self.short_by_bytes} B")
        v = self.content.verified
        bits.append("sha256 ok" if v else ("SHA256 MISMATCH" if v is False else "sha256 not recorded"))
        if self.accepted_for_analysis is False:
            bits.append("REJECTED by capture tool")
        return ", ".join(bits)


@dataclass
class FrameSet:
    """The decoded per-frame IQ, iterated lazily straight off the raw bytes.

    Deliberately not materialized: a session is hundreds of files and holding
    every frame of every one would be gigabytes. Iterating yields exactly what
    ``ild1.iter_frames`` always yielded, so extraction code is unchanged.
    """
    raw: bytes
    header: dict

    def __iter__(self):
        """Yield (frame_index, start_bin, complex[cpf, n_rx, count], complete)."""
        return iter_frames(self.raw, self.header)

    def complete_only(self):
        for idx, start, cplx, complete in self:
            if complete:
                yield idx, start, cplx

    @property
    def range_bin_starts(self) -> tuple:
        return self.header["range_bin_starts"]

    @property
    def range_bin_counts(self) -> tuple:
        return self.header["range_bin_counts"]

    @property
    def window_is_fixed(self) -> bool:
        return (len(set(self.range_bin_starts)) == 1
                and len(set(self.range_bin_counts)) == 1)

    def __len__(self) -> int:
        return len(self.range_bin_starts)


# ---------------------------------------------------------------------------
# the capture
# ---------------------------------------------------------------------------

@dataclass
class Capture:
    source: CaptureSource
    metadata: CaptureMetadata
    configuration: CaptureConfiguration
    channels: ChannelLayout
    timestamps: CaptureTimestamps
    integrity: CaptureIntegrity
    frames: FrameSet
    header: dict = field(repr=False, default_factory=dict)   # raw ILD1 meta
    raw: bytes = field(repr=False, default=b"")
    warnings: tuple = ()

    @property
    def is_range_snapshot(self) -> bool:
        """True when this holds post-range-FFT snapshots rather than raw ADC."""
        return self.source.sample_format in _RANGE_SNAPSHOT_FORMATS

    def require_range_snapshot(self) -> None:
        if not self.is_range_snapshot:
            raise ValueError(
                f"sample_fmt={self.source.sample_format} is raw ADC data, "
                "not a range snapshot"
            )

    def __str__(self) -> str:
        return (f"{self.source.name} [{self.source.sample_format_name}] "
                f"{self.channels.n_tx}TX x {self.channels.n_rx}RX, "
                f"{self.integrity.describe()}")


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------

def _sidecar_for(path: Path) -> Path | None:
    candidate = Path(path).with_suffix(".json")
    return candidate if candidate.exists() else None


def load_capture(
    path,
    *,
    sidecar: dict | None = None,
    sidecar_path=None,
    policy: ConfigurationPolicy | None = None,
    raw: bytes | None = None,
    verify: bool = True,
) -> Capture:
    """Parse one capture file into a normalized :class:`Capture`.

    ``sidecar`` may be passed directly (the loaders already read it); otherwise
    the matching ``.json`` beside the dump is used when present.

    ``verify`` hashes the bytes and compares against ``file_sha256`` in the
    sidecar or the directory's ``SHA256SUMS``. Captures with no recorded digest
    -- everything taken before that was added -- report ``sha256_verified is
    None`` rather than failing. Pass ``verify=False`` to skip hashing.
    """
    path = Path(path) if path is not None else None
    policy = policy or ConfigurationPolicy()

    if raw is None:
        raw = read_capture_bytes(path)
    header = parse_header(raw)
    _read_frame_metadata(raw, header)

    if sidecar is None and path is not None:
        sidecar_path = sidecar_path or _sidecar_for(path)
        if sidecar_path is not None:
            try:
                sidecar = json.loads(Path(sidecar_path).read_text())
            except (OSError, json.JSONDecodeError):
                sidecar = None
    sidecar = sidecar or {}
    info = sidecar.get("capture_info") or {}

    fmt = header["sample_fmt"]
    frames = FrameSet(raw=raw, header=header)

    rb, rb_source, warning = policy.resolve_range_bin_m(
        info.get("range_bin_m"), label=path.name if path is not None else "capture"
    )
    warnings = (warning,) if warning else ()

    fixed = frames.window_is_fixed
    configuration = CaptureConfiguration(
        range_bin_m=rb,
        range_bin_source=rb_source,
        frame_period_us=header.get("frame_period_us"),
        window_start_bin=frames.range_bin_starts[0] if fixed and len(frames) else None,
        window_bin_count=frames.range_bin_counts[0] if fixed and len(frames) else None,
        window_fixed=fixed,
        num_adc_samples=policy.num_adc_samples if rb_source == "derived" else None,
        slope_mhz_per_us=policy.slope_mhz_per_us if rb_source == "derived" else None,
        sample_rate_ksps=policy.sample_rate_ksps if rb_source == "derived" else None,
    )

    temps = dict(header.get("temperature_report") or {})
    timestamps = CaptureTimestamps(
        captured_at=sidecar.get("timestamp"),
        frame_time_offsets_us=header.get("frame_time_offsets_us"),
        device_time_ms=temps.pop("device_time_ms", None),
        temperatures_c=temps,
    )

    n_decoded = n_incomplete = 0
    for _idx, _start, _cplx, complete in frames:
        n_decoded += 1
        if not complete:
            n_incomplete += 1

    content = (verify_bytes(raw, path=path, sidecar=sidecar) if verify
               else ContentVerification(sha256=""))
    mentry = manifest_entry_for(path.parent, path.name) if path is not None else {}
    integrity = CaptureIntegrity(
        declared_frames=header["n_frames"],
        file_bytes=len(raw),
        short_by_bytes=info.get("short_by"),
        n_frames_decoded=n_decoded,
        n_frames_incomplete=n_incomplete,
        content=content,
        status=info.get("status"),
        expected_bytes=info.get("expected_bytes"),
        actual_bytes=info.get("actual_bytes"),
        extra_bytes=info.get("extra_bytes"),
        completion_seen=info.get("completion_seen"),
        accepted_for_analysis=sidecar.get("accepted_for_analysis"),
        manifest_status=mentry.get("status"),
    )
    if integrity.tampered:
        warnings = warnings + (
            f"{path.name if path is not None else 'capture'}: {content.describe()}",
        )
    if integrity.accepted_for_analysis is False:
        warnings = warnings + (
            f"{path.name if path is not None else 'capture'}: sidecar marks this capture "
            f"accepted_for_analysis=false"
            + (f" (status={integrity.status})" if integrity.status else ""),
        )

    return Capture(
        source=CaptureSource(
            path=path,
            sidecar_path=Path(sidecar_path) if sidecar_path else None,
            parser="ild1",
            format_version=header.get("version"),
            sample_format=fmt,
            sample_format_name=SAMPLE_FMT_NAMES.get(fmt, f"unknown ({fmt})"),
            config_path=sidecar.get("config_path"),
        ),
        metadata=CaptureMetadata(
            test_type=sidecar.get("test_type"),
            kind=sidecar.get("kind"),
            swatch=sidecar.get("swatch"),
            angle_deg=sidecar.get("angle_deg"),
            axis=sidecar.get("axis"),
            repeat_index=sidecar.get("repeat_index"),
            label=sidecar.get("label"),
            record=sidecar,
        ),
        configuration=configuration,
        channels=ChannelLayout(
            n_tx=header["n_tx"],
            n_rx=header["n_rx"],
            chirps_per_frame=header["chirps_per_frame"],
        ),
        timestamps=timestamps,
        integrity=integrity,
        frames=frames,
        header=header,
        raw=raw,
        warnings=warnings,
    )


def load_captures(entries, *, policy: ConfigurationPolicy | None = None) -> list[Capture]:
    """Load a list of loader entries (or bare paths) into Captures."""
    out = []
    for e in entries:
        if isinstance(e, Capture):
            out.append(e)
        elif isinstance(e, dict):
            out.append(load_capture(e["l3dump_path"], sidecar=e.get("record"),
                                    sidecar_path=e.get("sidecar_path"), policy=policy))
        else:
            out.append(load_capture(e, policy=policy))
    return out


def as_capture(obj, *, policy: ConfigurationPolicy | None = None) -> Capture:
    """Accept a Capture, a loader entry dict, or a path. Returns a Capture."""
    if isinstance(obj, Capture):
        return obj
    if isinstance(obj, dict):
        return load_capture(obj["l3dump_path"], sidecar=obj.get("record"),
                            sidecar_path=obj.get("sidecar_path"), policy=policy)
    return load_capture(obj, policy=policy)
