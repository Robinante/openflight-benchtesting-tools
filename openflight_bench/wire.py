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
    """Read one dump into memory and reject short transfers.

    The returned dict contains the plan, raw bytes, and a status. The old
    capture script treated a short payload as a usable final-frame capture;
    this reader never does that. If the firmware returns to the CLI early,
    bytes after the payload are recognized as completion text and excluded.
    """
    magic = read_until_magic(serial_port, timeout=timeout)
    header = magic + read_exact(serial_port, HEADER.size - len(magic), timeout=timeout, label="header")
    _magic, version, nf, _cpf, _ntx, _nrx, _ns, fmt, _pad, _trig, _period = HEADER.unpack(header)
    extension = read_exact(serial_port, TEMP.size if version == 7 else 0, timeout=timeout, label="temperature")
    metadata_len = nf * DESC.size + (nf * 2 if fmt == 5 else 0)
    metadata = read_exact(serial_port, metadata_len, timeout=timeout, label="metadata")
    plan = parse_plan(header, extension, metadata)

    payload = bytearray()
    started = time.monotonic()
    last_data = started
    trailing = bytearray()
    # Keep a small overlap so the ASCII completion marker is recognized even
    # when the USB serial driver splits it across two reads.
    probe = bytearray()
    while len(payload) < plan.expected_payload_bytes:
        now = time.monotonic()
        if now - started > timeout:
            break
        waiting = getattr(serial_port, "in_waiting", 0)
        chunk = serial_port.read(min(plan.expected_payload_bytes - len(payload), waiting or 1))
        if chunk:
            previous_probe = bytes(probe)
            marker_in_chunk = chunk.find(b"Done")
            if marker_in_chunk < 0:
                combined_probe = previous_probe + chunk
                marker = combined_probe.find(b"Done")
                if marker >= len(previous_probe):
                    marker_in_chunk = marker - len(previous_probe)
            if marker_in_chunk >= 0:
                # The firmware's post-dump text is "Done\\nl3dump:/>". A
                # random four-byte occurrence in IQ data is extraordinarily
                # unlikely, and requiring the prompt makes the guard safer.
                after = chunk[marker_in_chunk:]
                if b"l3dump:" in after or after == b"Done":
                    payload.extend(chunk[:marker_in_chunk])
                    trailing.extend(chunk[marker_in_chunk:])
                    break
            probe.extend(chunk)
            if len(probe) > 96:
                del probe[:-96]
            payload.extend(chunk)
            last_data = now
            continue
        if payload and now - last_data >= stall_timeout:
            # The normal firmware completion text is emitted only after the
            # binary stream. Read a bounded tail, then stop if it is present.
            tail = serial_port.read(128)
            trailing.extend(tail)
            if b"Done" in trailing:
                break
            last_data = now
        elif not payload and now - started >= stall_timeout:
            break

    extra_payload = bytearray()
    if len(payload) == plan.expected_payload_bytes:
        # A normal completion is ASCII `Done\nl3dump:/>`. Consume a short
        # postamble so an overlong transfer cannot remain in the UART and
        # corrupt the next command. Bytes before Done that are not ordinary
        # line whitespace are extra binary payload and make this capture fail.
        post = bytearray()
        deadline = time.monotonic() + 0.30
        while time.monotonic() < deadline and b"Done" not in post:
            waiting = getattr(serial_port, "in_waiting", 0)
            if waiting:
                post.extend(serial_port.read(min(waiting, 256 - len(post))))
            else:
                time.sleep(0.01)
        marker = bytes(post).find(b"Done")
        if marker >= 0:
            prefix = bytes(post[:marker])
            if any(byte not in (9, 10, 13, 32) for byte in prefix):
                extra_payload.extend(prefix)
                status = "rejected_extra"
            else:
                status = "complete"
            trailing.extend(post[marker:])
        elif post:
            # Preserve unexpected postamble bytes for diagnosis instead of
            # letting them leak into the next capture.
            extra_payload.extend(post)
            status = "rejected_extra"
        else:
            status = "complete"
    else:
        status = "rejected_short"
    raw = header + extension + metadata + bytes(payload) + bytes(extra_payload)
    return {
        "status": status,
        "raw": raw,
        "header": header,
        "extension": extension,
        "metadata": metadata,
        "plan": plan,
        "actual_bytes": len(raw),
        "expected_bytes": HEADER.size + len(extension) + len(metadata) + plan.expected_payload_bytes,
        "short_by": max(0, plan.expected_payload_bytes - len(payload)),
        "extra_bytes": len(extra_payload),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "trailing_text": bytes(trailing).decode("ascii", errors="replace"),
    }


def read_until_magic(serial_port, *, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    window = bytearray()
    while time.monotonic() < deadline:
        byte = serial_port.read(1)
        if not byte:
            continue
        window.extend(byte)
        del window[:-4]
        if bytes(window) == MAGIC:
            return MAGIC
    raise TimeoutError("timed out waiting for ILD1 magic")


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
