"""Both collectors use the same observed-runtime acquisition and evidence checks."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import minizoo_core_bootstrap
from minizoo_collection import runtime as _shared
if __name__ == "__main__":
    try:
        _shared.main()
    except Exception as error:
        print("Runtime evidence unavailable: " + str(error), file=sys.stderr)
        sys.exit(1)
else:
    sys.modules[__name__] = _shared
