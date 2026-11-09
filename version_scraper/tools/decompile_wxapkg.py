#!/usr/bin/env python3
"""Compatibility entry point; reconstruction is implemented only in the shared core."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import minizoo_core_bootstrap
from minizoo_collection.tools import decompile_wxapkg as _shared

if __name__ == "__main__":
    raise SystemExit(_shared.main())
else:
    sys.modules[__name__] = _shared
