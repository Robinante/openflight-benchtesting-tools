# Radar Workbench interface

Follow the [root quick start](../README.md#quick-start-analysis-on-windows), then
use **Launch Radar Workbench.bat** in the repository root. Alternatively, from
an activated environment at the root:

```bash
python -m streamlit run workbench/app.py
```

Point the sidebar's **Capture folder** at any directory of `.l3dump` + `.json`
pairs. A session folder with dated subdirectories works too -- it looks one
level down when the top level has no dumps.

| page | what it does |
|---|---|
| **Session** | every capture in the folder with its integrity state; verifies the folder against `SHA256SUMS` on demand; shows the `session_manifest.json` profile |
| **Analyze** | one capture: peak, SNR, noise floor, range profile, per-(TX,RX) level and phase grids, and the full normalized Capture |
| **Compare** | overlay any number of captures, pick a reference, get a delta table and a delta-SNR bar |
| **Material** | the material comparison, with baseline-drift and attenuation plots, every table, and CSV/JSON download. When the angle gate rejects everything it shows the candidate table instead of just failing |
| **Sweep** | the sweep analysis, same treatment |

It holds no analysis logic. Every page calls the same
`openflight_bench.analysis` functions the CLI calls, so a number shown here and
a number in the CSV come from the same code path. The GUI analyzes saved captures. For live acquisition, use the
[capture CLI](../docs/CAPTURE.md).
