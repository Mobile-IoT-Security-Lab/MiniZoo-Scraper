"""Find the installed shared collection core, without changing collection state."""
import os
import sys
from pathlib import Path

def load_core():
    configured = os.environ.get("MINIZOO_CORE_PATH")
    pointer = Path(__file__).with_name("minizoo_core_path.txt")
    if not configured and pointer.is_file():
        configured = pointer.read_text(encoding="utf-8").strip()
    if configured:
        candidates = [Path(configured)]
    else:
        candidates = [candidate for parent in Path(__file__).resolve().parents
                      for candidate in (parent / "MiniZooCollectionCore", parent / "HermesWorkspace/MiniZooCollectionCore")]
    for root in candidates:
        if (root / "minizoo_collection/__init__.py").is_file():
            sys.path.insert(0, str(root))
            import minizoo_collection
            return minizoo_collection
    raise RuntimeError("MiniZooCollectionCore unavailable; set MINIZOO_CORE_PATH to its source directory")

load_core()
