"""Refuse to run the local lifecycle scripts against production configuration.

This lives in a file rather than a `python -c` one-liner because the one-liner it
replaced contained double quotes: launched through `start-all.cmd`, cmd.exe stripped
them and Python saw a bare name, failing with `NameError: name 'mode' is not defined`
instead of performing the check. A guard that breaks depending on how it was invoked
is worse than no guard, and the quoting is unfixable in a portable way.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digitalbrain.configuration import load_config  # noqa: E402

mode = load_config()["mode"]
if mode != "development":
    sys.stderr.write(
        f"Refusing to run: configuration mode is {mode!r}, not 'development'.\n"
        "Local start-all/stop-all never migrate or collect static for production. "
        "Use the production deployment procedure instead.\n"
    )
    raise SystemExit(1)
