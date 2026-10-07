"""build_layout.py - the one place that says where a build's pieces live.

A yt2disc release is described in two files that run at different times:

* ``build_exe.py`` runs first - it draws ``assets/yt2disc.ico``, points out the
  version-info file, and calls PyInstaller;
* ``yt2disc.spec`` describes what PyInstaller packs, and reads the icon and the
  version file back off disk.

Both need to agree on the same handful of paths and names, and each used to
restate them.  That is a quiet kind of bug: rename ``dist`` to ``dist-win`` in
one file and the build still succeeds, with everything landing somewhere the
other half never looks.  The paths and the two executables' names therefore live
here, and both build files import them.

Nothing here imports anything heavy - ``pathlib`` and ``sys`` only - because
``yt2disc.spec`` is executed by PyInstaller's own interpreter and has to stay
cheap to load.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The repository root: this file sits in it.
ROOT = Path(__file__).resolve().parent

# Inputs the build reads.
ASSETS = ROOT / "assets"
ICON = ASSETS / "yt2disc.ico"  # drawn by build_exe.make_icon
VERSION_FILE = ASSETS / "version_info.txt"
SPEC = ROOT / "yt2disc.spec"
README = ROOT / "README.md"

# Where the build writes.
WORK = ROOT / "_pyi" / "work"
DIST = ROOT / "dist"
COLLECT_NAME = "yt2disc"
APP = DIST / COLLECT_NAME  # the one-folder build lands here

# The two executables, built in a single pass.
GUI_NAME = "yt2disc"  # windowed, the desktop GUI (double-click it)
CLI_NAME = "yt2disc-cli"  # console, for scripts and batch files

# Modules PyInstaller has to be told about: ``gui`` is imported lazily inside
# ``main()``, so the dependency is invisible to static analysis.
HIDDEN_IMPORTS = ("cli", "core", "gui")

EXE_SUFFIX = ".exe" if sys.platform.startswith("win") else ""


def exe_path(kind: str) -> Path:
    """``dist/yt2disc/yt2disc.exe`` for ``kind="gui"``, the CLI for ``"cli"``."""
    name = CLI_NAME if kind == "cli" else GUI_NAME
    return APP / f"{name}{EXE_SUFFIX}"
