"""ILD1 dump format: constants and low-level parsing.

Every definition here is copied verbatim from analyze_sweep.py, which was the
validated copy. analyze_material.py carried a byte-identical duplicate of the
constants and imported the parsing from a module (analyzer_validation) that is
missing from the repository -- see docs/CHUNK1_NOTES.md.
"""

from __future__ import annotations

import struct

import numpy as np

# NOTE: bodies marked "verbatim" below are copied unchanged from the
# original analyze_sweep.py / analyze_material.py so behavior is preserved
# exactly. Do not reformat them casually -- the regression tests in
# tests/test_analysis_regression.py compare against the originals.


MAGIC = b"ILD1"
HEADER = struct.Struct("<4sHHHBBHBBHH")  # 20 bytes
TEMP_REPORT = struct.Struct("<Ihhhhhhhhhh")  # 24 bytes
TIMED_FRAME_DESCRIPTOR = struct.Struct("<BBH")  # 4 bytes: start_bin, bin_count, delta_us
MAX_SUPPORTED_DUMP_VERSION = 7
SAMPLE_INT16_IQ = 0
SAMPLE_RANGE_FFT_IQ16 = 1
SAMPLE_RANGE_FFT_IQ16_WINDOWED = 2
SAMPLE_RANGE_FFT_IQ16_VARIABLE = 3
SAMPLE_RANGE_FFT_IQ16_VARIABLE_TIMED = 4
SAMPLE_RANGE_FFT_IQ8_VARIABLE_TIMED = 5
SAMPLE_FMT_NAMES = {
    SAMPLE_INT16_IQ: "raw ADC IQ16 (needs range-FFT)",
    SAMPLE_RANGE_FFT_IQ16: "range-FFT IQ16, fixed window",
    SAMPLE_RANGE_FFT_IQ16_WINDOWED: "range-FFT IQ16, per-frame window",
    SAMPLE_RANGE_FFT_IQ16_VARIABLE: "range-FFT IQ16, variable window",
    SAMPLE_RANGE_FFT_IQ16_VARIABLE_TIMED: "range-FFT IQ16, variable window (timed)",
    SAMPLE_RANGE_FFT_IQ8_VARIABLE_TIMED: "range-FFT IQ8, variable window (timed, scaled)",
}
_VARIABLE_SAMPLE_FORMATS = (
    SAMPLE_RANGE_FFT_IQ16_VARIABLE,
    SAMPLE_RANGE_FFT_IQ16_VARIABLE_TIMED,
    SAMPLE_RANGE_FFT_IQ8_VARIABLE_TIMED,
)
_TIMED_SAMPLE_FORMATS = (
    SAMPLE_RANGE_FFT_IQ16_VARIABLE_TIMED,
    SAMPLE_RANGE_FFT_IQ8_VARIABLE_TIMED,
)
_RANGE_SNAPSHOT_FORMATS = (
    SAMPLE_RANGE_FFT_IQ16,
    SAMPLE_RANGE_FFT_IQ16_WINDOWED,
    SAMPLE_RANGE_FFT_IQ16_VARIABLE,
    SAMPLE_RANGE_FFT_IQ16_VARIABLE_TIMED,
    SAMPLE_RANGE_FFT_IQ8_VARIABLE_TIMED,
)
DEFAULT_NUM_ADC_SAMPLES = 128
DEFAULT_SLOPE_MHZ_PER_US = 100.0
DEFAULT_SAMPLE_RATE_KSPS = 4000.0
SPEED_OF_LIGHT_M_S = 299_792_458.0


def range_bin_meters(
    num_adc_samples: int = DEFAULT_NUM_ADC_SAMPLES,
    slope_mhz_per_us: float = DEFAULT_SLOPE_MHZ_PER_US,
    sample_rate_ksps: float = DEFAULT_SAMPLE_RATE_KSPS,
) -> float:
    """Meters per range-FFT bin for a given chirp profile."""
    slope_hz_per_s = slope_mhz_per_us * 1e12
    sample_rate_hz = sample_rate_ksps * 1e3
    adc_window_s = num_adc_samples / sample_rate_hz
    bandwidth_hz = slope_hz_per_s * adc_window_s
    return SPEED_OF_LIGHT_M_S / (2.0 * bandwidth_hz)


class ShortHeader(ValueError):
    """Raised when even the fixed header/metadata can't be read."""


def parse_header(raw: bytes) -> dict:
    """Unpack the fixed ILD1 header and optional temperature extension."""
    if len(raw) < HEADER.size:
        raise ShortHeader(f"file is only {len(raw)} bytes -- smaller than the 20-byte ILD1 header")
    magic, ver, nf, cpf, ntx, nrx, ns, fmt, pad, trig, period_us = HEADER.unpack_from(raw, 0)
    if magic != MAGIC:
        raise ShortHeader(
            f"bad magic {magic!r} (expected {MAGIC!r}) -- this isn't an OpenFlight ILD1 capture"
        )
    if ver > MAX_SUPPORTED_DUMP_VERSION:
        raise ShortHeader(f"unsupported dump version {ver} (this tool understands up to {MAX_SUPPORTED_DUMP_VERSION})")
    if fmt not in SAMPLE_FMT_NAMES:
        raise ShortHeader(f"unsupported sample_fmt {fmt}")

    header_nbytes = HEADER.size
    has_temp = ver == 7 or (ver == 5 and fmt not in _VARIABLE_SAMPLE_FORMATS)
    temperature_report = None
    if has_temp:
        if len(raw) < header_nbytes + TEMP_REPORT.size:
            raise ShortHeader("file is truncated inside the temperature-report header extension")
        temp = TEMP_REPORT.unpack_from(raw, header_nbytes)
        temperature_report = dict(
            zip(
                (
                    "device_time_ms", "rx0_c", "rx1_c", "rx2_c", "rx3_c",
                    "tx0_c", "tx1_c", "tx2_c", "pm_c", "dig0_c", "dig1_c",
                ),
                temp,
                strict=True,
            )
        )
        header_nbytes += TEMP_REPORT.size

    if fmt == SAMPLE_RANGE_FFT_IQ8_VARIABLE_TIMED:
        metadata_nbytes = TIMED_FRAME_DESCRIPTOR.size * nf + 2 * nf
    elif fmt == SAMPLE_RANGE_FFT_IQ16_VARIABLE_TIMED:
        metadata_nbytes = TIMED_FRAME_DESCRIPTOR.size * nf
    elif fmt == SAMPLE_RANGE_FFT_IQ16_VARIABLE:
        metadata_nbytes = 2 * nf
    elif fmt == SAMPLE_RANGE_FFT_IQ16_WINDOWED:
        metadata_nbytes = nf
    else:
        metadata_nbytes = 0

    return dict(
        version=ver, n_frames=nf, chirps_per_frame=cpf, n_tx=ntx, n_rx=nrx,
        n_samples=ns, sample_fmt=fmt, trigger_frame=trig, frame_period_us=period_us,
        header_nbytes=header_nbytes, metadata_nbytes=metadata_nbytes,
        temperature_report=temperature_report,
    )


def _read_frame_metadata(raw: bytes, meta: dict) -> None:
    """Populate per-frame range-window (and IQ8 scale) tables."""
    fmt = meta["sample_fmt"]
    nf = meta["n_frames"]
    start = meta["header_nbytes"]
    stop = start + meta["metadata_nbytes"]
    if len(raw) < stop:
        raise ShortHeader("file is truncated inside the per-frame range-window table (metadata), before any payload")

    if fmt == SAMPLE_RANGE_FFT_IQ16_WINDOWED:
        meta["range_bin_starts"] = tuple(raw[start:stop])
        meta["range_bin_counts"] = (meta["n_samples"],) * nf
    elif fmt == SAMPLE_RANGE_FFT_IQ16_VARIABLE:
        table = raw[start:stop]
        meta["range_bin_starts"] = tuple(table[0::2])
        meta["range_bin_counts"] = tuple(table[1::2])
    elif fmt in _TIMED_SAMPLE_FORMATS:
        desc_stop = start + TIMED_FRAME_DESCRIPTOR.size * nf
        descriptors = list(TIMED_FRAME_DESCRIPTOR.iter_unpack(raw[start:desc_stop]))
        meta["range_bin_starts"] = tuple(d[0] for d in descriptors)
        meta["range_bin_counts"] = tuple(d[1] for d in descriptors)
        deltas_us = tuple(d[2] for d in descriptors)
        elapsed = 0
        offsets = []
        for d in deltas_us:
            elapsed += d
            offsets.append(elapsed)
        meta["frame_time_offsets_us"] = tuple(offsets)
        if fmt == SAMPLE_RANGE_FFT_IQ8_VARIABLE_TIMED:
            scale_start = desc_stop
            scales = np.frombuffer(raw, dtype="<u2", offset=scale_start, count=nf)
            meta["iq8_scales"] = tuple(int(s) for s in scales)
    else:
        meta["range_bin_starts"] = (meta.get("range_bin_start", 0),) * nf
        meta["range_bin_counts"] = (meta["n_samples"],) * nf


def iter_frames(raw: bytes, meta: dict):
    """Yield (frame_index, start_bin, complex_array[cpf, n_rx, count], complete)
    for every frame that can be fully read."""
    cpf, nrx = meta["chirps_per_frame"], meta["n_rx"]
    fmt = meta["sample_fmt"]
    starts = meta["range_bin_starts"]
    counts = meta["range_bin_counts"]
    byte_offset = meta["header_nbytes"] + meta["metadata_nbytes"]
    bytes_per_complex = 2 if fmt == SAMPLE_RANGE_FFT_IQ8_VARIABLE_TIMED else 4

    for i in range(meta["n_frames"]):
        count = counts[i]
        n = cpf * nrx * count
        need_bytes = n * bytes_per_complex
        have_bytes = len(raw) - byte_offset

        if have_bytes <= 0:
            return
        if have_bytes < need_bytes:
            if fmt == SAMPLE_RANGE_FFT_IQ8_VARIABLE_TIMED:
                usable_samples = have_bytes // 2
                raw_i8 = np.frombuffer(raw, dtype=np.int8, offset=byte_offset, count=usable_samples * 2)
                scale = meta["iq8_scales"][i]
                iq = raw_i8.astype(np.float64) * scale
            else:
                usable_samples = have_bytes // 4
                raw_i16 = np.frombuffer(raw, dtype="<i2", offset=byte_offset, count=usable_samples * 2)
                iq = raw_i16.astype(np.float64)
            cplx_flat = np.zeros(n, dtype=complex)
            got = iq[1::2] + 1j * iq[0::2]
            cplx_flat[: got.size] = got
            cplx = cplx_flat.reshape(cpf, nrx, count)
            yield i, starts[i], cplx, False
            return

        if fmt == SAMPLE_RANGE_FFT_IQ8_VARIABLE_TIMED:
            raw_i8 = np.frombuffer(raw, dtype=np.int8, offset=byte_offset, count=2 * n)
            scale = meta["iq8_scales"][i]
            iq = raw_i8.astype(np.float64) * scale
        else:
            raw_i16 = np.frombuffer(raw, dtype="<i2", offset=byte_offset, count=2 * n)
            iq = raw_i16.astype(np.float64)
        cplx = (iq[1::2] + 1j * iq[0::2]).reshape(cpf, nrx, count)
        yield i, starts[i], cplx, True
        byte_offset += need_bytes


def is_range_snapshot(meta: dict) -> bool:
    """True when this dump holds post-range-FFT snapshots (not raw ADC)."""
    return meta["sample_fmt"] in _RANGE_SNAPSHOT_FORMATS
