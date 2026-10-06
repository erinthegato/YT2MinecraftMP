"""_selftest.py - exercise the converter without a browser.

    python webapp/_selftest.py

Builds a throw-away tone with ffmpeg, converts it for real, and checks the
parts that are easy to get wrong: reading a time, sanitising the options,
building the ffmpeg command line, reporting progress, and an actual trim.
Every failure is collected; the exit code is the number of failures, so this
doubles as a smoke test.  Needs ffmpeg (bin/ffmpeg.exe is found for you).
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import converter  # noqa: E402
import core  # noqa: E402

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}: got {got!r}, wanted {want!r}")
    print(f"  FAIL {label}: got {got!r}, wanted {want!r}")


def check_close(label: str, got, want, tolerance: float = 0.35) -> None:
    if got is not None and abs(got - want) <= tolerance:
        print(f"  ok   {label} ({got:.2f}s)")
        return
    FAILURES.append(f"{label}: got {got!r}, wanted {want} +/- {tolerance}")
    print(f"  FAIL {label}: got {got!r}, wanted {want} +/- {tolerance}")


def check_raises(label: str, callable_, error) -> None:
    try:
        callable_()
    except error as exc:
        print(f"  ok   {label} ({exc})")
        return
    except Exception as exc:  # noqa: BLE001 - the point is to report anything else
        FAILURES.append(f"{label}: raised {type(exc).__name__}, wanted {error.__name__}")
        print(f"  FAIL {label}: raised {type(exc).__name__}, wanted {error.__name__}")
        return
    FAILURES.append(f"{label}: nothing raised, wanted {error.__name__}")
    print(f"  FAIL {label}: nothing raised, wanted {error.__name__}")


def tone(ffmpeg: Path, destination: Path, seconds: int = 3) -> Path:
    """A clean, known-length sine wave to convert."""
    subprocess.run(
        [
            str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i",
            f"sine=frequency=440:duration={seconds}",
            "-ac", "2", "-ar", "44100",
            str(destination),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return destination


def main() -> int:
    print("== pure helpers ==")
    check("parse_time('90')", converter.parse_time("90"), 90.0)
    check("parse_time('1:30')", converter.parse_time("1:30"), 90.0)
    check("parse_time('1:02:03')", converter.parse_time("1:02:03"), 3723.0)
    check("parse_time('1:00.5')", converter.parse_time("1:00.5"), 60.5)
    check("parse_time('')", converter.parse_time("  "), None)
    check_raises("parse_time('soon')",
                 lambda: converter.parse_time("soon"), core.InputError)

    check("progress_seconds(out_time=00:00:01.23)",
          round(converter.progress_seconds("out_time=00:00:01.23"), 3), 1.23)
    check("progress_seconds(out_time_ms=1500000)",
          converter.progress_seconds("out_time_ms=1500000"), 1.5)
    check("progress_seconds(junk)", converter.progress_seconds("frame=12"), None)

    defaults = converter.normalize_options({})
    check("default format", defaults["format"], "ogg")
    check("default bitrate", defaults["bitrate"], "192k")
    check("default extension", defaults["extension"], ".ogg")
    check("lossless drops bitrate",
          converter.normalize_options({"format": "wav", "bitrate": "320k"})["bitrate"], "")
    check("bad bitrate falls back",
          converter.normalize_options({"format": "mp3", "bitrate": "999k"})["bitrate"], "192k")
    check("bad channels falls back",
          converter.normalize_options({"channels": "5.1"})["channels"], "auto")
    check("checkbox 'on' is true",
          converter.normalize_options({"normalize": "on"})["normalize"], True)
    check_raises("format 'aiff'",
                 lambda: converter.normalize_options({"format": "aiff"}), core.InputError)
    check_raises("end before start",
                 lambda: converter.normalize_options({"start": "10", "end": "5"}),
                 core.InputError)

    check("output_name plain", converter.output_name("My Song.mp3", defaults), "my_song.ogg")
    cut = converter.normalize_options({"start": "30", "end": "90"})
    check("output_name trimmed", converter.output_name("My Song.mp3", cut),
          "my_song-from30s-to90s.ogg")
    check("output_name keeps a decimal",
          converter.output_name("x.wav", converter.normalize_options({"start": "0.5"})),
          "x-from0.5s.ogg")

    print("== ffmpeg ==")
    ffmpeg = core.find_binary("ffmpeg")
    if ffmpeg is None:
        FAILURES.append("ffmpeg not found (put it in bin/ or on PATH)")
        print("  FAIL ffmpeg not found - skipping the live checks")
        return report()

    print(f"  using {ffmpeg}")
    with tempfile.TemporaryDirectory(prefix="yt2disc-selftest-") as workdir:
        work = Path(workdir)
        source = tone(ffmpeg, work / "tone.wav")

        command = converter.build_command(ffmpeg, source, work / "out.ogg", defaults)
        for flag in ("-vn", "-progress", "pipe:1"):
            check(f"plain command carries {flag}", flag in command, True)
        check("plain command uses libvorbis", "libvorbis" in command, True)

        seen: list[float] = []
        plain = converter.convert(source, work / "out.ogg", defaults, progress=seen.append)
        check("plain output exists", plain.is_file() and plain.stat().st_size > 0, True)
        check_close("plain output length", core.probe_duration(plain, ffmpeg), 3.0)
        check("progress reached 100", seen[-1] if seen else None, 100.0)

        inside = converter.normalize_options({"start": "0.5", "end": "1.5"})
        trimmed = converter.convert(source, work / "cut.ogg", inside)
        check_close("trimmed output length", core.probe_duration(trimmed, ffmpeg), 1.0, 0.4)
        check_raises("trim past the end of the file",
                     lambda: converter.convert(source, work / "dead.ogg", cut),
                     core.InputError)

        mono = converter.normalize_options(
            {"format": "m4a", "channels": "mono", "normalize": True, "bitrate": "96k"}
        )
        loud = converter.convert(source, work / "mono.m4a", mono)
        check("mono/aac output exists", loud.is_file() and loud.stat().st_size > 0, True)
        check("mono/aac has a length", core.probe_duration(loud, ffmpeg) is not None, True)

        check_raises(
            "missing input",
            lambda: converter.convert(work / "ghost.mp3", work / "x.ogg", defaults),
            core.InputError,
        )

    return report()


def report() -> int:
    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s):")
        for line in FAILURES:
            print(f"  - {line}")
        return len(FAILURES)
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

