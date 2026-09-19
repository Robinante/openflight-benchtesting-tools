#!/usr/bin/env python3
"""Read-only structural gate for OpenFlight timed ILD1 dumps (v6/v7, fmt4/5).

Usage: python check_l3dump.py /path/to/captures
       python check_l3dump.py file1.l3dump file2.l3dump --json

Only Python's standard library is required. Directories are searched recursively.
Exit 0: all files have valid metadata and exactly the declared byte length.
Exit 1: at least one file was rejected (including unsupported formats).
Exit 2: command-line/path selection error.

An exact length is a necessary check, NOT an integrity guarantee: this wire
format has no payload checksum. No file is modified or repaired. This checker
does not diagnose RF saturation, channel mapping, phase, or material loss.
"""

import argparse
import glob
import hashlib
import json
from pathlib import Path
import struct

HEADER = struct.Struct("<4sHHHBBHBBHH")
TEMP = struct.Struct("<I10h")
DESC = struct.Struct("<BBH")


def inspect_bytes(raw):
    result = {"actual_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
              "status": "REJECT", "issues": []}
    if len(raw) < HEADER.size:
        result["issues"].append("Incomplete 20-byte header")
        return result
    magic, ver, nf, cpf, ntx, nrx, ns, fmt, pad, trig, period = HEADER.unpack_from(raw)
    result.update(version=ver, sample_fmt=fmt, n_frames=nf, chirps_per_frame=cpf,
                  n_tx=ntx, n_rx=nrx, max_bin_count=ns, frame_period_us=period)
    if magic != b"ILD1":
        result["issues"].append("ILD1 magic missing at byte zero")
    if ver not in (6, 7) or fmt not in (4, 5):
        result["issues"].append("Unsupported: this checker handles v6/v7, fmt4/fmt5 only")
    if not (nf > 0 and cpf > 0 and 1 <= ntx <= 3 and 1 <= nrx <= 4 and ns > 0):
        result["issues"].append("Invalid frame/chirp/antenna/bin geometry")
    elif cpf % ntx:
        result["issues"].append("Chirps per frame are not divisible by TX count")
    if result["issues"]:
        return result
    offset = HEADER.size + (TEMP.size if ver == 7 else 0)
    metadata_end = offset + nf * DESC.size + (nf * 2 if fmt == 5 else 0)
    if len(raw) < metadata_end:
        result["issues"].append("Truncated temperature/descriptor/scale metadata")
        return result
    if ver == 7:
        temp = TEMP.unpack_from(raw, HEADER.size)
        result["device_time_ms"] = temp[0]
        result["temperatures_c"] = list(temp[1:])
    descriptors = [DESC.unpack_from(raw, offset + i * DESC.size) for i in range(nf)]
    result["descriptors"] = [list(d) for d in descriptors]
    if any(not 1 <= count <= ns for _, count, _ in descriptors):
        result["issues"].append("Zero bin count or descriptor wider than header maximum")
    if descriptors[0][2] != 0:
        result["issues"].append("First timed descriptor has nonzero delta")
    if fmt == 5:
        scales = struct.unpack_from("<" + "H" * nf, raw, offset + nf * DESC.size)
        result["iq8_scales"] = list(scales)
        if any(s == 0 for s in scales):
            result["issues"].append("Zero IQ8 frame scale")
    sample_bytes = 2 if fmt == 5 else 4
    frame_bytes = [cpf * nrx * count * sample_bytes for _, count, _ in descriptors]
    expected = metadata_end + sum(frame_bytes)
    result.update(payload_offset=metadata_end, frame_payload_bytes=frame_bytes,
                  expected_bytes=expected, short_by=max(0, expected - len(raw)),
                  extra_bytes=max(0, len(raw) - expected))
    if len(raw) != expected:
        result["issues"].append(
            f"Length mismatch: expected {expected}, got {len(raw)}. Reject the whole dump; "
            "a short file can contain missing bytes before its last frame."
        )
    if not result["issues"]:
        result["status"] = "LENGTH_OK"
    return result


def select_files(arguments):
    files = []
    for argument in arguments:
        candidates = [Path(argument)] if Path(argument).exists() else [Path(p) for p in glob.glob(argument)]
        if not candidates:
            raise ValueError(f"Path/pattern matched nothing: {argument}")
        for path in candidates:
            if path.is_dir():
                found = sorted(path.rglob("*.l3dump"))
                if not found:
                    raise ValueError(f"No .l3dump files in: {path}")
                files.extend(found)
            elif path.is_file():
                files.append(path)
            else:
                raise ValueError(f"Not a regular file or directory: {path}")
    return list(dict.fromkeys(p.resolve() for p in files))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--json", action="store_true", help="Print full JSON results to stdout")
    args = parser.parse_args()
    try:
        paths = select_files(args.paths)
    except ValueError as exc:
        parser.error(str(exc))
    results = []
    for path in paths:
        try:
            result = inspect_bytes(path.read_bytes())
        except OSError as exc:
            result = {"status": "REJECT", "issues": [str(exc)]}
        result["path"] = str(path)
        results.append(result)
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for result in results:
            print(f"{result['status']:9}  {Path(result['path']).name}")
            if "expected_bytes" in result:
                print(f"           {result['actual_bytes']} / {result['expected_bytes']} bytes; "
                      f"short {result['short_by']}; extra {result['extra_bytes']}")
            for issue in result["issues"]:
                print(f"           {issue}")
        print("\nLENGTH_OK means structural checks passed. No payload checksum exists; "
              "this does not certify sample integrity or RF linearity.")
    return 1 if any(r["status"] != "LENGTH_OK" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
