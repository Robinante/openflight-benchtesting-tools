"""Compare UART captures using the driver in an existing OpenFlight clone.

Run with --repo PATH --cfg PATH --port COM7. No firmware or repository edits.
Uses the clone's driver, dump parser, and their dependencies without executing
the broad package __init__ imports. Serial methods are not wrapped or patched.
Returned bytes are saved unchanged; these are NOT a recording of every UART read.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib
from importlib.machinery import ModuleSpec
import json
from pathlib import Path
import subprocess
import sys
import time
import types


def load_upstream(repo: Path):
    source = repo / "src" / "openflight"
    if not (source / "iwr6843" / "driver.py").is_file():
        raise ValueError(f"Cannot find src/openflight/iwr6843/driver.py in {repo}")
    if any(n == "openflight" or n.startswith("openflight.") for n in sys.modules):
        raise RuntimeError("Run this script in a fresh Python process.")
    # Namespace shells skip unrelated product initializers. Every module below
    # them still executes directly from the selected clone, with no source edits.
    sys.dont_write_bytecode = True
    for name, directory in (("openflight", source),
                            ("openflight.iwr6843", source / "iwr6843")):
        module = types.ModuleType(name)
        module.__package__ = name
        module.__path__ = [str(directory)]
        module.__spec__ = ModuleSpec(name, loader=None, is_package=True)
        module.__spec__.submodule_search_locations = module.__path__
        sys.modules[name] = module
    driver = importlib.import_module("openflight.iwr6843.driver")
    dump = importlib.import_module("openflight.iwr6843.dump")
    sources = {}
    for name, module in list(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if name.startswith("openflight.") and filename:
            path = Path(filename).resolve()
            path.relative_to(source.resolve())  # Reject a different installed copy.
            sources[name] = {"path": str(path), "sha256": digest(path.read_bytes())}
    return driver, dump, sources


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_info(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(["git", "-C", str(repo), *args],
                                capture_output=True, text=True, timeout=10, check=True)
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unavailable: {exc}"


def describe_return(raw: bytes, dump) -> dict:
    """Inspect after read_dump returns; never alter the saved returned bytes."""
    result = {"returned_nbytes": len(raw), "returned_sha256": digest(raw)}
    start = raw.find(dump.MAGIC)
    if start < 0:
        return {**result, "status": "NO_MAGIC", "response": raw[:1000].decode(errors="replace")}
    binary = raw[start:]
    try:
        meta = dump.parse_header(binary)
        expected = meta["header_nbytes"] + dump.payload_nbytes(meta, binary)
    except (ValueError, KeyError) as exc:
        return {**result, "status": "INVALID_OR_INCOMPLETE_HEADER", "error": str(exc)}
    footer = next((f for f in (b"Done\r\nl3dump:/>", b"Done\nl3dump:/>")
                   if binary.endswith(f)), b"")
    # A short upstream return may retain the CLI footer. Report its apparent
    # binary length separately; keep the original bytes for confirmation.
    nbytes = len(binary) - len(footer)
    status = "SHORT" if nbytes < expected else "EXTRA" if nbytes > expected else "COMPLETE_BY_LENGTH"
    if len(binary) == expected and footer:
        status = "AMBIGUOUS_FOOTER"  # Could be a payload ending in those bytes.
    return {**result, "status": status, "magic_offset": start,
            "expected_nbytes": expected, "binary_nbytes_estimate": nbytes,
            "footer_suffix_nbytes": len(footer), "short_by": max(0, expected - nbytes),
            "extra_by": max(0, nbytes - expected), "header": meta}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True, help="OpenFlight source clone")
    parser.add_argument("--cfg", type=Path, required=True, help="Previously archived diagnostic CFG")
    parser.add_argument("--port", default="COM7")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--out", type=Path, default=Path("captures"))
    parser.add_argument("--check", action="store_true", help="Check imports/paths without opening UART")
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be positive")
    repo, cfg = args.repo.resolve(), args.cfg.resolve()
    try:
        cfg_bytes = cfg.read_bytes()
        driver, dump, sources = load_upstream(repo)
    except ModuleNotFoundError as exc:
        print(f"Missing dependency: {exc.name}. No serial port was opened.", file=sys.stderr)
        if exc.name in {"serial", "numpy"}:
            package = "pyserial" if exc.name == "serial" else "numpy"
            print(f"Install it with: python -m pip install {package}", file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        print(f"Preflight failed: {exc}", file=sys.stderr)
        return 2

    print(f"Driver: {driver.__file__}", flush=True)
    print(f"CFG:    {cfg}", flush=True)
    commit = git_info(repo, "rev-parse", "HEAD")
    print(f"Clone commit: {commit}", flush=True)
    if args.check:
        print("Preflight OK. No serial port opened.")
        return 0

    folder = args.out / ("upstream_uart_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    folder.mkdir(parents=True, exist_ok=False)
    archived_cfg = folder / "profile.cfg"
    archived_cfg.write_bytes(cfg_bytes)
    report = {"schema": "openflight_bench.upstream_probe.v1",
              "started_utc": datetime.now(timezone.utc).isoformat(),
              "python": sys.version, "python_executable": sys.executable,
              "repo": str(repo), "commit": commit,
              "source_status": git_info(repo, "status", "--porcelain", "--", "src/openflight"),
              "sources": sources, "cfg_original": str(cfg), "cfg_sha256": digest(cfg_bytes),
              "port": args.port, "baud": 1041667, "requested_captures": args.count,
              "reader": "clone IWR6843Radar.read_dump(), default arguments, no serial wrapper",
              "between_capture_pause_s": 1.0, "captures": []}

    def save_report():
        (folder / "summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    save_report()
    print(f"Output: {folder.resolve()}", flush=True)
    radar = None
    exit_code = 0
    try:
        radar = driver.IWR6843Radar(port=args.port, baud=1041667)
        radar.send_config(str(archived_cfg.resolve()))
        time.sleep(1.0)  # Fill the 59-pre-frame ring at 10 ms before the first trigger.
        report["stats_before"] = radar.stats()
        print(report["stats_before"], flush=True)
        save_report()
        for number in range(1, args.count + 1):
            started = time.monotonic()
            raw = radar.read_dump()  # Deliberately use the clone's untouched method/defaults.
            name = f"r{number:02d}.returned.bin"
            (folder / name).write_bytes(raw)
            item = {"file": name, "duration_s": round(time.monotonic() - started, 3),
                    **describe_return(raw, dump)}
            report["captures"].append(item)
            save_report()
            print(f"{number}/{args.count}: {item['status']}  returned={len(raw)}  "
                  f"expected={item.get('expected_nbytes', '?')}  "
                  f"short_by={item.get('short_by', '?')}  "
                  f"footer={item.get('footer_suffix_nbytes', 0)}  {item['duration_s']}s", flush=True)
            if item["status"] in {"NO_MAGIC", "INVALID_OR_INCOMPLETE_HEADER"}:
                exit_code = 1
                break
            if number < args.count:
                time.sleep(1.0)
        report["stats_after"] = radar.stats()
        print(report["stats_after"], flush=True)
    except (Exception, KeyboardInterrupt) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        print(f"Stopped: {report['error']}", file=sys.stderr)
        exit_code = 1
    finally:
        if radar is not None:
            try:
                radar.stop_sensor()
            except Exception as exc:
                report["stop_error"] = str(exc)
                print(f"Sensor stop was not confirmed: {exc}", file=sys.stderr)
                exit_code = 1
            finally:
                radar.close()
        report["complete_by_length"] = sum(c["status"] == "COMPLETE_BY_LENGTH" for c in report["captures"])
        save_report()
        print(f"Complete by length: {report['complete_by_length']}/{len(report['captures'])}", flush=True)
        print(f"Saved: {folder.resolve()}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
