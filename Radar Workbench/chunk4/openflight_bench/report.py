"""Small report helpers; full material/DOA analysis remains compatible with OpenFlight tools."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from .wire import inspect_bytes


def session_summary(folder: Path) -> list[dict]:
    rows = []
    for sidecar_path in sorted(Path(folder).glob("*.json")):
        if sidecar_path.name == "session_manifest.json":
            continue
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        if "capture_info" not in data:
            continue
        raw_path = sidecar_path.with_name(sidecar_path.stem + ".l3dump")
        rejected_path = sidecar_path.with_name(sidecar_path.stem + ".rejected.l3dump")
        if not raw_path.exists() and rejected_path.exists():
            raw_path = rejected_path
        accepted = raw_path.exists() and inspect_bytes(raw_path.read_bytes())["status"] == "complete"
        rows.append({
            "file": raw_path.name if raw_path.exists() else "",
            "status": "complete" if accepted else data["capture_info"].get("status", "missing"),
            "label": data.get("label", ""),
            "test_type": data.get("test_type", ""),
            "tx_mode": data.get("profile", {}).get("tx_mode", ""),
            "rxgain": data.get("profile", {}).get("rxgain", ""),
            "txbackoff": data.get("profile", {}).get("txbackoff", ""),
            "hpf1": data.get("profile", {}).get("hpf1", ""),
            "hpf2": data.get("profile", {}).get("hpf2", ""),
            "actual_bytes": data["capture_info"].get("actual_bytes", ""),
            "expected_bytes": data["capture_info"].get("expected_bytes", ""),
            "short_by": data["capture_info"].get("short_by", ""),
            "accepted_for_analysis": bool(accepted),
        })
    return rows


def write_summary_csv(folder: Path) -> Path:
    rows = session_summary(folder)
    output = Path(folder) / "session_summary.csv"
    if not rows:
        output.write_text("file,status\n", encoding="utf-8")
        return output
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return output
