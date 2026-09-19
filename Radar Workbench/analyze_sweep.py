#!/usr/bin/env python3
"""analyze_sweep.py -- thin wrapper kept so the existing command line works:

    python3 analyze_sweep.py captures\\my_sweep_session

The analysis itself now lives in openflight_bench.analysis/sweep.py. Same arguments, same
CSV columns, same numbers. To call it from Python instead:

    from openflight_bench.analysis import SweepOptions, analyze_sweep
    result = analyze_sweep(SweepOptions(capture_dir=Path("captures/my_session")))
    result.element_rows, result.summary_rows
"""

import sys

from openflight_bench.analysis.sweep import main

if __name__ == "__main__":
    sys.exit(main())
