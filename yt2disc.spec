# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build description for yt2disc.

Builds *two* executables in a single pass:

    yt2disc.exe      windowed, the desktop GUI (double-click it)
    yt2disc-cli.exe  console, for scripts and batch files

Usage (``build_exe.py`` does this for you, including the icon):

    pyinstaller --noconfirm --workpath _pyi\\work --distpath dist yt2disc.spec

Set the environment variable ``YT2DISC_ONEFILE=1`` to get two single-file
executables instead of a one-folder build.
"""

import os
import sys

ROOT = os.path.abspath(SPECPATH)
if ROOT not in sys.path:  # build_layout.py lives beside this file
    sys.path.insert(0, ROOT)

import build_layout as layout  # the one place the build's paths live

ONEFILE = os.environ.get("YT2DISC_ONEFILE", "").strip() not in ("", "0")

# Where the icon and the version info come from is one decision, made in
# build_layout and shared with build_exe.py.
ICON = str(layout.ICON) if os.path.isfile(layout.ICON) else None
VERSION_FILE = str(layout.VERSION_FILE) if os.path.isfile(layout.VERSION_FILE) else None

# The app reads nothing from its own bundle: bin/, discs/ and the JSON state all
# live next to the executable, so the folder stays portable and writable, and
# there is no data folder left to ship inside it.
datas = []

# gui is imported lazily inside main(), so ask for these explicitly.
hidden = list(layout.HIDDEN_IMPORTS)

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
)
pyz = PYZ(a.pure)

# Shared options.  upx stays off: it slows startup, breaks the odd tcl/tk dll
# and reliably upsets Defender's heuristics.
common = dict(
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    icon=ICON,
    version=VERSION_FILE,
    # keep the traceback dialog for the GUI so crashes are reportable
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

if ONEFILE:
    exe_gui = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        name=layout.GUI_NAME,
        console=False,
        **common,
    )
    exe_cli = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        name=layout.CLI_NAME,
        console=True,
        **common,
    )
else:
    exe_gui = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=layout.GUI_NAME,
        console=False,
        **common,
    )
    exe_cli = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=layout.CLI_NAME,
        console=True,
        **common,
    )
    COLLECT(
        exe_gui,
        exe_cli,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name=layout.COLLECT_NAME,
    )
