# Analysis guide

The Workbench GUI and the two root scripts use `openflight_bench.analysis`.
Install the analysis dependencies from the repository root in your virtual
environment:

```bash
python -m pip install -e ".[analysis]"
python analyze_material.py --help
python analyze_sweep.py --help
```

For example, write tables to a local exports directory:

```bash
mkdir -p exports
python analyze_material.py captures/material_session --element-csv exports/material_elements.csv --summary-csv exports/material_summary.csv
python analyze_sweep.py captures/sweep_session --element-csv exports/sweep_elements.csv --summary-csv exports/sweep_summary.csv
```

In PowerShell, create that folder with `New-Item -ItemType Directory -Force
exports`. Replace the example capture paths with the folder containing your
`.l3dump` and JSON sidecar pairs. Material analysis needs appropriate baseline
captures; the committed integrity fixture is not a material-test session.

## Python API

```python
from pathlib import Path
from openflight_bench.analysis import (
    MaterialOptions, SweepOptions, analyze_material, analyze_sweep,
    load_capture, analyze_capture, write_rows,
)

cap = load_capture("captures/session/example.l3dump")
analysis = analyze_capture(cap)
print(cap.integrity.describe())
print(analysis.profile)

result = analyze_material(MaterialOptions(capture_dir=Path("captures/material_session")))
# Or: result = analyze_sweep(SweepOptions(capture_dir=Path("captures/sweep_session")))
for name, rows in result.tables.items():
    write_rows(f"{name}.csv", rows)
```

Analysis returns result objects with a `.tables` mapping. Export helpers write
files only when called. To collect progress and export JSON:

```python
from openflight_bench.analysis import CollectingProgress, write_json
progress = CollectingProgress()
result = analyze_material(
    MaterialOptions(capture_dir=Path("captures/material_session")),
    progress=progress,
)
write_json("result.json", result)
```

## Integrity and interpretation

`load_capture` hashes the dump and compares it with a recorded sidecar digest
or the directory's `SHA256SUMS` when available. `cap.integrity.sha256_verified`
is `True`, `False`, or `None` when no reference digest exists. Use
`verify_directory(path)` to inspect a directory against its hash list.

Range-bin spacing prefers the recorded sidecar value over chirp-derived
fallbacks; an explicit override takes precedence. The full range profile is
available even if target annotation fails. Material/sweep analysis still uses
automatic bin selection and has its own angle-gating behavior.

See [analysis metrics](ANALYSIS_METRICS.md) for formulas and limitations,
[test fixtures](../tests/FIXTURES.md) for dataset-dependent regression checks,
and [historical notes](history/README.md) for implementation decisions.
The compatibility validation shim is present; parts of its original behavior
were reconstructed, as documented in the historical analysis-consolidation notes.
