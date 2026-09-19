"""Interactive command-line bench session for the OpenFlight IWR6843."""

from __future__ import annotations

import argparse
from pathlib import Path
import shlex

from .capture import BenchController
from .config import BenchProfile, TX_MODES, normalize_tx_mode
from .report import write_summary_csv


HELP = """
Profile commands
  show                                      show the applied/pending profile
  set rxgain 24                             set receiver gain (dB)
  set txbackoff 6                           set TX backoff code (dB)
  set hpf1 0|1|2|3                          set HPF1 corner code
  set hpf2 0|1|2|3                          set HPF2 corner code
  set hpf 0 0                              set HPF1 and HPF2 together
  set window 0 43                          set retained start bin and count
  set frames 5                             set retained post-trigger frames
  set period 10                            set frameCfg periodicity (ms)
  set tx all|off|tx0|tx1|tx2|tx02           select chirp TX masks
  set name my_profile                       name the next profile
  apply                                     send profile, restart RF, archive CFG

Capture commands
  raw label                                 one raw capture
  baseline label                            capture with test_type=material, kind=baseline
  material swatch_name                     capture with test_type=material
                                            (baseline/material ask for an angle:
                                            Enter alone = boresight, 0 deg)
  sweep azimuth|elevation angle label      capture with test_type=sweep
  repeat N raw label                        take N captures
  repeat N baseline|material label          N captures, one angle prompt for the batch
  run rxgain 24,30,36                       apply/capture one repeat per value
  run txbackoff 0,6,12                      apply/capture one repeat per value
  run hpf1 0,1,2,3                          apply/capture one repeat per value
  run hpf2 0,1,2,3                          apply/capture one repeat per value
  run tx off,tx0,tx1,tx2,tx02,all           apply/capture one repeat per TX mode

Other
  stats                                     query firmware counters
  summary                                   write session_summary.csv
  help                                      show this help
  quit                                      stop sensor and close

The first `apply` is automatic. Every profile change is applied only after
you type `apply`; the exact generated CFG and SHA-256 are saved in the session.
Rejected short transfers are saved as `.rejected.l3dump` and never count as
analysis-ready captures. Existing folders are not touched.
"""


def print_profile(profile: BenchProfile, *, applied: bool):
    p = profile.as_dict()
    print(
        f"  {'APPLIED' if applied else 'PENDING'} {profile.name}: "
        f"TX={profile.tx_mode} masks={p['tx_masks']} RX={profile.rxgain} dB "
        f"TXbackoff={profile.txbackoff} dB (0x{profile.packed_tx_backoff:06x}) "
        f"HPF1/2={profile.hpf1}/{profile.hpf2} "
        f"window={profile.start_bin}:{profile.start_bin + profile.bin_count} "
        f"frames={profile.post_frames} period={profile.frame_period_ms}ms loops={profile.loops}"
    )


def parse_value(current: BenchProfile, field: str, value: str) -> BenchProfile:
    if field in {"period", "frame", "frameperiod", "frame_period", "frame_period_ms"}:
        try:
            parsed = float(value)
        except ValueError as exc:
            raise ValueError("period must be a whole number of milliseconds") from exc
        if not parsed.is_integer():
            raise ValueError("period must be a whole number of milliseconds")
        return current.copy(frame_period_ms=int(parsed))
    if field == "tx":
        return current.copy(tx_mode=normalize_tx_mode(value))
    if field == "name":
        return current.copy(name=value)
    if field in {"rxgain", "txbackoff", "hpf1", "hpf2"}:
        try:
            parsed = int(value, 0)
        except ValueError as exc:
            raise ValueError(f"{field} must be an integer") from exc
        return current.copy(**{field: parsed})
    raise ValueError("set accepts rxgain, txbackoff, hpf1, hpf2, period, tx, or name")


def prompt_angle_axis(input_fn=input):
    """Ask where a baseline/material capture was taken; return (angle_deg, axis).

    Enter alone (or 0) means boresight: (0.0, "boresight"), the same value the
    original capture_benchtesting.py logged. The analysis loader skips any
    sidecar whose angle_deg or axis is missing, and it groups a swatch only
    with baselines taken at the same angle, so material/baseline captures must
    always carry both fields. An unparseable answer is asked again rather than
    silently logged as boresight.
    """
    while True:
        raw = input_fn("  angle deg (blank = boresight 0)> ").strip()
        if not raw:
            return 0.0, "boresight"
        try:
            angle = float(raw)
        except ValueError:
            print(f"  !! Didn't understand angle '{raw}'. Enter a number, or press Enter for boresight.")
            continue
        if angle == 0.0:
            return 0.0, "boresight"
        break
    while True:
        axis_raw = input_fn("  axis (azimuth/elevation)> ").strip().lower()
        if axis_raw in ("az", "azimuth"):
            return angle, "azimuth"
        if axis_raw in ("el", "elevation"):
            return angle, "elevation"
        print(f"  !! Didn't understand axis '{axis_raw}'. Type az or el.")


def print_capture_result(result):
    info = result["capture_info"]
    status = "ACCEPTED" if result["accepted_for_analysis"] else "REJECTED"
    print(f"  {status}: {result['path'].name}  {info['actual_bytes']}/{info['expected_bytes']} bytes  short_by={info['short_by']}")
    if not result["accepted_for_analysis"]:
        print("  !! This dump is excluded from analysis; repeat the capture.")


def run_sweep(controller: BenchController, field: str, values_text: str):
    values = [x.strip() for x in values_text.split(",") if x.strip()]
    if field == "tx":
        values = [normalize_tx_mode(x) for x in values]
    if field not in {"rxgain", "txbackoff", "hpf1", "hpf2", "period", "tx"} or not values:
        raise ValueError("run field must be rxgain, txbackoff, hpf1, hpf2, period, or tx")
    for value in values:
        controller.profile = parse_value(controller.profile, field, value)
        controller.profile = controller.profile.copy(name=f"{field}_{value}")
        print(f"\nApplying {field}={value}")
        response = controller.send_config(controller.profile)
        if "error" in response.lower():
            raise RuntimeError(f"radar rejected sweep setting: {response.strip()}")
        result = controller.capture(f"{field}_{value}", kind="raw")
        print_capture_result(result)


def main(argv=None):
    parser = argparse.ArgumentParser(description="OpenFlight IWR6843 bench capture session")
    parser.add_argument("--port", required=True, help="CLI UART, e.g. COM7")
    parser.add_argument("--out-dir", type=Path, default=Path("captures"), help="new session folder parent")
    parser.add_argument("--baud", type=int, default=1_041_667, help="data UART baud used by this firmware")
    parser.add_argument("--timeout", type=float, default=45.0, help="maximum seconds per binary dump")
    parser.add_argument("--no-apply", action="store_true", help="connect without automatically sending the default profile")
    args = parser.parse_args(argv)

    try:
        controller = BenchController(args.port, args.out_dir, args.baud, timeout=args.timeout)
    except Exception as exc:
        parser.error(str(exc))
    print(f"New session: {controller.out_dir}")
    print("Old capture folders are untouched. This session starts empty.")
    print_profile(controller.profile, applied=False)
    try:
        if not args.no_apply:
            print(
                "Applying default profile "
                f"(RX{controller.profile.rxgain} / "
                f"TX backoff {controller.profile.txbackoff} / "
                f"HPF {controller.profile.hpf1},{controller.profile.hpf2} / "
                f"{controller.profile.post_frames} post frames / "
                f"{controller.profile.frame_period_ms} ms)..."
            )
            # Stop/flush is harmless on an idle board and makes a rerun safe if
            # the previous terminal session left the sensor running.
            response = controller.send_config(controller.profile, first=False)
            if "error" in response.lower():
                raise RuntimeError(f"radar rejected default profile: {response.strip()}")
            print_profile(controller.profile, applied=True)
        print("Type `help` for commands. Use `set ...` then `apply` to change profiles.")
        while True:
            try:
                line = input("bench> ").strip()
            except EOFError:
                break
            if not line:
                continue
            parts = shlex.split(line)
            command = parts[0].lower()
            try:
                if command in {"quit", "q", "exit"}:
                    break
                if command == "help":
                    print(HELP)
                elif command == "show":
                    print_profile(controller.profile, applied=controller.applied)
                elif command == "stats":
                    print(controller.stats().get("raw", "(no response)"))
                elif command == "summary":
                    print(f"Wrote {write_summary_csv(controller.out_dir)}")
                elif command == "set" and len(parts) >= 3:
                    if parts[1].lower() == "hpf" and len(parts) == 4:
                        controller.profile = controller.profile.copy(hpf1=int(parts[2], 0), hpf2=int(parts[3], 0))
                    elif parts[1].lower() == "window" and len(parts) == 4:
                        controller.profile = controller.profile.copy(start_bin=int(parts[2], 0), bin_count=int(parts[3], 0))
                    elif parts[1].lower() == "frames" and len(parts) == 3:
                        controller.profile = controller.profile.copy(post_frames=int(parts[2], 0))
                    else:
                        controller.profile = parse_value(controller.profile, parts[1].lower(), " ".join(parts[2:]))
                    controller.applied = False
                    print_profile(controller.profile, applied=False)
                elif command == "apply":
                    response = controller.send_config(controller.profile)
                    if "error" in response.lower():
                        raise RuntimeError(response.strip())
                    print_profile(controller.profile, applied=True)
                elif command in {"raw", "baseline", "material"} and len(parts) >= 2:
                    kind = "material" if command in {"baseline", "material"} else "raw"
                    label = "_".join(parts[1:])
                    angle_deg, axis = prompt_angle_axis() if kind == "material" else (None, None)
                    result = controller.capture(label, kind=kind, swatch=(label if command == "material" else None), material_kind=(command if command in {"baseline", "material"} else None), angle_deg=angle_deg, axis=axis)
                    print_capture_result(result)
                elif command == "sweep" and len(parts) >= 4:
                    axis = parts[1].lower()
                    if axis not in {"azimuth", "elevation"}:
                        raise ValueError("sweep axis must be azimuth or elevation")
                    result = controller.capture("_".join(parts[3:]), kind="sweep", angle_deg=float(parts[2]), axis=axis)
                    print_capture_result(result)
                elif command == "repeat" and len(parts) >= 4:
                    count = int(parts[1])
                    mode = parts[2].lower()
                    label = "_".join(parts[3:])
                    if count < 1 or mode not in {"raw", "baseline", "material"}:
                        raise ValueError("repeat syntax: repeat N raw|baseline|material label")
                    angle_deg, axis = prompt_angle_axis() if mode != "raw" else (None, None)
                    for index in range(count):
                        result = controller.capture(f"{label}_r{index + 1}", kind=("material" if mode != "raw" else "raw"), material_kind=(mode if mode != "raw" else None), swatch=(label if mode == "material" else None), angle_deg=angle_deg, axis=axis)
                        print_capture_result(result)
                elif command == "run" and len(parts) == 3:
                    run_sweep(controller, parts[1].lower(), parts[2])
                else:
                    print("Unrecognized command. Type `help`.")
            except Exception as exc:
                print(f"  !! {exc}")
    except KeyboardInterrupt:
        print("\nInterrupted")
    finally:
        controller.close()
        print(f"Session saved in {controller.out_dir}")


if __name__ == "__main__":
    main()
