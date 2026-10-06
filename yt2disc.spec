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

ROOT = os.path.abspath(SPECPATH)
ONEFILE = os.environ.get("YT2DISC_ONEFILE", "").strip() not in ("", "0")

ICON = os.path.join(ROOT, "assets", "yt2disc.ico")
if not os.path.isfile(ICON):
    ICON = None  # not generated yet - PyInstaller falls back to its default

VERSION_FILE = os.path.join(ROOT, "assets", "version_info.txt")
if not os.path.isfile(VERSION_FILE):
    VERSION_FILE = None

# The JSON templates and the two fallback PNGs are the only files the app reads
# from its own bundle.  Everything else (bin/, discs/, build/, *.json) lives
# next to the executable, so the folder stays portable and writable.
datas = [(os.path.join(ROOT, "templates"), "templates")]

# gui is imported lazily inside main(), so ask for these explicitly.
hidden = ["cli", "core", "gui"]

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
        pyz, a.scripts, a.binaries, a.datas, name="yt2disc", console=False, **common
    )
    exe_cli = EXE(
        pyz, a.scripts, a.binaries, a.datas, name="yt2disc-cli", console=True, **common
    )
else:
    exe_gui = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="yt2disc",
        console=False,
        **common,
    )
    exe_cli = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="yt2disc-cli",
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
        name="yt2disc",
    )
