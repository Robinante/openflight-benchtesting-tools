"""Input discovery and raw byte loading.

``load_entries`` generalizes analyze_sweep.py's ``load_sweep_files`` with an
optional ``test_type`` filter, which is what analyze_material.py's
``load_material_files`` needs (it called ``load_entries(capture_dir, "material")``).
Sidecars with no ``test_type`` key are accepted by any filter so older captures
keep loading exactly as before.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

# NOTE: bodies marked "verbatim" below are copied unchanged from the
# original analyze_sweep.py / analyze_material.py so behavior is preserved
# exactly. Do not reformat them casually -- the regression tests in
# tests/test_analysis_regression.py compare against the originals.


def read_capture_bytes(path) -> bytes:
    """Read one capture file's raw bytes."""
    return Path(path).read_bytes()


def load_entries(capture_dir: Path, test_type: str | None = None) -> list[dict]:
    """Find every *.json sidecar capture_benchtesting.py wrote in
    capture_dir, pair it with its .l3dump, and return one entry per
    capture. Skips (with a printed warning, not a crash) any sidecar
    missing its .l3dump, or any .json that isn't valid JSON."""
    entries = []
    for json_path in sorted(capture_dir.glob("*.json")):
        try:
            record = json.loads(json_path.read_text())
        except json.JSONDecodeError as exc:
            print(f"  !! Skipping {json_path.name}: not valid JSON ({exc})")
            continue
        l3dump_path = json_path.with_suffix(".l3dump")
        if not l3dump_path.exists():
            print(f"  !! Skipping {json_path.name}: no matching {l3dump_path.name}")
            continue
        if record.get("angle_deg") is None or record.get("axis") is None:
            print(f"  !! Skipping {json_path.name}: sidecar is missing angle_deg/axis")
            continue
        if test_type is not None and record.get("test_type") not in (None, test_type):
            continue
        entries.append({
            "angle_deg": float(record["angle_deg"]),
            "axis": str(record["axis"]),
            "repeat_index": record.get("repeat_index"),
            # Material analysis groups by these; sweep analysis ignores them.
            # Carried for every entry so one loader serves both workflows.
            "kind": record.get("kind"),
            "swatch": record.get("swatch"),
            "timestamp": record.get("timestamp"),
            "test_type": record.get("test_type"),
            "record": record,
            "l3dump_path": l3dump_path,
            "sidecar_path": json_path,
        })
    if not entries:
        raise ValueError(f"no valid (.json + .l3dump) pairs found in {capture_dir}")
    return entries


def load_sweep_files(capture_dir: Path) -> list[dict]:
    """Backwards-compatible alias used by the sweep workflow."""
    return load_entries(capture_dir)


def load_material_files(capture_dir: Path) -> list[dict]:
    """Backwards-compatible alias used by the material workflow."""
    return load_entries(capture_dir, "material")


def group_by_angle(entries: list[dict]) -> dict:
    groups = defaultdict(list)
    for e in entries:
        groups[(e["axis"], e["angle_deg"])].append(e)
    return groups
