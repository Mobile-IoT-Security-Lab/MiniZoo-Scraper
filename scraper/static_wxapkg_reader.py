"""Compatibility import for the shared non-executing package metadata reader."""
import sys
import minizoo_core_bootstrap
from minizoo_collection import static_reader as _shared
sys.modules[__name__] = _shared
