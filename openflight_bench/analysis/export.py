"""Export helpers: CSV (unchanged) and JSON (new in Chunk 3).

Both original scripts repeated the same four-line write: mkdir the parent,
DictWriter with fieldnames taken from the first row's keys, header, rows.
``write_rows`` is that, unchanged, so column order and contents are identical.
"""

from __future__ import annotations

import csv as csv_module
import json
from datetime import datetime, timezone
from pathlib import Path


def write_rows(path, rows: list[dict]) -> int:
    """Write rows to a CSV, taking fieldnames from the first row's key order."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv_module.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def result_metadata(result) -> dict:
    """Provenance for a result: what produced it, from what, and how trustworthy
    each input was. This is the part a CSV cannot carry."""
    caps = []
    for c in getattr(result, "captures", []) or []:
        integ = c.integrity
        caps.append({
            "file": c.source.name,
            "sha256": integ.sha256 or None,
            "sha256_verified": integ.sha256_verified,
            "sha256_source": integ.content.source,
            "accepted_for_analysis": integ.accepted_for_analysis,
            "frames_declared": integ.declared_frames,
            "frames_decoded": integ.n_frames_decoded,
            "frames_incomplete": integ.n_frames_incomplete,
            "status": integ.status,
            "range_bin_m": c.configuration.range_bin_m,
            "range_bin_source": c.configuration.range_bin_source,
            "sample_format": c.source.sample_format_name,
            "config_path": c.source.config_path,
            "n_tx": c.channels.n_tx,
            "n_rx": c.channels.n_rx,
            "window_start_bin": c.configuration.window_start_bin,
            "window_bin_count": c.configuration.window_bin_count,
        })
    sources = sorted({c["range_bin_source"] for c in caps}) if caps else []
    verified = [c["sha256_verified"] for c in caps]
    return {
        "kind": getattr(result, "kind", "analysis"),
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "tool": "openflight_bench.analysis",
        "row_counts": result.row_counts() if hasattr(result, "row_counts") else {},
        "warnings": list(getattr(result, "warnings", []) or []),
        "range_bin_sources": sources,
        "n_captures": len(caps),
        "n_sha256_verified": sum(1 for v in verified if v is True),
        "n_sha256_unrecorded": sum(1 for v in verified if v is None),
        "n_sha256_mismatch": sum(1 for v in verified if v is False),
        "captures": caps,
    }


def write_json(path, result, *, indent: int = 1) -> int:
    """Write one JSON file holding every table in a result plus its provenance.

    Phase 1 asks for "CSV and JSON output". The CSVs stay exactly as they were;
    this is the machine-readable sibling that also records where each number's
    inputs came from and whether their bytes verified.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "metadata": result_metadata(result),
        "tables": {name: rows for name, rows in result.tables.items() if rows},
    }
    path.write_text(json.dumps(payload, indent=indent, default=_jsonable), encoding="utf-8")
    return sum(len(r) for r in payload["tables"].values())


def _jsonable(obj):
    """Last-resort encoder: Paths, numpy scalars, anything with .item()."""
    if isinstance(obj, Path):
        return str(obj)
    for attr in ("item", "tolist"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001
                pass
    return str(obj)
