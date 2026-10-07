"""build_exe.py - package yt2disc as Windows executables with PyInstaller.

    py -3 build_exe.py              one-folder build (recommended, starts fast)
    py -3 build_exe.py --onefile    two single-file executables
    py -3 build_exe.py --clean      wipe PyInstaller caches first
    py -3 build_exe.py --no-test    skip the automatic end-to-end smoke test

The result lands in ``dist/yt2disc/``:

    yt2disc.exe        windowed GUI - double-click it
    yt2disc-cli.exe    console build for scripts and batch files
    bin/  discs/       drop yt-dlp + ffmpeg into bin/, converted discs go in discs/
    README.md

Every build is validated by ``_e2e/run_e2e.py --exe`` (fake yt-dlp/ffmpeg
shims, no network, no helper binaries needed) unless ``--no-test`` is passed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:  # build_layout.py sits beside this file
    sys.path.insert(0, str(HERE))

import build_layout as layout  # noqa: E402  (the one place the paths live)

# Every path and name the build needs comes from build_layout, so this file and
# yt2disc.spec can never disagree about where a build lands.
SPEC = layout.SPEC
WORK = layout.WORK
DIST = layout.DIST
APP = layout.APP

ASSETS = layout.ASSETS
ICON = layout.ICON
VERSION_FILE = layout.VERSION_FILE

EXE_SUFFIX = layout.EXE_SUFFIX
GUI_EXE = APP / f"{layout.GUI_NAME}{EXE_SUFFIX}"
CLI_EXE = APP / f"{layout.CLI_NAME}{EXE_SUFFIX}"


def log(message: str = "") -> None:
    print(message, flush=True)


def ensure_pyinstaller() -> bool:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        log("PyInstaller is missing - install it once with:\n")
        log(f"    {Path(sys.executable).name} -m pip install pyinstaller\n")
        return False
    return True


def make_icon() -> bool:
    """Render ``assets/yt2disc.ico`` (all the usual sizes) from the pack icon."""
    try:
        from PIL import Image
    except ImportError:
        log("Pillow not installed - keeping the default icon (pip install pillow).")
        return False

    # The one icon-drawing routine lives beside the addon builder, and it is
    # pure standard library (it also draws the pack icons in the browser), so
    # reuse it here instead of keeping a second copy of the artwork.
    webapp = HERE / "webapp"
    if str(webapp) not in sys.path:
        sys.path.insert(0, str(webapp))
    import packs  # the icon is drawn where the rest of the pack is

    ASSETS.mkdir(parents=True, exist_ok=True)
    source = ASSETS / "_icon_256.png"
    source.write_bytes(packs.pack_icon(256))
    try:
        with Image.open(source) as image:
            image.save(
                ICON,
                format="ICO",
                sizes=[
                    (16, 16),
                    (24, 24),
                    (32, 32),
                    (48, 48),
                    (64, 64),
                    (128, 128),
                    (256, 256),
                ],
            )
    finally:
        source.unlink(missing_ok=True)
    log(f"icon       : assets/{ICON.name} ({ICON.stat().st_size:,} bytes)")
    return True


def run_pyinstaller(onefile: bool, clean: bool) -> bool:
    env = dict(os.environ)
    env["YT2DISC_ONEFILE"] = "1" if onefile else "0"

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--log-level",
        "WARN",
    ]
    if clean:
        cmd.append("--clean")
    cmd += ["--workpath", str(WORK), "--distpath", str(DIST), str(SPEC)]

    log("           : running PyInstaller, this takes a minute or two ...")
    proc = subprocess.run(
        cmd,
        cwd=str(HERE),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    WORK.mkdir(parents=True, exist_ok=True)
    log_file = WORK / "pyinstaller.log"
    log_file.write_text(proc.stdout or "", encoding="utf-8")
    if proc.returncode != 0:
        log("\nPyInstaller failed.  Last lines:\n")
        for line in (proc.stdout or "").strip().splitlines()[-25:]:
            log("  | " + line)
        log(f"\nfull log: {log_file}")
        return False
    return True


def clean_dist() -> None:
    """Drop the previous output so nothing stale survives into the shipped folder."""
    for exe in (GUI_EXE, CLI_EXE):
        (DIST / exe.name).unlink(missing_ok=True)
    if APP.is_dir():
        shutil.rmtree(APP, ignore_errors=True)


def assemble() -> None:
    """Add the portable skeleton (bin/, discs/, README) next to the exe."""
    # A one-file build drops the executables straight into dist/, a one-folder
    # build into dist/yt2disc/ - keep the shipped layout identical either way.
    for exe in (GUI_EXE, CLI_EXE):
        loose = DIST / exe.name
        if not exe.is_file() and loose.is_file():
            exe.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(loose), str(exe))
    APP.mkdir(parents=True, exist_ok=True)
    for folder in ("bin", "discs"):
        (APP / folder).mkdir(exist_ok=True)
    helper = HERE / "bin" / "README.txt"
    if helper.is_file():
        shutil.copy2(helper, APP / "bin" / "README.txt")
    readme = layout.README
    if readme.is_file():
        shutil.copy2(readme, APP / "README.md")
    # A state.json copied over from an earlier build must not decide this run's
    # library, so start the packaged copy clean.
    (APP / "state.json").unlink(missing_ok=True)


def smoke_test() -> bool:
    """Run the end-to-end suite against the freshly built console executable."""
    driver = HERE / "_e2e" / "run_e2e.py"
    if not driver.is_file():
        log("no _e2e/run_e2e.py - skipping the smoke test.")
        return True
    if not CLI_EXE.is_file():
        log(f"ERROR: {CLI_EXE.name} is missing, cannot smoke test.")
        return False

    log("           : running _e2e\\run_e2e.py --exe (fake helpers, no network)")
    proc = subprocess.run(
        [sys.executable, "-u", str(driver), "--exe", str(APP)],
        cwd=str(HERE),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = proc.stdout or ""
    WORK.mkdir(parents=True, exist_ok=True)
    log_file = WORK / "e2e.log"
    log_file.write_text(output, encoding="utf-8")

    passed = sum(1 for line in output.splitlines() if line.startswith("PASS"))
    failed = [line for line in output.splitlines() if line.startswith("FAIL")]
    log(f"           : {passed} checks passed, {len(failed)} failed")
    for line in failed:
        log("  " + line)
    log(f"           : full log {log_file}")
    return proc.returncode == 0 and not failed


def report() -> None:
    log("")
    log("=" * 72)
    log("build finished - dist/yt2disc/")
    log("=" * 72)
    for exe in (GUI_EXE, CLI_EXE):
        if exe.is_file():
            log(f"  {exe.name:<18} {exe.stat().st_size:>10,} bytes")
    log("  bin/  discs/        <-- portable data folders")
    log("")
    log("next steps")
    log("  1. drop yt-dlp.exe and ffmpeg.exe into dist/yt2disc/bin/")
    log("  2. run dist/yt2disc/yt2disc.exe  (GUI)  or")
    log("     dist/yt2disc/yt2disc-cli.exe search \"lofi hip hop\"")
    log("  3. to make an in-game music player instead, convert with the web")
    log("     front-end (index.html or app.py) and pick 'Minecraft music player'")
    log("")


def usage() -> int:
    log(__doc__.strip())
    return 0


def main() -> int:
    args = sys.argv[1:]
    if {"-h", "--help"} & set(args):
        return usage()

    onefile = "--onefile" in args
    clean = "--clean" in args
    test = "--no-test" not in args

    log("=" * 72)
    log("yt2disc - building Windows executables")
    log(f"python     : {sys.executable}")
    log(f"mode       : {'one-file' if onefile else 'one-folder'}")
    log(f"spec       : {SPEC.name}")
    log("=" * 72)

    if not ensure_pyinstaller():
        return 1
    make_icon()
    log(f"version info: {'assets/' + VERSION_FILE.name if VERSION_FILE.is_file() else 'default'}")
    clean_dist()

    if not run_pyinstaller(onefile, clean):
        return 1

    assemble()

    if not GUI_EXE.is_file() or not CLI_EXE.is_file():
        log("ERROR: the build did not produce both executables.")
        return 1

    report()

    if test and not smoke_test():
        log("SMOKE TEST FAILED - the executables are left in place for inspection.")
        return 1
    log("OK: executables built" + (" and smoke tested." if test else "."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
