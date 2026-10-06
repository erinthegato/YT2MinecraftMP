"""Start a program the way Explorer starts a double-clicked one.

    python _e2e/no_console.py <program> [args...]

Used by ``run_e2e.py`` to prove that the windowed ``yt2disc.exe`` falls back to
``yt2disc-cmd.log`` when there is neither a console nor standard output.

Two details make the difference:

*   ``run_e2e.py`` starts *this* helper with DETACHED_PROCESS, so it has no
    console - and neither does the program it starts, which means
    ``AttachConsole(ATTACH_PARENT_PROCESS)`` fails just like it does for a
    double-click.  (Launched from cmd.exe or PowerShell it would succeed and
    print to that window instead.)
*   the child is started *without* redirecting anything.  ``subprocess.run``
    defaults to ``close_fds=True``, so the child inherits no standard handles at
    all - passing ``stdout=subprocess.DEVNULL`` instead would hand it a perfectly
    valid NUL handle and defeat the whole point of the test.
"""

import subprocess
import sys


def main(argv) -> int:
    if not argv:
        return 2
    proc = subprocess.run(argv, close_fds=True)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
