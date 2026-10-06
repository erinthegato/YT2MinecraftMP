"""app.py - the hosted half of yt2disc, drawn with Gradio.

One file in, one file out, in the format you ask for: this is the converter and
nothing else.  There is no player here, no library, and nothing that reads a
Minecraft install - playing the result is the local add-on's job.

Gradio draws the whole front-end.  The upload box, the progress bar, the
download link and the error notice all come out of ``gr.Blocks``, so there is no
HTML, no stylesheet and no web framework for this module to keep in step with:
``webapp/converter.py`` is the entire back end, and this file is only the shape
of the form around it.

    python app.py                       # http://127.0.0.1:7860
    python app.py --share               # ... and a public https URL as well
    python app.py --host 0.0.0.0 --port 7860
    python app.py --key none            # ignore YT2DISC_WEB_TOKEN for this run

On Hugging Face Spaces the block at the top of ``README.md`` is what starts it.
It asks for ``sdk: static`` with ``app_file: index.html``: the Space hands the
browser ``index.html``, and Gradio Lite runs *this* file in the visitor's own
tab, on Pyodide, with ``webapp/browser_ffmpeg.py`` standing in for the ffmpeg
binary.  That deployment uploads nothing anywhere, installs nothing (a Static
Space cannot), and so needs no billing account and no card.
``requirements.txt`` and ``packages.txt`` beside this file are for the hosts
that *do* install things - the ``python app.py`` run below, and the Flask
front-end's Dockerfile.

Environment variables, all optional.  Every one of them describes the *server*
run above; a browser has no environment to set and needs none of it:

    GRADIO_SERVER_PORT / PORT   port to listen on (default 7860)
    GRADIO_SERVER_NAME / YT2DISC_WEB_HOST
                                address to listen on (default 127.0.0.1)
    YT2DISC_WEB_DATA            scratch folder (default: the system temp dir)
    YT2DISC_WEB_MAX_UPLOAD_MB   upload cap (default 200)
    YT2DISC_WEB_KEEP_MINUTES    how long a scratch folder lives (default 120)
    YT2DISC_FFMPEG              path to ffmpeg, otherwise PATH then bin/
    YT2DISC_WEB_TOKEN           shared key visitors must give (default: off)
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEBAPP = ROOT / "webapp"
# Gradio starts this file as a script from the repository root, but `core.py`
# sits beside us and `converter.py` in the package next to it, and neither is
# importable until we say so - on a host that is the difference between the app
# starting and the app dying on its first import.
for _entry in (str(ROOT), str(WEBAPP)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import gradio as gr  # noqa: E402  (after the sys.path fix above)

import browser_ffmpeg  # noqa: E402
import core  # noqa: E402
import converter  # noqa: E402

APP_NAME = "yt2minecraftdisc converter"
MEGABYTE = 1024 * 1024
# How much of ffmpeg's chatter the log box keeps.
LOG_LINES = 40


# --------------------------------------------------------------------------
# Where the work happens
# --------------------------------------------------------------------------

def int_env(name: str, default: int, low: int = 1, high: int = 1_000_000) -> int:
    """An integer environment variable, clamped, falling back when unreadable."""
    try:
        value = int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def scratch_root() -> Path:
    """The folder conversions are made in, and the one :func:`sweep` empties."""
    root = Path(
        os.environ.get("YT2DISC_WEB_DATA")
        or Path(tempfile.gettempdir()) / "yt2minecraftdisc-web"
    )
    root.mkdir(parents=True, exist_ok=True)
    return root


SCRATCH = scratch_root()
# The old job registry deleted a finished download after this long; the same
# number now decides how long a scratch folder is left alone, because Gradio
# serves the download link out of that folder.
KEEP_SECONDS = int_env("YT2DISC_WEB_KEEP_MINUTES", 120, 1, 100000) * 60.0
MAX_UPLOAD_MB = int_env("YT2DISC_WEB_MAX_UPLOAD_MB", 200, 1, 4096)


def sweep(keep=None) -> int:
    """Delete the scratch folders of conversions that finished long ago.

    Gradio hands the browser a link into the folder a conversion wrote, so that
    folder has to outlive the request which made it - while a converter left on
    the open internet must not slowly turn into a file store either.  One number
    balances the two: anything younger than ``YT2DISC_WEB_KEEP_MINUTES`` stays,
    and the rest goes at the start of the next conversion.  The folder about to
    be written is passed in as ``keep``, so a sweep can never delete the work it
    is running beside.
    """
    cutoff = time.time() - KEEP_SECONDS
    removed = 0
    for folder in SCRATCH.glob("job-*"):
        if keep is not None and folder == keep:
            continue
        try:
            if folder.is_dir() and folder.stat().st_mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
        except OSError:  # pragma: no cover - defensive
            pass
    return removed

# --------------------------------------------------------------------------
# The door, when there is one
# --------------------------------------------------------------------------

def credentials():
    """``YT2DISC_WEB_TOKEN`` as Gradio's ``(username, password)``, or ``None``.

    A converter on the open internet is an ffmpeg job that anyone who finds the
    URL can start, so a token puts a lock on the door.  Gradio's own lock wants
    a username as well as a password: a token written ``user:pass`` keeps both
    halves, and a bare token - which is what a generated secret looks like -
    becomes ``yt2disc`` plus that token, so one secret still covers the whole
    door.  Unset, this returns ``None`` and nothing changes at all, which keeps
    a run on your own PC behaving exactly as it always has.
    """
    token = (os.environ.get("YT2DISC_WEB_TOKEN") or "").strip()
    if not token:
        return None
    user, sep, password = token.partition(":")
    if sep and user and password:
        return (user, password)
    return ("yt2disc", token)


def server_note() -> str:
    """The line at the top of the page: where is the ffmpeg that does the work?"""
    if browser_ffmpeg.IS_BROWSER and browser_ffmpeg.ready():
        return (
            "**ffmpeg: in this browser.**  The conversion runs in your own tab, "
            "on WebAssembly, so your file is never uploaded - and the first "
            "visit downloads ffmpeg once (about 30 MB)."
        )
    ffmpeg = core.find_binary("ffmpeg")
    if ffmpeg is None:
        return (
            "**ffmpeg is not installed on this server**, so nothing can be "
            "converted yet.  Put `ffmpeg` in `bin/`, point `YT2DISC_FFMPEG` at "
            "it, or install it on the host's `PATH`."
        )
    return f"ffmpeg: `{ffmpeg}`"


# --------------------------------------------------------------------------
# One conversion
# --------------------------------------------------------------------------

def convert_upload(
    upload,
    format_key,
    bitrate,
    sample_rate,
    channels,
    start,
    end,
    normalize,
    progress=gr.Progress(),
):
    """The button: turn the upload into the file that was asked for.

    Everything the visitor typed goes through ``converter.normalize_options`` -
    the same validation the engine has always used - so a bad format or an
    unreadable trim is refused before ffmpeg is started.  The heavy lifting is
    ``converter.convert``, unchanged: it reports 0-100 as it goes, and this
    hands those numbers to Gradio's progress bar.

    A failure is *returned*, never raised at Gradio: the page keeps working, the
    reason is written where the download link would be, and ffmpeg's own last
    words are kept in the log box instead of being swallowed.
    """
    log: list = []

    def note(line=None) -> None:
        if line:
            log.append(str(line))

    def tail() -> str:
        return "\n".join(log[-LOG_LINES:]) or "ffmpeg did not say why"

    try:
        if not upload:
            raise core.InputError("choose a file to convert first")
        source = Path(str(upload))
        if not source.is_file():
            raise core.InputError("that upload did not arrive as a file")

        size = source.stat().st_size
        if size > MAX_UPLOAD_MB * MEGABYTE:
            raise core.InputError(
                f"that file is {core.human_size(size)}, over the "
                f"{MAX_UPLOAD_MB} MB this converter accepts"
            )

        options = converter.normalize_options(
            {
                "format": format_key,
                "bitrate": bitrate,
                "sample_rate": sample_rate,
                "channels": channels,
                "start": start,
                "end": end,
                "normalize": normalize,
            }
        )

        work = SCRATCH / f"job-{uuid.uuid4().hex[:12]}"
        work.mkdir(parents=True, exist_ok=True)
        sweep(keep=work)
        # One file per conversion, named the way the engine has always named
        # it - a trim still says so in the name.
        destination = work / converter.output_name(source.name, options)

        def report(percent) -> None:
            value = min(100.0, max(0.0, float(percent)))
            progress(value / 100.0, desc=f"converting ({value:.0f}%)")

        note(f"source: {source.name} ({core.human_size(size)})")
        progress(0.0, desc="converting")
        converter.convert(source, destination, options, log=note, progress=report)

        duration = core.probe_duration(destination)
        made = destination.stat().st_size
        note(f"wrote: {destination.name} ({core.human_size(made)})")

        try:
            source.unlink()  # the upload has done its job
        except OSError:  # pragma: no cover - defensive
            pass

        rate = f" at {options['bitrate']}" if options["bitrate"] else ""
        status = (
            "### Ready to download\n\n"
            f"`{source.name}` &rarr; `{destination.name}`\n\n"
            "| | |\n| --- | --- |\n"
            f"| format | {options['format']}{rate} |\n"
            f"| length | {core.fmt_duration(duration) if duration else '--:--'} |\n"
            f"| size | {core.human_size(made)} |\n\n"
            "Put it in the `discs/` folder next to the player and it shows up in "
            "the playlist picker on its own."
        )
        return str(destination), status, tail()

    except core.YT2DiscError as exc:
        # The engine's own complaint is already written for a person to read.
        gr.Warning(str(exc))
        return None, f"### That did not work\n\n{exc}", tail()
    except Exception as exc:  # noqa: BLE001 - a conversion must never hang
        gr.Warning("unexpected error")
        return None, f"### That did not work\n\nunexpected error: `{exc!r}`", tail()

# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------

def build_demo():
    """The Gradio interface: pick a file, say what to make, press the button."""
    formats = converter.format_choices()
    format_table = "\n".join(
        f"| {item['label']} | `{item['extension']}` | {item['hint']} |"
        for item in formats
    )
    # 0 means "leave it alone", which reads better as words than as a number.
    sample_rates = [
        (f"{rate} Hz", rate) if rate else ("keep the original", 0)
        for rate in converter.SAMPLE_RATES
    ]

    with gr.Blocks(title=APP_NAME) as demo:
        gr.Markdown(
            f"# {APP_NAME}\n\n"
            "Take a file, turn it into another file, hand it back.  Audio and "
            "video both work: from a video the sound is taken and the picture "
            "is thrown away.  This page only converts - playing the result is "
            "the local add-on's job."
        )
        ffmpeg_note = gr.Markdown(server_note())

        upload = gr.File(
            label="1. Pick a file",
            file_types=list(converter.SUPPORTED_INPUTS),
            type="filepath",
        )

        with gr.Row():
            format_key = gr.Dropdown(
                label="2. Format",
                choices=[
                    (f"{item['label']} ({item['extension']})", item["key"])
                    for item in formats
                ],
                value=converter.DEFAULT_FORMAT,
            )
            bitrate = gr.Dropdown(
                label="Quality",
                choices=list(converter.BITRATES),
                value=converter.DEFAULT_BITRATE,
                info="compressed formats only",
            )
            sample_rate = gr.Dropdown(
                label="Sample rate", choices=sample_rates, value=0
            )
            channels = gr.Dropdown(
                label="Channels",
                choices=list(converter.CHANNEL_CHOICES),
                value="auto",
            )

        with gr.Row():
            start = gr.Textbox(label="3. Start", placeholder="0:30 or 90")
            end = gr.Textbox(label="End", placeholder="1:45 or 105")
        normalize = gr.Checkbox(
            label="Even out the loudness",
            value=False,
            info="worth it for Minecraft: one track is then not far louder "
            "than the rest",
        )

        button = gr.Button("Convert it", variant="primary")

        made = gr.File(label="4. Download", interactive=False)
        status = gr.Markdown(
            "Seconds (`90`) or `mm:ss` (`1:30`), and the trim boxes can stay "
            "empty for the whole file.  A conversion takes as long as the "
            "encode takes - the bar that appears when you press the button "
            "follows it."
        )
        with gr.Accordion("ffmpeg's own words", open=False):
            log_box = gr.Textbox(
                label="log",
                lines=10,
                max_lines=10,
                interactive=False,
                show_copy_button=True,
            )
        with gr.Accordion("What each format is for", open=False):
            gr.Markdown(
                "| Format | Output | Good for |\n| --- | --- | --- |\n"
                + format_table
            )

        # Asked again on every visit, so a Space that has just woken from sleep
        # says what it finds now rather than what was true when it started.
        demo.load(server_note, outputs=ffmpeg_note)
        button.click(
            convert_upload,
            inputs=[
                upload,
                format_key,
                bitrate,
                sample_rate,
                channels,
                start,
                end,
                normalize,
            ],
            outputs=[made, status, log_box],
            # One ffmpeg at a time, which is what the old job registry's slots
            # allowed: a stranger with a long video cannot pin every core.
            concurrency_limit=1,
        )

    return demo

# --------------------------------------------------------------------------
# Starting it
# --------------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    """``python app.py --help``: the few choices worth having on the command line."""
    parser = argparse.ArgumentParser(
        prog="python app.py",
        description="Serve the yt2disc converter as a Gradio app.",
    )
    parser.add_argument(
        "--host",
        default=(
            os.environ.get("GRADIO_SERVER_NAME")
            or os.environ.get("YT2DISC_WEB_HOST")
            or "127.0.0.1"
        ),
        help="address to listen on (default: GRADIO_SERVER_NAME, else "
        "YT2DISC_WEB_HOST, else 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int_env(
            "GRADIO_SERVER_PORT", int_env("PORT", 7860, 1, 65535), 1, 65535
        ),
        help="port to listen on (default: GRADIO_SERVER_PORT, else PORT, "
        "else 7860)",
    )
    parser.add_argument(
        "--share",
        action="store_true",
        help="also ask Gradio for a public https URL, for a phone that is not "
        "on this network",
    )
    parser.add_argument(
        "--key",
        default=None,
        help="shared key for this run: 'none' leaves the converter open, and an "
        "exported YT2DISC_WEB_TOKEN wins over both",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    """Say what was found, then serve the page until Ctrl+C."""
    args = parse_args(argv)
    port = max(1, min(65535, args.port))

    # Gradio reads these two itself, so setting them here is what the page
    # actually sees - and it is what makes --host/--port agree with the
    # GRADIO_* names a Hugging Face Space sets for us.
    os.environ["GRADIO_SERVER_NAME"] = args.host
    os.environ["GRADIO_SERVER_PORT"] = str(port)

    if args.key is not None:
        key = args.key.strip()
        if key.lower() == "none":
            os.environ.pop("YT2DISC_WEB_TOKEN", None)
        else:
            os.environ["YT2DISC_WEB_TOKEN"] = key

    ffmpeg = core.find_binary("ffmpeg")
    creds = credentials()

    print(f"{APP_NAME} - http://{args.host}:{port}")
    print(f"  ffmpeg  : {ffmpeg or 'NOT FOUND (put it in bin/ or on PATH)'}")
    print(f"  scratch : {SCRATCH}  (a folder is kept {KEEP_SECONDS / 60:.0f} min)")
    print(f"  cap     : {MAX_UPLOAD_MB} MB per upload")
    print(f"  key     : {'required' if creds else 'not required'}")
    if ffmpeg is None:
        print("  warning : conversions will fail until ffmpeg is available")
    print("  stop with Ctrl+C")

    demo = build_demo()
    launch_options = {
        "server_name": args.host,
        "server_port": port,
        "share": args.share,
        # Our own banner is above and says more than Gradio's, so Gradio's is
        # silenced - except the public URL below, which only Gradio knows.
        "quiet": True,
        "show_error": True,
    }
    if creds:
        launch_options["auth"] = creds
        launch_options["auth_message"] = (
            "This converter is locked.  Give the shared key from the host."
        )

    result = demo.launch(**launch_options)
    if args.share and isinstance(result, tuple) and len(result) > 2 and result[2]:
        print(f"  anywhere: {result[2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
