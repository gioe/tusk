"""tusk_loader — generic loader for hyphenated bin/tusk-*.py modules.

Python cannot import hyphenated filenames directly. This module provides a
single load() function that handles importlib boilerplate for all callers.

Usage in any bin/tusk-*.py script:

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import tusk_loader

    _db_lib = tusk_loader.load("tusk-db-lib")
    _pricing = tusk_loader.load("tusk-pricing-lib")
"""

import importlib.util
import os
import sys

_BIN_DIR = os.path.dirname(os.path.abspath(__file__))


def load(name: str):
    """Load a bin/tusk-*.py module by its hyphenated filename stem.

    Args:
        name: the stem of the file, e.g. "tusk-db-lib" or "tusk-pricing-lib".

    Returns:
        The loaded module object.
    """
    module_name = name.replace("-", "_")
    path = os.path.join(_BIN_DIR, f"{name}.py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod
