"""converter.py - the one job the hosted web app does: convert a file.

There is deliberately no playback in here.  The hosted half of yt2disc only
turns something you upload into something you can download - an Ogg Vorbis
file that a Minecraft resource pack will accept, by default - and the local
add-on is what plays music.

ffmpeg does all the real work.  Everything else (locating the binary, probing
durations, formatting sizes, cleaning up file names) is borrowed from
:mod:`core`, so the hosted converter and the local player agree on what a
track is.  Nothing here imports a web framework, which keeps it testable on
its own.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# The app is started as ``webapp.app:app`` (or ``python -m webapp.app``) from
# the repository root, but ``PYTHONPATH`` is not always set for us - for
# instance under gunicorn on a host.  Make sure ``core.py`` is importable.
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import core  # noqa: E402  (import after the sys.path fix above)
import packs  # noqa: E402  (the Bedrock addon builder, beside this file)

# --------------------------------------------------------------------------
# What we accept, and what we produce
# --------------------------------------------------------------------------

# Audio we can read (straight from the player's own list) plus the containers
# people usually have a song trapped inside.
VIDEO_EXTENSIONS = (
    ".mp4",
    ".m4v",
    ".webm",
    ".mkv",
    ".mov",
    ".avi",
    ".flv",
    ".wmv",
    ".ts",
    ".mpg",
    ".mpeg",
    ".3gp",
    ".ogv",
)
SUPPORTED_INPUTS = tuple(dict.fromkeys(tuple(core.AUDIO_EXTENSIONS) + VIDEO_EXTENSIONS))

# The output table.  ``args`` is the codec switch for the format; the only
# placeholder is {bitrate}, and only lossy formats use it.  ``extension`` must
# match what ffmpeg's muxer expects, because ffmpeg picks the muxer from it.
OUTPUT_FORMATS: dict[str, dict] = {
    "ogg": {
        "label": "Ogg Vorbis",
        "extension": ".ogg",
        "mime": "audio/ogg",
        "lossy": True,
        "args": ["-c:a", "libvorbis", "-b:a", "{bitrate}"],
        "hint": "Minecraft resource packs read this one.",
    },
    "opus": {
        "label": "Opus",
        "extension": ".opus",
        "mime": "audio/ogg",
        "lossy": True,
        "args": ["-c:a", "libopus", "-b:a", "{bitrate}"],
        "hint": "Smaller than Vorbis, also fine for Minecraft.",
    },
    "mp3": {
        "label": "MP3",
        "extension": ".mp3",
        "mime": "audio/mpeg",
        "lossy": True,
        "args": ["-c:a", "libmp3lame", "-b:a", "{bitrate}"],
        "hint": "Plays on anything.",
    },
    "m4a": {
        "label": "AAC (m4a)",
        "extension": ".m4a",
        "mime": "audio/mp4",
        "lossy": True,
        "args": ["-c:a", "aac", "-b:a", "{bitrate}"],
        "hint": "Good for phones and iTunes.",
    },
    "wav": {
        "label": "WAV",
        "extension": ".wav",
        "mime": "audio/wav",
        "lossy": False,
        "args": ["-c:a", "pcm_s16le"],
        "hint": "Uncompressed and big; what the Windows player wants.",
    },
    "flac": {
        "label": "FLAC",
        "extension": ".flac",
        "mime": "audio/flac",
        "lossy": False,
        "args": ["-c:a", "flac"],
        "hint": "Lossless archive copy.",
    },
    # Not a codec but a *deliverable*: the audio is encoded as Ogg (so it keeps
    # the Vorbis arguments above) and then wrapped, with a behavior pack, into
    # an addon the game can load.  "pack" is the flag that tells convert() the
    # zip is the answer and the Ogg is only a step on the way.
    "pack": {
        "label": "Minecraft music player",
        "extension": ".mcaddon",
        "mime": "application/zip",
        "lossy": True,
        "args": ["-c:a", "libvorbis", "-b:a", "{bitrate}"],
        "hint": "In-game player: run /yt2disc:music to open the menu.",
        "pack": True,
    },
}
DEFAULT_FORMAT = "ogg"

BITRATES = ("96k", "128k", "192k", "256k", "320k")
DEFAULT_BITRATE = "192k"

# 0 means "leave it alone".
SAMPLE_RATES = (0, 22050, 44100, 48000)
CHANNEL_CHOICES = ("auto", "mono", "stereo")

# Loudness targets that stop one track from being much louder than the game
# it plays over (the same EBU R128 numbers most players aim for).
LOUDNORM_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=11"


# --------------------------------------------------------------------------
# Small parsing helpers
# --------------------------------------------------------------------------

# ffmpeg's ``-progress pipe:1`` writes ``out_time=00:00:01.23`` and
# ``out_time_ms=<microseconds>`` (a long-standing misnomer).  The text form has
# to be tried first, or ``out_time=00`` would win and report zero forever.
_OUT_TIME_TEXT_RE = re.compile(r"out_time=(\d+):(\d\d):(\d\d(?:\.\d+)?)")
_OUT_TIME_MICROS_RE = re.compile(r"out_time(?:_ms|_us)=(\d+)")
_TIME_RE = re.compile(r"^(?:(\d+):)?(?:(\d+):)?(\d+(?:\.\d+)?)$")


def parse_time(value) -> float | None:
    """Read ``90``, ``1:30`` or ``1:02:03.5`` as seconds; ``None`` when empty.

    A value we cannot read raises, because silently ignoring a typo in a trim
    box would hand back the wrong audio.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        return None if seconds <= 0 else seconds
    text = str(value).strip().replace(",", ".")
    if not text:
        return None
    match = _TIME_RE.match(text)
    if not match:
        raise core.InputError(
            f"'{value}' is not a time - write seconds (90) or mm:ss (1:30)"
        )
    first, second, last = match.groups()
    if first is not None and second is None:
        # "1:30" means one minute thirty, not one hour and thirty seconds.
        hours, minutes, seconds_text = 0, int(first), last
    else:
        hours = int(first or 0)
        minutes = int(second or 0)
        seconds_text = last
    total = hours * 3600 + minutes * 60 + float(seconds_text)
    return None if total <= 0 else total


def _as_int(value, allowed, default=0) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return number if number in allowed else default


def _as_bool(value) -> bool:
    """A checkbox survives a round trip through HTML as 'on'/'true'/'1'."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "on", "true", "yes", "y")


def normalize_options(raw) -> dict:
    """Validate the form/JSON options into the dict the converter expects."""
    raw = raw if isinstance(raw, dict) else {}

    key = str(raw.get("format") or DEFAULT_FORMAT).strip().lower().lstrip(".")
    if key not in OUTPUT_FORMATS:
        raise core.InputError(
            f"'{raw.get('format')}' is not a format we make "
            f"({', '.join(OUTPUT_FORMATS)})"
        )
    spec = OUTPUT_FORMATS[key]

    bitrate = str(raw.get("bitrate") or DEFAULT_BITRATE).strip().lower()
    if spec["lossy"]:
        if bitrate not in BITRATES:
            bitrate = DEFAULT_BITRATE
    else:
        bitrate = ""  # lossless formats have nothing to spend a bitrate on

    channels = str(raw.get("channels") or "auto").strip().lower()
    if channels not in CHANNEL_CHOICES:
        channels = "auto"

    start = parse_time(raw.get("start"))
    end = parse_time(raw.get("end"))
    if start is not None and end is not None and end <= start:
        raise core.InputError(
            f"the end ({core.fmt_duration(end)}) has to come after the "
            f"start ({core.fmt_duration(start)})"
        )

    return {
        "format": key,
        "extension": spec["extension"],
        "mime": spec["mime"],
        "lossy": spec["lossy"],
        "pack": bool(spec.get("pack")),
        "bitrate": bitrate,
        "sample_rate": _as_int(raw.get("sample_rate"), SAMPLE_RATES, 0),
        "channels": channels,
        "start": start,
        "end": end,
        "normalize": _as_bool(raw.get("normalize")),
    }


def progress_seconds(line) -> float | None:
    """The ``out_time`` ffmpeg just reported, in seconds."""
    text = line or ""
    match = _OUT_TIME_TEXT_RE.search(text)
    if match:
        hours, minutes, seconds = match.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    match = _OUT_TIME_MICROS_RE.search(text)
    if match:
        return int(match.group(1)) / 1_000_000.0
    return None


def format_choices() -> list[dict]:
    """The output formats, in a shape a template can loop over."""
    return [{"key": key, **spec} for key, spec in OUTPUT_FORMATS.items()]


def is_pack(options) -> bool:
    """True when the output is the in-game addon, not a bare audio file.

    The addon is a zip of Oggs, so ``convert`` still runs ffmpeg - only the
    last step differs.  Keeping the choice in the one output table is what lets
    every front-end offer it without a branch of its own.
    """
    spec = OUTPUT_FORMATS.get(str((options or {}).get("format")))
    return bool(spec and spec.get("pack"))


def safe_stem(name) -> str:
    """A short, filesystem-safe stem for an uploaded file's name."""
    return core.slugify(Path(str(name or "track")).stem)


def output_name(source_name, options) -> str:
    """``My Song.mp4`` + ogg -> ``my-song.ogg``; a cut says so in the name.

    Writing the trim into the file name keeps a folder of downloads readable
    when several edits of one song sit next to each other.
    """
    stem = safe_stem(source_name)
    start, end = options.get("start"), options.get("end")
    if start is None and end is None:
        return f"{stem}{options['extension']}"
    bits = []
    for label, value in (("from", start), ("to", end)):
        if value is not None:
            # :g keeps 30.0 as "30" and 0.5 as "0.5" - rounding here would
            # stamp a half-second cut as "from0s".
            bits.append(f"{label}{value:g}s")
    return f"{stem}-{'-'.join(bits)}{options['extension']}"


def build_command(ffmpeg, source, destination, options) -> list[str]:
    """The exact ffmpeg argument list for one conversion (pure, testable)."""
    spec = OUTPUT_FORMATS[options["format"]]
    start, end = options.get("start"), options.get("end")

    cmd = [
        str(ffmpeg),
        "-y",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "warning",
        # -progress is independent of -loglevel, so the job still gets a
        # percentage while the console stays quiet.
        "-progress",
        "pipe:1",
        "-nostats",
    ]
    # -ss before -i makes ffmpeg jump instead of decoding everything first.
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(source)]
    if end is not None:
        cmd += ["-t", f"{max(0.0, end - (start or 0.0)):.3f}"]

    cmd += ["-vn"]  # it is a music file: throw the picture away
    if options["sample_rate"]:
        cmd += ["-ar", str(options["sample_rate"])]
    if options["channels"] == "mono":
        cmd += ["-ac", "1"]
    elif options["channels"] == "stereo":
        cmd += ["-ac", "2"]
    if options["normalize"]:
        cmd += ["-af", LOUDNORM_FILTER]
    cmd += [part.format(bitrate=options["bitrate"]) for part in spec["args"]]
    cmd.append(str(destination))
    return cmd


def reported_total(full, options) -> float | None:
    """How long the *output* will be, from the input's length and the trim."""
    start, end = options.get("start"), options.get("end")
    if start is None and end is None:
        return full
    if full is None:
        return None if end is None else max(0.0, end - (start or 0.0))
    return max(0.0, (full if end is None else min(end, full)) - (start or 0.0))


# ffmpeg answers a trim that starts past the end of the file with an empty
# container *and* exit code 0, so its own words are the only honest signal.
EMPTY_OUTPUT_MARK = "nothing was encoded"


def check_trim(full, options) -> None:
    """Refuse a trim that cannot produce audio, before ffmpeg wastes the work.

    Only the start is policed.  An end that overshoots simply means "to the
    end of the file", which is a reasonable thing to ask for.
    """
    start = options.get("start")
    if full and start is not None and start >= full:
        raise core.InputError(
            f"the start ({core.fmt_duration(start)}) is at or past the end of "
            f"the file ({core.fmt_duration(full)})"
        )


def convert(
    source, destination, options, log=None, progress=None, ffmpeg=None, title=None
) -> Path:
    """Convert one file.  Failures quote ffmpeg's own last words.

    ``log`` receives every ffmpeg line; ``progress`` receives a 0-100 float.
    ``title`` names the song inside a pack, for a caller whose source file was
    saved under a scratch name of its own (a server does exactly that).
    """
    source = Path(str(source))
    destination = Path(str(destination))
    if not source.is_file():
        raise core.InputError(f"'{source.name}' is not a file we can read")

    ffmpeg = Path(str(ffmpeg)) if ffmpeg else core.find_binary("ffmpeg")
    if ffmpeg is None:
        raise core.BinaryMissingError("ffmpeg")

    # One probe, used twice: to size up the progress bar, and to catch a trim
    # that points off the end of the file.
    full = core.probe_duration(source, ffmpeg)
    check_trim(full, options)
    total = reported_total(full, options)

    destination.parent.mkdir(parents=True, exist_ok=True)
    # An addon is a zip of Ogg files, so ffmpeg writes the audio first and the
    # pack is built around it afterwards - ffmpeg has no .mcaddon muxer, and
    # would refuse the extension.  Otherwise the audio *is* the answer and lands
    # straight at ``destination``.
    pack = is_pack(options)
    audio = (
        destination.with_name(destination.stem + ".ogg") if pack else destination
    )
    # Same trick as core.prepare_playable: the part file keeps the real suffix,
    # because ffmpeg picks its muxer from the extension alone.
    tmp = audio.with_name(audio.stem + ".part" + audio.suffix)

    def on_line(line):
        if log:
            log(line)
        if not progress or not total:
            return
        seen = progress_seconds(line)
        if seen is not None:
            progress(min(99.0, max(0.0, seen / total * 100.0)))

    code, tail = core.run_ffmpeg(
        build_command(ffmpeg, source, tmp, options), log=on_line, tail_size=60
    )
    empty = any(EMPTY_OUTPUT_MARK in line for line in tail)
    if code != 0 or empty or not tmp.is_file() or tmp.stat().st_size <= 44:
        try:
            tmp.unlink()
        except OSError:  # pragma: no cover - defensive
            pass
        if empty and code == 0:
            raise core.InputError(
                f"ffmpeg produced no audio for '{source.name}' - the trim has to "
                f"fall inside the file, which is {core.fmt_duration(full)} long"
            )
        detail = "\n".join(tail[-10:]) or "ffmpeg did not say why"
        raise core.YT2DiscError(f"ffmpeg could not convert '{source.name}':\n{detail}")

    os.replace(tmp, audio)

    if pack:
        # The slug - which is also the sound id - comes from the download's own
        # name, so two cuts of one song stay distinct entries instead of
        # colliding.  The title is what the menu shows: the name the song was
        # uploaded under, which is not always the source file's name, because a
        # server saves an upload to a bland scratch name of its own.
        song = Path(str(title or source.name)).stem.strip() or audio.stem
        packs.build_addon(
            destination,
            [
                {
                    "slug": safe_stem(destination.stem),
                    "name": song,
                    "audio": audio,
                    "duration": total,
                }
            ],
            log=log,
        )

    if progress:
        progress(100.0)
    if log:
        log(f"converted: {source.name} -> {destination.name}")
    return destination


