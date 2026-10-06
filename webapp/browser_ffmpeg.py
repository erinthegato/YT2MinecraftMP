"""browser_ffmpeg.py - the browser's ffmpeg, which is not a program at all.

On a server ``converter.convert()`` ends up in ``core.run_ffmpeg()``, which
starts ``subprocess.Popen(ffmpeg, ...)`` and reads the lines ffmpeg prints while
it works.  In a browser there are no processes: Python *itself* is running
inside WebAssembly, and there is no ffmpeg anywhere to start.  This module puts
a different ffmpeg in its place - the same FFmpeg, built to WebAssembly - and
gives it the same entry points the engine already expects.

The seam is three names on the ``core`` module.  ``converter.py`` looks up
``core.run_ffmpeg``, ``core.probe_duration`` and ``core.find_binary`` every time
it calls them, so :func:`install` rebinds those three and from then on
``converter.convert()`` is running on ffmpeg.wasm with not one line of it
changed.  That is deliberate: ``core.py`` is the *player's* engine too, and the
player never runs in a browser, so the browser's differences belong in a
browser-shaped file rather than in a function the desktop build also depends on.

Why this can be synchronous
---------------------------

``@ffmpeg/ffmpeg`` is a thin wrapper that owns a web worker, which is the right
shape for a page whose Python lives on the main thread.  Our Python is already
in a worker, so a second worker buys nothing and costs a message round trip per
log line.  ``@ffmpeg/core`` on its own is a plain Emscripten module, and its
``exec()`` is a *synchronous* call into the WebAssembly: it returns ffmpeg's
exit code the way ``subprocess.run`` would, and its ``setLogger()`` callback
fires on this same thread while the conversion runs.  So ``core.run_ffmpeg``'s
contract - watch the lines as they arrive, then report - is kept with nothing
wrapped in ``async``, and the progress bar moves *during* a conversion rather
than after it.

The price is that a conversion holds this worker.  That is fine, and in fact
desirable: the worker is where Python lives; Gradio's interface is somewhere
else, so the tab stays responsive throughout.

Files
-----

Pyodide and ffmpeg each have their own filesystem and neither can see the other.
A conversion therefore copies the *input* into ffmpeg's filesystem, runs there,
and copies the *output* back out.  Paths can only be found by looking for them,
because a command is just a list of strings: an argument naming an existing file
is an input, and the last argument of a conversion is where ffmpeg writes
(``converter.build_command`` always ends with the destination).  Everything
else - ``-c:a``, ``libvorbis``, ``192k`` - passes straight through.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# This module sits in webapp/ and needs `core.py` from the folder above it, the
# same way converter.py does - and for the same reason: on a host, neither
# package is importable until somebody says so.
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Pyodide is the only place this module is meant to run, but importing it is
# harmless anywhere: app.py imports it on a server too, to ask whether this is a
# browser at all.
try:
    from pyodide.ffi import to_js
except ImportError:  # pragma: no cover - a real CPython: the server half
    to_js = None

# Pyodide identifies itself this way, and nothing else does.
IS_BROWSER = sys.platform == "emscripten"

# The one ffmpeg build this module knows how to drive: FFmpeg 5.1 with libvorbis,
# libopus and libmp3lame compiled in, which is every codec the output table in
# converter.py asks for.  (wav, flac and m4a use FFmpeg's own encoders, so they
# need no external library at all.)  Pinned, because a build that quietly lost
# libvorbis would break the default output format.
CORE_VERSION = "0.12.10"
CDN = f"https://cdn.jsdelivr.net/npm/@ffmpeg/core@{CORE_VERSION}/dist"
ESM_CORE_URL = f"{CDN}/esm/ffmpeg-core.js"
UMD_CORE_URL = f"{CDN}/umd/ffmpeg-core.js"
ESM_WASM_URL = f"{CDN}/esm/ffmpeg-core.wasm"
# The single-threaded core has no worker of its own; this is only here because
# ffmpeg.wasm passes both URLs through one channel and never asks for this one.
ESM_WORKER_URL = f"{CDN}/esm/ffmpeg-core.worker.js"

# ffmpeg's own filesystem, where staged files live.  It is memory, and it dies
# with the tab, so there is nothing to clean up when a conversion ends.
WORK = "/yt2disc-work"

# What core.find_binary() reports in here.  There is no binary, but the value is
# only ever interpolated into a log line and dropped from the argv we build, so
# any stable name does - and this one says what it really is.
WASM_FFMPEG = Path("ffmpeg.wasm")

_CORE = None
_CORE_MODULE = None
# A callback handed to ffmpeg must outlive the call that installed it, or the
# JavaScript side is left holding a proxy whose Python function has been freed.
_KEEPALIVE: list = []
# The one staged input we keep: converter.convert() probes the source and then
# converts it, and copying a 200 MB upload into ffmpeg's filesystem twice to do
# that would be silly.  Only ever one, so memory stays flat.
_STAGED: dict = {}
_COUNTER = 0


# --------------------------------------------------------------------------
# Importing `core` at all
# --------------------------------------------------------------------------

class _NoProcesses:
    """A stand-in for ``subprocess`` that explains itself if it is ever used.

    Pyodide has no processes, so this only ever answers "there is no such
    thing" - but ``core.py`` imports the module at the top and names its
    constants inside function bodies, so an importable stand-in is all it takes
    for the parts of ``core`` this app *does* use to load.  Nothing ever calls
    :class:`Popen` here; :func:`install` replaces every function that would.
    """

    PIPE = -1
    STDOUT = -2
    DEVNULL = -3
    SubprocessError = OSError
    TimeoutExpired = OSError
    CalledProcessError = OSError

    class Popen:  # noqa: D106 - the message is the whole point of the class
        def __init__(self, *args, **kwargs):
            raise OSError(
                "this is a browser: there are no processes to start.  yt2disc "
                "runs its conversions in WebAssembly instead."
            )


def prepare() -> None:
    """Make ``import core`` possible *before* anything imports it.

    ``core.py`` imports ``subprocess`` at the top, because the player shells out
    to ffmpeg and, on Windows, to the console.  Pyodide ships no
    ``_posixsubprocess`` for it to lean on, so that import can fail - and it
    would fail at *import* time, taking ``core`` and everything downstream with
    it, including the parts of ``core`` that have nothing to do with processes
    (formatting, naming, the error types).

    The page's bootstrap calls this before ``core`` is first imported.  On a
    real CPython it does nothing at all.
    """
    if not IS_BROWSER:
        return
    try:
        import subprocess  # noqa: F401  (Pyodide may or may not ship one)
    except ImportError:
        sys.modules.setdefault("subprocess", _NoProcesses)


def _core():
    """``core``, imported on first use - :func:`prepare` explains the wait."""
    global _CORE_MODULE
    if _CORE_MODULE is None:
        import core

        _CORE_MODULE = core
    return _CORE_MODULE


# --------------------------------------------------------------------------
# ffmpeg's filesystem, which is not Python's
# --------------------------------------------------------------------------

def _fresh(suffix: str) -> str:
    """A name nothing has used in ffmpeg's filesystem yet.

    The suffix is kept, because ffmpeg picks its demuxer and its muxer from the
    extension alone - the same reason ``core.prepare_playable`` keeps the real
    suffix on its part file.
    """
    global _COUNTER
    _COUNTER += 1
    return f"{WORK}/f{_COUNTER}{suffix}"


def _put(name: str, data: bytes) -> None:
    _CORE.FS.writeFile(name, to_js(data))


def _get(name: str):
    """A file out of ffmpeg's filesystem, or None when it was never written."""
    try:
        return bytes(_CORE.FS.readFile(name).to_py())
    except Exception:  # pragma: no cover - the interesting case: no file
        return None


def _drop(name) -> None:
    try:
        _CORE.FS.unlink(name)
    except Exception:  # pragma: no cover - nothing to unlink
        pass


def _stage(path) -> str:
    """Copy a file from Python's filesystem into ffmpeg's, and say where it went."""
    path = Path(str(path))
    try:
        stat = path.stat()
        key = (str(path), stat.st_size, int(stat.st_mtime))
    except OSError:  # pragma: no cover - a file that has gone missing
        key = (str(path), -1, -1)

    if _STAGED.get("key") == key:
        return _STAGED["name"]

    if _STAGED:
        _drop(_STAGED["name"])  # only ever hold one, so memory stays flat
        _STAGED.clear()

    name = _fresh(path.suffix)  # the suffix is how ffmpeg recognizes the format
    _put(name, path.read_bytes())
    _STAGED.update(key=key, name=name)
    return name


def _is_file(arg) -> bool:
    """Is this argument a file on Python's side?  Flags and their values are not."""
    text = str(arg)
    if not text or text.startswith("-"):
        return False
    try:
        return Path(text).is_file()
    except (OSError, ValueError):  # pragma: no cover - defensive
        return False


def _mirror(args) -> tuple[list, list]:
    """Rewrite a command so ffmpeg's own filesystem can see it.

    Returns the rewritten arguments, and ``[(name in ffmpeg's fs, path in
    Python's fs)]`` for the files ffmpeg is about to create.
    """
    last = len(args) - 1
    translated: list = []
    created: list = []
    for index, arg in enumerate(args):
        if index == last and index > 0 and not str(arg).startswith("-"):
            # The destination.  It does not exist yet, so it cannot be found by
            # looking for it; converter.build_command() ends with it, which is
            # what makes this rule a safe one.
            name = _fresh(Path(str(arg)).suffix)
            created.append((name, arg))
            translated.append(name)
        elif _is_file(arg):
            translated.append(_stage(arg))
        else:
            translated.append(arg)
    return translated, created


def _bring_back(name: str, path) -> None:
    """Copy what ffmpeg wrote in its own filesystem back out to Python's."""
    data = _get(name)
    _drop(name)
    if data is None:
        # ffmpeg never made the file.  That is not this function's news to
        # deliver: converter.convert() looks for the file itself, and the exit
        # code and the tail are what say why it is missing.
        return
    target = Path(str(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


# --------------------------------------------------------------------------
# Running it
# --------------------------------------------------------------------------

def _exec(args, log=None, progress=None, tail_size: int = 40):
    """Run ffmpeg, streaming its lines the way core._stream_subprocess() does.

    ffmpeg.wasm hands every line to a callback while the conversion is running,
    on this thread, so this is a loop spelled inside out - which is why the
    progress bar moves while the work happens instead of after it.
    """
    core = _core()
    tail: list = []

    def report(line: str) -> None:
        tail.append(line)
        if len(tail) > tail_size:
            del tail[0]
        # The very same percentage the server reads off the very same ffmpeg
        # output, so a conversion reports identically in both places.
        percent = core._parse_percent(line)
        if percent is not None and progress:
            progress(percent)
        if log:
            log(line)

    def on_event(event) -> None:
        for line in str(getattr(event, "message", "") or "").splitlines():
            if line.strip():
                report(line)

    _KEEPALIVE[:] = [on_event]
    _CORE.setLogger(on_event)
    try:
        code = int(_CORE.exec(*[str(part) for part in args]))
    except Exception as exc:
        if not tail:
            raise core.YT2DiscError(
                f"ffmpeg (WebAssembly) stopped: {exc}"
            ) from exc
        code = -1  # let converter.convert() quote the tail it already has
    _CORE.reset()
    return code, tail


def run_ffmpeg(cmd, log=None, progress=None, tail_size: int = 40):
    """``core.run_ffmpeg``'s job, done by WebAssembly instead of a subprocess."""
    cmd = [str(part) for part in cmd]
    if log:
        # The native path logs the command before it runs it; the browser should
        # look and read the same.
        log("$ " + " ".join(cmd))

    # argv[0] is the program name, and here the program is built in.
    args, created = _mirror(cmd[1:])
    code, tail = _exec(args, log=log, progress=progress, tail_size=tail_size)
    for name, path in created:
        _bring_back(name, path)
    return code, tail


def probe_duration(path, ffmpeg=None):
    """``core.probe_duration``'s job: the Duration line out of ffmpeg's banner."""
    core = _core()
    lines: list = []
    try:
        _exec(["-hide_banner", "-i", _stage(path)], log=lines.append)
    except Exception:
        return None  # best effort, exactly like the native version
    match = core._DURATION_RE.search("\n".join(lines))
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def find_binary(tool: str = "ffmpeg"):
    """``core.find_binary``'s job, which in here is a polite fiction."""
    if tool != "ffmpeg" or not ready():
        return None
    return WASM_FFMPEG


def ready() -> bool:
    """Has :func:`install` run?  The page asks before it believes the banner."""
    return _CORE is not None


# --------------------------------------------------------------------------
# Loading it
# --------------------------------------------------------------------------

async def _factory(js):
    """The core's Emscripten factory, however this worker will take it.

    A module worker can only fetch the ES module; a classic worker can only
    ``importScripts`` the UMD build.  Which kind Gradio Lite gives us is its
    business rather than ours, so both are tried - the ES module first, because
    importing it is the cleaner of the two.
    """
    try:
        importer = js.eval("(url) => import(url)")
        module = await importer(ESM_CORE_URL)
        return module.default, ESM_CORE_URL
    except Exception:
        js.importScripts(UMD_CORE_URL)
        return js.createFFmpegCore, UMD_CORE_URL


def _options(js, core_url):
    """The one argument the core's factory takes.

    Emscripten would look for ``ffmpeg-core.wasm`` beside the script, which is
    where it happens to be.  ffmpeg.wasm passes the pair through
    ``mainScriptUrlOrBlob`` as a base64 fragment instead, and doing the same
    keeps the wasm URL ours to change rather than something ``_locateFile`` has
    to guess at.
    """
    payload = js.btoa(
        json.dumps({"wasmURL": ESM_WASM_URL, "workerURL": ESM_WORKER_URL})
    )
    options = {"mainScriptUrlOrBlob": f"{core_url}#{payload}"}
    return to_js(options, dict_converter=js.Object.fromEntries)


async def install() -> None:
    """Load ffmpeg.wasm once, and put it where ``core`` will find it.

    Rebinding instead of reimplementing is the whole trick: ``converter.py``
    resolves these three names on the ``core`` module each time it calls them,
    so replacing them here moves the engine onto WebAssembly with ``core.py`` -
    which the desktop player also uses - left exactly as it was.
    """
    global _CORE
    if _CORE is not None:
        return

    import js  # only ever importable in here

    factory, core_url = await _factory(js)
    _CORE = await factory(_options(js, core_url))
    try:
        _CORE.FS.mkdir(WORK)
    except Exception:  # pragma: no cover - it is already there
        pass

    core = _core()
    core.run_ffmpeg = run_ffmpeg
    core.probe_duration = probe_duration
    core.find_binary = find_binary
