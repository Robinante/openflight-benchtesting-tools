"""Strict ILD1 timed-dump reader and structural validator."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import struct
import time

HEADER = struct.Struct("<4sHHHBBHBBHH")
TEMP = struct.Struct("<I10h")
DESC = struct.Struct("<BBH")
MAGIC = b"ILD1"
COMPLETIONS = (b"Done\r\nl3dump:/>", b"Done\nl3dump:/>")


class RecordingSerial:
    """Keep exactly the bytes returned to this reader, including CLI text."""

    def __init__(self, serial_port):
        self.serial_port = serial_port
        self.received = bytearray()

    @property
    def in_waiting(self):
        return getattr(self.serial_port, "in_waiting", 0)

    def read(self, count=1):
        chunk = self.serial_port.read(count)
        self.received.extend(chunk)
        return chunk


@dataclass
class DumpPlan:
    version: int
    n_frames: int
    chirps_per_frame: int
    n_tx: int
    n_rx: int
    max_bin_count: int
    sample_fmt: int
    header_bytes: int
    metadata_bytes: int
    descriptors: list[tuple[int, int, int]]
    expected_payload_bytes: int

    @property
    def expected_bytes(self) -> int:
        return self.header_bytes + self.metadata_bytes + self.expected_payload_bytes


def parse_plan(header: bytes, extension: bytes, metadata: bytes) -> DumpPlan:
    if len(header) != HEADER.size:
        raise ValueError("incomplete ILD1 header")
    magic, version, nf, cpf, ntx, nrx, ns, fmt, _pad, _trigger, _period = HEADER.unpack(header)
    if magic != MAGIC:
        raise ValueError(f"bad magic {magic!r}")
    if version not in (6, 7) or fmt not in (4, 5):
        raise ValueError("this package supports only v6/v7 timed IQ16/IQ8 dumps")
    if version == 7 and len(extension) != TEMP.size:
        raise ValueError("incomplete v7 temperature extension")
    desc_bytes = nf * DESC.size
    scales_bytes = nf * 2 if fmt == 5 else 0
    if len(metadata) != desc_bytes + scales_bytes:
        raise ValueError("incomplete timed descriptor metadata")
    descriptors = [DESC.unpack_from(metadata, i * DESC.size) for i in range(nf)]
    if not descriptors or descriptors[0][2] != 0:
        raise ValueError("first descriptor must have zero elapsed-time delta")
    if any(not 1 <= count <= ns for _start, count, _delta in descriptors):
        raise ValueError("descriptor bin count is outside header maximum")
    if not (nf and cpf and 1 <= ntx <= 3 and 1 <= nrx <= 4 and ns):
        raise ValueError("invalid ILD1 geometry")
    bytes_per_complex = 2 if fmt == 5 else 4
    payload = sum(cpf * nrx * count * bytes_per_complex for _s, count, _d in descriptors)
    return DumpPlan(
        version, nf, cpf, ntx, nrx, ns, fmt,
        HEADER.size + len(extension), len(metadata), descriptors, payload,
    )


def read_exact(serial_port, count: int, *, timeout: float, label: str) -> bytes:
    result = bytearray()
    deadline = time.monotonic() + timeout
    while len(result) < count:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out reading {label}: {len(result)}/{count} bytes")
        chunk = serial_port.read(count - len(result))
        if chunk:
            result.extend(chunk)
    return bytes(result)


def read_dump(serial_port, *, timeout: float = 45.0, stall_timeout: float = 5.0):
    """Read a dump and retain an unmodified record of the received UART bytes."""
    recorded = RecordingSerial(serial_port)
    try:
        result = _read_dump(recorded, timeout=timeout, stall_timeout=stall_timeout)
    except Exception as exc:
        exc.wire_bytes = bytes(recorded.received)
        raise
    result["wire_bytes"] = bytes(recorded.received)
    return result


def _read_dump(serial_port, *, timeout: float, stall_timeout: float):
    magic = read_until_magic(serial_port, timeout=timeout)
    header = magic + read_exact(serial_port, HEADER.size - len(magic), timeout=timeout, label="header")
    _magic, version, nf, _cpf, _ntx, _nrx, _ns, fmt, _pad, _trig, _period = HEADER.unpack(header)
    extension = read_exact(serial_port, TEMP.size if version == 7 else 0, timeout=timeout, label="temperature")
    metadata_len = nf * DESC.size + (nf * 2 if fmt == 5 else 0)
    metadata = read_exact(serial_port, metadata_len, timeout=timeout, label="metadata")
    plan = parse_plan(header, extension, metadata)

    body = bytearray()
    started = time.monotonic()
    last_data = started
    # Accumulate across read boundaries. A four-byte "Done" occurrence alone
    # is never a delimiter. Wait for a quiet tail before recognizing the full
    # Done + prompt suffix, so a marker within continuing IQ data is retained.
    while time.monotonic() - started < timeout:
        waiting = getattr(serial_port, "in_waiting", 0)
        chunk = serial_port.read(min(waiting or 1, 65536))
        if chunk:
            body.extend(chunk)
            last_data = time.monotonic()
            continue
        idle = time.monotonic() - last_data
        if any(body.endswith(marker) for marker in COMPLETIONS):
            if idle >= min(0.15, stall_timeout):
                break
        elif len(body) >= plan.expected_payload_bytes:
            if idle >= min(0.30, stall_timeout):
                break
        if idle >= stall_timeout:
            break

    trailing = b""
    payload = bytes(body)
    for marker in COMPLETIONS:
        if payload.endswith(marker):
            payload, trailing = payload[:-len(marker)], marker
            break
    # Preserve the previous allowance for CLI line whitespace after an exact
    # binary payload. Never trim whitespace within the advertised payload.
    extra = payload[plan.expected_payload_bytes:]
    if trailing and extra and all(byte in (9, 10, 13, 32) for byte in extra):
        trailing = extra + trailing
        payload = payload[:plan.expected_payload_bytes]
    short_by = max(0, plan.expected_payload_bytes - len(payload))
    extra_bytes = max(0, len(payload) - plan.expected_payload_bytes)
    status = "rejected_short" if short_by else "rejected_extra" if extra_bytes else "complete"
    raw = header + extension + metadata + payload
    return {
        "status": status,
        "raw": raw,
        "header": header,
        "extension": extension,
        "metadata": metadata,
        "plan": plan,
        "actual_bytes": len(raw),
        "expected_bytes": HEADER.size + len(extension) + len(metadata) + plan.expected_payload_bytes,
        "short_by": short_by,
        "extra_bytes": extra_bytes,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "trailing_text": trailing.decode("ascii", errors="replace"),
        "completion_seen": bool(trailing),
    }


def read_until_magic(serial_port, *, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    window = bytearray()
    while time.monotonic() < deadline:
        byte = serial_port.read(1)
        if not byte:
            continue
        window.extend(byte)
        if window.endswith(MAGIC):
            return MAGIC
        if window.endswith(b"l3dump:/>"):
            response = bytes(window).decode("ascii", errors="replace").strip()
            raise RuntimeError(f"firmware returned without ILD1: {response}")
        # The recorder retains the full stream; only the error preview is bounded.
        if len(window) > 4096:
            del window[:-4096]
    response = bytes(window).decode("ascii", errors="replace").strip()
    detail = f"; received: {response}" if response else "; no UART bytes received"
    raise TimeoutError("timed out waiting for ILD1 magic" + detail)


def inspect_bytes(raw: bytes) -> dict:
    """Validate a saved dump without changing or decoding its samples."""
    if len(raw) < HEADER.size:
        return {"status": "reject", "reason": "short header", "actual_bytes": len(raw)}
    header = raw[:HEADER.size]
    _magic, version, nf, _cpf, _ntx, _nrx, _ns, fmt, _pad, _trig, _period = HEADER.unpack(header)
    ext_len = TEMP.size if version == 7 else 0
    meta_len = nf * DESC.size + (nf * 2 if fmt == 5 else 0)
    try:
        plan = parse_plan(header, raw[HEADER.size:HEADER.size + ext_len], raw[HEADER.size + ext_len:HEADER.size + ext_len + meta_len])
    except ValueError as exc:
        return {"status": "reject", "reason": str(exc), "actual_bytes": len(raw)}
    expected = plan.expected_bytes
    return {
        "status": "complete" if len(raw) == expected else "reject",
        "reason": "" if len(raw) == expected else f"expected {expected}, got {len(raw)}",
        "actual_bytes": len(raw), "expected_bytes": expected,
        "short_by": max(0, expected - len(raw)), "extra_bytes": max(0, len(raw) - expected),
        "plan": plan,
    }
