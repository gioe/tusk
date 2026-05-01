"""Canonical list of underscore-named bin/ helper modules.

These files use underscores instead of the ``tusk-*.py`` hyphenated convention
so they can be imported directly (no ``importlib.util.spec_from_file_location``
needed). That same naming choice means they are NOT picked up by the ``tusk-*.py``
glob used elsewhere in the distribution pipeline, so each one historically had
to be enumerated by hand at every callsite.

This module is the single source of truth: every Python callsite that needs to
distribute, lint, or copy these files imports from here. Bash callers in
install.sh and test fixtures still enumerate explicitly — see Convention 21
for the full list of remaining sites.

The module is itself one of the files it enumerates, so it self-distributes
to ``.claude/bin/`` alongside the others.
"""

import os

UNDERSCORE_BIN_FILES = (
    "tusk_loader.py",
    "tusk_skill_filter.py",
    "tusk_github.py",
    "tusk_underscore_bin_files.py",
)


def get_underscore_bin_files(script_dir):
    """Return the canonical underscore-named bin/ files present under ``script_dir/bin/``.

    ``script_dir`` is the directory that contains a ``bin/`` subdirectory —
    the tusk source-repo root in dev, or the unpacked tarball root during
    upgrade. Returns a list of basenames in canonical order, filtered to
    those actually on disk so callers degrade gracefully when a file is
    later removed.
    """
    bin_dir = os.path.join(script_dir, "bin")
    return [name for name in UNDERSCORE_BIN_FILES
            if os.path.isfile(os.path.join(bin_dir, name))]
