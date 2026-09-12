"""Serial orchestration and sidecar writing for the bench CLI."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time

from .config import BenchProfile, config_hash, make_config
from .wire import HEADER, read_dump


def safe_label(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.+-]+", "_", value.strip())
    return value.strip("._") or "capture"


class BenchSerial:
    def __init__(self, port: str, baud: int = 1_041_667):
        try:
            import serial
        except ImportError as exc:
            raise RuntimeError("pyserial is required: python -m pip install pyserial") from exc
        self.ser = serial.Serial()
        self.ser.port = port
        self.ser.baudrate = baud
        self.ser.timeout = 0.25
        self.ser.dtr = False
        self.ser.rts = False
        self.ser.open()

    @property
    def in_waiting(self):
        return self.ser.in_waiting

    def read(self, count=1):
        return self.ser.read(count)

    def write_line(self, line: str):
        self.ser.write((line.rstrip() + "\n").encode("ascii"))
        self.ser.flush()

    def close(self):
        self.ser.close()


class BenchController:
    def __init__(self, port: str, out_dir: Path, baud: int = 1_041_667, *, timeout: float = 45.0):
        self.port, self.baud, self.timeout = port, baud, timeout
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.out_dir = Path(out_dir) / f"bench_{stamp}"
        self.out_dir.mkdir(parents=True, exist_ok=False)
        self.serial = BenchSerial(port, baud)
        # Discard boot banner/prompt text before the first generated command;
        # never let stale CLI text become part of a binary dump.
        self.drain_text(0.35)
        self.profile = BenchProfile()
        self.applied = False
        self.capture_number = 0
        self.manifest = {
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "port": port,
            "baud": baud,
            "package": "openflight_bench",
            "default_profile": self.profile.as_dict(),
            "captures": [],
        }
        self._write_manifest()

    def _write_manifest(self):
        (self.out_dir / "session_manifest.json").write_text(json.dumps(self.manifest, indent=2) + "\n", encoding="utf-8")

    def drain_text(self, seconds: float = 0.35) -> str:
        end = time.monotonic() + seconds
        result = bytearray()
        while time.monotonic() < end:
            chunk = self.serial.read(getattr(self.serial, "in_waiting", 0) or 1)
            if chunk:
                result.extend(chunk)
                end = time.monotonic() + 0.15
            else:
                time.sleep(0.01)
        return result.decode("ascii", errors="replace")

    def command(self, line: str, *, wait: float = 0.45) -> str:
        self.serial.write_line(line)
        return self.drain_text(wait)

    def send_config(self, profile: BenchProfile, *, first: bool = False) -> str:
        if not first:
            self.command("sensorStop", wait=0.5)
            self.command("flushCfg", wait=0.5)
        text = make_config(profile, include_sensor_start=False)
        responses = []
        for line in text.splitlines():
            response = self.command(line)
            responses.append(response)
            if "error" in response.lower():
                raise RuntimeError(f"radar rejected '{line}': {response.strip()}")
        response = self.command("sensorStart", wait=0.8)
        responses.append(response)
        self.profile = profile
        self.applied = True
        cfg_path = self.out_dir / f"profile_{safe_label(profile.name)}.cfg"
        cfg_text = make_config(profile)
        cfg_path.write_text(cfg_text, encoding="ascii")
        self.manifest["last_applied_profile"] = profile.as_dict()
        self.manifest.setdefault("profiles", {})[profile.name] = {
            "config_file": cfg_path.name,
            "config_sha256": config_hash(cfg_text),
            "profile": profile.as_dict(),
        }
        self._write_manifest()
        return "".join(responses)

    def stats(self) -> dict:
        raw = self.command("stats", wait=0.7)
        fields = {}
        for key, val in re.findall(r"(\w+)=(0x[0-9a-fA-F]+|-?\d+)", raw):
            fields[key] = int(val, 16) if val.lower().startswith("0x") else int(val)
        fields["raw"] = raw
        return fields

    def capture(self, label: str, *, kind: str = "raw", angle_deg: float | None = None, axis: str | None = None, swatch: str | None = None, material_kind: str | None = None) -> dict:
        if not self.applied:
            raise RuntimeError("no profile has been applied")
        self.capture_number += 1
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = f"{kind}_{safe_label(label)}_{stamp}_g{self.capture_number:04d}"
        # l3dump is a binary-producing CLI command.  Do not use command(),
        # because that method drains the port as text and could consume the
        # beginning of the ILD1 stream before read_dump() sees it.
        self.serial.write_line("l3dump")
        result = read_dump(self.serial, timeout=self.timeout)
        plan = result["plan"]
        status = result["status"]
        suffix = ".l3dump" if status == "complete" else ".rejected.l3dump"
        path = self.out_dir / f"{stem}{suffix}"
        path.write_bytes(result["raw"])
        # On a complete transfer the firmware's Done/prompt text follows the
        # payload. Consume it before issuing stats, so health fields belong to
        # this capture rather than an earlier command response.
        self.drain_text(0.18)
        stats = self.stats()
        info = {
            "version": plan.version,
            "n_frames": plan.n_frames,
            "chirps_per_frame": plan.chirps_per_frame,
            "frame_period_us": HEADER.unpack(result["header"])[10],
            "n_tx": plan.n_tx,
            "n_rx": plan.n_rx,
            "sample_fmt": plan.sample_fmt,
            "window_start_bin": plan.descriptors[0][0],
            "window_bin_count": plan.descriptors[0][1],
            "window_fixed_across_frames": len({(d[0], d[1]) for d in plan.descriptors}) == 1,
            "window_range_m": [
                plan.descriptors[0][0] * self.profile.range_bin_m,
                (plan.descriptors[0][0] + plan.descriptors[0][1]) * self.profile.range_bin_m,
            ],
            "range_bin_m": self.profile.range_bin_m,
            "actual_bytes": result["actual_bytes"],
            "expected_bytes": result["expected_bytes"],
            "total_bytes": result["actual_bytes"],
            "short_by": result["short_by"],
            "extra_bytes": result.get("extra_bytes", 0),
            "status": status,
        }
        info["stats"] = stats
        timestamp = datetime.now(timezone.utc).isoformat()
        sidecar = {
            "schema": "openflight_bench.capture.v1",
            "timestamp": timestamp,
            "timestamp_utc": timestamp,
            "port": self.port,
            "baud": self.baud,
            "test_type": kind,
            "kind": material_kind if material_kind is not None else kind,
            "label": label,
            "repeat_index": self.capture_number,
            "swatch": swatch,
            "angle_deg": angle_deg,
            "axis": axis,
            "config_path": f"profile_{safe_label(self.profile.name)}.cfg",
            "config_sha256": config_hash(make_config(self.profile)),
            "profile": self.profile.as_dict(),
            "capture_info": info,
            "stats": stats,
            "file_sha256": result["sha256"],
            "accepted_for_analysis": status == "complete",
        }
        sidecar_path = self.out_dir / f"{stem}.json"
        sidecar_path.write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")
        self.manifest["captures"].append({"file": path.name, "sidecar": sidecar_path.name, "status": status, "profile": self.profile.as_dict()})
        self._write_manifest()
        return {"path": path, "sidecar": sidecar_path, **sidecar}

    def close(self):
        try:
            if self.applied:
                self.command("sensorStop", wait=0.5)
        finally:
            self.serial.close()
