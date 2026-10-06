"""main.py - single entry point for yt2disc.

    python main.py                     # desktop GUI (no arguments)
    python main.py --cli <command>     # command line (--cli is stripped)
    python main.py playlist new Chill  # command line directly

The GUI is imported lazily so the command line keeps working on systems
where tkinter is not available.
"""

from __future__ import annotations

import sys

import cli
import core


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # `--cli` is accepted (and stripped) so both entry styles behave the same.
    explicit_cli = "--cli" in args
    args = [argument for argument in args if argument != "--cli"]

    if not args:
        if explicit_cli:
            # `--cli` with no command means "no GUI": show the CLI usage.
            core.ensure_console()
            return cli.run([])
        import gui

        return gui.main()

    # Everything below prints, so make sure print() works in a windowed build.
    core.ensure_console()

    if args[0] in ("--version", "-V"):
        print("yt2disc - a portable Minecraft music player")
        print(f"  app folder      : {core.SCRIPT_DIR}")
        print(f"  playlists       : {core.PLAYLISTS_PATH}")
        print(f"  converted discs : {core.DISCS_DIR}")
        print(f"  minecraft songs : {core.find_minecraft_content() or 'not found'}")
        print(f"  ffmpeg          : {core.find_binary('ffmpeg') or 'not found'}")
        return 0

    if args[0] in ("-h", "--help", "help"):
        return cli.run(["--help"])

    return cli.run(args)


if __name__ == "__main__":
    raise SystemExit(main())

