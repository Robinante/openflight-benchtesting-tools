#!/usr/bin/env python3
"""analyze_material.py -- thin wrapper kept so the existing command line works:

    python3 analyze_material.py captures\\material_session1 \\
        --element-csv material_elements.csv \\
        --summary-csv material_summary.csv \\
        --baseline-timeline-csv baseline_timeline.csv

The analysis itself now lives in openflight_bench.analysis/material.py. Same arguments,
same CSV columns, same numbers. To call it from Python instead:

    from openflight_bench.analysis import MaterialOptions, analyze_material
    result = analyze_material(MaterialOptions(capture_dir=Path("captures/mat")))
    result.element_rows, result.summary_rows, result.baseline_timeline_rows
"""

import sys

from openflight_bench.analysis.material import main

if __name__ == "__main__":
    sys.exit(main())
