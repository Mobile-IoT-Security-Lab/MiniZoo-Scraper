"""Compatibility import for the shared, tested acquisition primitives."""
import sys
import minizoo_core_bootstrap
from minizoo_collection import artifacts as _shared
sys.modules[__name__] = _shared
