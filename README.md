# OpenFlight Radar Workbench

Tools for capturing, inspecting, and comparing OpenFlight IWR6843 radar data.
Use the **desktop browser interface** to analyze saved captures, and the
**command-line capture tool** to control the radar and record sessions.

This is an independent experimental bench-testing project. The GUI currently
works with saved captures; live acquisition uses the CLI. It includes session
integrity checks, full range profiles, capture comparisons, material testing,
and angle-sweep analysis.

## Quick start: analysis on Windows

Install Python 3.10 or newer and Git, then run in PowerShell:

```powershell
git clone https://github.com/Robinante/OpenFlight-Radar-Workbench.git
Set-Location OpenFlight-Radar-Workbench
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[workbench]"
& '.\Launch Radar Workbench.bat'
```

After setup, double-click **Launch Radar Workbench.bat**. In the sidebar, set
**Capture folder** to the session you want to inspect. It can live anywhere on
your computer. No radar connection is needed to analyze saved files.

Keep each `.l3dump` with its JSON sidecar, configuration, manifest, and any wire
recordings or hashes. The repo includes one integrity-test capture under
`tests/fixtures/integrity_session`; use that folder for a quick inspection.
It is a partial test fixture, so its hash list intentionally references other
files that are not included.

Already using the nested `Radar Workbench` folder? Follow the
[local migration guide](docs/LOCAL_MIGRATION.md).

## Linux / Raspberry Pi

```bash
git clone https://github.com/Robinante/OpenFlight-Radar-Workbench.git
cd OpenFlight-Radar-Workbench
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[workbench]"
python -m streamlit run workbench/app.py
```

For a capture-only host, install `python -m pip install -e .` instead. This
keeps the GUI and numerical dependencies optional. Follow the
[capture guide](docs/CAPTURE.md) for serial permissions, interface selection,
validated profiles, and capture commands.

**Host validation:** Raspberry Pi/Linux was the validated capture host in the
September 14 tests. The tested Windows CP2105 path intermittently truncated
UART transfers; Windows remains useful for analysis and diagnostics. See the
[measured results](docs/PI_UART_BASELINE_2026-09-14.md).

## Using the tools

| Task | Entry point |
| --- | --- |
| Browse and analyze saved captures | [Workbench pages](workbench/README.md) |
| Control the radar and record sessions | `python -m openflight_bench` — [capture guide](docs/CAPTURE.md) |
| Analyze material or sweep sessions from the terminal | `analyze_material.py`, `analyze_sweep.py` — [analysis guide](docs/ANALYSIS.md) |
| Check an ILD1 dump | `python check_l3dump.py --help` |
| Compare the upstream UART driver | `python upstream_uart_probe.py --help` |
| Understand the metrics and limitations | [Analysis metrics](docs/ANALYSIS_METRICS.md) |
| Browse reports and technical notes | [Documentation index](docs/README.md) |

## Repository layout

| Path | Purpose |
| --- | --- |
| `openflight_bench/` | Capture CLI, transport, configuration, and shared analysis package |
| `workbench/` | Streamlit interface for saved captures |
| `profiles/` | Reference radar configuration |
| `tests/` | Automated tests and the committed integrity fixture |
| `docs/` | Current guides and technical reference |
| `docs/reports/` | Dated published reports and plots |
| `docs/history/` | Historical development notes and audits |
| `docs/golden/` | Reference analysis CSV outputs |
| `legacy/` | Frozen analysis implementations used by regression tests |
| `captures/` | Optional local data directory; ignored by Git |

The small root analysis scripts and `analyzer_validation.py` remain as
compatibility entry points. Keep `legacy/`: regression tests import it.

## Development

From an activated virtual environment at the repository root:

```bash
python -m pip install -e ".[workbench,dev]"
python -m pytest -q -rs
```

Some regression tests need external material and sweep datasets and will skip
without them. A passing clean-clone test run does not replace those checks.
See [test fixtures](tests/FIXTURES.md) to point tests at your local datasets.

Commit reusable code, profiles, tests, and intentional reports. Keep captures,
virtual environments, generated exports, and local assistant configuration out
of Git. Historical notes document earlier states and are not current setup
instructions.
