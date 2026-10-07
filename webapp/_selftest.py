"""_selftest.py - exercise the converter without a browser.

    python webapp/_selftest.py

Builds a throw-away tone with ffmpeg, converts it for real, and checks the
parts that are easy to get wrong: reading a time, sanitising the options,
building the ffmpeg command line, reporting progress, and an actual trim.
Every failure is collected; the exit code is the number of failures, so this
doubles as a smoke test.  Needs ffmpeg (bin/ffmpeg.exe is found for you).
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import converter  # noqa: E402
import core  # noqa: E402
import jobs  # noqa: E402
import packs  # noqa: E402

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

    pack_opts = converter.normalize_options({"format": "pack", "bitrate": "128k"})
    check("the pack choice is flagged", pack_opts["pack"], True)
    check("the pack extension", pack_opts["extension"], ".mcaddon")
    check("is_pack(pack)", converter.is_pack(pack_opts), True)
    check("is_pack(ogg)", converter.is_pack(defaults), False)
    check("the pack is offered in the choices",
          "pack" in [item["key"] for item in converter.format_choices()], True)
    check("pack output_name", converter.output_name("My Song.mp3", pack_opts),
          "my_song.mcaddon")

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

        # The in-game addon: the same tone, wrapped as a loadable pack.
        bundle = converter.convert(source, work / "tone.mcaddon", pack_opts)
        check("pack output exists", bundle.is_file() and bundle.stat().st_size > 0, True)
        rp, bp = packs.pack_folder_names()
        with zipfile.ZipFile(bundle) as archive:
            names = set(archive.namelist())
            corrupt = archive.testzip()
            rp_manifest = json.loads(archive.read(f"{rp}/manifest.json"))
            bp_manifest = json.loads(archive.read(f"{bp}/manifest.json"))
            definitions = json.loads(
                archive.read(f"{rp}/sounds/sound_definitions.json")
            )
            inner = archive.read(f"{rp}/sounds/tone.ogg")
            main_js = archive.read(f"{bp}/scripts/main.js").decode("utf-8")
            tracks_js = archive.read(f"{bp}/scripts/tracks.js").decode("utf-8")
            icon = archive.read(f"{rp}/pack_icon.png")
        check("pack zip has no corrupt member", corrupt, None)
        check("pack carries a resource pack", f"{rp}/manifest.json" in names, True)
        check("pack carries a behavior pack", f"{bp}/manifest.json" in names, True)
        check("pack RP module is resources",
              rp_manifest["modules"][0]["type"], "resources")
        check("pack BP modules", [m["type"] for m in bp_manifest["modules"]],
              ["data", "script"])
        check("pack BP depends on server-ui",
              any(d["module_name"] == "@minecraft/server-ui"
                  for d in bp_manifest["dependencies"]), True)
        check("one sound definition", list(definitions["sound_definitions"]),
              ["yt2disc.tone"])
        check("the sound points at the ogg",
              definitions["sound_definitions"]["yt2disc.tone"]["sounds"][0]["name"],
              "sounds/tone")
        check("the audio inside is an ogg", inner[:4], b"OggS")

        # The way in is a stable custom command, not a chat keyword - no
        # experiment, no server.
        check("the script registers a custom command",
              "customCommandRegistry.registerCommand" in main_js, True)
        check("the command is the namespaced one",
              f'"{packs.COMMAND_NAME}"' in main_js, True)
        check("the command is open to any player",
              "CommandPermissionLevel.Any" in main_js, True)
        check("the command needs no cheats",
              "cheatsRequired: false" in main_js, True)
        check("the chat keyword is gone", "chatSend" in main_js, False)
        check("the scriptevent fallback remains",
              "scriptEventReceive" in main_js, True)
        check("the track list is the data the script imports",
              f'id: "{packs.NAMESPACE}.tone"' in tracks_js, True)
        check("the pack icon is a png",
              icon[:8], b"\x89PNG\r\n\x1a\n")
        check("the icon is what the builder draws", icon, packs.pack_icon())

        # ---- pack regression ------------------------------------------
        # The game keys a pack by its UUID, so the *same* pack built twice has
        # to carry the same UUIDs - otherwise an update installs beside the old
        # pack instead of over it.  These assertions are what keep the manifests
        # deterministic from one run to the next.
        first = packs.resource_manifest(packs.DEFAULT_PACK_NAME, "d")
        again = packs.resource_manifest(packs.DEFAULT_PACK_NAME, "d")
        check("the resource manifest is reproducible", first, again)
        check("its header and module use different uuids",
              first["header"]["uuid"] != first["modules"][0]["uuid"], True)
        check("the behavior manifest is reproducible",
              packs.behavior_manifest(packs.DEFAULT_PACK_NAME, "d"),
              packs.behavior_manifest(packs.DEFAULT_PACK_NAME, "d"))

        # Build the very same addon twice, and compare what landed in the zips.
        def build(name, song):
            return packs.build_addon(
                work / name,
                [{"slug": "tone", "name": song, "audio": source, "duration": 3.0}],
            )

        one = build("one.mcaddon", "My Song")
        two = build("two.mcaddon", "My Song")
        with zipfile.ZipFile(one) as a, zipfile.ZipFile(two) as b:
            rp_a = json.loads(a.read(f"{rp}/manifest.json"))
            rp_b = json.loads(b.read(f"{rp}/manifest.json"))
            bp_a = json.loads(a.read(f"{bp}/manifest.json"))
            bp_b = json.loads(b.read(f"{bp}/manifest.json"))
            songs = a.read(f"{bp}/scripts/tracks.js").decode("utf-8")
        check("two builds share the resource uuid",
              rp_a["header"]["uuid"], rp_b["header"]["uuid"])
        check("two builds share the behavior uuid",
              bp_a["header"]["uuid"], bp_b["header"]["uuid"])
        check("the pack names the song from the source title",
              '"My Song"' in songs, True)

        # The length comes from the source audio, never from the addon: an
        # .mcaddon is a zip, so ffmpeg finds no stream inside it to time.
        check("the addon itself has no readable duration",
              core.probe_duration(one, ffmpeg), None)
        check_close("the source audio carries the duration",
                    core.probe_duration(source, ffmpeg), 3.0)
        rows = packs._clean_tracks(
            [{"slug": "tone", "name": "My Song", "audio": source, "duration": 3.0}]
        )
        check("the source duration is carried into the pack rows",
              rows[0]["duration"], 3.0)

        # ---- a length that cannot be read is a clear error ------------
        check("a missing file probes to None when lenient",
              core.probe_duration(work / "ghost.ogg", ffmpeg), None)
        check_raises(
            "a missing file raises when strict",
            lambda: core.probe_duration(work / "ghost.ogg", ffmpeg, strict=True),
            core.InputError,
        )
        check_raises(
            "a file with no audio raises when strict",
            lambda: core.probe_duration(one, ffmpeg, strict=True),
            core.InputError,
        )

        # ---- a run past its deadline is stopped ------------------------
        check("timeout is spelled in minutes", core._fmt_timeout(1800), "30 min")
        check("a short timeout stays in seconds", core._fmt_timeout(45), "45s")
        slow = [
            str(ffmpeg), "-y", "-hide_banner", "-nostdin",
            "-progress", "pipe:1", "-nostats",
            "-f", "lavfi", "-i", "sine=frequency=440",
            "-t", "600", "-c:a", "libvorbis", "-b:a", "96k",
            str(work / "slow.ogg"),
        ]
        check_raises(
            "a conversion past its deadline is stopped",
            lambda: core.run_ffmpeg(slow, timeout=1.0),
            core.YT2DiscError,
        )

    print("== hosted job limits ==")
    with tempfile.TemporaryDirectory(prefix="yt2disc-limits-") as workdir:
        registry = jobs.JobRegistry(
            root=Path(workdir),
            slots=1,
            timeout_seconds=60,
            max_upload_bytes=10,
        )
        check("the registry keeps its timeout", registry.timeout_seconds, 60.0)
        check("the registry keeps its upload cap", registry.max_upload_bytes, 10)
        check_raises(
            "an unknown upload type is refused",
            lambda: registry.submit("notes.txt", io.BytesIO(b"x"), defaults),
            core.InputError,
        )
        check_raises(
            "an upload over the cap is refused",
            lambda: registry.submit("song.mp3", io.BytesIO(b"0" * 100), defaults),
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

